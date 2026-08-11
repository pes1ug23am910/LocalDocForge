"""Pipeline-backed structural PDF inspection for isolated transports."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from localdocforge.config.settings import Settings
from localdocforge.domain.models import (
    ConversionReport,
    InputArtifact,
    JobContext,
    ProgressCallback,
    ValidationCheck,
    ValidationResult,
)
from localdocforge.engines.adapters import OP_INSPECT, OP_PDF_TO_MD
from localdocforge.engines.registry import default_registry
from localdocforge.jobs.workspace import CollisionPolicy
from localdocforge.operations.organize import inspect_pdf
from localdocforge.pipelines.runner import CandidateOutput, ExecuteResult, run_pipeline


@dataclass
class InspectOptions:
    collision: CollisionPolicy | None = None
    settings: Settings | None = None
    password: str | None = None
    progress: ProgressCallback | None = None


def _validate_inspection_json(path: Path) -> ValidationResult:
    checks: list[ValidationCheck] = []
    exists = path.is_file() and path.stat().st_size > 0
    checks.append(ValidationCheck(name="file-exists", passed=exists, detail=path.name))
    if exists:
        try:
            payload = json.loads(path.read_text(encoding="utf-8", errors="strict"))
            valid = isinstance(payload, dict) and {
                "file",
                "page_count",
                "page_text_stats",
                "text_coverage",
            } <= payload.keys()
        except (OSError, UnicodeError, json.JSONDecodeError):
            valid = False
        checks.append(
            ValidationCheck(
                name="inspection-json-schema",
                passed=valid,
                detail="structural inventory object" if valid else "invalid inspection JSON",
            )
        )
    return ValidationResult.combine(checks)


def inspect_pdf_to_json(
    input_path: Path,
    output: Path,
    *,
    options: InspectOptions | None = None,
) -> ConversionReport:
    """Inspect a PDF inside the standard pipeline and publish validated JSON."""
    options = options or InspectOptions()
    registry = default_registry()
    structural_engine = registry.engine_for(OP_INSPECT)
    text_engine = registry.engine_for(OP_PDF_TO_MD)
    structural_info = structural_engine.probe()
    text_info = text_engine.probe()

    def execute(context: JobContext, artifacts: list[InputArtifact]) -> ExecuteResult:
        context.check_cancelled()
        inventory = inspect_pdf(
            artifacts[0].path,
            password=options.password,
            settings=options.settings,
        )
        candidate = context.workspace / "inspection.json"
        candidate.write_text(
            json.dumps(inventory, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
            errors="strict",
            newline="\n",
        )
        context.check_cancelled()
        return ExecuteResult(
            candidates=[
                CandidateOutput(
                    workspace_path=candidate,
                    destination=output,
                    media_type="application/json",
                    validator=_validate_inspection_json,
                )
            ],
            details={
                "structural_engine": structural_info.name,
                "text_inventory_engine": text_info.name,
            },
            output_page_count=inventory.get("page_count"),
        )

    return run_pipeline(
        operation="inspect",
        input_paths=[input_path],
        execute=execute,
        engine_name=structural_info.name,
        engine_version=structural_info.version,
        collision=options.collision,
        settings=options.settings,
        progress=options.progress,
    )
