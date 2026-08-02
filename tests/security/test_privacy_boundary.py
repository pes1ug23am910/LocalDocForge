"""Regression tests for LocalDocForge's privacy and localhost API boundary."""

from __future__ import annotations

import asyncio
import io
import json
import logging
import socket
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from reportlab.pdfgen import canvas
from typer.testing import CliRunner

import localdocforge.api.app as api_module
from localdocforge.api.app import create_app
from localdocforge.cli.main import app as cli_app
from localdocforge.config.settings import Settings, get_settings, set_settings
from localdocforge.domain.models import ResourceLimits
from localdocforge.domain.pages import PageRange
from localdocforge.operations.organize import OrganizeOptions, extract_pages

TOKEN = "privacy-boundary-test-token"


def _auth() -> dict[str, str]:
    return {"X-LDF-Token": TOKEN}


def _upload(path: Path, *, name: str | None = None):
    return ("files", (name or path.name, io.BytesIO(path.read_bytes()), "application/pdf"))


def _strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from _strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _strings(child)


def _assert_private_root_absent(response, root: Path) -> None:
    private_root = root.resolve().as_posix().casefold()
    for value in _strings(response.json()):
        assert private_root not in value.replace("\\", "/").casefold()


def _assert_hardened(response) -> None:
    assert response.headers["Cache-Control"] == "no-store"
    assert "Content-Security-Policy" in response.headers
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert "camera=()" in response.headers["Permissions-Policy"]


def test_ldf_strict_offline_environment_survives_cli_without_flag(monkeypatch, tmp_path):
    previous = get_settings()
    monkeypatch.setenv("LDF_STRICT_OFFLINE", "true")
    monkeypatch.setenv("LDF_JOBS_ROOT", str(tmp_path / "jobs"))
    try:
        result = CliRunner().invoke(cli_app, ["doctor"])
        assert result.exit_code == 0, result.output
        assert get_settings().strict_offline is True
    finally:
        set_settings(previous)


def test_strict_offline_mode_is_recorded_in_conversion_report(fixtures_dir, tmp_path):
    settings = Settings(strict_offline=True, jobs_root=tmp_path / "jobs")
    report = extract_pages(
        fixtures_dir / "simple-3page.pdf",
        tmp_path / "one-page.pdf",
        PageRange(spec="1"),
        options=OrganizeOptions(settings=settings),
    )

    assert report.details["strict_offline"] is True


@pytest.mark.parametrize(
    ("strict_offline", "expected_text", "forbidden_text"),
    [
        (
            False,
            "strict-offline mode is not enabled",
            "Python network guards are enabled as defense in depth",
        ),
        (
            True,
            "Python network guards are enabled as defense in depth",
            "strict-offline mode is not enabled",
        ),
    ],
)
def test_ui_privacy_wording_and_health_match_runtime_mode(
    strict_offline, expected_text, forbidden_text, tmp_path
):
    settings = Settings(strict_offline=strict_offline, jobs_root=tmp_path / "jobs")
    with TestClient(create_app(settings, token=TOKEN), base_url="http://127.0.0.1") as client:
        index = client.get("/")
        health = client.get("/api/health", headers=_auth())

    assert index.status_code == 200
    assert expected_text in index.text
    assert forbidden_text not in index.text
    assert "nothing leaves this machine" not in index.text
    assert health.status_code == 200
    assert health.json()["strict_offline"] is strict_offline
    assert health.json()["loopback_only"] is True


def test_api_responses_never_disclose_absolute_jobs_root(fixtures_dir, tmp_path):
    jobs_root = tmp_path / "private-jobs-root-marker"
    app = create_app(Settings(jobs_root=jobs_root), token=TOKEN)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        success = client.post(
            "/api/jobs/extract-pages",
            headers=_auth(),
            files=[_upload(fixtures_dir / "simple-3page.pdf")],
            data={"pages": "1"},
        )
        assert success.status_code == 201, success.text
        job_id = success.json()["job_id"]
        detail = client.get(f"/api/jobs/{job_id}", headers=_auth())
        capabilities = client.get("/api/capabilities", headers=_auth())
        error = client.post(
            "/api/jobs/extract-pages",
            headers=_auth(),
            files=[_upload(fixtures_dir / "garbage.pdf")],
            data={"pages": "1"},
        )

        assert detail.status_code == 200
        assert capabilities.status_code == 200
        assert error.status_code == 422
        for response in (success, detail, capabilities, error):
            _assert_private_root_absent(response, jobs_root)


def test_invalid_numeric_parameter_is_hardened_and_leaves_no_job(fixtures_dir, tmp_path):
    app = create_app(Settings(jobs_root=tmp_path / "jobs"), token=TOKEN)
    state = app.state.ldf
    with TestClient(app, base_url="http://127.0.0.1") as client:
        response = client.post(
            "/api/jobs/split",
            headers=_auth(),
            files=[_upload(fixtures_dir / "simple-3page.pdf", name="sensitive.pdf")],
            data={"every": "not-an-integer"},
        )

        assert response.status_code == 422
        _assert_hardened(response)
        assert list(state.data_root.iterdir()) == []


def test_unexpected_runner_error_is_generic_hardened_and_clean(
    fixtures_dir, tmp_path, monkeypatch, caplog
):
    jobs_root = tmp_path / "private-jobs"
    private_path = jobs_root / "PRIVATE-SENSITIVE-PATH.pdf"

    def explode(*_args, **_kwargs):
        raise RuntimeError(f"unexpected failure at {private_path}")

    monkeypatch.setitem(api_module._OPERATIONS, "audit-boom", explode)
    app = create_app(Settings(jobs_root=jobs_root), token=TOKEN)
    state = app.state.ldf
    caplog.set_level(logging.DEBUG)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        response = client.post(
            "/api/jobs/audit-boom",
            headers=_auth(),
            files=[_upload(fixtures_dir / "simple-3page.pdf", name="sensitive.pdf")],
        )

        assert response.status_code == 500
        assert response.json() == {"detail": "Internal server error"}
        _assert_hardened(response)
        assert str(private_path) not in response.text
        assert str(private_path) not in caplog.text
        assert list(state.data_root.iterdir()) == []


def test_cumulative_input_limit_applies_across_all_uploads(fixtures_dir, tmp_path):
    source = fixtures_dir / "simple-3page.pdf"
    per_job_limit = source.stat().st_size + 1
    assert source.stat().st_size < per_job_limit < 2 * source.stat().st_size
    settings = Settings(
        jobs_root=tmp_path / "jobs",
        limits=ResourceLimits(max_input_bytes=per_job_limit),
    )
    app = create_app(settings, token=TOKEN)
    state = app.state.ldf
    with TestClient(app, base_url="http://127.0.0.1") as client:
        response = client.post(
            "/api/jobs/merge",
            headers=_auth(),
            files=[_upload(source, name="first.pdf"), _upload(source, name="second.pdf")],
        )

        assert response.status_code == 413
        _assert_hardened(response)
        assert list(state.data_root.iterdir()) == []


def test_output_limit_rejects_before_publish_and_cleans_job(fixtures_dir, tmp_path):
    settings = Settings(
        jobs_root=tmp_path / "jobs",
        limits=ResourceLimits(max_output_bytes=1),
    )
    app = create_app(settings, token=TOKEN)
    state = app.state.ldf
    with TestClient(app, base_url="http://127.0.0.1") as client:
        response = client.post(
            "/api/jobs/extract-pages",
            headers=_auth(),
            files=[_upload(fixtures_dir / "simple-3page.pdf")],
            data={"pages": "1"},
        )

        assert response.status_code == 422
        assert "output limit" in response.json()["detail"]
        _assert_hardened(response)
        assert list(state.data_root.iterdir()) == []


async def _send_chunked_request(app, body: bytes, *, content_type: str):
    chunks = [body[index : index + 64 * 1024] for index in range(0, len(body), 64 * 1024)]
    cursor = 0
    sent: list[dict[str, Any]] = []

    async def receive():
        nonlocal cursor
        if cursor >= len(chunks):
            return {"type": "http.disconnect"}
        chunk = chunks[cursor]
        cursor += 1
        return {"type": "http.request", "body": chunk, "more_body": cursor < len(chunks)}

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/jobs/extract-pages",
        "raw_path": b"/api/jobs/extract-pages",
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"host", b"127.0.0.1"),
            (b"x-ldf-token", TOKEN.encode("ascii")),
            (b"content-type", content_type.encode("ascii")),
            (b"transfer-encoding", b"chunked"),
        ],
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 8477),
    }
    assert all(name != b"content-length" for name, _value in scope["headers"])
    await app(scope, receive, send)

    start = next(message for message in sent if message["type"] == "http.response.start")
    response_body = b"".join(
        message.get("body", b"") for message in sent if message["type"] == "http.response.body"
    )
    headers = {name.decode("latin-1"): value.decode("latin-1") for name, value in start["headers"]}
    return start["status"], headers, response_body


def test_chunked_multipart_without_content_length_hits_receive_cap(tmp_path):
    boundary = "privacy-audit-boundary"
    file_bytes = b"%PDF-1.7\n" + b"x" * (3 * 1024 * 1024)
    body = (
        (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="files"; filename="oversized.pdf"\r\n'
            "Content-Type: application/pdf\r\n\r\n"
        ).encode("ascii")
        + file_bytes
        + f"\r\n--{boundary}--\r\n".encode("ascii")
    )
    jobs_root = tmp_path / "jobs"
    settings = Settings(
        jobs_root=jobs_root,
        limits=ResourceLimits(max_input_bytes=128),
    )
    app = create_app(settings, token=TOKEN)

    status, headers, response_body = asyncio.run(
        _send_chunked_request(
            app,
            body,
            content_type=f"multipart/form-data; boundary={boundary}",
        )
    )

    assert status == 413
    assert json.loads(response_body) == {"detail": "Request body exceeds the configured limit"}
    assert headers["cache-control"] == "no-store"
    assert "content-security-policy" in headers
    assert not jobs_root.exists()


def test_current_api_session_is_removed_on_shutdown(fixtures_dir, tmp_path):
    app = create_app(Settings(jobs_root=tmp_path / "jobs"), token=TOKEN)
    session_root = app.state.ldf.data_root
    with TestClient(app, base_url="http://127.0.0.1") as client:
        response = client.post(
            "/api/jobs/extract-pages",
            headers=_auth(),
            files=[_upload(fixtures_dir / "simple-3page.pdf")],
            data={"pages": "1"},
        )
        assert response.status_code == 201, response.text
        assert any(path.is_file() for path in session_root.rglob("*"))

    assert not session_root.exists()


def test_delete_cleanup_failure_returns_error_and_retains_job(fixtures_dir, tmp_path, monkeypatch):
    app = create_app(Settings(jobs_root=tmp_path / "jobs"), token=TOKEN)
    state = app.state.ldf
    with TestClient(app, base_url="http://127.0.0.1") as client:
        created = client.post(
            "/api/jobs/extract-pages",
            headers=_auth(),
            files=[_upload(fixtures_dir / "simple-3page.pdf")],
            data={"pages": "1"},
        )
        assert created.status_code == 201, created.text
        job_id = created.json()["job_id"]
        job_root = state.jobs[job_id].output_dir.parent

        with monkeypatch.context() as patcher:
            patcher.setattr(api_module, "remove_tree_with_retries", lambda _path: False)
            response = client.delete(f"/api/jobs/{job_id}", headers=_auth())

        assert response.status_code == 500
        _assert_hardened(response)
        assert job_id in state.jobs
        assert job_root.is_dir()
        assert client.get(f"/api/jobs/{job_id}", headers=_auth()).status_code == 200


def test_nonlocal_host_is_accepted_only_with_explicit_opt_in(tmp_path):
    local_app = create_app(Settings(jobs_root=tmp_path / "local"), token=TOKEN)
    with TestClient(local_app, base_url="http://hostile.example") as client:
        assert client.get("/api/health", headers=_auth()).status_code == 400

    nonlocal_app = create_app(
        Settings(jobs_root=tmp_path / "nonlocal"),
        token=TOKEN,
        allow_nonlocal=True,
    )
    with TestClient(nonlocal_app, base_url="http://hostile.example") as client:
        assert client.get("/api/health").status_code == 401
        health = client.get("/api/health", headers=_auth())
        index = client.get("/")

    assert health.status_code == 200
    assert health.json()["loopback_only"] is False
    assert "Non-loopback access is enabled" in index.text
    assert "processed locally; nothing leaves this machine" not in index.text


def test_strict_offline_overrides_nonlocal_web_opt_in(tmp_path):
    with pytest.raises(ValueError, match="strict-offline.*non-loopback"):
        create_app(
            Settings(strict_offline=True, jobs_root=tmp_path / "strict"),
            token=TOKEN,
            allow_nonlocal=True,
        )


def test_strict_operation_never_uses_socket_or_dns(monkeypatch, tmp_path):
    source = tmp_path / "external-uri.pdf"
    pdf = canvas.Canvas(str(source))
    pdf.drawString(72, 720, "Synthetic external URI annotation")
    pdf.linkURL("http://127.0.0.1:9/must-not-fetch", (72, 700, 300, 735), relative=0)
    pdf.save()

    calls: list[str] = []

    def blocked(name):
        def fail(*_args, **_kwargs):
            calls.append(name)
            raise AssertionError(f"network primitive used: {name}")

        return fail

    with monkeypatch.context() as patcher:
        patcher.setattr(socket, "socket", blocked("socket"))
        patcher.setattr(socket, "create_connection", blocked("create_connection"))
        patcher.setattr(socket, "getaddrinfo", blocked("getaddrinfo"))
        patcher.setattr(socket, "gethostbyname", blocked("gethostbyname"))
        report = extract_pages(
            source,
            tmp_path / "strict-output.pdf",
            PageRange(spec="1"),
            options=OrganizeOptions(
                settings=Settings(strict_offline=True, jobs_root=tmp_path / "jobs")
            ),
        )

    assert report.status.value == "success"
    assert report.details["strict_offline"] is True
    assert calls == []
