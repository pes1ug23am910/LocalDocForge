"""Registry-derived MCP tool definitions and shared argument validation."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, ClassVar, Self

from mcp import types
from pydantic import AfterValidator, Field, ValidationError, model_validator

from localdocforge.api.operations import (
    CompressParams,
    ConvertImagesParams,
    CropParams,
    ExtractPagesParams,
    ImagesToPdfParams,
    InspectParams,
    MdToPdfParams,
    MergeParams,
    OcrParams,
    OperationParameters,
    OrganizeParams,
    PdfToImagesParams,
    PdfToMdParams,
    RemovePagesParams,
    RotateParams,
    SplitParams,
)
from localdocforge.engines.registry import CAPABILITY_SPECS, CapabilitySpec
from localdocforge.jobs.workspace import CollisionPolicy

MAX_TOOL_INPUTS = 256
_MAX_VALIDATION_MESSAGE_CHARS = 2048


class ToolRegistryError(RuntimeError):
    """The implemented capability registry and MCP bindings disagree."""


class ToolLookupError(ValueError):
    """A requested public tool is unknown or is not implemented."""


class ToolArgumentsError(ValueError):
    """A recognized tool received invalid, safely formatted arguments."""


def _normalize_absolute_path(value: Path) -> Path:
    if not value.is_absolute():
        raise ValueError("path must be absolute")
    try:
        # Keep parent-side schema validation lexical. Filesystem resolution can
        # probe an existing UNC target before strict-offline worker containment
        # is active; the standard pipeline performs all policy-aware I/O later.
        normalized = Path(os.path.normpath(os.fspath(value)))
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError("path could not be normalized") from exc
    if not normalized.is_absolute():  # pragma: no cover - defensive platform guard
        raise ValueError("path must be absolute")
    return normalized


AbsolutePath = Annotated[
    Path,
    AfterValidator(_normalize_absolute_path),
    Field(description="Absolute local filesystem path."),
]


class _McpPaths(OperationParameters):
    """Path/collision fields added by the local-agent transport."""

    _operation_fields: ClassVar[frozenset[str]] = frozenset()

    def operation_arguments(self) -> dict[str, Any]:
        return {
            key: value
            for key, value in self.model_dump(mode="python").items()
            if key in self._operation_fields
        }


class _OutputFile:
    output: AbsolutePath = Field(description="Absolute destination file path.")
    collision: CollisionPolicy = Field(
        default=CollisionPolicy.FAIL,
        description="Behavior when an output already exists: fail, rename, or overwrite.",
    )


class _OutputDirectory:
    output_dir: AbsolutePath = Field(description="Absolute destination directory path.")
    collision: CollisionPolicy = Field(
        default=CollisionPolicy.FAIL,
        description="Behavior when a generated output already exists: fail, rename, or overwrite.",
    )


class MergeToolParams(MergeParams, _McpPaths, _OutputFile):
    inputs: list[AbsolutePath] = Field(
        min_length=2,
        max_length=MAX_TOOL_INPUTS,
        description="Ordered absolute PDF input paths.",
    )
    _operation_fields = frozenset(MergeParams.model_fields)

    @model_validator(mode="after")
    def page_ranges_match_inputs(self) -> Self:
        if self.pages is not None and len(self.pages) != len(self.inputs):
            raise ValueError("pages must contain one entry per input")
        return self


class SplitToolParams(SplitParams, _McpPaths, _OutputDirectory):
    input: AbsolutePath = Field(description="Absolute PDF input path.")
    _operation_fields = frozenset(SplitParams.model_fields)


class RemovePagesToolParams(RemovePagesParams, _McpPaths, _OutputFile):
    input: AbsolutePath = Field(description="Absolute PDF input path.")
    _operation_fields = frozenset(RemovePagesParams.model_fields)


class ExtractPagesToolParams(ExtractPagesParams, _McpPaths, _OutputFile):
    input: AbsolutePath = Field(description="Absolute PDF input path.")
    _operation_fields = frozenset(ExtractPagesParams.model_fields)


class OrganizeToolParams(OrganizeParams, _McpPaths, _OutputFile):
    input: AbsolutePath = Field(description="Absolute PDF input path.")
    _operation_fields = frozenset(OrganizeParams.model_fields)


class RotateToolParams(RotateParams, _McpPaths, _OutputFile):
    input: AbsolutePath = Field(description="Absolute PDF input path.")
    _operation_fields = frozenset(RotateParams.model_fields)


class CropToolParams(CropParams, _McpPaths, _OutputFile):
    input: AbsolutePath = Field(description="Absolute PDF input path.")
    _operation_fields = frozenset(CropParams.model_fields)


class InspectToolParams(InspectParams, _McpPaths):
    input: AbsolutePath = Field(description="Absolute PDF input path.")
    _operation_fields = frozenset(InspectParams.model_fields)


class CompressToolParams(CompressParams, _McpPaths, _OutputFile):
    input: AbsolutePath = Field(description="Absolute PDF input path.")
    _operation_fields = frozenset(CompressParams.model_fields)


class OcrToolParams(OcrParams, _McpPaths, _OutputFile):
    input: AbsolutePath = Field(description="Absolute PDF input path.")
    sidecar: AbsolutePath | None = Field(
        default=None,
        description="Optional absolute UTF-8 text sidecar destination.",
    )
    _operation_fields = frozenset(OcrParams.model_fields)


class ImagesToPdfToolParams(ImagesToPdfParams, _McpPaths, _OutputFile):
    inputs: list[AbsolutePath] = Field(
        min_length=1,
        max_length=MAX_TOOL_INPUTS,
        description="Ordered absolute image input paths.",
    )
    _operation_fields = frozenset(ImagesToPdfParams.model_fields)


class PdfToImagesToolParams(PdfToImagesParams, _McpPaths, _OutputDirectory):
    input: AbsolutePath = Field(description="Absolute PDF input path.")
    _operation_fields = frozenset(PdfToImagesParams.model_fields)


class ConvertImagesToolParams(ConvertImagesParams, _McpPaths, _OutputDirectory):
    inputs: list[AbsolutePath] = Field(
        min_length=1,
        max_length=MAX_TOOL_INPUTS,
        description="Ordered absolute image input paths.",
    )
    _operation_fields = frozenset(ConvertImagesParams.model_fields)


class PdfToMdToolParams(PdfToMdParams, _McpPaths, _OutputFile):
    input: AbsolutePath = Field(description="Absolute PDF input path.")
    _operation_fields = frozenset(PdfToMdParams.model_fields)


class MdToPdfToolParams(MdToPdfParams, _McpPaths, _OutputFile):
    input: AbsolutePath = Field(description="Absolute Markdown input path.")
    assets: list[AbsolutePath] = Field(
        default_factory=list,
        max_length=MAX_TOOL_INPUTS,
        description="Absolute image paths referenced by Markdown, keyed by basename.",
    )
    _operation_fields = frozenset(MdToPdfParams.model_fields)

    @model_validator(mode="after")
    def asset_basenames_are_unambiguous(self) -> Self:
        names = [os.path.normcase(path.name) for path in self.assets]
        if len(names) != len(set(names)) or os.path.normcase(self.input.name) in names:
            raise ValueError("input and assets must have distinct basenames")
        return self


MCP_OPERATION_MODELS: dict[str, type[_McpPaths]] = {
    "merge": MergeToolParams,
    "split": SplitToolParams,
    "remove-pages": RemovePagesToolParams,
    "extract-pages": ExtractPagesToolParams,
    "organize": OrganizeToolParams,
    "rotate": RotateToolParams,
    "crop": CropToolParams,
    "inspect": InspectToolParams,
    "compress": CompressToolParams,
    "ocr": OcrToolParams,
    "images-to-pdf": ImagesToPdfToolParams,
    "pdf-to-images": PdfToImagesToolParams,
    "convert-images": ConvertImagesToolParams,
    "pdf-to-md": PdfToMdToolParams,
    "md-to-pdf": MdToPdfToolParams,
}


@dataclass(frozen=True)
class ToolBinding:
    spec: CapabilitySpec
    model: type[_McpPaths]


def tool_bindings() -> tuple[ToolBinding, ...]:
    """Derive the entire public list from implemented capability specs."""
    bindings: list[ToolBinding] = []
    seen_names: set[str] = set()
    for spec in CAPABILITY_SPECS:
        if not spec.implemented:
            continue
        if spec.id in seen_names:
            raise ToolRegistryError(f"Duplicate implemented capability id {spec.id!r}")
        seen_names.add(spec.id)
        if spec.operation is None:
            raise ToolRegistryError(
                f"Implemented capability {spec.id!r} has no operation binding"
            )
        model = MCP_OPERATION_MODELS.get(spec.operation)
        if model is None:
            raise ToolRegistryError(
                f"Implemented capability {spec.id!r} has no MCP parameter model"
            )
        bindings.append(ToolBinding(spec=spec, model=model))
    return tuple(bindings)


def tool_definitions() -> list[types.Tool]:
    definitions: list[types.Tool] = []
    for binding in tool_bindings():
        description = binding.spec.title
        if binding.spec.notes:
            description += f". {binding.spec.notes}"
        definitions.append(
            types.Tool(
                name=binding.spec.id,
                title=binding.spec.title,
                description=description,
                inputSchema=binding.model.model_json_schema(mode="validation"),
                annotations=types.ToolAnnotations(
                    readOnlyHint=binding.spec.id == "inspect",
                    destructiveHint=binding.spec.id != "inspect",
                    idempotentHint=binding.spec.id == "inspect",
                    openWorldHint=False,
                ),
            )
        )
    return definitions


def resolve_tool(name: str) -> ToolBinding:
    for binding in tool_bindings():
        if binding.spec.id == name:
            return binding
    for spec in CAPABILITY_SPECS:
        if spec.id == name:
            raise ToolLookupError("Tool is not implemented")
    raise ToolLookupError("Unknown tool")


def _safe_validation_message(
    exc: ValidationError,
    allowed_fields: frozenset[str],
    secrets: tuple[str, ...],
) -> str:
    safe_errors = exc.errors(
        include_url=False,
        include_context=False,
        include_input=False,
    )
    summaries: list[str] = []
    for error in safe_errors[:8]:
        raw_location = error.get("loc", ())
        first = raw_location[0] if raw_location else None
        location_parts = [first] if isinstance(first, str) and first in allowed_fields else []
        location_parts.extend(
            str(item) for item in raw_location[1:4] if isinstance(item, int)
        )
        location = ".".join(location_parts) or "arguments"
        message = str(error.get("msg", "invalid value")).removeprefix("Value error, ")
        summaries.append(f"{location}: {message}")
    if len(safe_errors) > len(summaries):
        summaries.append(f"{len(safe_errors) - len(summaries)} additional error(s)")
    combined = "; ".join(summaries) or "invalid tool arguments"
    for secret in secrets:
        if secret:
            combined = combined.replace(secret, "<redacted>")
    if len(combined) > _MAX_VALIDATION_MESSAGE_CHARS:
        return combined[:_MAX_VALIDATION_MESSAGE_CHARS] + "…"
    return combined


def validate_tool_arguments(name: str, arguments: dict[str, Any]) -> _McpPaths:
    binding = resolve_tool(name)
    try:
        return binding.model.model_validate(arguments)
    except ValidationError as exc:
        password = arguments.get("password")
        secrets = (password,) if isinstance(password, str) else ()
        raise ToolArgumentsError(
            _safe_validation_message(
                exc,
                frozenset(binding.model.model_fields),
                secrets,
            )
        ) from exc


__all__ = [
    "MCP_OPERATION_MODELS",
    "MAX_TOOL_INPUTS",
    "ToolArgumentsError",
    "ToolBinding",
    "ToolLookupError",
    "ToolRegistryError",
    "resolve_tool",
    "tool_bindings",
    "tool_definitions",
    "validate_tool_arguments",
]
