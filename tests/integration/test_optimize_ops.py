"""Lossless compression: real reduction, preservation, refusals, fail-closed compare."""

from __future__ import annotations

import pikepdf
import pytest

from localdocforge.operations import optimize as optimize_ops
from localdocforge.operations.organize import EncryptedInputError, OrganizeOptions
from localdocforge.pipelines.runner import PipelineError


def _bloated_copy(fixtures_dir, tmp_path):
    """An uncompressed, object-stream-free copy that lossless mode must shrink."""
    bloated = tmp_path / "bloated.pdf"
    with pikepdf.open(fixtures_dir / "simple-3page.pdf") as pdf:
        pdf.save(
            bloated,
            compress_streams=False,
            object_stream_mode=pikepdf.ObjectStreamMode.disable,
        )
    return bloated


class TestCompress:
    def test_lossless_compression_shrinks_and_render_verifies(self, fixtures_dir, tmp_path):
        bloated = _bloated_copy(fixtures_dir, tmp_path)
        output = tmp_path / "compressed.pdf"
        report = optimize_ops.compress_pdf(bloated, output)

        assert report.status == "success"
        assert output.is_file()
        assert report.output_bytes is not None and report.input_bytes is not None
        assert report.output_bytes < report.input_bytes
        assert report.engine == "pikepdf"

        stats = report.details["compression"]
        assert stats["input_bytes"] == bloated.stat().st_size
        assert stats["output_bytes"] == output.stat().st_size
        assert stats["bytes_saved"] > 0
        assert stats["reduction_percent"] > 0
        assert stats["images_recompressed"] == 0  # lossless never touches image data

        compare = report.details["render_compare"]
        assert compare["identical"] is True
        assert compare["pages_compared"] == [1, 2, 3]
        assert compare["max_channel_delta"] == 0

        assert report.validation is not None and report.validation.passed
        with pikepdf.open(output) as pdf:
            assert len(pdf.pages) == 3

    def test_document_level_structures_survive(self, fixtures_dir, tmp_path):
        output = tmp_path / "outline-compressed.pdf"
        report = optimize_ops.compress_pdf(fixtures_dir / "outline-6page.pdf", output)

        assert report.status == "success"
        with pikepdf.open(output) as pdf:
            assert "/Outlines" in pdf.Root  # page-moving ops drop this; compress must not
            assert str(pdf.docinfo.get("/Title")) == "Outlined Fixture"
            assert len(pdf.pages) == 6
        page_moving_codes = {"outlines-dropped", "xmp-metadata-dropped", "attachments-dropped"}
        assert not page_moving_codes & {w.code for w in report.fidelity_warnings}

    def test_no_reduction_is_reported_never_hidden(self, fixtures_dir, tmp_path):
        first = tmp_path / "first.pdf"
        second = tmp_path / "second.pdf"
        optimize_ops.compress_pdf(fixtures_dir / "simple-3page.pdf", first)
        report = optimize_ops.compress_pdf(first, second)

        stats = report.details["compression"]
        warned = any(w.code == "compress-no-reduction" for w in report.fidelity_warnings)
        assert warned == (stats["output_bytes"] >= stats["input_bytes"])

    def test_planned_preset_is_refused_honestly(self, fixtures_dir, tmp_path):
        with pytest.raises(PipelineError, match="not implemented"):
            optimize_ops.compress_pdf(
                fixtures_dir / "simple-3page.pdf", tmp_path / "n.pdf", preset="balanced"
            )

    def test_unknown_preset_is_refused(self, fixtures_dir, tmp_path):
        with pytest.raises(PipelineError, match="Unknown compression preset"):
            optimize_ops.compress_pdf(
                fixtures_dir / "simple-3page.pdf", tmp_path / "n.pdf", preset="maximum"
            )

    def test_encrypted_input_needs_password_and_warns(
        self, fixtures_dir, tmp_path, fixture_password
    ):
        target = tmp_path / "decrypted-compressed.pdf"
        with pytest.raises(EncryptedInputError):
            optimize_ops.compress_pdf(fixtures_dir / "encrypted.pdf", target)

        report = optimize_ops.compress_pdf(
            fixtures_dir / "encrypted.pdf",
            target,
            options=OrganizeOptions(password=fixture_password),
        )
        assert report.status == "success"
        assert any(
            w.code == "input-encryption-removed" and w.severity == "critical"
            for w in report.security_warnings
        )
        with pikepdf.open(target) as pdf:  # opens without a password
            assert len(pdf.pages) == 3

    def test_syntax_damaged_input_is_refused_not_repaired(self, fixtures_dir, tmp_path):
        with pytest.raises(PipelineError, match="repair is not an implemented operation"):
            optimize_ops.compress_pdf(fixtures_dir / "bad-xref.pdf", tmp_path / "n.pdf")

    def test_content_type_mismatch_is_refused(self, fixtures_dir, tmp_path):
        with pytest.raises(PipelineError):
            optimize_ops.compress_pdf(fixtures_dir / "fake.pdf", tmp_path / "n.pdf")

    def test_render_comparison_fails_closed(self, fixtures_dir, tmp_path, monkeypatch):
        def mismatch(*_args, **_kwargs):
            return {
                "pages_compared": [1],
                "identical": False,
                "mismatched_pages": [1],
                "max_channel_delta": 255,
            }

        monkeypatch.setattr(optimize_ops, "compare_page_renders", mismatch)
        output = tmp_path / "never-published.pdf"
        with pytest.raises(PipelineError, match="rendered differently"):
            optimize_ops.compress_pdf(fixtures_dir / "simple-3page.pdf", output)
        assert not output.exists()


class TestRenderComparison:
    def test_identical_documents_compare_identical(self, fixtures_dir):
        source = fixtures_dir / "simple-3page.pdf"
        result = optimize_ops.compare_page_renders(source, source, 3)
        assert result["identical"] is True
        assert result["mismatched_pages"] == []
        assert result["max_channel_delta"] == 0

    def test_visually_different_documents_are_detected(self, fixtures_dir):
        result = optimize_ops.compare_page_renders(
            fixtures_dir / "simple-3page.pdf", fixtures_dir / "rotated-mixed.pdf", 3
        )
        assert result["identical"] is False
        assert 2 in result["mismatched_pages"]  # page 2 is rotated in the fixture

    def test_sampling_includes_both_endpoints(self):
        indices = optimize_ops._sampled_page_indices(100, 5)
        assert indices[0] == 0
        assert indices[-1] == 99
        assert len(indices) == 5
