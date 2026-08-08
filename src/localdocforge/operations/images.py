"""Image ⇄ PDF and image ⇄ image conversion.

images-to-pdf composes pages with Pillow (EXIF orientation respected,
multipage TIFF expanded, decompression-bomb limits enforced). pdf-to-images
renders through PDFium at a chosen DPI. convert-images transcodes images —
including iPhone HEIC/HEIF via the decode-only pi-heif plugin — to
PNG/JPEG/WebP/TIFF, with an ``llm`` preset sized for AI-assistant ingestion.
All run through the standard pipeline: isolated workspace, validation,
atomic publish, reports.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from localdocforge.config.settings import Settings, get_settings
from localdocforge.domain.models import (
    ConversionReport,
    FidelityWarning,
    InputArtifact,
    JobContext,
    ProgressCallback,
    SecurityWarning,
    WarningSeverity,
)
from localdocforge.domain.pages import PageRange
from localdocforge.engines.adapters import (
    OP_CONVERT_IMAGES,
    OP_IMAGES_TO_PDF,
    OP_PDF_TO_IMAGES,
)
from localdocforge.engines.registry import default_registry
from localdocforge.jobs.workspace import CollisionPolicy
from localdocforge.pipelines.runner import (
    CandidateOutput,
    ExecuteResult,
    PipelineError,
    run_pipeline,
)
from localdocforge.security.filenames import sanitize_filename

IMAGE_MEDIA_TYPES = (
    "image/jpeg",
    "image/png",
    "image/tiff",
    "image/bmp",
    "image/webp",
    "image/heif",
)

_heif_opener_registered = False


def _ensure_heif_opener() -> None:
    """Register the decode-only HEIF plugin with Pillow exactly once.

    Raises PipelineError with an actionable message if the pi-heif engine is
    missing, so a HEIC input fails clearly instead of as an unidentified image.
    """
    global _heif_opener_registered
    if _heif_opener_registered:
        return
    try:
        from pi_heif import register_heif_opener
    except ImportError as exc:
        raise PipelineError(
            "HEIC/HEIF input requires the pi-heif engine, which failed to "
            "import. Reinstall LocalDocForge or run: pip install pi-heif"
        ) from exc
    register_heif_opener()
    _heif_opener_registered = True

#: Named page sizes in PDF points (1 pt = 1/72 in).
PAGE_SIZES_PT: dict[str, tuple[float, float]] = {
    "a4": (595.276, 841.89),
    "letter": (612.0, 792.0),
    "legal": (612.0, 1008.0),
}

_PT_PER_UNIT = {"pt": 1.0, "mm": 72.0 / 25.4, "cm": 72.0 / 2.54, "in": 72.0}


def parse_page_size(value: str) -> tuple[float, float] | None:
    """Parse 'A4' | 'letter' | 'legal' | 'image' | '<W>x<H>[pt|mm|cm|in]'.

    Returns (width_pt, height_pt), or None for 'image' (page follows image size).
    """
    lowered = value.strip().lower()
    if lowered == "image":
        return None
    if lowered in PAGE_SIZES_PT:
        return PAGE_SIZES_PT[lowered]
    unit = "mm"
    body = lowered
    for suffix in _PT_PER_UNIT:
        if lowered.endswith(suffix):
            unit, body = suffix, lowered[: -len(suffix)]
            break
    try:
        width_text, height_text = body.split("x", 1)
        width, height = float(width_text), float(height_text)
    except ValueError:
        raise PipelineError(
            f"Invalid page size {value!r}. Use A4, Letter, Legal, image, or WxH "
            f"with an optional pt/mm/cm/in suffix (default mm), e.g. 210x297mm"
        ) from None
    if not math.isfinite(width) or not math.isfinite(height) or width <= 0 or height <= 0:
        raise PipelineError("Page size must be positive")
    factor = _PT_PER_UNIT[unit]
    return (width * factor, height * factor)


@dataclass
class ImagesToPdfOptions:
    page_size: str = "A4"
    fit: str = "fit"  # fit | stretch | center
    margin_pt: float = 24.0
    background: str = "white"
    dpi: int = 200  # canvas resolution when composing on a fixed page size
    jpeg_quality: int = 95
    collision: CollisionPolicy | None = None
    settings: Settings | None = None
    progress: ProgressCallback | None = None


def _load_image_pages(path: Path, max_pixels: int | None, media_type: str = ""):
    """Yield PIL images for every frame (multipage TIFF aware), EXIF-corrected."""
    from PIL import Image, ImageOps, ImageSequence

    if media_type == "image/heif":
        _ensure_heif_opener()
    previous_limit = Image.MAX_IMAGE_PIXELS
    Image.MAX_IMAGE_PIXELS = max_pixels
    try:
        with Image.open(path) as image:
            for frame in ImageSequence.Iterator(image):
                corrected = ImageOps.exif_transpose(frame)
                has_alpha = "A" in corrected.getbands() or "transparency" in corrected.info
                yield corrected.convert("RGBA" if has_alpha else "RGB")
    finally:
        Image.MAX_IMAGE_PIXELS = previous_limit


def _compose_page(
    image,
    size_pt,
    options: ImagesToPdfOptions,
    max_pixels: int | None,
):
    """Place ``image`` on a page canvas of ``size_pt`` according to fit mode."""
    from PIL import Image, ImageOps

    scale = options.dpi / 72.0
    canvas_px = (max(1, round(size_pt[0] * scale)), max(1, round(size_pt[1] * scale)))
    if max_pixels is not None and canvas_px[0] * canvas_px[1] > max_pixels:
        raise PipelineError(
            f"Composed page would contain {canvas_px[0] * canvas_px[1]:,} pixels, "
            f"over the configured limit of {max_pixels:,}"
        )
    margin_px = max(0, round(options.margin_pt * scale))
    if 2 * margin_px >= canvas_px[0] or 2 * margin_px >= canvas_px[1]:
        raise PipelineError("Margin leaves no drawable page area")
    inner = (canvas_px[0] - 2 * margin_px, canvas_px[1] - 2 * margin_px)
    canvas = Image.new("RGB", canvas_px, options.background)
    placed = image
    try:
        if options.fit == "stretch":
            placed = image.resize(inner)
            offset = (margin_px, margin_px)
        elif options.fit == "center":
            # Native size at the canvas DPI; downscale only if it overflows.
            if image.width > inner[0] or image.height > inner[1]:
                placed = ImageOps.contain(image, inner)
            offset = (
                margin_px + (inner[0] - placed.width) // 2,
                margin_px + (inner[1] - placed.height) // 2,
            )
        else:  # fit (default): scale to fit inside margins, keep aspect ratio
            placed = ImageOps.contain(image, inner)
            offset = (
                margin_px + (inner[0] - placed.width) // 2,
                margin_px + (inner[1] - placed.height) // 2,
            )
        canvas.paste(placed, offset, placed if "A" in placed.getbands() else None)
    finally:
        if placed is not image:
            placed.close()
    return canvas


def _flatten_alpha(image, background: str):
    if "A" not in image.getbands():
        return image
    from PIL import Image

    flattened = Image.new("RGB", image.size, background)
    flattened.paste(image, (0, 0), image)
    image.close()
    return flattened


def images_to_pdf(
    inputs: list[Path],
    output: Path,
    *,
    options: ImagesToPdfOptions | None = None,
) -> ConversionReport:
    options = options or ImagesToPdfOptions()
    if options.fit not in {"fit", "stretch", "center"}:
        raise PipelineError(f"Unknown fit mode {options.fit!r}; use fit, stretch, or center")
    if not 36 <= options.dpi <= 600:
        raise PipelineError("DPI must be between 36 and 600")
    if not 1 <= options.jpeg_quality <= 100:
        raise PipelineError("JPEG quality must be between 1 and 100")
    if not math.isfinite(options.margin_pt) or options.margin_pt < 0:
        raise PipelineError("Margin must be a finite, non-negative number")
    size_pt = parse_page_size(options.page_size)
    settings = options.settings or get_settings()

    registry = default_registry()
    engine = registry.engine_for(OP_IMAGES_TO_PDF)
    engine_info = engine.probe()

    def execute(context: JobContext, artifacts: list[InputArtifact]) -> ExecuteResult:
        fidelity: list[FidelityWarning] = [
            FidelityWarning(
                code="images-reencoded",
                message="Images are re-encoded while composing PDF pages; for "
                "photographs this is a lossy JPEG step",
                severity="info",
            )
        ]
        pages = []
        decompressed_bytes = 0
        try:
            for index, artifact in enumerate(artifacts):
                context.emit(
                    "compose", current=index, total=len(artifacts), message=artifact.path.name
                )
                for frame in _load_image_pages(
                    artifact.path, context.limits.max_image_pixels, artifact.media_type
                ):
                    context.check_cancelled()
                    try:
                        page = (
                            _flatten_alpha(frame, options.background)
                            if size_pt is None
                            else _compose_page(
                                frame, size_pt, options, context.limits.max_image_pixels
                            )
                        )
                    except BaseException:
                        frame.close()
                        raise
                    if page is not frame:
                        frame.close()
                    decompressed_bytes += page.width * page.height * len(page.getbands())
                    byte_limit = context.limits.max_decompressed_bytes
                    if byte_limit is not None and decompressed_bytes > byte_limit:
                        page.close()
                        raise PipelineError(
                            f"Decoded image pages exceed the configured {byte_limit:,}-byte limit"
                        )
                    pages.append(page)
                    page_limit = context.limits.max_pages
                    if page_limit is not None and len(pages) > page_limit:
                        raise PipelineError(
                            f"{len(pages)} pages exceeds the configured limit of {page_limit}"
                        )
            if not pages:
                raise PipelineError("No images could be read")
            staging = context.workspace / "images.pdf"
            first, rest = pages[0], pages[1:]
            first.save(
                staging,
                format="PDF",
                save_all=bool(rest),
                append_images=rest,
                resolution=float(options.dpi),
                quality=options.jpeg_quality,
            )
            page_count = len(pages)
        finally:
            for page in pages:
                page.close()
        return ExecuteResult(
            candidates=[
                CandidateOutput(
                    workspace_path=staging,
                    destination=output,
                    expected_pages=page_count,
                    render_all=page_count <= 50,
                )
            ],
            fidelity_warnings=fidelity,
            output_page_count=page_count,
            details={
                "page_size": options.page_size,
                "fit": options.fit,
                "margin_pt": options.margin_pt,
                "background": options.background,
                "dpi": options.dpi,
                "jpeg_quality": options.jpeg_quality,
                "source_images": len(inputs),
            },
        )

    return run_pipeline(
        operation="images-to-pdf",
        input_paths=inputs,
        execute=execute,
        engine_name=engine.name,
        engine_version=engine_info.version,
        input_types=IMAGE_MEDIA_TYPES,
        collision=options.collision,
        settings=settings,
        progress=options.progress,
    )


@dataclass
class PdfToImagesOptions:
    image_format: str = "png"  # png | jpeg | webp | tiff
    dpi: int = 150
    pages: PageRange | None = None
    jpeg_quality: int = 90
    collision: CollisionPolicy | None = None
    settings: Settings | None = None
    progress: ProgressCallback | None = None
    password: str | None = None


_FORMAT_INFO = {
    "png": ("PNG", "image/png", ".png"),
    "jpeg": ("JPEG", "image/jpeg", ".jpg"),
    "jpg": ("JPEG", "image/jpeg", ".jpg"),
    "webp": ("WEBP", "image/webp", ".webp"),
    "tiff": ("TIFF", "image/tiff", ".tiff"),
}

#: Raster output formats accepted by pdf-to-images and convert-images.
OUTPUT_IMAGE_FORMATS = tuple(_FORMAT_INFO)


def pdf_to_images(
    input_path: Path,
    output_dir: Path,
    *,
    options: PdfToImagesOptions | None = None,
) -> ConversionReport:
    options = options or PdfToImagesOptions()
    format_key = options.image_format.lower()
    if format_key not in _FORMAT_INFO:
        raise PipelineError(
            f"Unsupported image format {options.image_format!r}; use png, jpeg, webp, or tiff"
        )
    pil_format, media_type, extension = _FORMAT_INFO[format_key]
    if not 18 <= options.dpi <= 1200:
        raise PipelineError("DPI must be between 18 and 1200")
    if not 1 <= options.jpeg_quality <= 100:
        raise PipelineError("Image quality must be between 1 and 100")

    registry = default_registry()
    engine = registry.engine_for(OP_PDF_TO_IMAGES)
    engine_info = engine.probe()
    stem = sanitize_filename(input_path.stem, fallback="document")

    def execute(context: JobContext, artifacts: list[InputArtifact]) -> ExecuteResult:
        import pikepdf
        import pypdfium2 as pdfium

        security: list[SecurityWarning] = []
        try:
            with pikepdf.open(artifacts[0].path, password=options.password or "") as parsed:
                syntax_issues = list(parsed.check_pdf_syntax())
                encrypted = parsed.is_encrypted
        except pikepdf.PasswordError as exc:
            raise PipelineError(
                f"{artifacts[0].path.name!r} is encrypted or the supplied password is wrong"
            ) from exc
        if syntax_issues:
            raise PipelineError(
                f"{artifacts[0].path.name!r} has {len(syntax_issues)} structural syntax "
                "warning(s); automatic repair is not an implemented operation"
            )
        if encrypted:
            security.append(
                SecurityWarning(
                    code="input-encryption-removed",
                    message=(
                        "The input PDF was password protected. Rendered image outputs are "
                        "not password protected and must be secured separately."
                    ),
                    severity=WarningSeverity.CRITICAL,
                )
            )
        pdf = pdfium.PdfDocument(str(artifacts[0].path), password=options.password)
        try:
            total = len(pdf)
            page_limit = context.limits.max_pages
            if page_limit is not None and total > page_limit:
                raise PipelineError(
                    f"Input has {total} pages after opening, over the configured limit of "
                    f"{page_limit}"
                )
            selection = (options.pages or PageRange(spec="all")).resolve(total)
            if page_limit is not None and len(selection) > page_limit:
                raise PipelineError(
                    f"Requested output has {len(selection)} pages, over the configured limit of "
                    f"{page_limit}"
                )
            candidates: list[CandidateOutput] = []
            page_occurrences: dict[int, int] = {}
            decompressed_bytes = 0
            output_bytes = 0
            for order, page_number in enumerate(selection):
                context.emit(
                    "render", current=order, total=len(selection), message=f"page {page_number}"
                )
                page = pdf[page_number - 1]
                bitmap = None
                try:
                    width_pt, height_pt = page.get_size()
                    pixel_count = max(1, round(width_pt * options.dpi / 72.0)) * max(
                        1, round(height_pt * options.dpi / 72.0)
                    )
                    pixel_limit = context.limits.max_image_pixels
                    if pixel_limit is not None and pixel_count > pixel_limit:
                        raise PipelineError(
                            f"Rendered page {page_number} would contain {pixel_count:,} pixels, "
                            f"over the configured limit of {pixel_limit:,}"
                        )
                    bitmap = page.render(scale=options.dpi / 72.0)
                    image = bitmap.to_pil()
                except BaseException:
                    if bitmap is not None:
                        bitmap.close()
                    raise
                finally:
                    page.close()
                occurrence = page_occurrences.get(page_number, 0) + 1
                page_occurrences[page_number] = occurrence
                repeat = "" if occurrence == 1 else f"-repeat-{occurrence:03d}"
                name = f"{stem}-page-{page_number:03d}{repeat}{extension}"
                staging = context.workspace / name
                save_kwargs: dict[str, object] = {"format": pil_format}
                if pil_format == "JPEG":
                    image = image.convert("RGB")
                    save_kwargs["quality"] = options.jpeg_quality
                    save_kwargs["dpi"] = (options.dpi, options.dpi)
                elif pil_format == "WEBP":
                    save_kwargs["quality"] = options.jpeg_quality
                elif pil_format in ("PNG", "TIFF"):
                    save_kwargs["dpi"] = (options.dpi, options.dpi)
                try:
                    decompressed_bytes += image.width * image.height * len(image.getbands())
                    byte_limit = context.limits.max_decompressed_bytes
                    if byte_limit is not None and decompressed_bytes > byte_limit:
                        raise PipelineError(
                            f"Rendered pages exceed the configured {byte_limit:,}-byte "
                            "decompressed limit"
                        )
                    image.save(staging, **save_kwargs)
                finally:
                    image.close()
                    if bitmap is not None:
                        bitmap.close()
                output_bytes += staging.stat().st_size
                output_limit = context.limits.max_output_bytes
                if output_limit is not None and output_bytes > output_limit:
                    raise PipelineError(
                        f"Generated images exceed the configured {output_limit:,}-byte output "
                        "limit"
                    )
                candidates.append(
                    CandidateOutput(
                        workspace_path=staging,
                        destination=output_dir / name,
                        media_type=media_type,
                    )
                )
        finally:
            pdf.close()
        return ExecuteResult(
            candidates=candidates,
            security_warnings=security,
            details={
                "dpi": options.dpi,
                "format": format_key,
                "jpeg_quality": options.jpeg_quality,
                "pages_rendered": len(candidates),
            },
            output_page_count=len(candidates),
        )

    return run_pipeline(
        operation="pdf-to-images",
        input_paths=[input_path],
        execute=execute,
        engine_name=engine.name,
        engine_version=engine_info.version,
        collision=options.collision,
        settings=options.settings,
        progress=options.progress,
    )


#: Presets fill only the convert-images options the caller left unset.
#: ``llm`` targets compatible AI-image ingestion: JPEG keeps
#: photo outputs small, quality 85 is visually clean, and 1568 px on the long
#: edge is the largest size no current assistant downscales before reading.
CONVERT_PRESETS: dict[str, dict[str, Any]] = {
    "llm": {"image_format": "jpeg", "quality": 85, "max_dimension": 1568},
}

_CONVERT_DEFAULTS: dict[str, Any] = {
    "image_format": "jpeg",
    "quality": 90,
    "max_dimension": None,
}


@dataclass
class ConvertImagesOptions:
    #: None means "use the preset's value, else the documented default".
    image_format: str | None = None  # png | jpeg | webp | tiff
    quality: int | None = None  # JPEG/WebP quality
    max_dimension: int | None = None  # long-edge bound in px; never upscales
    preset: str | None = None
    keep_metadata: bool = False
    background: str = "white"  # used when alpha must be flattened
    collision: CollisionPolicy | None = None
    settings: Settings | None = None
    progress: ProgressCallback | None = None


def resolve_convert_options(options: ConvertImagesOptions) -> dict[str, Any]:
    """Merge explicit values over the preset over the defaults."""
    if options.preset is not None and options.preset not in CONVERT_PRESETS:
        available = ", ".join(sorted(CONVERT_PRESETS))
        raise PipelineError(
            f"Unknown preset {options.preset!r}; available presets: {available}"
        )
    resolved = dict(_CONVERT_DEFAULTS)
    if options.preset is not None:
        resolved.update(CONVERT_PRESETS[options.preset])
    for key in ("image_format", "quality", "max_dimension"):
        value = getattr(options, key)
        if value is not None:
            resolved[key] = value
    return resolved


def _profile_is_srgb(icc_bytes: bytes) -> bool:
    from io import BytesIO

    from PIL import ImageCms

    try:
        profile = ImageCms.ImageCmsProfile(BytesIO(icc_bytes))
        description = ImageCms.getProfileDescription(profile)
    except Exception:
        return False
    return "srgb" in (description or "").lower()


def _convert_to_srgb(image, icc_bytes: bytes):
    """Return the image with pixels converted to sRGB, or None on failure."""
    from io import BytesIO

    from PIL import ImageCms

    if image.mode not in ("RGB", "RGBA"):
        return None
    try:
        source = ImageCms.ImageCmsProfile(BytesIO(icc_bytes))
        target = ImageCms.createProfile("sRGB")
        return ImageCms.profileToProfile(image, source, target, outputMode=image.mode)
    except Exception:
        return None


def _output_stem(path: Path, used: set[str]) -> str:
    stem = sanitize_filename(path.stem, fallback="image")
    candidate = stem
    suffix = 2
    while candidate.lower() in used:
        candidate = f"{stem}-{suffix}"
        suffix += 1
    used.add(candidate.lower())
    return candidate


def convert_images(
    inputs: list[Path],
    output_dir: Path,
    *,
    options: ConvertImagesOptions | None = None,
) -> ConversionReport:
    """Convert images (HEIC/JPG/PNG/TIFF/BMP/WebP) to PNG/JPEG/WebP/TIFF files.

    Defaults are chosen for sharing with people and AI assistants: EXIF
    orientation is applied, pixels tagged with a non-sRGB ICC profile are
    converted to sRGB, and EXIF/XMP metadata — including any GPS position —
    is stripped unless ``keep_metadata`` is set.
    """
    options = options or ConvertImagesOptions()
    resolved = resolve_convert_options(options)
    format_key = str(resolved["image_format"]).lower()
    if format_key not in _FORMAT_INFO:
        raise PipelineError(
            f"Unsupported image format {resolved['image_format']!r}; "
            "use png, jpeg, webp, or tiff"
        )
    pil_format, media_type, extension = _FORMAT_INFO[format_key]
    quality = int(resolved["quality"])
    if not 1 <= quality <= 100:
        raise PipelineError("Image quality must be between 1 and 100")
    max_dimension: int | None = resolved["max_dimension"]
    if max_dimension is not None:
        max_dimension = int(max_dimension)
        if not 16 <= max_dimension <= 30000:
            raise PipelineError("--max-dimension must be between 16 and 30000 pixels")

    registry = default_registry()
    engine = registry.engine_for(OP_CONVERT_IMAGES)
    engine_info = engine.probe()

    def execute(context: JobContext, artifacts: list[InputArtifact]) -> ExecuteResult:
        from PIL import Image, ImageOps, ImageSequence

        candidates: list[CandidateOutput] = []
        used_stems: set[str] = set()
        decompressed_bytes = 0
        output_bytes = 0
        outputs_written = 0
        downscaled = 0
        alpha_flattened = 0
        profiles_converted = 0
        profiles_retained = 0
        stripped_outputs = 0
        stripped_gps_inputs = 0
        xmp_dropped = 0
        kept_gps_inputs = 0
        heif_decoded = False

        for index, artifact in enumerate(artifacts):
            context.emit(
                "convert", current=index, total=len(artifacts), message=artifact.path.name
            )
            context.check_cancelled()
            if artifact.media_type == "image/heif":
                _ensure_heif_opener()
                heif_decoded = True
            stem = _output_stem(artifact.path, used_stems)
            previous_limit = Image.MAX_IMAGE_PIXELS
            Image.MAX_IMAGE_PIXELS = context.limits.max_image_pixels
            try:
                with Image.open(artifact.path) as source:
                    frame_total = getattr(source, "n_frames", 1)
                    for frame_index, raw_frame in enumerate(
                        ImageSequence.Iterator(source)
                    ):
                        context.check_cancelled()

                        def swap(current, replacement):
                            """Adopt a derived image, closing the one it replaces."""
                            if replacement is not current and current is not raw_frame:  # noqa: B023
                                current.close()
                            return replacement

                        # exif_transpose returns a copy with the orientation
                        # applied and the orientation tag cleared.
                        frame = ImageOps.exif_transpose(raw_frame)
                        try:
                            exif_bytes = frame.info.get("exif")
                            icc_bytes = frame.info.get("icc_profile")
                            has_xmp = bool(
                                frame.info.get("xmp")
                                or frame.info.get("XML:com.adobe.xmp")
                            )
                            gps_present = False
                            if exif_bytes:
                                try:
                                    gps_present = bool(frame.getexif().get_ifd(0x8825))
                                except Exception:
                                    gps_present = False

                            if icc_bytes and not _profile_is_srgb(icc_bytes):
                                if options.keep_metadata:
                                    pass  # profile travels with the pixels untouched
                                else:
                                    converted = _convert_to_srgb(frame, icc_bytes)
                                    if converted is not None:
                                        frame = swap(frame, converted)
                                        icc_bytes = None
                                        profiles_converted += 1
                                    else:
                                        profiles_retained += 1
                            elif icc_bytes and not options.keep_metadata:
                                icc_bytes = None  # already sRGB; the tag adds nothing

                            if max_dimension is not None and (
                                frame.width > max_dimension
                                or frame.height > max_dimension
                            ):
                                frame = swap(
                                    frame,
                                    ImageOps.contain(
                                        frame,
                                        (max_dimension, max_dimension),
                                        Image.Resampling.LANCZOS,
                                    ),
                                )
                                downscaled += 1

                            has_alpha = (
                                "A" in frame.getbands() or "transparency" in frame.info
                            )
                            if pil_format == "JPEG":
                                if has_alpha:
                                    flattened = Image.new(
                                        "RGB", frame.size, options.background
                                    )
                                    rgba = frame.convert("RGBA")
                                    try:
                                        flattened.paste(rgba, (0, 0), rgba)
                                    finally:
                                        if rgba is not frame:
                                            rgba.close()
                                    frame = swap(frame, flattened)
                                    alpha_flattened += 1
                                elif frame.mode not in ("RGB", "L"):
                                    frame = swap(frame, frame.convert("RGB"))
                            elif frame.mode == "P":
                                frame = swap(
                                    frame,
                                    frame.convert("RGBA" if has_alpha else "RGB"),
                                )

                            decompressed_bytes += (
                                frame.width * frame.height * len(frame.getbands())
                            )
                            byte_limit = context.limits.max_decompressed_bytes
                            if byte_limit is not None and decompressed_bytes > byte_limit:
                                raise PipelineError(
                                    f"Decoded images exceed the configured "
                                    f"{byte_limit:,}-byte decompressed limit"
                                )

                            # Pillow's writers fall back to image.info for EXIF
                            # and ICC data, so stripping must clear it explicitly.
                            for key in (
                                "exif",
                                "icc_profile",
                                "xmp",
                                "XML:com.adobe.xmp",
                            ):
                                frame.info.pop(key, None)
                            save_kwargs: dict[str, object] = {"format": pil_format}
                            if pil_format in ("JPEG", "WEBP"):
                                save_kwargs["quality"] = quality
                            if options.keep_metadata:
                                if exif_bytes:
                                    save_kwargs["exif"] = exif_bytes
                                if gps_present:
                                    kept_gps_inputs += 1
                            else:
                                stripped_outputs += 1
                                if gps_present:
                                    stripped_gps_inputs += 1
                            if has_xmp:
                                xmp_dropped += 1
                            if icc_bytes:
                                save_kwargs["icc_profile"] = icc_bytes

                            name = (
                                f"{stem}{extension}"
                                if frame_total == 1
                                else f"{stem}-frame-{frame_index + 1:03d}{extension}"
                            )
                            staging = context.workspace / name
                            frame.save(staging, **save_kwargs)
                        finally:
                            if frame is not raw_frame:
                                frame.close()
                        outputs_written += 1
                        output_bytes += staging.stat().st_size
                        output_limit = context.limits.max_output_bytes
                        if output_limit is not None and output_bytes > output_limit:
                            raise PipelineError(
                                f"Converted images exceed the configured "
                                f"{output_limit:,}-byte output limit"
                            )
                        candidates.append(
                            CandidateOutput(
                                workspace_path=staging,
                                destination=output_dir / name,
                                media_type=media_type,
                            )
                        )
            finally:
                Image.MAX_IMAGE_PIXELS = previous_limit

        if not candidates:
            raise PipelineError("No images could be read")

        fidelity: list[FidelityWarning] = [
            FidelityWarning(
                code="image-reencoded",
                message=(
                    f"Outputs are re-encoded as {format_key.upper()}"
                    + (
                        f" at quality {quality} (lossy)"
                        if pil_format in ("JPEG", "WEBP")
                        else ""
                    )
                ),
                severity=WarningSeverity.INFO,
            )
        ]
        if downscaled:
            fidelity.append(
                FidelityWarning(
                    code="image-downscaled",
                    message=(
                        f"{downscaled} image(s) were downscaled to at most "
                        f"{max_dimension} px on the long edge"
                    ),
                    severity=WarningSeverity.INFO,
                )
            )
        if alpha_flattened:
            fidelity.append(
                FidelityWarning(
                    code="alpha-flattened",
                    message=(
                        f"{alpha_flattened} image(s) had transparency flattened "
                        f"onto a {options.background} background (JPEG is opaque)"
                    ),
                    severity=WarningSeverity.INFO,
                )
            )
        if profiles_converted:
            fidelity.append(
                FidelityWarning(
                    code="color-profile-converted",
                    message=(
                        f"{profiles_converted} image(s) were converted from an "
                        "embedded color profile (for iPhone photos, typically "
                        "Display P3) to sRGB so colors stay correct without "
                        "the profile"
                    ),
                    severity=WarningSeverity.INFO,
                )
            )
        if profiles_retained:
            fidelity.append(
                FidelityWarning(
                    code="color-profile-retained",
                    message=(
                        f"{profiles_retained} embedded color profile(s) could not "
                        "be converted to sRGB and were kept in the output instead"
                    ),
                    severity=WarningSeverity.INFO,
                )
            )
        if stripped_outputs:
            gps_note = (
                f", including GPS position data in {stripped_gps_inputs} file(s)"
                if stripped_gps_inputs
                else ""
            )
            fidelity.append(
                FidelityWarning(
                    code="metadata-stripped",
                    message=(
                        f"EXIF metadata{gps_note} was removed from "
                        f"{stripped_outputs} output(s); use --keep-metadata to "
                        "retain it"
                    ),
                    severity=WarningSeverity.INFO,
                )
            )
        if xmp_dropped:
            fidelity.append(
                FidelityWarning(
                    code="xmp-metadata-dropped",
                    message=(
                        f"XMP metadata present in {xmp_dropped} input(s) is not "
                        "carried into converted outputs"
                    ),
                    severity=WarningSeverity.INFO,
                )
            )
        security: list[SecurityWarning] = []
        if kept_gps_inputs:
            security.append(
                SecurityWarning(
                    code="location-metadata-retained",
                    message=(
                        f"--keep-metadata preserved EXIF GPS position data from "
                        f"{kept_gps_inputs} input(s) in the converted outputs"
                    ),
                    severity=WarningSeverity.WARNING,
                )
            )

        details: dict[str, object] = {
            "format": format_key,
            "quality": quality,
            "max_dimension": max_dimension,
            "preset": options.preset,
            "metadata": "kept" if options.keep_metadata else "stripped",
            "images_converted": outputs_written,
        }
        if heif_decoded:
            import pi_heif

            details["heif_decoder"] = f"pi-heif {pi_heif.__version__}"
        return ExecuteResult(
            candidates=candidates,
            fidelity_warnings=fidelity,
            security_warnings=security,
            details=details,
        )

    return run_pipeline(
        operation="convert-images",
        input_paths=inputs,
        execute=execute,
        engine_name=engine.name,
        engine_version=engine_info.version,
        input_types=IMAGE_MEDIA_TYPES,
        collision=options.collision,
        settings=options.settings,
        progress=options.progress,
    )
