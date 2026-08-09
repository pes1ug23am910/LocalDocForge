"""The pipeline lifecycle every operation runs through.

Order of events (see docs/ARCHITECTURE.md):
 1. validate parameters and actual input signatures (magic bytes);
 2. enforce configured resource limits;
 3. create an isolated per-job workspace;
 4. run the operation, writing candidates only inside the workspace;
 5. reopen and render-validate every generated PDF;
 6. atomically publish validated candidates to their destinations;
 7. produce a ConversionReport (JSON + human-readable);
 8. clean the workspace on success, failure, and cancellation.

Source files are never written to, and nothing appears at a destination
unless it passed validation.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from localdocforge.config.settings import Settings, get_settings
from localdocforge.domain.models import (
    ArtifactKind,
    ConversionReport,
    FidelityWarning,
    InputArtifact,
    JobCancelled,
    JobContext,
    OutputArtifact,
    ProgressCallback,
    ReportStatus,
    SecurityWarning,
    ValidationCheck,
    ValidationResult,
    WarningSeverity,
    utc_now,
)
from localdocforge.jobs.workspace import (
    CollisionPolicy,
    JobWorkspace,
    OutputCollisionError,
    atomic_publish,
    contained_output_path,
)
from localdocforge.security.paths import PathSecurityError, validate_path_before_access
from localdocforge.security.sniff import ContentTypeError, require_media_type
from localdocforge.validation.pdf_checks import count_pdf_pages, validate_pdf


class PipelineError(Exception):
    """Operation failed; the report carries details. No output was published."""

    def __init__(self, message: str, report: ConversionReport | None = None) -> None:
        super().__init__(message)
        self.report = report


@dataclass
class CandidateOutput:
    """A file produced inside the workspace, awaiting validation + publish."""

    workspace_path: Path
    destination: Path
    media_type: str = "application/pdf"
    kind: ArtifactKind = ArtifactKind.PRIMARY
    expected_pages: int | None = None
    #: High-risk candidates get every page rendered; others a sampled render.
    render_all: bool = False
    #: Non-PDF operations may supply semantic validation that runs before
    #: publication. The validator must be deterministic and must not mutate
    #: the candidate.
    validator: Callable[[Path], ValidationResult] | None = None


@dataclass
class ExecuteResult:
    """What an operation hands back to the pipeline."""

    candidates: list[CandidateOutput]
    details: dict[str, Any] = field(default_factory=dict)
    security_warnings: list[SecurityWarning] = field(default_factory=list)
    fidelity_warnings: list[FidelityWarning] = field(default_factory=list)
    output_page_count: int | None = None


ExecuteFn = Callable[[JobContext, list[InputArtifact]], ExecuteResult]

_RENDER_SAMPLE_LIMIT = 20  # pages rendered for routine (non-high-risk) validation


def _paths_alias(first: Path, second: Path) -> bool:
    if first == second:
        return True
    try:
        return first.exists() and second.exists() and os.path.samefile(first, second)
    except OSError:
        return False


def _validate_image_candidate(path: Path) -> ValidationResult:
    """A generated image must actually decode, not merely exist."""
    checks: list[ValidationCheck] = []
    exists = path.is_file() and path.stat().st_size > 0
    checks.append(ValidationCheck(name="file-exists", passed=exists, detail=path.name))
    if exists:
        try:
            from PIL import Image

            with Image.open(path) as image:
                image.load()
                checks.append(
                    ValidationCheck(
                        name="image-decodes",
                        passed=True,
                        detail=f"{image.width}x{image.height} {image.format}",
                    )
                )
        except Exception as exc:
            checks.append(
                ValidationCheck(name="image-decodes", passed=False, detail=type(exc).__name__)
            )
    return ValidationResult.combine(checks)


def _gather_inputs(
    paths: list[Path],
    expected_types: tuple[str, ...],
    settings: Settings,
    max_text_input_bytes: int | None = None,
) -> list[InputArtifact]:
    artifacts: list[InputArtifact] = []
    max_bytes = settings.limits.max_input_bytes
    total_bytes = 0
    for path in paths:
        try:
            path = validate_path_before_access(
                path,
                what="input path",
                require_local=settings.strict_offline,
            )
        except PathSecurityError as exc:
            if "UNC or mapped network-drive" in str(exc):
                raise ContentTypeError(
                    "strict-offline mode forbids network filesystem inputs"
                ) from exc
            raise ContentTypeError(str(exc)) from exc
        size = path.stat().st_size
        total_bytes += size
        if max_bytes is not None and total_bytes > max_bytes:
            raise ContentTypeError(
                f"Inputs total {total_bytes:,} bytes, over the configured per-job input "
                f"limit of {max_bytes:,} bytes"
            )
        media = require_media_type(
            path,
            *expected_types,
            max_text_bytes=max_text_input_bytes,
        )
        artifact = InputArtifact(path=path.resolve(), media_type=media, size_bytes=size)
        if media == "application/pdf":
            # Encrypted or damaged inputs keep page_count=None here; the
            # operation itself decides how to handle them.
            with contextlib.suppress(Exception):
                artifact = artifact.model_copy(update={"page_count": count_pdf_pages(path)})
        artifacts.append(artifact)
    return artifacts


def _check_page_limit(artifacts: list[InputArtifact], settings: Settings) -> None:
    limit = settings.limits.max_pages
    if limit is None:
        return
    total = sum(artifact.page_count or 0 for artifact in artifacts)
    if total > limit:
        raise PipelineError(
            f"Inputs total {total} pages, over the configured limit of {limit}. "
            f"Raise LDF_LIMITS__MAX_PAGES if this is intentional."
        )


def run_pipeline(
    *,
    operation: str,
    input_paths: list[Path],
    execute: ExecuteFn,
    engine_name: str,
    engine_version: str | None,
    input_types: tuple[str, ...] = ("application/pdf",),
    collision: CollisionPolicy | None = None,
    settings: Settings | None = None,
    progress: ProgressCallback | None = None,
    fallback_engine: str | None = None,
    details: dict[str, Any] | None = None,
    max_text_input_bytes: int | None = None,
) -> ConversionReport:
    """Run one operation end to end and return its report.

    Raises PipelineError (with the failed report attached) if anything —
    input checking, execution, validation, or publishing — fails. On success
    the report's outputs list the final published artifacts.
    """
    settings = settings or get_settings()
    collision = collision or settings.collision
    job_id = uuid.uuid4().hex
    started = time.monotonic()

    report_details = dict(details or {})
    # This is authoritative runtime state, not caller-supplied descriptive data.
    report_details["strict_offline"] = settings.strict_offline
    report = ConversionReport(
        operation=operation,
        status=ReportStatus.FAILED,
        job_id=job_id,
        engine=engine_name,
        engine_version=engine_version,
        fallback_engine=fallback_engine,
        details=report_details,
    )

    try:
        inputs = _gather_inputs(
            input_paths,
            input_types,
            settings,
            max_text_input_bytes=max_text_input_bytes,
        )
    except (ContentTypeError, FileNotFoundError, OSError) as exc:
        report.errors.append(str(exc))
        raise PipelineError(str(exc), report) from exc

    report.inputs = inputs
    report.input_bytes = sum(artifact.size_bytes for artifact in inputs)
    page_counts = [a.page_count for a in inputs if a.page_count is not None]
    report.input_page_count = sum(page_counts) if page_counts else None
    _check_page_limit(inputs, settings)

    workspace = JobWorkspace(job_id, root=settings.jobs_root)
    context = JobContext(
        job_id=job_id,
        workspace=workspace.path,
        limits=settings.limits,
        progress=progress,
    )

    try:
        context.emit("execute", message=f"running {operation}")
        result = execute(context, inputs)
        report.details.update(result.details)
        report.security_warnings.extend(result.security_warnings)
        report.fidelity_warnings.extend(result.fidelity_warnings)
        report.output_page_count = result.output_page_count

        if not result.candidates:
            raise PipelineError(f"{operation} produced no output", report)

        # Resolve candidates once, before validation, and enforce the aggregate
        # output bound before spending time rendering or publishing anything.
        output_limit = settings.limits.max_output_bytes
        candidate_bytes = 0
        destinations: list[Path] = []
        input_paths = [artifact.path.resolve(strict=False) for artifact in inputs]
        for candidate in result.candidates:
            candidate.workspace_path = workspace.contain(candidate.workspace_path)
            try:
                candidate.destination = validate_path_before_access(
                    candidate.destination,
                    what="output path",
                    require_local=settings.strict_offline,
                )
                candidate.destination = contained_output_path(
                    candidate.destination, settings.allowed_output_roots
                )
            except PathSecurityError as exc:
                if "UNC or mapped network-drive" in str(exc):
                    raise PipelineError(
                        "strict-offline mode forbids network filesystem outputs", report
                    ) from exc
                raise PipelineError(str(exc), report) from exc
            if any(_paths_alias(candidate.destination, input_path) for input_path in input_paths):
                raise PipelineError(
                    "Output path aliases an input file; in-place source modification is forbidden",
                    report,
                )
            if any(_paths_alias(candidate.destination, prior) for prior in destinations):
                raise PipelineError("Multiple outputs resolve to the same destination", report)
            destinations.append(candidate.destination)
            if candidate.workspace_path.is_file():
                candidate_bytes += candidate.workspace_path.stat().st_size
            if output_limit is not None and candidate_bytes > output_limit:
                raise PipelineError(
                    f"Generated outputs total {candidate_bytes:,} bytes, over the configured "
                    f"per-job output limit of {output_limit:,} bytes; nothing was published",
                    report,
                )

        if collision is CollisionPolicy.FAIL:
            for destination in destinations:
                if destination.exists():
                    raise OutputCollisionError(
                        f"Output already exists: {destination}. "
                        "Choose --collision rename or --collision overwrite."
                    )

        # Validate every candidate before anything is published.
        context.emit("validate", total=len(result.candidates))
        all_checks: list[ValidationCheck] = []
        for index, candidate in enumerate(result.candidates):
            if candidate.validator is not None:
                validation = candidate.validator(candidate.workspace_path)
            elif candidate.media_type == "application/pdf":
                validation = validate_pdf(
                    candidate.workspace_path,
                    expected_pages=candidate.expected_pages,
                    render_pages=True,
                    render_sample_limit=None if candidate.render_all else _RENDER_SAMPLE_LIMIT,
                )
            elif candidate.media_type.startswith("image/"):
                validation = _validate_image_candidate(candidate.workspace_path)
            else:
                exists = (
                    candidate.workspace_path.is_file()
                    and candidate.workspace_path.stat().st_size > 0
                )
                validation = ValidationResult(
                    passed=exists,
                    checks=[
                        ValidationCheck(
                            name="file-exists",
                            passed=exists,
                            detail=candidate.workspace_path.name,
                        )
                    ],
                )
            prefix = candidate.destination.name
            all_checks.extend(
                check.model_copy(update={"name": f"{prefix}:{check.name}"})
                for check in validation.checks
            )
            if not validation.passed:
                report.validation = ValidationResult.combine(all_checks)
                raise PipelineError(
                    f"Validation failed for {candidate.destination.name}; nothing was written "
                    f"to the destination",
                    report,
                )
            context.emit("validate", current=index + 1, total=len(result.candidates))

        report.validation = ValidationResult.combine(all_checks)

        # Publish all candidates atomically (validation passed for every one).
        context.emit("publish", total=len(result.candidates))
        published: list[OutputArtifact] = []
        published_paths: list[Path] = []
        overwrite_backups: dict[Path, Path] = {}
        if collision is CollisionPolicy.OVERWRITE:
            backup_dir = workspace.subdir("publish-backups")
            for index, destination in enumerate(destinations):
                if destination.is_file():
                    # Do not mirror an attacker-controlled destination name in
                    # the private backup component; it could exceed filesystem
                    # filename limits even when the destination itself exists.
                    backup = backup_dir / f"{index:04d}.bak"
                    shutil.copy2(destination, backup)
                    overwrite_backups[destination] = backup
        try:
            for candidate in result.candidates:
                final_path = atomic_publish(
                    candidate.workspace_path, candidate.destination, collision=collision
                )
                published_paths.append(final_path)
                pages: int | None = None
                if candidate.media_type == "application/pdf":
                    pages = candidate.expected_pages or count_pdf_pages(final_path)
                published.append(
                    OutputArtifact(
                        path=final_path,
                        media_type=candidate.media_type,
                        size_bytes=final_path.stat().st_size,
                        kind=candidate.kind,
                        page_count=pages,
                    )
                )
        except BaseException:
            rollback_failed = False
            for final_path in reversed(published_paths):
                try:
                    backup = overwrite_backups.get(final_path)
                    if backup is not None:
                        atomic_publish(backup, final_path, collision=CollisionPolicy.OVERWRITE)
                    else:
                        final_path.unlink(missing_ok=True)
                except OSError:
                    rollback_failed = True
            if rollback_failed:
                report.security_warnings.append(
                    SecurityWarning(
                        code="publication-rollback-incomplete",
                        message=(
                            "A multi-output publication failed and at least one destination "
                            "could not be restored automatically."
                        ),
                        severity=WarningSeverity.CRITICAL,
                    )
                )
            raise

        report.outputs = published
        report.output_bytes = sum(artifact.size_bytes for artifact in published)
        report.status = ReportStatus.SUCCESS
        return report
    except JobCancelled as exc:
        report.status = ReportStatus.CANCELLED
        report.errors.append(str(exc))
        raise PipelineError(str(exc), report) from exc
    except PipelineError as exc:
        if exc.report is None:
            exc.report = report
        report.errors.append(str(exc))
        raise
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        report.errors.append(message)
        raise PipelineError(message, report) from exc
    finally:
        report.elapsed_seconds = round(time.monotonic() - started, 3)
        report.finished_at = utc_now()
        if not workspace.cleanup():
            report.security_warnings.append(
                SecurityWarning(
                    code="workspace-cleanup-incomplete",
                    message=(
                        "Private job workspace cleanup was incomplete; close files that may be "
                        "open and remove the stale workspace on the next startup."
                    ),
                    severity=WarningSeverity.CRITICAL,
                )
            )
