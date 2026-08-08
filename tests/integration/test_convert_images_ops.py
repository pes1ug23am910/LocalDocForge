"""Integration tests for convert-images, including HEIC decode through pi-heif."""

from __future__ import annotations

import pytest
from PIL import Image

from localdocforge.domain.models import ReportStatus, WarningSeverity
from localdocforge.operations.images import (
    CONVERT_PRESETS,
    ConvertImagesOptions,
    convert_images,
    images_to_pdf,
    resolve_convert_options,
)
from localdocforge.pipelines.runner import PipelineError


def fidelity_codes(report):
    return {warning.code for warning in report.fidelity_warnings}


def security_codes(report):
    return {warning.code for warning in report.security_warnings}


class TestPresetResolution:
    def test_defaults_without_preset(self):
        resolved = resolve_convert_options(ConvertImagesOptions())
        assert resolved == {"image_format": "jpeg", "quality": 90, "max_dimension": None}

    def test_llm_preset_values(self):
        resolved = resolve_convert_options(ConvertImagesOptions(preset="llm"))
        assert resolved == {"image_format": "jpeg", "quality": 85, "max_dimension": 1568}

    def test_explicit_flags_override_preset(self):
        resolved = resolve_convert_options(
            ConvertImagesOptions(preset="llm", image_format="png", max_dimension=640)
        )
        assert resolved["image_format"] == "png"
        assert resolved["max_dimension"] == 640
        assert resolved["quality"] == 85  # still the preset's value

    def test_unknown_preset_is_refused(self):
        with pytest.raises(PipelineError, match="Unknown preset"):
            resolve_convert_options(ConvertImagesOptions(preset="tiny"))
        assert "llm" in CONVERT_PRESETS


class TestHeicInput:
    def test_heic_to_jpeg_llm_preset(self, fixtures_dir, out_dir):
        report = convert_images(
            [fixtures_dir / "images" / "photo.heic"],
            out_dir,
            options=ConvertImagesOptions(preset="llm"),
        )
        assert report.status == ReportStatus.SUCCESS
        assert report.details["heif_decoder"].startswith("pi-heif ")
        assert report.details["metadata"] == "stripped"
        (output,) = report.outputs
        assert output.path.name == "photo.jpg"
        with Image.open(output.path) as image:
            assert image.format == "JPEG"
            # 640x480 is already inside the 1568 px bound: never upscaled.
            assert image.size == (640, 480)
            assert not image.info.get("exif")
        assert "image-downscaled" not in fidelity_codes(report)

    def test_heic_gps_metadata_stripped_by_default_and_reported(
        self, fixtures_dir, out_dir
    ):
        report = convert_images(
            [fixtures_dir / "images" / "photo.heic"],
            out_dir,
            options=ConvertImagesOptions(),
        )
        stripped = [
            warning
            for warning in report.fidelity_warnings
            if warning.code == "metadata-stripped"
        ]
        assert stripped and "GPS position data" in stripped[0].message
        assert not security_codes(report)

    def test_keep_metadata_retains_gps_and_warns(self, fixtures_dir, out_dir):
        report = convert_images(
            [fixtures_dir / "images" / "photo.heic"],
            out_dir,
            options=ConvertImagesOptions(keep_metadata=True),
        )
        assert report.status == ReportStatus.SUCCESS
        warning = next(
            item
            for item in report.security_warnings
            if item.code == "location-metadata-retained"
        )
        assert warning.severity == WarningSeverity.WARNING
        assert "metadata-stripped" not in fidelity_codes(report)
        with Image.open(report.outputs[0].path) as image:
            gps = image.getexif().get_ifd(0x8825)
            assert gps and gps[1] == "N"

    def test_heic_alpha_flattens_for_jpeg_and_survives_for_png(
        self, fixtures_dir, out_dir
    ):
        source = fixtures_dir / "images" / "alpha.heic"
        jpeg_report = convert_images(
            [source], out_dir / "jpeg", options=ConvertImagesOptions()
        )
        assert "alpha-flattened" in fidelity_codes(jpeg_report)
        with Image.open(jpeg_report.outputs[0].path) as image:
            assert image.mode == "RGB"
        png_report = convert_images(
            [source], out_dir / "png", options=ConvertImagesOptions(image_format="png")
        )
        assert "alpha-flattened" not in fidelity_codes(png_report)
        with Image.open(png_report.outputs[0].path) as image:
            assert image.mode == "RGBA"

    def test_multi_image_heic_names_every_frame(self, fixtures_dir, out_dir):
        report = convert_images(
            [fixtures_dir / "images" / "burst.heic"],
            out_dir,
            options=ConvertImagesOptions(image_format="png"),
        )
        names = sorted(output.path.name for output in report.outputs)
        assert names == ["burst-frame-001.png", "burst-frame-002.png"]

    def test_heic_input_composes_into_pdf(self, fixtures_dir, out_dir):
        out = out_dir / "photo.pdf"
        report = images_to_pdf([fixtures_dir / "images" / "photo.heic"], out)
        assert report.status == ReportStatus.SUCCESS
        assert report.output_page_count == 1


class TestGeneralConversion:
    def test_jpeg_to_png_transcode(self, fixtures_dir, out_dir):
        report = convert_images(
            [fixtures_dir / "images" / "photo.jpg"],
            out_dir,
            options=ConvertImagesOptions(image_format="png"),
        )
        assert report.status == ReportStatus.SUCCESS
        assert report.outputs[0].media_type == "image/png"
        assert "image-reencoded" in fidelity_codes(report)

    def test_exif_orientation_applied_before_stripping(self, fixtures_dir, out_dir):
        # Stored 600x400 with orientation "rotate 90": output must be 400x600.
        report = convert_images(
            [fixtures_dir / "images" / "exif-rotated.jpg"],
            out_dir,
            options=ConvertImagesOptions(),
        )
        with Image.open(report.outputs[0].path) as image:
            assert image.size == (400, 600)
            assert not image.info.get("exif")

    def test_max_dimension_downscales_long_edge_only(self, fixtures_dir, out_dir):
        report = convert_images(
            [fixtures_dir / "images" / "photo.jpg"],  # 800x600
            out_dir,
            options=ConvertImagesOptions(max_dimension=400),
        )
        with Image.open(report.outputs[0].path) as image:
            assert image.size == (400, 300)
        assert "image-downscaled" in fidelity_codes(report)

    def test_srgb_profile_dropped_silently(self, fixtures_dir, out_dir):
        report = convert_images(
            [fixtures_dir / "images" / "srgb-tagged.jpg"],
            out_dir,
            options=ConvertImagesOptions(),
        )
        codes = fidelity_codes(report)
        assert "color-profile-converted" not in codes
        assert "color-profile-retained" not in codes
        with Image.open(report.outputs[0].path) as image:
            assert "icc_profile" not in image.info

    def test_unparseable_profile_is_retained_with_warning(self, fixtures_dir, out_dir):
        report = convert_images(
            [fixtures_dir / "images" / "bad-profile.jpg"],
            out_dir,
            options=ConvertImagesOptions(),
        )
        assert "color-profile-retained" in fidelity_codes(report)
        with Image.open(report.outputs[0].path) as image:
            assert image.info.get("icc_profile")

    def test_keep_metadata_keeps_profile_untouched(self, fixtures_dir, out_dir):
        report = convert_images(
            [fixtures_dir / "images" / "srgb-tagged.jpg"],
            out_dir,
            options=ConvertImagesOptions(keep_metadata=True),
        )
        assert "color-profile-converted" not in fidelity_codes(report)
        with Image.open(report.outputs[0].path) as image:
            assert image.info.get("icc_profile")

    def test_same_stem_inputs_get_distinct_names(self, fixtures_dir, tmp_path, out_dir):
        first_dir = tmp_path / "a"
        second_dir = tmp_path / "b"
        first_dir.mkdir()
        second_dir.mkdir()
        with Image.open(fixtures_dir / "images" / "photo.jpg") as image:
            image.save(first_dir / "scan.jpg")
            image.save(second_dir / "scan.jpg")
        report = convert_images(
            [first_dir / "scan.jpg", second_dir / "scan.jpg"],
            out_dir,
            options=ConvertImagesOptions(),
        )
        names = sorted(output.path.name for output in report.outputs)
        assert names == ["scan-2.jpg", "scan.jpg"]

    def test_multipage_tiff_converts_every_frame(self, fixtures_dir, out_dir):
        report = convert_images(
            [fixtures_dir / "images" / "scan-3page.tiff"],
            out_dir,
            options=ConvertImagesOptions(image_format="png"),
        )
        assert len(report.outputs) == 3

    def test_pdf_input_is_rejected_by_content(self, fixtures_dir, out_dir):
        with pytest.raises(PipelineError, match="application/pdf"):
            convert_images(
                [fixtures_dir / "simple-3page.pdf"],
                out_dir,
                options=ConvertImagesOptions(),
            )

    def test_parameter_validation(self, fixtures_dir, out_dir):
        source = fixtures_dir / "images" / "photo.jpg"
        with pytest.raises(PipelineError, match="Unsupported image format"):
            convert_images(
                [source], out_dir, options=ConvertImagesOptions(image_format="gif")
            )
        with pytest.raises(PipelineError, match="quality"):
            convert_images([source], out_dir, options=ConvertImagesOptions(quality=0))
        with pytest.raises(PipelineError, match="max-dimension"):
            convert_images(
                [source], out_dir, options=ConvertImagesOptions(max_dimension=8)
            )
