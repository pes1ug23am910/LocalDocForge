"""Regression coverage for independently audited PDF integrity failures.

All fixtures are synthetic and stay inside pytest's temporary directory.  The
tests exercise real pikepdf/PDFium/Pillow paths rather than mock validators.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pikepdf
import pytest
from PIL import Image, ImageDraw
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

import localdocforge.validation.pdf_checks as pdf_checks
from localdocforge.config.settings import Settings
from localdocforge.domain.models import WarningSeverity
from localdocforge.domain.pages import PageRange
from localdocforge.operations.images import (
    ImagesToPdfOptions,
    PdfToImagesOptions,
    images_to_pdf,
    pdf_to_images,
)
from localdocforge.operations.organize import (
    OrganizeOptions,
    merge_pdfs,
    organize_pdf,
    remove_pages,
    rotate_pages,
)
from localdocforge.pipelines.runner import PipelineError
from localdocforge.validation.pdf_checks import render_pdf_page, validate_pdf


def _settings(root: Path) -> Settings:
    return Settings(
        strict_offline=True,
        jobs_root=root / "jobs",
        allowed_output_roots=[root],
    )


@pytest.fixture()
def transparent_png(tmp_path: Path) -> Path:
    """Transparent red pixels surround an opaque blue center marker."""
    path = tmp_path / "transparent-red.png"
    image = Image.new("RGBA", (32, 32), (255, 0, 0, 0))
    ImageDraw.Draw(image).rectangle((10, 10, 21, 21), fill=(0, 0, 255, 255))
    image.save(path)
    image.close()
    return path


def _make_rich_pdf(path: Path, label: str) -> Path:
    """Create a two-page PDF containing the document features under audit."""
    c = canvas.Canvas(str(path), pagesize=letter)
    c.setTitle(f"Rich {label} DocInfo")
    c.setAuthor("LocalDocForge synthetic audit")
    width, height = letter

    c.bookmarkPage(f"{label}-p1")
    c.addOutlineEntry(f"{label} page 1", f"{label}-p1", level=0)
    c.drawString(48, height - 60, f"MARKER-{label}-PAGE-1")
    c.linkRect(
        "",
        f"{label}-p2",
        (45, height - 100, 240, height - 75),
        relative=0,
        thickness=1,
    )
    c.acroForm.textfield(
        name="shared",
        tooltip="synthetic field",
        x=48,
        y=height - 150,
        width=160,
        height=24,
        value=f"{label}-value",
        forceBorder=True,
    )
    c.showPage()

    c.bookmarkPage(f"{label}-p2")
    c.addOutlineEntry(f"{label} page 2", f"{label}-p2", level=0)
    c.drawString(48, height - 60, f"MARKER-{label}-PAGE-2")
    c.showPage()
    c.save()

    staged = path.with_name(f"{path.stem}-enriched.pdf")
    with pikepdf.open(path) as pdf:
        pdf.attachments[f"{label}-attachment.txt"] = (
            f"SYNTHETIC ATTACHMENT {label}".encode("ascii")
        )

        action = pdf.make_indirect(
            pikepdf.Dictionary(
                S=pikepdf.Name("/JavaScript"),
                JS=pikepdf.String("app.alert('synthetic audit marker')"),
            )
        )
        names = pdf.Root.get("/Names")
        names["/JavaScript"] = pikepdf.Dictionary(
            Names=pikepdf.Array([pikepdf.String(f"{label}-DocJS"), action])
        )
        pdf.Root["/OpenAction"] = action
        pdf.Root["/PageLabels"] = pikepdf.Dictionary(
            Nums=pikepdf.Array([0, pikepdf.Dictionary(S=pikepdf.Name("/r"))])
        )

        xmp = (
            b"<x:xmpmeta xmlns:x='adobe:ns:meta/'>"
            b"<rdf:RDF xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'/>"
            b"</x:xmpmeta>"
        )
        metadata = pdf.make_stream(xmp)
        metadata["/Type"] = pikepdf.Name("/Metadata")
        metadata["/Subtype"] = pikepdf.Name("/XML")
        pdf.Root["/Metadata"] = metadata

        page = pdf.pages[0]
        signature = pdf.make_indirect(
            pikepdf.Dictionary(
                Type=pikepdf.Name("/Sig"),
                Filter=pikepdf.Name("/Adobe.PPKLite"),
                SubFilter=pikepdf.Name("/adbe.pkcs7.detached"),
                ByteRange=pikepdf.Array([0, 0, 0, 0]),
                Contents=pikepdf.String("SYNTHETIC-NOT-A-REAL-SIGNATURE"),
            )
        )
        widget = pdf.make_indirect(
            pikepdf.Dictionary(
                Type=pikepdf.Name("/Annot"),
                Subtype=pikepdf.Name("/Widget"),
                FT=pikepdf.Name("/Sig"),
                T=pikepdf.String(f"{label}-Signature1"),
                Rect=pikepdf.Array([260, 500, 460, 540]),
                P=page.obj,
                V=signature,
                F=4,
            )
        )
        page.obj["/Annots"].append(widget)
        pdf.Root.AcroForm["/Fields"].append(widget)
        pdf.save(staged)
    staged.replace(path)
    return path


@pytest.fixture()
def rich_pdfs(tmp_path: Path) -> tuple[Path, Path]:
    return (
        _make_rich_pdf(tmp_path / "rich-a.pdf", "A"),
        _make_rich_pdf(tmp_path / "rich-b.pdf", "B"),
    )


def test_images_to_pdf_flattens_alpha_onto_requested_background(
    tmp_path: Path, transparent_png: Path
) -> None:
    output = tmp_path / "alpha.pdf"
    source_hash = hashlib.sha256(transparent_png.read_bytes()).digest()
    report = images_to_pdf(
        [transparent_png],
        output,
        options=ImagesToPdfOptions(
            page_size="32x32pt",
            margin_pt=0,
            background="white",
            dpi=72,
            settings=_settings(tmp_path),
        ),
    )

    assert report.validation is not None and report.validation.passed
    assert hashlib.sha256(transparent_png.read_bytes()).digest() == source_hash
    rendered = render_pdf_page(output, 0, scale=1.0).convert("RGB")
    try:
        corner = rendered.getpixel((2, 2))
        center = rendered.getpixel((16, 16))
        assert all(channel >= 245 for channel in corner), corner
        assert center[2] > center[0] + 80 and center[2] > center[1] + 80, center
    finally:
        rendered.close()


def test_images_to_pdf_rejects_margin_that_erases_drawable_area(
    tmp_path: Path, transparent_png: Path
) -> None:
    output = tmp_path / "blank.pdf"
    with pytest.raises(PipelineError, match="no drawable page area"):
        images_to_pdf(
            [transparent_png],
            output,
            options=ImagesToPdfOptions(
                page_size="A4",
                margin_pt=1000,
                settings=_settings(tmp_path),
            ),
        )
    assert not output.exists()


def test_pdf_to_webp_quality_changes_encoded_output(fixtures_dir: Path, tmp_path: Path) -> None:
    low_dir = tmp_path / "low"
    high_dir = tmp_path / "high"
    low_dir.mkdir()
    high_dir.mkdir()
    source = fixtures_dir / "simple-3page.pdf"

    for output_dir, quality in ((low_dir, 1), (high_dir, 100)):
        report = pdf_to_images(
            source,
            output_dir,
            options=PdfToImagesOptions(
                image_format="webp",
                dpi=72,
                pages=PageRange(spec="1"),
                jpeg_quality=quality,
                settings=_settings(tmp_path),
            ),
        )
        assert report.validation is not None and report.validation.passed

    low = next(low_dir.glob("*.webp"))
    high = next(high_dir.glob("*.webp"))
    assert low.read_bytes() != high.read_bytes()
    with Image.open(low) as low_image, Image.open(high) as high_image:
        low_image.load()
        high_image.load()
        assert low_image.size == high_image.size


def test_damaged_xref_is_rejected_by_validation_and_all_pdf_inputs(
    fixtures_dir: Path, tmp_path: Path
) -> None:
    damaged = fixtures_dir / "bad-xref.pdf"
    validation = validate_pdf(damaged, render_pages=True, render_sample_limit=None)
    syntax = next(check for check in validation.checks if check.name == "pdf-syntax")
    assert not validation.passed
    assert not syntax.passed

    organized = tmp_path / "organized.pdf"
    with pytest.raises(PipelineError):
        organize_pdf(
            damaged,
            organized,
            PageRange(spec="all"),
            options=OrganizeOptions(settings=_settings(tmp_path)),
        )
    assert not organized.exists()

    rendered_dir = tmp_path / "rendered"
    rendered_dir.mkdir()
    with pytest.raises(PipelineError):
        pdf_to_images(
            damaged,
            rendered_dir,
            options=PdfToImagesOptions(dpi=72, settings=_settings(tmp_path)),
        )
    assert list(rendered_dir.iterdir()) == []


def test_page_moves_report_every_document_level_loss(
    tmp_path: Path, rich_pdfs: tuple[Path, Path]
) -> None:
    output = tmp_path / "merged.pdf"
    with pytest.warns(pikepdf.PageCopyWarning):
        report = merge_pdfs(
            list(rich_pdfs),
            output,
            options=OrganizeOptions(settings=_settings(tmp_path)),
        )
    codes = {warning.code for warning in report.fidelity_warnings}
    assert {
        "outlines-dropped",
        "form-fields-detached",
        "attachments-dropped",
        "xmp-metadata-dropped",
        "page-labels-dropped",
        "document-actions-dropped",
        "signature-semantics-dropped",
        "form-field-name-conflict",
    } <= codes
    signature_warnings = [
        warning
        for warning in report.fidelity_warnings
        if warning.code == "signature-semantics-dropped"
    ]
    assert signature_warnings
    assert all(warning.severity == WarningSeverity.CRITICAL for warning in signature_warnings)

    with pikepdf.open(output) as pdf:
        assert len(pdf.pages) == 4
        assert list(pdf.check_pdf_syntax()) == []
        assert str(pdf.docinfo.get("/Title")) == "Rich A DocInfo"
        assert "/Outlines" not in pdf.Root
        assert "/AcroForm" not in pdf.Root
        assert "/Metadata" not in pdf.Root
        assert "/PageLabels" not in pdf.Root
        assert "/OpenAction" not in pdf.Root
        names = pdf.Root.get("/Names", {})
        assert "/EmbeddedFiles" not in names
        assert "/JavaScript" not in names


def test_remove_pages_refuses_unrewritable_page_references(
    tmp_path: Path, rich_pdfs: tuple[Path, Path]
) -> None:
    output = tmp_path / "removed.pdf"
    with pytest.raises(PipelineError, match="cannot safely rewrite"):
        remove_pages(
            rich_pdfs[0],
            output,
            PageRange(spec="2"),
            options=OrganizeOptions(settings=_settings(tmp_path)),
        )
    assert not output.exists()


def test_encrypted_inputs_disclose_unprotected_outputs(
    fixtures_dir: Path, fixture_password: str, tmp_path: Path
) -> None:
    source = fixtures_dir / "encrypted.pdf"
    source_hash = hashlib.sha256(source.read_bytes()).digest()

    rotated = tmp_path / "rotated.pdf"
    rotate_report = rotate_pages(
        source,
        rotated,
        degrees=90,
        options=OrganizeOptions(password=fixture_password, settings=_settings(tmp_path)),
    )
    rendered_dir = tmp_path / "rendered-encrypted"
    rendered_dir.mkdir()
    image_report = pdf_to_images(
        source,
        rendered_dir,
        options=PdfToImagesOptions(
            dpi=72,
            pages=PageRange(spec="1"),
            password=fixture_password,
            settings=_settings(tmp_path),
        ),
    )

    for report in (rotate_report, image_report):
        warnings = [
            warning
            for warning in report.security_warnings
            if warning.code == "input-encryption-removed"
        ]
        assert warnings and warnings[0].severity == WarningSeverity.CRITICAL
    with pikepdf.open(rotated) as pdf:
        assert not pdf.is_encrypted
    with Image.open(next(rendered_dir.glob("*.png"))) as image:
        image.load()
    assert hashlib.sha256(source.read_bytes()).digest() == source_hash


def test_rotate_reports_cryptographic_signature_invalidation(
    tmp_path: Path, rich_pdfs: tuple[Path, Path]
) -> None:
    source = rich_pdfs[0]
    source_hash = hashlib.sha256(source.read_bytes()).digest()
    output = tmp_path / "rotated-signed.pdf"

    report = rotate_pages(
        source,
        output,
        degrees=90,
        options=OrganizeOptions(settings=_settings(tmp_path)),
    )

    warnings = [
        warning
        for warning in report.security_warnings
        if warning.code == "signature-invalidated"
    ]
    assert warnings and warnings[0].severity == WarningSeverity.CRITICAL
    with pikepdf.open(output) as pdf:
        assert list(pdf.check_pdf_syntax()) == []
        assert int(pdf.pages[0].obj.get("/Rotate", 0)) == 90
    assert hashlib.sha256(source.read_bytes()).digest() == source_hash


def test_routine_render_sampling_includes_first_and_last_pages(
    fixtures_dir: Path, monkeypatch
) -> None:
    rendered_indices: list[int] = []
    real_render = pdf_checks.render_pdf_page

    def recording_render(path, page_index, **kwargs):
        rendered_indices.append(page_index)
        return real_render(path, page_index, **kwargs)

    monkeypatch.setattr(pdf_checks, "render_pdf_page", recording_render)
    result = pdf_checks.validate_pdf(
        fixtures_dir / "outline-6page.pdf",
        render_pages=True,
        render_sample_limit=3,
    )

    assert result.passed
    assert len(rendered_indices) == 3
    assert rendered_indices[0] == 0
    assert rendered_indices[-1] == 5
