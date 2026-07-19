"""Security tests: hostile inputs, limits, containment, and cleanup."""

from __future__ import annotations

import pytest

from localdocforge.config.settings import Settings
from localdocforge.domain.models import ResourceLimits
from localdocforge.domain.pages import PageRange
from localdocforge.operations.images import ImagesToPdfOptions, images_to_pdf
from localdocforge.operations.organize import (
    OrganizeOptions,
    extract_pages,
    merge_pdfs,
)
from localdocforge.pipelines.runner import PipelineError
from localdocforge.security.subproc import ToolError, find_executable, minimal_env, run_tool


class TestInputHardening:
    def test_content_sniffing_beats_extension(self, fixtures_dir, out_dir):
        """A PNG renamed to .pdf must be rejected before any engine touches it."""
        with pytest.raises(PipelineError, match="image/png"):
            merge_pdfs(
                [fixtures_dir / "simple-3page.pdf", fixtures_dir / "fake.pdf"],
                out_dir / "never.pdf",
            )
        assert not (out_dir / "never.pdf").exists()

    def test_input_size_limit_enforced(self, fixtures_dir, out_dir):
        settings = Settings(limits=ResourceLimits(max_input_bytes=100))
        with pytest.raises(PipelineError, match="input.*limit|over the configured"):
            extract_pages(
                fixtures_dir / "simple-3page.pdf",
                out_dir / "never.pdf",
                PageRange(spec="1"),
                options=OrganizeOptions(settings=settings),
            )

    def test_page_limit_enforced(self, fixtures_dir, out_dir):
        settings = Settings(limits=ResourceLimits(max_pages=2))
        with pytest.raises(PipelineError, match="limit"):
            extract_pages(
                fixtures_dir / "simple-3page.pdf",
                out_dir / "never.pdf",
                PageRange(spec="1"),
                options=OrganizeOptions(settings=settings),
            )

    def test_decompression_bomb_guard(self, fixtures_dir, out_dir):
        settings = Settings(limits=ResourceLimits(max_image_pixels=10_000))
        with pytest.raises(PipelineError, match="DecompressionBomb"):
            images_to_pdf(
                [fixtures_dir / "images" / "photo.jpg"],
                out_dir / "never.pdf",
                options=ImagesToPdfOptions(settings=settings),
            )

    def test_malformed_pdf_fails_without_partial_output(self, fixtures_dir, out_dir):
        with pytest.raises(PipelineError):
            merge_pdfs(
                [fixtures_dir / "garbage.pdf", fixtures_dir / "simple-3page.pdf"],
                out_dir / "never.pdf",
            )
        assert list(out_dir.iterdir()) == []

    def test_no_staging_files_leak_into_destination(self, fixtures_dir, out_dir):
        extract_pages(fixtures_dir / "simple-3page.pdf", out_dir / "ok.pdf", PageRange(spec="1"))
        names = [p.name for p in out_dir.iterdir()]
        assert names == ["ok.pdf"]


class TestOutputContainment:
    def test_output_outside_allowed_roots_rejected(self, fixtures_dir, out_dir, tmp_path):
        jail = tmp_path / "jail"
        jail.mkdir()
        settings = Settings(allowed_output_roots=[jail])
        with pytest.raises(PipelineError, match="allowed output root|escapes"):
            extract_pages(
                fixtures_dir / "simple-3page.pdf",
                out_dir / "escape.pdf",  # outside the jail
                PageRange(spec="1"),
                options=OrganizeOptions(settings=settings),
            )
        assert not (out_dir / "escape.pdf").exists()

    def test_output_inside_allowed_roots_accepted(self, fixtures_dir, tmp_path):
        jail = tmp_path / "jail"
        jail.mkdir()
        settings = Settings(allowed_output_roots=[jail])
        report = extract_pages(
            fixtures_dir / "simple-3page.pdf",
            jail / "fine.pdf",
            PageRange(spec="1"),
            options=OrganizeOptions(settings=settings),
        )
        assert report.outputs[0].path == (jail / "fine.pdf").resolve()


class TestSubprocessHardening:
    def test_non_allowlisted_executable_refused(self):
        with pytest.raises(ToolError, match="allowlist"):
            run_tool("curl", ["https://example.com"])

    def test_find_executable_requires_allowlist(self):
        with pytest.raises(ToolError):
            find_executable("powershell")

    def test_minimal_env_drops_secrets(self, monkeypatch):
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "leaky")
        monkeypatch.setenv("LDF_INTERNAL_TOKEN", "leaky")
        env = minimal_env()
        assert "AWS_SECRET_ACCESS_KEY" not in env
        assert "LDF_INTERNAL_TOKEN" not in env
        assert "PATH" in env

    def test_missing_tool_reports_not_installed(self):
        # verapdf is absent in this environment (asserted by doctor tests);
        # calling it must fail cleanly, not attempt anything else.
        if find_executable("verapdf") is not None:
            pytest.skip("veraPDF unexpectedly installed")
        with pytest.raises(ToolError, match="not installed"):
            run_tool("verapdf", ["--version"])


class TestReportHygiene:
    def test_reports_never_contain_document_text(self, fixtures_dir, out_dir, fixture_password):
        report = merge_pdfs(
            [fixtures_dir / "encrypted.pdf", fixtures_dir / "simple-3page.pdf"],
            out_dir / "merged.pdf",
            options=OrganizeOptions(password=fixture_password),
        )
        serialized = report.model_dump_json()
        assert "MARKER-ALPHA" not in serialized  # no extracted document text
        assert fixture_password not in serialized  # never the password
