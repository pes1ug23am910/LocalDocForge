"""Integration tests for images-to-pdf and pdf-to-images with real decoding."""

from __future__ import annotations

from typing import get_type_hints

import pikepdf
import pytest
from PIL import Image

from localdocforge.config.settings import Settings
from localdocforge.domain.models import ReportStatus, ResourceLimits
from localdocforge.domain.pages import PageRange
from localdocforge.jobs.workspace import CollisionPolicy
from localdocforge.operations.images import (
    CONVERT_PRESETS,
    ImagesToPdfOptions,
    PdfToImagesOptions,
    images_to_pdf,
    parse_page_size,
    pdf_to_images,
    resolve_pdf_to_images_options,
)
from localdocforge.pipelines.runner import PipelineError


def page_sizes(path):
    with pikepdf.open(path) as pdf:
        return [
            (round(float(p.mediabox[2]) - float(p.mediabox[0]), 1),
             round(float(p.mediabox[3]) - float(p.mediabox[1]), 1))
            for p in pdf.pages
        ]


def fidelity_codes(report):
    return {warning.code for warning in report.fidelity_warnings}


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

    def test_jpeg2000_is_rejected_before_bundled_codec_decode(self, tmp_path, out_dir):
        source = tmp_path / "untrusted.jp2"
        source.write_bytes(b"\x00\x00\x00\x0cjP  \r\n\x87\n\x00\x00\x00\x14ftypjp2 ")
        output = out_dir / "never.pdf"

        with pytest.raises(PipelineError, match="Could not identify"):
            images_to_pdf([source], output)

        assert not output.exists()


class TestPdfToImages:
    def test_defaults_and_llm_preset_resolution(self):
        options = PdfToImagesOptions()
        hints = get_type_hints(PdfToImagesOptions)
        assert hints["image_format"] is str
        assert hints["dpi"] is int
        assert hints["jpeg_quality"] is int
        assert options.image_format == "png"
        assert options.dpi == 150
        assert options.jpeg_quality == 90
        defaults = resolve_pdf_to_images_options(options)
        assert defaults == {
            "image_format": "png",
            "quality": 90,
            "dpi": 150,
            "max_dimension": None,
        }

        preset_options = PdfToImagesOptions(preset="llm")
        preset = resolve_pdf_to_images_options(preset_options)
        assert preset == CONVERT_PRESETS["llm"] | {"dpi": 150}

        explicit_defaults = PdfToImagesOptions(
            image_format="png", dpi=150, jpeg_quality=90
        )
        explicit_preset_defaults = PdfToImagesOptions(
            image_format="png", dpi=150, jpeg_quality=90, preset="llm"
        )
        assert options == explicit_defaults
        assert preset_options != explicit_preset_defaults

        positional = PdfToImagesOptions(
            "jpeg", 72, None, 80, CollisionPolicy.RENAME
        )
        assert positional.collision is CollisionPolicy.RENAME
        assert positional.preset is None

    def test_explicit_format_and_quality_override_llm_preset(self):
        resolved = resolve_pdf_to_images_options(
            PdfToImagesOptions(preset="llm", image_format="webp", jpeg_quality=37)
        )
        assert resolved["image_format"] == "webp"
        assert resolved["quality"] == 37
        assert resolved["max_dimension"] == CONVERT_PRESETS["llm"]["max_dimension"]

        explicit_legacy_defaults = resolve_pdf_to_images_options(
            PdfToImagesOptions(
                preset="llm", image_format="png", jpeg_quality=90, dpi=150
            )
        )
        assert explicit_legacy_defaults["image_format"] == "png"
        assert explicit_legacy_defaults["quality"] == 90
        assert explicit_legacy_defaults["dpi"] == 150
        assert explicit_legacy_defaults["max_dimension"] is None

    def test_explicit_dpi_disables_llm_pixel_cap(self):
        resolved = resolve_pdf_to_images_options(
            PdfToImagesOptions(preset="llm", dpi=200)
        )
        assert resolved["dpi"] == 200
        assert resolved["max_dimension"] is None
        assert resolved["image_format"] == CONVERT_PRESETS["llm"]["image_format"]

    def test_unknown_preset_is_refused(self):
        with pytest.raises(PipelineError, match="Unknown preset"):
            resolve_pdf_to_images_options(PdfToImagesOptions(preset="tiny"))

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

        renamed = pdf_to_images(
            fixtures_dir / "simple-3page.pdf",
            out_dir,
            options=PdfToImagesOptions(
                image_format="jpeg",
                dpi=72,
                pages=PageRange(spec="2"),
                collision=CollisionPolicy.RENAME,
            ),
        )
        assert renamed.outputs[0].path != files[0]
        assert renamed.outputs[0].path.is_file()
        assert renamed.details["dimensions"][0]["output_index"] == 0
        assert "output" not in renamed.details["dimensions"][0]

    def test_llm_preset_caps_mixed_pages_without_upscaling_small_page(
        self, fixtures_dir, out_dir
    ):
        report = pdf_to_images(
            fixtures_dir / "mixed-sizes.pdf",
            out_dir,
            options=PdfToImagesOptions(preset="llm"),
        )
        files = sorted(out_dir.glob("*.jpg"))
        assert len(files) == 3
        actual_dimensions = []
        for path in files:
            with Image.open(path) as image:
                assert image.format == "JPEG"
                assert image.mode == "RGB"
                actual_dimensions.append(image.size)
        assert actual_dimensions == [(1109, 1568), (1568, 1212), (625, 625)]
        assert max(actual_dimensions[2]) == 625  # 300 pt at the 150-DPI ceiling

        details = report.details
        assert details["format"] == CONVERT_PRESETS["llm"]["image_format"]
        assert details["quality"] == CONVERT_PRESETS["llm"]["quality"]
        assert details["configured_quality"] == CONVERT_PRESETS["llm"]["quality"]
        assert details["jpeg_quality"] == CONVERT_PRESETS["llm"]["quality"]
        assert details["max_dimension"] == CONVERT_PRESETS["llm"]["max_dimension"]
        assert details["dpi_mode"] == "per-page-cap"
        assert [
            (item["width"], item["height"]) for item in details["dimensions"]
        ] == actual_dimensions
        assert details["dimensions"][2]["effective_dpi"] == 150.0
        assert "image-downscaled" in fidelity_codes(report)

    def test_llm_preset_fractional_page_never_rounds_to_1569(
        self, fixtures_dir, out_dir
    ):
        report = pdf_to_images(
            fixtures_dir / "fractional-size.pdf",
            out_dir,
            options=PdfToImagesOptions(preset="llm"),
        )
        with Image.open(report.outputs[0].path) as image:
            assert max(image.size) == CONVERT_PRESETS["llm"]["max_dimension"]
            assert image.size == (
                report.details["dimensions"][0]["width"],
                report.details["dimensions"][0]["height"],
            )
            pixel_count = image.width * image.height

        limited_dir = out_dir / "pixel-limit"
        limited_settings = Settings(
            jobs_root=out_dir / "jobs",
            limits=ResourceLimits(max_image_pixels=pixel_count - 1),
        )
        with pytest.raises(PipelineError, match="over the configured limit"):
            pdf_to_images(
                fixtures_dir / "fractional-size.pdf",
                limited_dir,
                options=PdfToImagesOptions(preset="llm", settings=limited_settings),
            )
        assert not limited_dir.exists()

    def test_llm_explicit_overrides_are_independent(self, fixtures_dir, out_dir):
        webp_report = pdf_to_images(
            fixtures_dir / "mixed-sizes.pdf",
            out_dir / "webp",
            options=PdfToImagesOptions(
                preset="llm",
                image_format="webp",
                jpeg_quality=37,
                pages=PageRange(spec="1"),
            ),
        )
        with Image.open(webp_report.outputs[0].path) as image:
            assert image.format == "WEBP"
            assert max(image.size) == CONVERT_PRESETS["llm"]["max_dimension"]
        assert webp_report.details["quality"] == 37

        png_report = pdf_to_images(
            fixtures_dir / "mixed-sizes.pdf",
            out_dir / "png",
            options=PdfToImagesOptions(
                preset="llm",
                image_format="png",
                jpeg_quality=41,
                pages=PageRange(spec="1"),
            ),
        )
        with Image.open(png_report.outputs[0].path) as image:
            assert image.format == "PNG"
            assert max(image.size) == CONVERT_PRESETS["llm"]["max_dimension"]
        assert png_report.details["quality"] is None
        assert png_report.details["configured_quality"] == 41

        dpi_report = pdf_to_images(
            fixtures_dir / "mixed-sizes.pdf",
            out_dir / "dpi",
            options=PdfToImagesOptions(
                preset="llm", dpi=200, pages=PageRange(spec="1")
            ),
        )
        with Image.open(dpi_report.outputs[0].path) as image:
            assert max(image.size) > CONVERT_PRESETS["llm"]["max_dimension"]
        assert dpi_report.details["max_dimension"] is None
        assert dpi_report.details["dpi_mode"] == "fixed"
        assert "image-downscaled" not in fidelity_codes(dpi_report)

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
        assert [item["page"] for item in report.details["dimensions"]] == [1, 1]
        assert [item["occurrence"] for item in report.details["dimensions"]] == [1, 2]
        assert [item["output_index"] for item in report.details["dimensions"]] == [0, 1]

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
