"""Image ⇄ PDF conversion.

images-to-pdf composes pages with Pillow (EXIF orientation respected,
multipage TIFF expanded, decompression-bomb limits enforced). pdf-to-images
renders through PDFium at a chosen DPI. Both run through the standard
pipeline: isolated workspace, validation, atomic publish, reports.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from localdocforge.config.settings import Settings, get_settings
from localdocforge.domain.models import (
    ConversionReport,
    FidelityWarning,
    InputArtifact,
    JobContext,
    ProgressCallback,
)
from localdocforge.domain.pages import PageRange
from localdocforge.engines.adapters import OP_IMAGES_TO_PDF, OP_PDF_TO_IMAGES
from localdocforge.engines.registry import default_registry
from localdocforge.jobs.workspace import CollisionPolicy
from localdocforge.pipelines.runner import (
    CandidateOutput,
    ExecuteResult,
    PipelineError,
    run_pipeline,
)

IMAGE_MEDIA_TYPES = (
    "image/jpeg",
    "image/png",
    "image/tiff",
    "image/bmp",
    "image/webp",
)

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
    if width <= 0 or height <= 0:
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


def _load_image_pages(path: Path, max_pixels: int | None):
    """Yield PIL images for every frame (multipage TIFF aware), EXIF-corrected."""
    from PIL import Image, ImageOps, ImageSequence

    previous_limit = Image.MAX_IMAGE_PIXELS
    Image.MAX_IMAGE_PIXELS = max_pixels
    try:
        with Image.open(path) as image:
            for frame in ImageSequence.Iterator(image):
                corrected = ImageOps.exif_transpose(frame)
                yield corrected.convert("RGB")
    finally:
        Image.MAX_IMAGE_PIXELS = previous_limit


def _compose_page(image, size_pt, options: ImagesToPdfOptions):
    """Place ``image`` on a page canvas of ``size_pt`` according to fit mode."""
    from PIL import Image, ImageOps

    scale = options.dpi / 72.0
    canvas_px = (max(1, round(size_pt[0] * scale)), max(1, round(size_pt[1] * scale)))
    margin_px = max(0, round(options.margin_pt * scale))
    inner = (max(1, canvas_px[0] - 2 * margin_px), max(1, canvas_px[1] - 2 * margin_px))
    canvas = Image.new("RGB", canvas_px, options.background)
    if options.fit == "stretch":
        placed = image.resize(inner)
        canvas.paste(placed, (margin_px, margin_px))
    elif options.fit == "center":
        # Native size at the canvas DPI; downscale only if it overflows.
        if image.width > inner[0] or image.height > inner[1]:
            image = ImageOps.contain(image, inner)
        offset = (
            margin_px + (inner[0] - image.width) // 2,
            margin_px + (inner[1] - image.height) // 2,
        )
        canvas.paste(image, offset)
    else:  # fit (default): scale to fit inside margins, keep aspect ratio
        placed = ImageOps.contain(image, inner)
        offset = (
            margin_px + (inner[0] - placed.width) // 2,
            margin_px + (inner[1] - placed.height) // 2,
        )
        canvas.paste(placed, offset)
    return canvas


def images_to_pdf(
    inputs: list[Path],
    output: Path,
    *,
    options: ImagesToPdfOptions | None = None,
) -> ConversionReport:
    options = options or ImagesToPdfOptions()
    if options.fit not in {"fit", "stretch", "center"}:
        raise PipelineError(f"Unknown fit mode {options.fit!r}; use fit, stretch, or center")
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
        for index, artifact in enumerate(artifacts):
            context.emit(
                "compose", current=index, total=len(artifacts), message=artifact.path.name
            )
            for frame in _load_image_pages(artifact.path, context.limits.max_image_pixels):
                if size_pt is None:
                    pages.append(frame)
                else:
                    pages.append(_compose_page(frame, size_pt, options))
        if not pages:
            raise PipelineError("No images could be read")
        limit = context.limits.max_pages
        if limit is not None and len(pages) > limit:
            raise PipelineError(f"{len(pages)} pages exceeds the configured limit of {limit}")
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
        return ExecuteResult(
            candidates=[
                CandidateOutput(
                    workspace_path=staging,
                    destination=output,
                    expected_pages=len(pages),
                    render_all=len(pages) <= 50,
                )
            ],
            fidelity_warnings=fidelity,
            output_page_count=len(pages),
            details={
                "page_size": options.page_size,
                "fit": options.fit,
                "dpi": options.dpi,
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

    registry = default_registry()
    engine = registry.engine_for(OP_PDF_TO_IMAGES)
    engine_info = engine.probe()
    stem = input_path.stem

    def execute(context: JobContext, artifacts: list[InputArtifact]) -> ExecuteResult:
        import pypdfium2 as pdfium

        pdf = pdfium.PdfDocument(str(artifacts[0].path), password=options.password)
        try:
            total = len(pdf)
            selection = (options.pages or PageRange(spec="all")).resolve(total)
            candidates: list[CandidateOutput] = []
            for order, page_number in enumerate(selection):
                context.emit(
                    "render", current=order, total=len(selection), message=f"page {page_number}"
                )
                page = pdf[page_number - 1]
                try:
                    bitmap = page.render(scale=options.dpi / 72.0)
                    image = bitmap.to_pil()
                finally:
                    page.close()
                name = f"{stem}-page-{page_number:03d}{extension}"
                staging = context.workspace / name
                save_kwargs: dict[str, object] = {"format": pil_format}
                if pil_format == "JPEG":
                    image = image.convert("RGB")
                    save_kwargs["quality"] = options.jpeg_quality
                    save_kwargs["dpi"] = (options.dpi, options.dpi)
                elif pil_format in ("PNG", "TIFF"):
                    save_kwargs["dpi"] = (options.dpi, options.dpi)
                image.save(staging, **save_kwargs)
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
            details={"dpi": options.dpi, "format": format_key, "pages_rendered": len(candidates)},
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
