"""Typed domain models shared by every layer of LocalDocForge."""

from localdocforge.domain.models import (
    Capability,
    ConversionReport,
    EngineInfo,
    FidelityWarning,
    InputArtifact,
    JobContext,
    OperationSpec,
    OutputArtifact,
    ProgressEvent,
    ReportStatus,
    ResourceLimits,
    SecurityWarning,
    ValidationCheck,
    ValidationResult,
    WarningSeverity,
)
from localdocforge.domain.pages import PageRange, PageRangeError

__all__ = [
    "Capability",
    "ConversionReport",
    "EngineInfo",
    "FidelityWarning",
    "InputArtifact",
    "JobContext",
    "OperationSpec",
    "OutputArtifact",
    "PageRange",
    "PageRangeError",
    "ProgressEvent",
    "ReportStatus",
    "ResourceLimits",
    "SecurityWarning",
    "ValidationCheck",
    "ValidationResult",
    "WarningSeverity",
]
