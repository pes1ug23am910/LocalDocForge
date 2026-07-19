"""Integration tests for structural operations on real generated PDFs.

Every assertion inspects actual output content — page counts via pikepdf,
text via pypdf extraction, boxes and rotation via the page dictionaries —
never mere file existence.
"""

from __future__ import annotations

import pikepdf
import pytest
from pypdf import PdfReader

from localdocforge.config.settings import Settings
from localdocforge.domain.models import ReportStatus
from localdocforge.domain.pages import PageRange
from localdocforge.jobs.workspace import CollisionPolicy
from localdocforge.operations.organize import (
    EncryptedInputError,
    OrganizeOptions,
    crop_pages,
    extract_pages,
    inspect_pdf,
    merge_pdfs,
    organize_pdf,
    remove_pages,
    rotate_pages,
    split_pdf,
)
from localdocforge.pipelines.runner import PipelineError


def page_text(path, index):
    return PdfReader(path).pages[index].extract_text()


def page_count(path):
    with pikepdf.open(path) as pdf:
        return len(pdf.pages)


class TestMerge:
    def test_merge_whole_files(self, fixtures_dir, out_dir):
        out = out_dir / "merged.pdf"
        report = merge_pdfs(
            [fixtures_dir / "simple-3page.pdf", fixtures_dir / "second-2page.pdf"], out
        )
        assert report.status == ReportStatus.SUCCESS
        assert report.engine == "pikepdf"
        assert report.validation is not None and report.validation.passed
        assert page_count(out) == 5
        assert "MARKER-ALPHA-PAGE-1" in page_text(out, 0)
        assert "MARKER-BETA-PAGE-2" in page_text(out, 4)

    def test_merge_with_ranges(self, fixtures_dir, out_dir):
        out = out_dir / "merged-ranges.pdf"
        report = merge_pdfs(
            [fixtures_dir / "simple-3page.pdf", fixtures_dir / "second-2page.pdf"],
            out,
            page_ranges=[PageRange(spec="1,3"), PageRange(spec="2")],
        )
        assert report.status == ReportStatus.SUCCESS
        assert page_count(out) == 3
        assert "MARKER-ALPHA-PAGE-1" in page_text(out, 0)
        assert "MARKER-ALPHA-PAGE-3" in page_text(out, 1)
        assert "MARKER-BETA-PAGE-2" in page_text(out, 2)

    def test_merge_copies_docinfo_from_first_input(self, fixtures_dir, out_dir):
        out = out_dir / "merged-meta.pdf"
        merge_pdfs([fixtures_dir / "simple-3page.pdf", fixtures_dir / "second-2page.pdf"], out)
        with pikepdf.open(out) as pdf:
            assert str(pdf.docinfo.get("/Title")) == "Simple Fixture"

    def test_merge_outline_input_reports_fidelity_loss(self, fixtures_dir, out_dir):
        report = merge_pdfs(
            [fixtures_dir / "outline-6page.pdf", fixtures_dir / "simple-3page.pdf"],
            out_dir / "with-outline.pdf",
        )
        codes = [warning.code for warning in report.fidelity_warnings]
        assert "outlines-dropped" in codes

    def test_merge_single_input_rejected(self, fixtures_dir, out_dir):
        with pytest.raises(PipelineError, match="at least two"):
            merge_pdfs([fixtures_dir / "simple-3page.pdf"], out_dir / "nope.pdf")

    def test_merge_garbage_input_fails_cleanly(self, fixtures_dir, out_dir):
        out = out_dir / "never.pdf"
        with pytest.raises(PipelineError) as excinfo:
            merge_pdfs([fixtures_dir / "simple-3page.pdf", fixtures_dir / "garbage.pdf"], out)
        assert not out.exists()
        assert excinfo.value.report is not None
        assert excinfo.value.report.status == ReportStatus.FAILED
        assert excinfo.value.report.errors

    def test_merge_polyglot_png_rejected_by_sniffing(self, fixtures_dir, out_dir):
        with pytest.raises(PipelineError, match="image/png"):
            merge_pdfs(
                [fixtures_dir / "simple-3page.pdf", fixtures_dir / "fake.pdf"],
                out_dir / "never.pdf",
            )

    def test_merge_encrypted_requires_password(self, fixtures_dir, out_dir):
        with pytest.raises(EncryptedInputError):
            merge_pdfs(
                [fixtures_dir / "encrypted.pdf", fixtures_dir / "simple-3page.pdf"],
                out_dir / "never.pdf",
            )

    def test_merge_encrypted_with_password(self, fixtures_dir, out_dir, fixture_password):
        out = out_dir / "from-encrypted.pdf"
        report = merge_pdfs(
            [fixtures_dir / "encrypted.pdf", fixtures_dir / "simple-3page.pdf"],
            out,
            options=OrganizeOptions(password=fixture_password),
        )
        assert report.status == ReportStatus.SUCCESS
        assert page_count(out) == 6

    def test_merge_unicode_filename(self, fixtures_dir, out_dir):
        out = out_dir / "unicode-merged.pdf"
        report = merge_pdfs(
            [fixtures_dir / "résumé-履歴書.pdf", fixtures_dir / "simple-3page.pdf"], out
        )
        assert report.status == ReportStatus.SUCCESS
        assert page_count(out) == 4


class TestSplit:
    def test_split_single_pages_default(self, fixtures_dir, out_dir):
        report = split_pdf(fixtures_dir / "simple-3page.pdf", out_dir)
        assert report.status == ReportStatus.SUCCESS
        files = sorted(out_dir.glob("*.pdf"))
        assert [f.name for f in files] == [
            "simple-3page-page-001.pdf",
            "simple-3page-page-002.pdf",
            "simple-3page-page-003.pdf",
        ]
        assert all(page_count(f) == 1 for f in files)
        assert "MARKER-ALPHA-PAGE-2" in page_text(files[1], 0)

    def test_split_by_ranges(self, fixtures_dir, out_dir):
        report = split_pdf(
            fixtures_dir / "simple-3page.pdf", out_dir, pages=PageRange(spec="1-2,3")
        )
        assert report.status == ReportStatus.SUCCESS
        names = sorted(f.name for f in out_dir.glob("*.pdf"))
        assert names == ["simple-3page-pages-1_2.pdf", "simple-3page-pages-3.pdf"]
        assert page_count(out_dir / "simple-3page-pages-1_2.pdf") == 2
        assert page_count(out_dir / "simple-3page-pages-3.pdf") == 1

    def test_split_every_n(self, fixtures_dir, out_dir):
        report = split_pdf(fixtures_dir / "outline-6page.pdf", out_dir, every=4)
        assert report.status == ReportStatus.SUCCESS
        assert page_count(out_dir / "outline-6page-part-001.pdf") == 4
        assert page_count(out_dir / "outline-6page-part-002.pdf") == 2

    def test_split_modes_are_exclusive(self, fixtures_dir, out_dir):
        with pytest.raises(PipelineError, match="not both"):
            split_pdf(
                fixtures_dir / "simple-3page.pdf",
                out_dir,
                pages=PageRange(spec="1"),
                every=2,
            )


class TestRemoveExtractOrganize:
    def test_remove_pages(self, fixtures_dir, out_dir):
        out = out_dir / "removed.pdf"
        report = remove_pages(fixtures_dir / "simple-3page.pdf", out, PageRange(spec="2"))
        assert report.status == ReportStatus.SUCCESS
        assert page_count(out) == 2
        full_text = page_text(out, 0) + page_text(out, 1)
        assert "MARKER-ALPHA-PAGE-2" not in full_text
        assert "MARKER-ALPHA-PAGE-1" in page_text(out, 0)
        assert "MARKER-ALPHA-PAGE-3" in page_text(out, 1)

    def test_remove_everything_rejected(self, fixtures_dir, out_dir):
        with pytest.raises(PipelineError, match="empty document"):
            remove_pages(
                fixtures_dir / "simple-3page.pdf", out_dir / "never.pdf", PageRange(spec="all")
            )

    def test_extract_pages_in_order(self, fixtures_dir, out_dir):
        out = out_dir / "extracted.pdf"
        report = extract_pages(fixtures_dir / "simple-3page.pdf", out, PageRange(spec="3,1"))
        assert report.status == ReportStatus.SUCCESS
        assert page_count(out) == 2
        assert "MARKER-ALPHA-PAGE-3" in page_text(out, 0)
        assert "MARKER-ALPHA-PAGE-1" in page_text(out, 1)

    def test_organize_reverse(self, fixtures_dir, out_dir):
        out = out_dir / "reversed.pdf"
        organize_pdf(fixtures_dir / "simple-3page.pdf", out, PageRange(spec="reverse"))
        assert "MARKER-ALPHA-PAGE-3" in page_text(out, 0)
        assert "MARKER-ALPHA-PAGE-1" in page_text(out, 2)

    def test_organize_with_duplicates(self, fixtures_dir, out_dir):
        out = out_dir / "duplicated.pdf"
        report = organize_pdf(fixtures_dir / "simple-3page.pdf", out, PageRange(spec="1,1,2"))
        assert report.status == ReportStatus.SUCCESS
        assert page_count(out) == 3
        assert "MARKER-ALPHA-PAGE-1" in page_text(out, 1)


class TestRotate:
    def test_rotate_all_pages(self, fixtures_dir, out_dir):
        out = out_dir / "rotated.pdf"
        report = rotate_pages(fixtures_dir / "simple-3page.pdf", out, degrees=90)
        assert report.status == ReportStatus.SUCCESS
        with pikepdf.open(out) as pdf:
            for page in pdf.pages:
                assert int(page.obj.get("/Rotate", 0)) == 90

    def test_rotate_selected_pages_is_relative(self, fixtures_dir, out_dir):
        out = out_dir / "rotated-relative.pdf"
        rotate_pages(
            fixtures_dir / "rotated-mixed.pdf", out, degrees=90, pages=PageRange(spec="2")
        )
        with pikepdf.open(out) as pdf:
            assert int(pdf.pages[0].obj.get("/Rotate", 0)) == 0
            # fixture page 2 already had /Rotate 90; +90 relative = 180
            assert int(pdf.pages[1].obj.get("/Rotate", 0)) == 180

    def test_rotate_rejects_non_right_angles(self, fixtures_dir, out_dir):
        with pytest.raises(PipelineError, match="multiple of 90"):
            rotate_pages(fixtures_dir / "simple-3page.pdf", out_dir / "never.pdf", degrees=45)


class TestCrop:
    def test_crop_sets_cropbox_and_warns_not_redaction(self, fixtures_dir, out_dir):
        out = out_dir / "cropped.pdf"
        report = crop_pages(
            fixtures_dir / "simple-3page.pdf", out, box=(50, 50, 400, 500)
        )
        assert report.status == ReportStatus.SUCCESS
        assert any(w.code == "crop-is-not-redaction" for w in report.security_warnings)
        with pikepdf.open(out) as pdf:
            box = [float(v) for v in pdf.pages[0].obj["/CropBox"]]
            assert box == [50.0, 50.0, 400.0, 500.0]

    def test_crop_clamped_to_page(self, fixtures_dir, out_dir):
        out = out_dir / "cropped-clamped.pdf"
        report = crop_pages(
            fixtures_dir / "simple-3page.pdf", out, box=(0, 0, 5000, 5000)
        )
        assert any(w.code == "crop-clamped" for w in report.fidelity_warnings)
        with pikepdf.open(out) as pdf:
            box = [float(v) for v in pdf.pages[0].obj["/CropBox"]]
            media = [float(v) for v in pdf.pages[0].mediabox]
            assert box == media

    def test_crop_outside_page_rejected(self, fixtures_dir, out_dir):
        with pytest.raises(PipelineError, match="does not intersect"):
            crop_pages(
                fixtures_dir / "simple-3page.pdf",
                out_dir / "never.pdf",
                box=(2000, 2000, 3000, 3000),
            )


class TestCollisionAndReports:
    def test_collision_fail_preserves_existing(self, fixtures_dir, out_dir):
        out = out_dir / "existing.pdf"
        out.write_bytes(b"%PDF-existing")
        with pytest.raises(PipelineError, match="already exists"):
            extract_pages(
                fixtures_dir / "simple-3page.pdf", out, PageRange(spec="1"),
                options=OrganizeOptions(collision=CollisionPolicy.FAIL),
            )
        assert out.read_bytes() == b"%PDF-existing"

    def test_collision_rename_writes_sibling(self, fixtures_dir, out_dir):
        out = out_dir / "existing.pdf"
        out.write_bytes(b"%PDF-existing")
        report = extract_pages(
            fixtures_dir / "simple-3page.pdf", out, PageRange(spec="1"),
            options=OrganizeOptions(collision=CollisionPolicy.RENAME),
        )
        published = report.outputs[0].path
        assert published.name == "existing (1).pdf"
        assert page_count(published) == 1
        assert out.read_bytes() == b"%PDF-existing"

    def test_collision_overwrite_replaces(self, fixtures_dir, out_dir):
        out = out_dir / "existing.pdf"
        out.write_bytes(b"%PDF-existing")
        extract_pages(
            fixtures_dir / "simple-3page.pdf", out, PageRange(spec="1"),
            options=OrganizeOptions(collision=CollisionPolicy.OVERWRITE),
        )
        assert page_count(out) == 1

    def test_report_bookkeeping_fields(self, fixtures_dir, out_dir):
        report = extract_pages(
            fixtures_dir / "simple-3page.pdf", out_dir / "one.pdf", PageRange(spec="1")
        )
        assert report.job_id
        assert report.elapsed_seconds is not None and report.elapsed_seconds >= 0
        assert report.finished_at is not None
        assert report.input_bytes and report.output_bytes
        assert report.input_page_count == 3
        assert report.output_page_count == 1
        human = report.to_human()
        assert "extract-pages" in human and "success" in human

    def test_workspace_cleaned_after_success_and_failure(self, fixtures_dir, out_dir, tmp_path):
        jobs_root = tmp_path / "jobs"
        settings = Settings(jobs_root=jobs_root)
        extract_pages(
            fixtures_dir / "simple-3page.pdf", out_dir / "ok.pdf", PageRange(spec="1"),
            options=OrganizeOptions(settings=settings),
        )
        with pytest.raises(PipelineError):
            merge_pdfs(
                [fixtures_dir / "simple-3page.pdf", fixtures_dir / "garbage.pdf"],
                out_dir / "never.pdf",
                options=OrganizeOptions(settings=settings),
            )
        leftovers = list(jobs_root.glob("ldf-job-*")) if jobs_root.exists() else []
        assert leftovers == []


class TestInspect:
    def test_inspect_simple(self, fixtures_dir):
        info = inspect_pdf(fixtures_dir / "simple-3page.pdf")
        assert info["page_count"] == 3
        assert info["encrypted"] is False
        assert info["has_outlines"] is False
        assert info["has_acroform"] is False
        assert info["docinfo"].get("/Title") == "Simple Fixture"

    def test_inspect_outline(self, fixtures_dir):
        info = inspect_pdf(fixtures_dir / "outline-6page.pdf")
        assert info["page_count"] == 6
        assert info["has_outlines"] is True

    def test_inspect_encrypted(self, fixtures_dir, fixture_password):
        with pytest.raises(EncryptedInputError):
            inspect_pdf(fixtures_dir / "encrypted.pdf")
        info = inspect_pdf(fixtures_dir / "encrypted.pdf", password=fixture_password)
        assert info["encrypted"] is True
        assert info["page_count"] == 3

    def test_inspect_rejects_non_pdf(self, fixtures_dir):
        from localdocforge.security.sniff import ContentTypeError

        with pytest.raises(ContentTypeError):
            inspect_pdf(fixtures_dir / "fake.pdf")
