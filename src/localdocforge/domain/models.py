"""Core typed models used by operations, pipelines, engines, CLI, and API."""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


def utc_now() -> datetime:
    return datetime.now(UTC)


class WarningSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class SecurityWarning(BaseModel):
    """A security-relevant observation attached to a report (never document text)."""

    code: str
    message: str
    severity: WarningSeverity = WarningSeverity.WARNING


class FidelityWarning(BaseModel):
    """A fidelity-relevant observation (lost feature, substitution, degradation)."""

    code: str
    message: str
    severity: WarningSeverity = WarningSeverity.WARNING
    page: int | None = None


class ResourceLimits(BaseModel):
    """Bounds enforced on every job. ``None`` disables an individual bound."""

    max_input_bytes: int | None = 2 * 1024**3  # 2 GiB
    max_output_bytes: int | None = 4 * 1024**3
    max_pages: int | None = 5000
    max_image_pixels: int | None = 200_000_000  # per decoded image
    max_decompressed_bytes: int | None = 4 * 1024**3
    max_archive_entries: int | None = 10_000
    max_archive_expansion_ratio: float | None = 200.0
    timeout_seconds: float | None = 600.0
    max_subprocesses: int | None = 8


class ArtifactKind(StrEnum):
    PRIMARY = "primary"
    SIDECAR = "sidecar"
    REPORT = "report"
    ASSET = "asset"


class InputArtifact(BaseModel):
    """An immutable source file, as detected — never trusted from its extension."""

    path: Path
    media_type: str
    size_bytes: int
    page_count: int | None = None
    sha256: str | None = None


class OutputArtifact(BaseModel):
    """A generated file that passed validation and was atomically moved into place."""

    path: Path
    media_type: str
    size_bytes: int
    kind: ArtifactKind = ArtifactKind.PRIMARY
    page_count: int | None = None
    sha256: str | None = None


class ValidationCheck(BaseModel):
    name: str
    passed: bool
    detail: str = ""


class ValidationResult(BaseModel):
    """Outcome of structural/render validation of a generated document."""

    passed: bool
    checks: list[ValidationCheck] = Field(default_factory=list)

    @classmethod
    def combine(cls, checks: list[ValidationCheck]) -> ValidationResult:
        return cls(passed=all(check.passed for check in checks), checks=checks)


class EngineKind(StrEnum):
    PYTHON_LIBRARY = "python_library"
    EXECUTABLE = "executable"


class EngineInfo(BaseModel):
    """Result of probing one engine at runtime."""

    name: str
    kind: EngineKind
    available: bool
    version: str | None = None
    path: str | None = None
    license: str | None = None
    notes: str = ""
    install_hint: str = ""


class Capability(BaseModel):
    """A user-facing feature; may only be advertised when a real probe passed."""

    id: str
    title: str
    category: str
    available: bool
    engines: list[str] = Field(default_factory=list)
    missing_requirements: list[str] = Field(default_factory=list)
    install_hint: str = ""
    notes: str = ""


class ProgressEvent(BaseModel):
    job_id: str
    stage: str
    current: int = 0
    total: int = 0
    message: str = ""
    timestamp: datetime = Field(default_factory=utc_now)


class OperationSpec(BaseModel):
    """Base class for typed operation parameters. Subclasses set ``operation``."""

    model_config = ConfigDict(extra="forbid")

    operation: str


class ReportStatus(StrEnum):
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ConversionReport(BaseModel):
    """Machine-readable record of one operation. Never contains document text,
    passwords, or redacted content."""

    operation: str
    status: ReportStatus
    job_id: str
    engine: str | None = None
    engine_version: str | None = None
    fallback_engine: str | None = None
    inputs: list[InputArtifact] = Field(default_factory=list)
    outputs: list[OutputArtifact] = Field(default_factory=list)
    input_page_count: int | None = None
    output_page_count: int | None = None
    input_bytes: int | None = None
    output_bytes: int | None = None
    started_at: datetime = Field(default_factory=utc_now)
    finished_at: datetime | None = None
    elapsed_seconds: float | None = None
    security_warnings: list[SecurityWarning] = Field(default_factory=list)
    fidelity_warnings: list[FidelityWarning] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    validation: ValidationResult | None = None
    details: dict[str, Any] = Field(default_factory=dict)

    def to_human(self) -> str:
        """Render a compact human-readable summary."""
        lines = [f"Operation : {self.operation}", f"Status    : {self.status.value}"]
        if self.engine:
            version = f" {self.engine_version}" if self.engine_version else ""
            lines.append(f"Engine    : {self.engine}{version}")
        for artifact in self.inputs:
            pages = f", {artifact.page_count} pages" if artifact.page_count else ""
            lines.append(f"Input     : {artifact.path.name} ({artifact.size_bytes:,} B{pages})")
        for artifact in self.outputs:
            pages = f", {artifact.page_count} pages" if artifact.page_count else ""
            lines.append(f"Output    : {artifact.path} ({artifact.size_bytes:,} B{pages})")
        if self.elapsed_seconds is not None:
            lines.append(f"Elapsed   : {self.elapsed_seconds:.2f}s")
        if self.validation is not None:
            state = "passed" if self.validation.passed else "FAILED"
            lines.append(f"Validation: {state} ({len(self.validation.checks)} checks)")
        for warning in self.security_warnings:
            lines.append(f"Security  : [{warning.severity.value}] {warning.message}")
        for warning in self.fidelity_warnings:
            page = f" (page {warning.page})" if warning.page else ""
            lines.append(f"Fidelity  : [{warning.severity.value}] {warning.message}{page}")
        for error in self.errors:
            lines.append(f"Error     : {error}")
        return "\n".join(lines)


ProgressCallback = Callable[[ProgressEvent], None]


class JobCancelled(Exception):
    """Raised inside a pipeline when the job's cancel event is set."""


@dataclass
class JobContext:
    """Runtime context handed to pipelines and engines for a single job."""

    job_id: str
    workspace: Path
    limits: ResourceLimits = field(default_factory=ResourceLimits)
    cancel_event: threading.Event = field(default_factory=threading.Event)
    progress: ProgressCallback | None = None
    started_at: datetime = field(default_factory=utc_now)

    def check_cancelled(self) -> None:
        if self.cancel_event.is_set():
            raise JobCancelled(f"Job {self.job_id} was cancelled")

    def emit(self, stage: str, current: int = 0, total: int = 0, message: str = "") -> None:
        self.check_cancelled()
        if self.progress is not None:
            self.progress(
                ProgressEvent(
                    job_id=self.job_id, stage=stage, current=current, total=total, message=message
                )
            )
