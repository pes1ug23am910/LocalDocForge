"""Child-process execution of validated MCP tools through standard pipelines."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from localdocforge.config.settings import Settings
from localdocforge.domain.models import ConversionReport, ProgressCallback
from localdocforge.domain.pages import PageRange
from localdocforge.operations import images as image_ops
from localdocforge.operations import markdown as markdown_ops
from localdocforge.operations import ocr as ocr_ops
from localdocforge.operations import optimize as optimize_ops
from localdocforge.operations import organize as organize_ops
from localdocforge.operations import text as text_ops
from localdocforge.operations.inspect import InspectOptions, inspect_pdf_to_json

from .tools import (
    CompressToolParams,
    ConvertImagesToolParams,
    CropToolParams,
    ExtractPagesToolParams,
    ImagesToPdfToolParams,
    InspectToolParams,
    MdToPdfToolParams,
    MergeToolParams,
    OcrToolParams,
    OrganizeToolParams,
    PdfToImagesToolParams,
    PdfToMdToolParams,
    RemovePagesToolParams,
    RotateToolParams,
    SplitToolParams,
    validate_tool_arguments,
)

MAX_STRUCTURED_RESULT_BYTES = 512 * 1024


class McpExecutionError(ValueError):
    """A controlled MCP execution failure whose text contains no document data."""


@dataclass(frozen=True)
class McpExecutionResult:
    report: ConversionReport
    data: dict[str, Any] = field(default_factory=dict)


def _range(value: str | None) -> PageRange | None:
    return None if value is None else PageRange(spec=value)


def _settings_for_internal_inspection(
    settings: Settings,
    inspection_output: Path,
) -> Settings:
    """Permit only inspect's private pipeline artifact beside configured roots."""
    if settings.allowed_output_roots is None:
        return settings
    data = settings.model_dump(mode="python")
    data["allowed_output_roots"] = [
        *settings.allowed_output_roots,
        inspection_output.parent,
    ]
    return Settings.model_validate(data)


def _organize_options(
    args: MergeToolParams
    | SplitToolParams
    | RemovePagesToolParams
    | ExtractPagesToolParams
    | OrganizeToolParams
    | RotateToolParams
    | CropToolParams
    | CompressToolParams,
    settings: Settings,
    progress: ProgressCallback | None,
) -> organize_ops.OrganizeOptions:
    return organize_ops.OrganizeOptions(
        collision=args.collision,
        settings=settings,
        password=args.password,
        progress=progress,
    )


def execute_tool(
    tool_name: str,
    arguments: dict[str, Any],
    settings: Settings,
    *,
    inspection_output: Path,
    progress: ProgressCallback | None = None,
) -> McpExecutionResult:
    """Validate again in the spawned child, then invoke one standard pipeline."""
    args = validate_tool_arguments(tool_name, arguments)

    if isinstance(args, MergeToolParams):
        ranges = None if args.pages is None else [_range(value) for value in args.pages]
        report = organize_ops.merge_pdfs(
            args.inputs,
            args.output,
            page_ranges=ranges,
            options=_organize_options(args, settings, progress),
        )
    elif isinstance(args, SplitToolParams):
        report = organize_ops.split_pdf(
            args.input,
            args.output_dir,
            pages=_range(args.pages),
            every=args.every,
            options=_organize_options(args, settings, progress),
        )
    elif isinstance(args, RemovePagesToolParams):
        report = organize_ops.remove_pages(
            args.input,
            args.output,
            PageRange(spec=args.pages),
            options=_organize_options(args, settings, progress),
        )
    elif isinstance(args, ExtractPagesToolParams):
        report = organize_ops.extract_pages(
            args.input,
            args.output,
            PageRange(spec=args.pages),
            options=_organize_options(args, settings, progress),
        )
    elif isinstance(args, OrganizeToolParams):
        report = organize_ops.organize_pdf(
            args.input,
            args.output,
            PageRange(spec=args.order),
            options=_organize_options(args, settings, progress),
        )
    elif isinstance(args, RotateToolParams):
        report = organize_ops.rotate_pages(
            args.input,
            args.output,
            degrees=args.degrees,
            pages=_range(args.pages),
            options=_organize_options(args, settings, progress),
        )
    elif isinstance(args, CropToolParams):
        report = organize_ops.crop_pages(
            args.input,
            args.output,
            box=args.box,
            pages=_range(args.pages),
            options=_organize_options(args, settings, progress),
        )
    elif isinstance(args, CompressToolParams):
        report = optimize_ops.compress_pdf(
            args.input,
            args.output,
            preset=args.preset,
            options=_organize_options(args, settings, progress),
        )
    elif isinstance(args, OcrToolParams):
        report = ocr_ops.ocr_pdf(
            args.input,
            args.output,
            options=ocr_ops.OcrOptions(
                language=args.language,
                sidecar=args.sidecar,
                skip_text=args.skip_text,
                force_ocr=args.force_ocr,
                collision=args.collision,
                settings=settings,
                progress=progress,
                password=args.password,
            ),
        )
    elif isinstance(args, ImagesToPdfToolParams):
        report = image_ops.images_to_pdf(
            args.inputs,
            args.output,
            options=image_ops.ImagesToPdfOptions(
                page_size=args.page_size,
                fit=args.fit,
                margin_pt=args.margin,
                background=args.background,
                dpi=args.dpi,
                jpeg_quality=args.quality,
                collision=args.collision,
                settings=settings,
                progress=progress,
            ),
        )
    elif isinstance(args, PdfToImagesToolParams):
        options = image_ops.PdfToImagesOptions(
            pages=_range(args.pages),
            preset=args.preset,
            collision=args.collision,
            settings=settings,
            password=args.password,
            progress=progress,
        )
        if args.format is not None:
            options.image_format = args.format
        if args.dpi is not None:
            options.dpi = args.dpi
        if args.quality is not None:
            options.jpeg_quality = args.quality
        report = image_ops.pdf_to_images(args.input, args.output_dir, options=options)
    elif isinstance(args, ConvertImagesToolParams):
        report = image_ops.convert_images(
            args.inputs,
            args.output_dir,
            options=image_ops.ConvertImagesOptions(
                image_format=args.format,
                quality=args.quality,
                max_dimension=args.max_dimension,
                preset=args.preset,
                keep_metadata=args.keep_metadata,
                background=args.background,
                collision=args.collision,
                settings=settings,
                progress=progress,
            ),
        )
    elif isinstance(args, PdfToMdToolParams):
        report = text_ops.pdf_to_md(
            args.input,
            args.output,
            options=text_ops.PdfToMdOptions(
                output_format=args.format,
                pages=_range(args.pages),
                page_anchors=args.page_anchors,
                tables=args.tables,
                collision=args.collision,
                settings=settings,
                progress=progress,
                password=args.password,
            ),
        )
    elif isinstance(args, MdToPdfToolParams):
        report = markdown_ops.md_to_pdf(
            args.input,
            args.output,
            options=markdown_ops.MdToPdfOptions(
                paper=args.paper,
                margin_mm=args.margin,
                toc=args.toc,
                collision=args.collision,
                settings=settings,
                progress=progress,
            ),
            image_inputs={path.name: path for path in args.assets},
        )
    elif isinstance(args, InspectToolParams):
        report = inspect_pdf_to_json(
            args.input,
            inspection_output,
            options=InspectOptions(
                settings=_settings_for_internal_inspection(
                    settings,
                    inspection_output,
                ),
                password=args.password,
                progress=progress,
            ),
        )
        inspection = json.loads(inspection_output.read_text(encoding="utf-8", errors="strict"))
        if not isinstance(inspection, dict):  # pragma: no cover - validator guarantees this
            raise RuntimeError("Inspection pipeline returned an invalid result")
        encoded = json.dumps(
            inspection,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded) > MAX_STRUCTURED_RESULT_BYTES:
            raise McpExecutionError("Inspection result exceeds the MCP response limit")
        return McpExecutionResult(report=report, data={"inspection": inspection})
    else:  # pragma: no cover - registry/model drift is checked before execution
        raise RuntimeError("MCP tool model has no executor")

    return McpExecutionResult(report=report)


__all__ = [
    "MAX_STRUCTURED_RESULT_BYTES",
    "McpExecutionError",
    "McpExecutionResult",
    "execute_tool",
]
