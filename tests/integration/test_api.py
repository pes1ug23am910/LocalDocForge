"""Local HTTP API: auth, hardening headers, job flows, containment."""

from __future__ import annotations

import io

import pikepdf
import pytest
from fastapi.testclient import TestClient

from localdocforge.api.app import create_app
from localdocforge.cli.main import bind_allowed
from localdocforge.config.settings import Settings
from localdocforge.domain.models import ResourceLimits

TOKEN = "test-token-abcdef"


@pytest.fixture()
def client(tmp_path):
    settings = Settings(jobs_root=tmp_path / "jobs")
    app = create_app(settings, token=TOKEN)
    with TestClient(app, base_url="http://127.0.0.1") as test_client:
        yield test_client


def auth() -> dict[str, str]:
    return {"X-LDF-Token": TOKEN}


def upload(path, name=None):
    return ("files", (name or path.name, io.BytesIO(path.read_bytes()), "application/pdf"))


class TestSecurityBaseline:
    def test_non_loopback_host_rejected(self, tmp_path):
        app = create_app(Settings(jobs_root=tmp_path / "jobs"), token=TOKEN)
        with TestClient(app, base_url="http://evil.example.com") as bad_client:
            response = bad_client.get("/api/health", headers=auth())
        assert response.status_code == 400

    def test_api_requires_token(self, client):
        assert client.get("/api/health").status_code == 401
        assert client.get("/api/capabilities").status_code == 401

    def test_wrong_token_rejected(self, client):
        response = client.get("/api/health", headers={"X-LDF-Token": "wrong"})
        assert response.status_code == 401

    def test_cookie_alone_is_not_enough(self, client, fixtures_dir):
        # Browser flow sets the cookie on GET /; a CSRF request would carry the
        # cookie but cannot set the custom header. It must be refused.
        index = client.get("/")
        assert index.status_code == 200
        assert "ldf_token" in index.cookies
        response = client.post(
            "/api/jobs/rotate",
            files=[upload(fixtures_dir / "simple-3page.pdf")],
            data={"degrees": "90"},
        )  # cookie jar carries ldf_token automatically, header absent
        assert response.status_code == 401

    def test_hardening_headers_everywhere(self, client):
        for response in (client.get("/"), client.get("/api/health", headers=auth())):
            assert "Content-Security-Policy" in response.headers
            assert response.headers["X-Content-Type-Options"] == "nosniff"
            assert response.headers["X-Frame-Options"] == "DENY"

    def test_index_is_honest_about_pending_features(self, client):
        page = client.get("/").text
        assert "Not available yet" in page
        assert "OCR" in page  # unimplemented features are listed as such
        assert "interface coverage varies" in page
        assert "Everything below is usable" not in page

    def test_bind_guard(self):
        assert bind_allowed("127.0.0.1", False)
        assert bind_allowed("::1", False)
        assert not bind_allowed("0.0.0.0", False)
        assert bind_allowed("0.0.0.0", True)


class TestHealthAndCapabilities:
    def test_health(self, client):
        response = client.get("/api/health", headers=auth())
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    def test_capabilities_match_registry_honesty(self, client):
        payload = client.get("/api/capabilities", headers=auth()).json()
        capability_map = {c["id"]: c for c in payload["capabilities"]}
        assert capability_map["merge"]["available"] is True
        assert capability_map["ocr"]["available"] is False


class TestJobFlow:
    def test_merge_upload_download(self, client, fixtures_dir):
        response = client.post(
            "/api/jobs/merge",
            headers=auth(),
            files=[
                upload(fixtures_dir / "simple-3page.pdf"),
                upload(fixtures_dir / "second-2page.pdf"),
            ],
        )
        assert response.status_code == 201, response.text
        payload = response.json()
        assert payload["report"]["status"] == "success"
        assert len(payload["outputs"]) == 1

        job_id = payload["job_id"]
        detail = client.get(f"/api/jobs/{job_id}", headers=auth())
        assert detail.status_code == 200
        downloaded = client.get(f"/api/jobs/{job_id}/outputs/0", headers=auth())
        assert downloaded.status_code == 200
        with pikepdf.open(io.BytesIO(downloaded.content)) as pdf:
            assert len(pdf.pages) == 5

    def test_rotate_with_params(self, client, fixtures_dir):
        response = client.post(
            "/api/jobs/rotate",
            headers=auth(),
            files=[upload(fixtures_dir / "simple-3page.pdf")],
            data={"degrees": "90", "pages": "1"},
        )
        assert response.status_code == 201, response.text
        job_id = response.json()["job_id"]
        content = client.get(f"/api/jobs/{job_id}/outputs/0", headers=auth()).content
        with pikepdf.open(io.BytesIO(content)) as pdf:
            assert int(pdf.pages[0].obj.get("/Rotate", 0)) == 90

    def test_pdf_to_images_multiple_outputs(self, client, fixtures_dir):
        response = client.post(
            "/api/jobs/pdf-to-images",
            headers=auth(),
            files=[upload(fixtures_dir / "simple-3page.pdf")],
            data={"dpi": "72"},
        )
        assert response.status_code == 201, response.text
        outputs = response.json()["outputs"]
        assert len(outputs) == 3
        job_id = response.json()["job_id"]
        image = client.get(f"/api/jobs/{job_id}/outputs/2", headers=auth())
        assert image.content[:8] == b"\x89PNG\r\n\x1a\n"

    def test_compress_job_reports_reduction_and_downloads(self, client, fixtures_dir):
        response = client.post(
            "/api/jobs/compress",
            headers=auth(),
            files=[upload(fixtures_dir / "simple-3page.pdf")],
        )
        assert response.status_code == 201, response.text
        payload = response.json()
        assert payload["report"]["status"] == "success"
        stats = payload["report"]["details"]["compression"]
        assert stats["input_bytes"] > 0 and stats["output_bytes"] > 0
        assert payload["report"]["details"]["render_compare"]["identical"] is True

        job_id = payload["job_id"]
        downloaded = client.get(f"/api/jobs/{job_id}/outputs/0", headers=auth())
        assert downloaded.status_code == 200
        with pikepdf.open(io.BytesIO(downloaded.content)) as pdf:
            assert len(pdf.pages) == 3

    def test_compress_rejects_unimplemented_preset(self, client, fixtures_dir):
        response = client.post(
            "/api/jobs/compress",
            headers=auth(),
            files=[upload(fixtures_dir / "simple-3page.pdf")],
            data={"preset": "balanced"},
        )
        assert response.status_code == 422

    def test_jobs_listed_and_deleted(self, client, fixtures_dir):
        created = client.post(
            "/api/jobs/extract-pages",
            headers=auth(),
            files=[upload(fixtures_dir / "simple-3page.pdf")],
            data={"pages": "1"},
        )
        job_id = created.json()["job_id"]
        listing = client.get("/api/jobs", headers=auth()).json()["jobs"]
        assert any(job["job_id"] == job_id for job in listing)
        assert client.delete(f"/api/jobs/{job_id}", headers=auth()).status_code == 200
        assert client.get(f"/api/jobs/{job_id}", headers=auth()).status_code == 404


class TestJobErrors:
    def test_unknown_operation(self, client, fixtures_dir):
        response = client.post(
            "/api/jobs/teleport",
            headers=auth(),
            files=[upload(fixtures_dir / "simple-3page.pdf")],
        )
        assert response.status_code == 404

    def test_unimplemented_operation_not_reachable(self, client, fixtures_dir):
        response = client.post(
            "/api/jobs/ocr",
            headers=auth(),
            files=[upload(fixtures_dir / "simple-3page.pdf")],
        )
        assert response.status_code == 404

    def test_unknown_job_and_output(self, client):
        assert client.get("/api/jobs/deadbeef", headers=auth()).status_code == 404
        assert client.get("/api/jobs/deadbeef/outputs/0", headers=auth()).status_code == 404

    def test_output_index_out_of_range(self, client, fixtures_dir):
        created = client.post(
            "/api/jobs/extract-pages",
            headers=auth(),
            files=[upload(fixtures_dir / "simple-3page.pdf")],
            data={"pages": "1"},
        )
        job_id = created.json()["job_id"]
        assert client.get(f"/api/jobs/{job_id}/outputs/7", headers=auth()).status_code == 404

    def test_garbage_pdf_reports_failure(self, client, fixtures_dir):
        response = client.post(
            "/api/jobs/extract-pages",
            headers=auth(),
            files=[upload(fixtures_dir / "garbage.pdf")],
            data={"pages": "1"},
        )
        assert response.status_code == 422
        assert "report" in response.json() or "detail" in response.json()

    def test_upload_size_limit(self, tmp_path, fixtures_dir):
        settings = Settings(
            jobs_root=tmp_path / "jobs", limits=ResourceLimits(max_input_bytes=200)
        )
        app = create_app(settings, token=TOKEN)
        with TestClient(app, base_url="http://127.0.0.1") as small_client:
            response = small_client.post(
                "/api/jobs/extract-pages",
                headers=auth(),
                files=[upload(fixtures_dir / "simple-3page.pdf")],
                data={"pages": "1"},
            )
        assert response.status_code == 413

    def test_missing_required_param(self, client, fixtures_dir):
        response = client.post(
            "/api/jobs/extract-pages",
            headers=auth(),
            files=[upload(fixtures_dir / "simple-3page.pdf")],
        )
        assert response.status_code == 422

    def test_unknown_form_field_is_rejected(self, client, fixtures_dir):
        response = client.post(
            "/api/jobs/extract-pages",
            headers=auth(),
            files=[upload(fixtures_dir / "simple-3page.pdf")],
            data={"pages": "1", "ignored_option": "surprise"},
        )
        assert response.status_code == 422
        assert "Unknown form field" in response.json()["detail"]

    def test_image_api_honors_layout_and_quality_parameters(self, client, fixtures_dir):
        response = client.post(
            "/api/jobs/images-to-pdf",
            headers=auth(),
            files=[upload(fixtures_dir / "images" / "diagram.png")],
            data={
                "page_size": "64x48pt",
                "fit": "center",
                "margin": "2.5",
                "background": "#00ff00",
                "dpi": "72",
                "quality": "41",
            },
        )
        assert response.status_code == 201, response.text
        details = response.json()["report"]["details"]
        assert details | {
            "page_size": "64x48pt",
            "fit": "center",
            "margin_pt": 2.5,
            "background": "#00ff00",
            "dpi": 72,
            "jpeg_quality": 41,
        } == details

    def test_pdf_image_api_honors_webp_quality(self, client, fixtures_dir):
        outputs = []
        for quality in (1, 100):
            response = client.post(
                "/api/jobs/pdf-to-images",
                headers=auth(),
                files=[upload(fixtures_dir / "simple-3page.pdf")],
                data={
                    "format": "webp",
                    "dpi": "72",
                    "pages": "1",
                    "quality": str(quality),
                },
            )
            assert response.status_code == 201, response.text
            payload = response.json()
            assert payload["report"]["details"]["jpeg_quality"] == quality
            outputs.append(
                client.get(
                    f"/api/jobs/{payload['job_id']}/outputs/0", headers=auth()
                ).content
            )
        assert outputs[0] != outputs[1]

    def test_hostile_upload_filename_neutralized(self, client, fixtures_dir):
        response = client.post(
            "/api/jobs/extract-pages",
            headers=auth(),
            files=[upload(fixtures_dir / "simple-3page.pdf", name="..\\..\\evil.pdf")],
            data={"pages": "1"},
        )
        assert response.status_code == 201, response.text
