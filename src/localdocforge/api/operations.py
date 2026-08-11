"""FastAPI-free operation parameters and worker runner bindings.

The HTTP transport supplies strings from multipart form fields while local
agent transports supply native JSON values.  Both cross the same Pydantic
models here; runners receive typed parameters and never parse transport data.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Literal, Self, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)

from localdocforge.config.settings import Settings
from localdocforge.domain.models import ConversionReport, ProgressCallback
from localdocforge.domain.pages import PageRange, PageRangeError
from localdocforge.engines.base import EngineUnavailableError
from localdocforge.jobs.workspace import CollisionPolicy
from localdocforge.operations import images as image_ops
from localdocforge.operations import markdown as markdown_ops
from localdocforge.operations import ocr as ocr_ops
from localdocforge.operations import optimize as optimize_ops
from localdocforge.operations import organize as organize_ops
from localdocforge.operations import text as text_ops
from localdocforge.pipelines.runner import PipelineError


class _ApiError(Exception):
    """Controlled operation/API error safe to return to a local caller."""

    def __init__(self, status: int, message: str) -> None:
        self.status = status
        self.message = message
        super().__init__(message)


class OperationParameters(BaseModel):
    """Base for typed operation fields shared by HTTP and agent transports."""

    model_config = ConfigDict(extra="forbid")


class _PasswordParams(OperationParameters):
    """Operation parameters with a repr-safe input password."""

    password: str | None = Field(default=None, repr=False)


def _optional_range(value: Any, *, what: str) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError(f"Invalid {what}: page range must be text")
    try:
        PageRange(spec=value)
    except (PageRangeError, ValueError) as exc:
        raise ValueError(f"Invalid {what}: {exc}") from exc
    return value


def _required_range(value: Any, *, what: str) -> str:
    checked = _optional_range(value, what=what)
    if checked is None:
        raise ValueError(f"'{what}' is required")
    return checked


def _integer(
    value: Any,
    *,
    key: str,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    if isinstance(value, bool):
        raise ValueError(f"'{key}' must be an integer")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = int(value)
        except ValueError:
            raise ValueError(f"'{key}' must be an integer") from None
    else:
        raise ValueError(f"'{key}' must be an integer")
    if minimum is not None and parsed < minimum:
        raise ValueError(f"'{key}' must be at least {minimum}")
    if maximum is not None and parsed > maximum:
        raise ValueError(f"'{key}' must be at most {maximum}")
    return parsed


def _number(
    value: Any,
    *,
    key: str,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    if isinstance(value, bool):
        raise ValueError(f"'{key}' must be a number")
    if isinstance(value, (int, float)):
        parsed = float(value)
    elif isinstance(value, str):
        try:
            parsed = float(value)
        except ValueError:
            raise ValueError(f"'{key}' must be a number") from None
    else:
        raise ValueError(f"'{key}' must be a number")
    if not math.isfinite(parsed):
        raise ValueError(f"'{key}' must be finite")
    if minimum is not None and parsed < minimum:
        raise ValueError(f"'{key}' must be at least {minimum:g}")
    if maximum is not None and parsed > maximum:
        raise ValueError(f"'{key}' must be at most {maximum:g}")
    return parsed


def _strict_bool(value: Any, *, key: str, allow_binary_text: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized == "true" or (allow_binary_text and normalized == "1"):
            return True
        if normalized == "false" or (allow_binary_text and normalized == "0"):
            return False
    raise ValueError(f"'{key}' must be true or false")


class MergeParams(_PasswordParams):
    pages: list[str | None] | None = Field(default=None, max_length=256)

    @field_validator("pages", mode="before")
    @classmethod
    def parse_legacy_pages(cls, value: Any) -> Any:
        if value in (None, ""):
            return None
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError as exc:
                raise ValueError("'pages' must be valid JSON") from exc
        if not isinstance(value, list):
            raise ValueError("'pages' must be a JSON list with one entry per file")
        if any(item is not None and not isinstance(item, str) for item in value):
            raise ValueError("Each 'pages' entry must be a string or null")
        for item in value:
            _optional_range(item, what="pages")
        return value


class SplitParams(_PasswordParams):
    pages: str | None = None
    every: int | None = Field(default=None, ge=1)

    @field_validator("pages", mode="before")
    @classmethod
    def validate_pages(cls, value: Any) -> str | None:
        return _optional_range(value, what="pages")

    @field_validator("every", mode="before")
    @classmethod
    def validate_every(cls, value: Any) -> int | None:
        if value in (None, ""):
            return None
        return _integer(value, key="every", minimum=1)

    @model_validator(mode="after")
    def selection_modes_are_exclusive(self) -> Self:
        if self.pages is not None and self.every is not None:
            raise ValueError("Choose either --pages or --every, not both")
        return self


class RemovePagesParams(_PasswordParams):
    pages: str

    @field_validator("pages", mode="before")
    @classmethod
    def validate_pages(cls, value: Any) -> str:
        return _required_range(value, what="pages")


class ExtractPagesParams(RemovePagesParams):
    pass


class OrganizeParams(_PasswordParams):
    order: str

    @field_validator("order", mode="before")
    @classmethod
    def validate_order(cls, value: Any) -> str:
        return _required_range(value, what="order")


class RotateParams(_PasswordParams):
    degrees: int = Field(multiple_of=90)
    pages: str | None = None

    @field_validator("degrees", mode="before")
    @classmethod
    def validate_degrees(cls, value: Any) -> int:
        parsed = _integer(value, key="degrees")
        if parsed % 90:
            raise ValueError("Rotation must be a multiple of 90 degrees")
        return parsed

    @field_validator("pages", mode="before")
    @classmethod
    def validate_pages(cls, value: Any) -> str | None:
        return _optional_range(value, what="pages")


class CropParams(_PasswordParams):
    box: tuple[float, float, float, float]
    pages: str | None = None

    @field_validator("box", mode="before")
    @classmethod
    def parse_legacy_box(cls, value: Any) -> tuple[float, float, float, float]:
        raw_values: Any = value.split(",") if isinstance(value, str) else value
        if not isinstance(raw_values, (list, tuple)) or len(raw_values) != 4:
            raise ValueError("'box' must be 'x0,y0,x1,y1' in points")
        try:
            parsed = tuple(float(item) for item in raw_values)
        except (TypeError, ValueError):
            raise ValueError("'box' must be 'x0,y0,x1,y1' in points") from None
        if not all(math.isfinite(item) for item in parsed):
            raise ValueError("'box' must be 'x0,y0,x1,y1' in points")
        return cast(tuple[float, float, float, float], parsed)

    @field_validator("pages", mode="before")
    @classmethod
    def validate_pages(cls, value: Any) -> str | None:
        return _optional_range(value, what="pages")


class InspectParams(_PasswordParams):
    pass


class CompressParams(_PasswordParams):
    preset: Literal["lossless"] = "lossless"

    @field_validator("preset", mode="before")
    @classmethod
    def validate_preset(cls, value: Any) -> str:
        if value != "lossless":
            raise ValueError("'preset' must be one of: lossless")
        return "lossless"


class OcrParams(_PasswordParams):
    language: str = "eng"
    skip_text: bool = False
    force_ocr: bool = False

    @field_validator("language", mode="before")
    @classmethod
    def validate_language(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("'language' must be OCR language codes joined by '+'")
        try:
            ocr_ops._language_codes(value)
        except PipelineError as exc:
            raise ValueError(str(exc)) from exc
        return value

    @field_validator("skip_text", "force_ocr", mode="before")
    @classmethod
    def validate_booleans(cls, value: Any, info: ValidationInfo) -> bool:
        return _strict_bool(value, key=info.field_name or "parameter")

    @model_validator(mode="after")
    def text_modes_are_exclusive(self) -> Self:
        if self.skip_text and self.force_ocr:
            raise ValueError("'skip_text' and 'force_ocr' are mutually exclusive")
        return self


class OcrHttpParams(OcrParams):
    sidecar: bool = False

    @field_validator("sidecar", mode="before")
    @classmethod
    def validate_sidecar(cls, value: Any) -> bool:
        return _strict_bool(value, key="sidecar")


class ImagesToPdfParams(OperationParameters):
    page_size: str = "A4"
    fit: Literal["fit", "stretch", "center"] = "fit"
    margin: float = Field(default=24.0, ge=0, allow_inf_nan=False)
    background: str = "white"
    dpi: int = Field(default=200, ge=36, le=600)
    quality: int = Field(default=95, ge=1, le=100)

    @field_validator("fit", mode="before")
    @classmethod
    def validate_fit(cls, value: Any) -> str:
        if value not in {"fit", "stretch", "center"}:
            raise ValueError("'fit' must be one of: fit, stretch, center")
        return cast(str, value)

    @field_validator("margin", mode="before")
    @classmethod
    def validate_margin(cls, value: Any) -> float:
        return _number(value, key="margin", minimum=0)

    @field_validator("dpi", mode="before")
    @classmethod
    def validate_dpi(cls, value: Any) -> int:
        return _integer(value, key="dpi", minimum=36, maximum=600)

    @field_validator("quality", mode="before")
    @classmethod
    def validate_quality(cls, value: Any) -> int:
        return _integer(value, key="quality", minimum=1, maximum=100)


ImageFormat = Literal["png", "jpeg", "jpg", "webp", "tiff"]


def _image_format(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str) or value.lower() not in image_ops.OUTPUT_IMAGE_FORMATS:
        raise ValueError("'format' must be one of: png, jpeg, webp, tiff")
    return value.lower()


def _image_preset(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str) or value not in image_ops.CONVERT_PRESETS:
        raise ValueError("'preset' must be one of: " + ", ".join(sorted(image_ops.CONVERT_PRESETS)))
    return value


class PdfToImagesParams(_PasswordParams):
    format: ImageFormat | None = None
    dpi: int | None = Field(default=None, ge=18, le=1200)
    pages: str | None = None
    quality: int | None = Field(default=None, ge=1, le=100)
    preset: Literal["llm"] | None = None

    @field_validator("format", mode="before")
    @classmethod
    def validate_format(cls, value: Any) -> str | None:
        return _image_format(value)

    @field_validator("dpi", mode="before")
    @classmethod
    def validate_dpi(cls, value: Any) -> int | None:
        if value in (None, ""):
            return None
        return _integer(value, key="dpi", minimum=18, maximum=1200)

    @field_validator("pages", mode="before")
    @classmethod
    def validate_pages(cls, value: Any) -> str | None:
        return _optional_range(value, what="pages")

    @field_validator("quality", mode="before")
    @classmethod
    def validate_quality(cls, value: Any) -> int | None:
        if value in (None, ""):
            return None
        return _integer(value, key="quality", minimum=1, maximum=100)

    @field_validator("preset", mode="before")
    @classmethod
    def validate_preset(cls, value: Any) -> str | None:
        return _image_preset(value)


class PdfToMdParams(_PasswordParams):
    pages: str | None = None
    format: Literal["md", "txt", "jsonl"] = "md"
    page_anchors: bool = True
    tables: bool = False

    @field_validator("pages", mode="before")
    @classmethod
    def validate_pages(cls, value: Any) -> str | None:
        return _optional_range(value, what="pages")

    @field_validator("format", mode="before")
    @classmethod
    def validate_format(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("'format' must be one of: " + ", ".join(text_ops.TEXT_OUTPUT_FORMATS))
        normalized = value.strip().lower()
        if normalized not in text_ops.TEXT_OUTPUT_FORMATS:
            raise ValueError("'format' must be one of: " + ", ".join(text_ops.TEXT_OUTPUT_FORMATS))
        return normalized

    @field_validator("page_anchors", "tables", mode="before")
    @classmethod
    def validate_booleans(cls, value: Any, info: ValidationInfo) -> bool:
        return _strict_bool(value, key=info.field_name or "parameter")

    @model_validator(mode="after")
    def tables_require_markdown(self) -> Self:
        if self.tables and self.format != "md":
            raise ValueError("'tables' requires 'format' to be 'md'")
        return self


class MdToPdfParams(OperationParameters):
    paper: Literal["A4", "Letter", "Legal"] = "A4"
    margin: float = Field(default=20.0, ge=0, allow_inf_nan=False)
    toc: bool = False

    @field_validator("paper", mode="before")
    @classmethod
    def validate_paper(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("Unknown paper size; use A4, Letter, Legal")
        try:
            return markdown_ops.normalize_paper(value)[0]
        except PipelineError as exc:
            raise ValueError(str(exc)) from exc

    @field_validator("margin", mode="before")
    @classmethod
    def validate_margin_number(cls, value: Any) -> float:
        return _number(value, key="margin", minimum=0)

    @field_validator("toc", mode="before")
    @classmethod
    def validate_toc(cls, value: Any) -> bool:
        return _strict_bool(value, key="toc")

    @model_validator(mode="after")
    def margin_leaves_drawable_area(self) -> Self:
        try:
            markdown_ops.validate_margin(
                self.margin,
                markdown_ops.normalize_paper(self.paper),
            )
        except PipelineError as exc:
            raise ValueError(str(exc)) from exc
        return self


class ConvertImagesParams(OperationParameters):
    format: ImageFormat | None = None
    quality: int | None = Field(default=None, ge=1, le=100)
    max_dimension: int | None = Field(default=None, ge=16, le=30000)
    preset: Literal["llm"] | None = None
    keep_metadata: bool = False
    background: str = "white"

    @field_validator("format", mode="before")
    @classmethod
    def validate_format(cls, value: Any) -> str | None:
        return _image_format(value)

    @field_validator("quality", mode="before")
    @classmethod
    def validate_quality(cls, value: Any) -> int | None:
        if value in (None, ""):
            return None
        return _integer(value, key="quality", minimum=1, maximum=100)

    @field_validator("max_dimension", mode="before")
    @classmethod
    def validate_max_dimension(cls, value: Any) -> int | None:
        if value in (None, ""):
            return None
        return _integer(value, key="max_dimension", minimum=16, maximum=30000)

    @field_validator("preset", mode="before")
    @classmethod
    def validate_preset(cls, value: Any) -> str | None:
        return _image_preset(value)

    @field_validator("keep_metadata", mode="before")
    @classmethod
    def validate_keep_metadata(cls, value: Any) -> bool:
        return _strict_bool(value, key="keep_metadata", allow_binary_text=True)


OPERATION_MODELS: dict[str, type[OperationParameters]] = {
    "merge": MergeParams,
    "split": SplitParams,
    "remove-pages": RemovePagesParams,
    "extract-pages": ExtractPagesParams,
    "organize": OrganizeParams,
    "rotate": RotateParams,
    "crop": CropParams,
    "inspect": InspectParams,
    "compress": CompressParams,
    "ocr": OcrHttpParams,
    "images-to-pdf": ImagesToPdfParams,
    "pdf-to-images": PdfToImagesParams,
    "pdf-to-md": PdfToMdParams,
    "md-to-pdf": MdToPdfParams,
    "convert-images": ConvertImagesParams,
}

OPERATION_PARAMS: dict[str, frozenset[str]] = {
    operation: frozenset(model.model_fields) for operation, model in OPERATION_MODELS.items()
}


def _validation_message(operation: str, exc: ValidationError) -> str:
    error = exc.errors(include_url=False, include_input=False)[0]
    location = error.get("loc", ())
    field = str(location[-1]) if location else "parameters"
    if error.get("type") == "missing":
        if operation == "crop" and field == "box":
            return "'box' must be 'x0,y0,x1,y1' in points"
        return f"'{field}' is required"
    message = str(error.get("msg", "Invalid operation parameters"))
    return message.removeprefix("Value error, ")


def parse_operation_params(
    operation: str,
    raw: Mapping[str, Any] | OperationParameters,
) -> OperationParameters:
    """Validate HTTP strings or native JSON values through one shared model."""
    model = OPERATION_MODELS.get(operation)
    if model is None:
        raise _ApiError(404, f"Unknown or unavailable operation {operation!r}")
    if isinstance(raw, model):
        return raw
    try:
        return model.model_validate(dict(raw))
    except ValidationError as exc:
        raise _ApiError(422, _validation_message(operation, exc)) from exc


def _range_or_none(value: str | None) -> PageRange | None:
    return None if value is None else PageRange(spec=value)


def _one_input(paths: list[Path], operation: str) -> Path:
    if len(paths) != 1:
        raise _ApiError(422, f"{operation} needs exactly one file")
    return paths[0]


def _organize_options(
    settings: Settings,
    params: _PasswordParams,
    progress: ProgressCallback | None = None,
) -> organize_ops.OrganizeOptions:
    return organize_ops.OrganizeOptions(
        collision=CollisionPolicy.RENAME,
        settings=settings,
        password=params.password,
        progress=progress,
    )


def _run_merge(
    paths: list[Path],
    output_dir: Path,
    raw_params: OperationParameters,
    settings: Settings,
    progress: ProgressCallback | None = None,
) -> ConversionReport:
    params = cast(MergeParams, raw_params)
    if len(paths) < 2:
        raise _ApiError(422, "merge needs at least two files")
    ranges = None
    if params.pages is not None:
        if len(params.pages) != len(paths):
            raise _ApiError(422, "'pages' must be a JSON list with one entry per file")
        ranges = [_range_or_none(spec) for spec in params.pages]
    return organize_ops.merge_pdfs(
        paths,
        output_dir / "merged.pdf",
        page_ranges=ranges,
        options=_organize_options(settings, params, progress),
    )


def _run_split(
    paths: list[Path],
    output_dir: Path,
    raw_params: OperationParameters,
    settings: Settings,
    progress: ProgressCallback | None = None,
) -> ConversionReport:
    params = cast(SplitParams, raw_params)
    source = _one_input(paths, "split")
    return organize_ops.split_pdf(
        source,
        output_dir,
        pages=_range_or_none(params.pages),
        every=params.every,
        options=_organize_options(settings, params, progress),
    )


def _run_remove_pages(
    paths: list[Path],
    output_dir: Path,
    raw_params: OperationParameters,
    settings: Settings,
    progress: ProgressCallback | None = None,
) -> ConversionReport:
    params = cast(RemovePagesParams, raw_params)
    return organize_ops.remove_pages(
        _one_input(paths, "remove-pages"),
        output_dir / "result.pdf",
        PageRange(spec=params.pages),
        options=_organize_options(settings, params, progress),
    )


def _run_extract_pages(
    paths: list[Path],
    output_dir: Path,
    raw_params: OperationParameters,
    settings: Settings,
    progress: ProgressCallback | None = None,
) -> ConversionReport:
    params = cast(ExtractPagesParams, raw_params)
    return organize_ops.extract_pages(
        _one_input(paths, "extract-pages"),
        output_dir / "result.pdf",
        PageRange(spec=params.pages),
        options=_organize_options(settings, params, progress),
    )


def _run_organize(
    paths: list[Path],
    output_dir: Path,
    raw_params: OperationParameters,
    settings: Settings,
    progress: ProgressCallback | None = None,
) -> ConversionReport:
    params = cast(OrganizeParams, raw_params)
    return organize_ops.organize_pdf(
        _one_input(paths, "organize"),
        output_dir / "result.pdf",
        PageRange(spec=params.order),
        options=_organize_options(settings, params, progress),
    )


def _run_rotate(
    paths: list[Path],
    output_dir: Path,
    raw_params: OperationParameters,
    settings: Settings,
    progress: ProgressCallback | None = None,
) -> ConversionReport:
    params = cast(RotateParams, raw_params)
    return organize_ops.rotate_pages(
        _one_input(paths, "rotate"),
        output_dir / "rotated.pdf",
        degrees=params.degrees,
        pages=_range_or_none(params.pages),
        options=_organize_options(settings, params, progress),
    )


def _run_crop(
    paths: list[Path],
    output_dir: Path,
    raw_params: OperationParameters,
    settings: Settings,
    progress: ProgressCallback | None = None,
) -> ConversionReport:
    params = cast(CropParams, raw_params)
    return organize_ops.crop_pages(
        _one_input(paths, "crop"),
        output_dir / "cropped.pdf",
        box=params.box,
        pages=_range_or_none(params.pages),
        options=_organize_options(settings, params, progress),
    )


def _run_compress(
    paths: list[Path],
    output_dir: Path,
    raw_params: OperationParameters,
    settings: Settings,
    progress: ProgressCallback | None = None,
) -> ConversionReport:
    params = cast(CompressParams, raw_params)
    return optimize_ops.compress_pdf(
        _one_input(paths, "compress"),
        output_dir / "compressed.pdf",
        preset=params.preset,
        options=_organize_options(settings, params, progress),
    )


def _run_ocr(
    paths: list[Path],
    output_dir: Path,
    raw_params: OperationParameters,
    settings: Settings,
    progress: ProgressCallback | None = None,
) -> ConversionReport:
    params = cast(OcrHttpParams, raw_params)
    options = ocr_ops.OcrOptions(
        language=params.language,
        sidecar=output_dir / "document.txt" if params.sidecar else None,
        skip_text=params.skip_text,
        force_ocr=params.force_ocr,
        collision=CollisionPolicy.RENAME,
        settings=settings,
        progress=progress,
        password=params.password,
    )
    try:
        return ocr_ops.ocr_pdf(
            _one_input(paths, "ocr"),
            output_dir / "document.pdf",
            options=options,
        )
    except EngineUnavailableError as exc:
        raise _ApiError(503, str(exc)) from exc


def _run_images_to_pdf(
    paths: list[Path],
    output_dir: Path,
    raw_params: OperationParameters,
    settings: Settings,
    progress: ProgressCallback | None = None,
) -> ConversionReport:
    params = cast(ImagesToPdfParams, raw_params)
    options = image_ops.ImagesToPdfOptions(
        page_size=params.page_size,
        fit=params.fit,
        margin_pt=params.margin,
        background=params.background,
        dpi=params.dpi,
        jpeg_quality=params.quality,
        collision=CollisionPolicy.RENAME,
        settings=settings,
        progress=progress,
    )
    return image_ops.images_to_pdf(paths, output_dir / "images.pdf", options=options)


def _run_pdf_to_images(
    paths: list[Path],
    output_dir: Path,
    raw_params: OperationParameters,
    settings: Settings,
    progress: ProgressCallback | None = None,
) -> ConversionReport:
    params = cast(PdfToImagesParams, raw_params)
    source = _one_input(paths, "pdf-to-images")
    options = image_ops.PdfToImagesOptions(
        pages=_range_or_none(params.pages),
        preset=params.preset,
        collision=CollisionPolicy.RENAME,
        settings=settings,
        password=params.password,
        progress=progress,
    )
    if params.format is not None:
        options.image_format = params.format
    if params.dpi is not None:
        options.dpi = params.dpi
    if params.quality is not None:
        options.jpeg_quality = params.quality
    return image_ops.pdf_to_images(source, output_dir, options=options)


def _run_pdf_to_md(
    paths: list[Path],
    output_dir: Path,
    raw_params: OperationParameters,
    settings: Settings,
    progress: ProgressCallback | None = None,
) -> ConversionReport:
    params = cast(PdfToMdParams, raw_params)
    source = _one_input(paths, "pdf-to-md")
    options = text_ops.PdfToMdOptions(
        output_format=params.format,
        pages=_range_or_none(params.pages),
        page_anchors=params.page_anchors,
        tables=params.tables,
        collision=CollisionPolicy.RENAME,
        settings=settings,
        progress=progress,
        password=params.password,
    )
    return text_ops.pdf_to_md(
        source,
        output_dir / f"document.{params.format}",
        options=options,
    )


def _transport_upload_alias(path: Path) -> str:
    """Undo the private numeric transport prefix while retaining sanitization."""
    prefix, separator, alias = path.name.partition("-")
    if separator and prefix.isdecimal() and alias:
        return alias
    return path.name


def _run_md_to_pdf(
    paths: list[Path],
    output_dir: Path,
    raw_params: OperationParameters,
    settings: Settings,
    progress: ProgressCallback | None = None,
) -> ConversionReport:
    params = cast(MdToPdfParams, raw_params)
    aliases = {_transport_upload_alias(path): path for path in paths}
    if len(aliases) != len(paths):
        raise _ApiError(422, "Markdown uploads must have distinct sanitized basenames")
    markdown_names = [
        name for name in aliases if Path(name).suffix.casefold() in {".md", ".markdown"}
    ]
    if len(markdown_names) != 1:
        raise _ApiError(422, "md-to-pdf needs exactly one .md or .markdown upload")
    source_name = markdown_names[0]
    source = aliases.pop(source_name)
    options = markdown_ops.MdToPdfOptions(
        paper=params.paper,
        margin_mm=params.margin,
        toc=params.toc,
        collision=CollisionPolicy.RENAME,
        settings=settings,
        progress=progress,
    )
    try:
        return markdown_ops.md_to_pdf(
            source,
            output_dir / "document.pdf",
            options=options,
            image_inputs=aliases,
        )
    except EngineUnavailableError as exc:
        raise _ApiError(503, str(exc)) from exc


def _run_convert_images(
    paths: list[Path],
    output_dir: Path,
    raw_params: OperationParameters,
    settings: Settings,
    progress: ProgressCallback | None = None,
) -> ConversionReport:
    params = cast(ConvertImagesParams, raw_params)
    options = image_ops.ConvertImagesOptions(
        image_format=params.format,
        quality=params.quality,
        max_dimension=params.max_dimension,
        preset=params.preset,
        keep_metadata=params.keep_metadata,
        background=params.background,
        collision=CollisionPolicy.RENAME,
        settings=settings,
        progress=progress,
    )
    return image_ops.convert_images(paths, output_dir, options=options)


OperationRunner = Callable[
    [list[Path], Path, OperationParameters, Settings, ProgressCallback | None],
    ConversionReport,
]

OPERATIONS: dict[str, OperationRunner] = {
    "merge": _run_merge,
    "split": _run_split,
    "remove-pages": _run_remove_pages,
    "extract-pages": _run_extract_pages,
    "organize": _run_organize,
    "rotate": _run_rotate,
    "crop": _run_crop,
    "compress": _run_compress,
    "ocr": _run_ocr,
    "images-to-pdf": _run_images_to_pdf,
    "pdf-to-images": _run_pdf_to_images,
    "pdf-to-md": _run_pdf_to_md,
    "md-to-pdf": _run_md_to_pdf,
    "convert-images": _run_convert_images,
}


__all__ = [
    "CompressParams",
    "ConvertImagesParams",
    "CropParams",
    "ExtractPagesParams",
    "ImagesToPdfParams",
    "InspectParams",
    "MdToPdfParams",
    "MergeParams",
    "OcrHttpParams",
    "OcrParams",
    "OPERATIONS",
    "OPERATION_MODELS",
    "OPERATION_PARAMS",
    "OperationParameters",
    "OrganizeParams",
    "PdfToImagesParams",
    "PdfToMdParams",
    "RemovePagesParams",
    "RotateParams",
    "SplitParams",
    "_ApiError",
    "parse_operation_params",
]
