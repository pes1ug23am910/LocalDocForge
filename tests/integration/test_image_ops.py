"""Integration tests for images-to-pdf and pdf-to-images with real decoding."""

from __future__ import annotations

import pikepdf
import pytest
from PIL import Image

from localdocforge.domain.models import ReportStatus
from localdocforge.domain.pages import PageRange
from localdocforge.operations.images import (
    ImagesToPdfOptions,
    PdfToImagesOptions,
    images_to_pdf,
    parse_page_size,
    pdf_to_images,
)
from localdocforge.pipelines.runner import PipelineError


def page_sizes(path):
    with pikepdf.open(path) as pdf:
        return [
            (round(float(p.mediabox[2]) - float(p.mediabox[0]), 1),
             round(float(p.mediabox[3]) - float(p.mediabox[1]), 1))
            for p in pdf.pages
        ]


class TestParsePageSize:
    def test_named_sizes(self):
        assert parse_page_size("A4") == (595.276, 841.89)
        assert parse_page_size("letter") == (612.0, 792.0)
        assert parse_page_size("image") is None

    def test_custom_sizes(self):
        width, height = parse_page_size("210x297mm")
        assert round(width, 1) == 595.3 and round(height, 1) == 841.9
        assert parse_page_size("612x792pt") == (612.0, 792.0)
        width_in, _ = parse_page_size("8.5x11in")
        assert round(width_in, 1) == 612.0

    def test_invalid(self):
        with pytest.raises(PipelineError):
            parse_page_size("banana")
        with pytest.raises(PipelineError):
            parse_page_size("0x100mm")


class TestImagesToPdf:
    def test_two_images_a4(self, fixtures_dir, out_dir):
        out = out_dir / "album.pdf"
        report = images_to_pdf(
            [fixtures_dir / "images" / "photo.jpg", fixtures_dir / "images" / "diagram.png"],
            out,
        )
        assert report.status == ReportStatus.SUCCESS
        assert report.validation is not None and report.validation.passed
        sizes = page_sizes(out)
        assert len(sizes) == 2
        for width, height in sizes:
            assert abs(width - 595.3) < 1 and abs(height - 841.9) < 1

    def test_multipage_tiff_expands(self, fixtures_dir, out_dir):
        out = out_dir / "scans.pdf"
        report = images_to_pdf([fixtures_dir / "images" / "scan-3page.tiff"], out)
        assert report.status == ReportStatus.SUCCESS
        assert len(page_sizes(out)) == 3

    def test_mixed_formats(self, fixtures_dir, out_dir):
        images = fixtures_dir / "images"
        out = out_dir / "mixed.pdf"
        report = images_to_pdf(
            [images / "photo.jpg", images / "bitmap.bmp", images / "web.webp"], out
        )
        assert report.status == ReportStatus.SUCCESS
        assert len(page_sizes(out)) == 3

    def test_page_size_image_follows_source(self, fixtures_dir, out_dir):
        out = out_dir / "native.pdf"
        options = ImagesToPdfOptions(page_size="image", dpi=200)
        report = images_to_pdf([fixtures_dir / "images" / "photo.jpg"], out, options=options)
        assert report.status == ReportStatus.SUCCESS
        (size,) = page_sizes(out)
        # 800x600 px at 200 dpi -> 288 x 216 pt
        assert abs(size[0] - 288.0) < 1 and abs(size[1] - 216.0) < 1

    def test_exif_orientation_respected(self, fixtures_dir, out_dir):
        out = out_dir / "exif.pdf"
        options = ImagesToPdfOptions(page_size="image")
        images_to_pdf([fixtures_dir / "images" / "exif-rotated.jpg"], out, options=options)
        (size,) = page_sizes(out)
        assert size[1] > size[0], "EXIF orientation 6 must produce a portrait page"

    def test_invalid_fit_mode(self, fixtures_dir, out_dir):
        with pytest.raises(PipelineError, match="fit mode"):
            images_to_pdf(
                [fixtures_dir / "images" / "photo.jpg"],
                out_dir / "never.pdf",
                options=ImagesToPdfOptions(fit="magic"),
            )

    def test_non_image_rejected(self, fixtures_dir, out_dir):
        with pytest.raises(PipelineError, match="application/pdf"):
            images_to_pdf([fixtures_dir / "simple-3page.pdf"], out_dir / "never.pdf")


class TestPdfToImages:
    def test_render_all_pages_png(self, fixtures_dir, out_dir):
        report = pdf_to_images(
            fixtures_dir / "simple-3page.pdf",
            out_dir,
            options=PdfToImagesOptions(dpi=96),
        )
        assert report.status == ReportStatus.SUCCESS
        files = sorted(out_dir.glob("*.png"))
        assert [f.name for f in files] == [
            "simple-3page-page-001.png",
            "simple-3page-page-002.png",
            "simple-3page-page-003.png",
        ]
        with Image.open(files[0]) as image:
            # A4 at 96 dpi
            assert abs(image.width - 794) <= 2
            assert abs(image.height - 1123) <= 2

    def test_page_selection_and_jpeg(self, fixtures_dir, out_dir):
        report = pdf_to_images(
            fixtures_dir / "simple-3page.pdf",
            out_dir,
            options=PdfToImagesOptions(image_format="jpeg", dpi=72, pages=PageRange(spec="2")),
        )
        assert report.status == ReportStatus.SUCCESS
        files = list(out_dir.glob("*.jpg"))
        assert len(files) == 1
        assert files[0].name == "simple-3page-page-002.jpg"
        with Image.open(files[0]) as image:
            assert image.format == "JPEG"

    def test_repeated_page_selection_gets_deterministic_unique_names(
        self, fixtures_dir, out_dir
    ):
        report = pdf_to_images(
            fixtures_dir / "simple-3page.pdf",
            out_dir,
            options=PdfToImagesOptions(dpi=72, pages=PageRange(spec="1,1")),
        )
        assert report.status == ReportStatus.SUCCESS
        files = sorted(out_dir.glob("*.png"))
        assert [path.name for path in files] == [
            "simple-3page-page-001-repeat-002.png",
            "simple-3page-page-001.png",
        ]
        for path in files:
            with Image.open(path) as image:
                image.load()

    def test_unsupported_format_rejected(self, fixtures_dir, out_dir):
        with pytest.raises(PipelineError, match="Unsupported image format"):
            pdf_to_images(
                fixtures_dir / "simple-3page.pdf",
                out_dir,
                options=PdfToImagesOptions(image_format="gif"),
            )

    def test_garbage_pdf_fails_cleanly(self, fixtures_dir, out_dir):
        with pytest.raises(PipelineError):
            pdf_to_images(fixtures_dir / "garbage.pdf", out_dir)
        assert list(out_dir.iterdir()) == []
