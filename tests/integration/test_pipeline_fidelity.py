"""End-to-end publication behavior for the fidelity gate."""

from __future__ import annotations

from pathlib import Path

import pytest

from localdocforge.config.settings import Settings
from localdocforge.domain.models import (
    FidelityBasis,
    FidelityCoverage,
    FidelityImpact,
    FidelityStatus,
    FidelityWarning,
)
from localdocforge.jobs.workspace import CollisionPolicy, OutputCollisionError
from localdocforge.pipelines.runner import (
    CandidateOutput,
    ExecuteResult,
    PipelineError,
    StrictFidelityRefused,
    run_pipeline,
)
from localdocforge.security.paths import PathSecurityError


def _candidate(context, destination: Path, name: str) -> CandidateOutput:
    staged = context.workspace / name
    staged.write_text("validated output", encoding="utf-8")
    return CandidateOutput(
        workspace_path=staged,
        destination=destination,
        media_type="text/plain",
    )


def test_strict_fidelity_allows_complete_assessment_with_only_advisories(
    fixtures_dir: Path,
    tmp_path: Path,
) -> None:
    output = tmp_path / "allowed.txt"

    def execute(context, _inputs):
        return ExecuteResult(
            candidates=[_candidate(context, output, "allowed.txt")],
            fidelity_coverage=FidelityCoverage.COMPLETE,
            details={"strict_offline": True, "strict_fidelity": False},
            fidelity_warnings=[
                FidelityWarning(
                    code="synthetic-advisory",
                    message="This observation does not require review",
                    basis=FidelityBasis.STRUCTURAL,
                    impact=FidelityImpact.ADVISORY,
                )
            ],
        )

    report = run_pipeline(
        operation="synthetic",
        input_paths=[fixtures_dir / "simple-3page.pdf"],
        execute=execute,
        engine_name="test",
        engine_version="1",
        settings=Settings(strict_fidelity=True, jobs_root=tmp_path / "jobs"),
    )

    assert output.read_text(encoding="utf-8") == "validated output"
    assert report.fidelity_status is FidelityStatus.NO_KNOWN_LOSS
    assert report.outputs[0].fidelity_status is FidelityStatus.NO_KNOWN_LOSS
    assert report.details["strict_offline"] is False
    assert report.details["strict_fidelity"] is True


def test_strict_fidelity_refuses_unassessed_multi_output_as_one_set(
    fixtures_dir: Path,
    tmp_path: Path,
) -> None:
    outputs = [tmp_path / "one.txt", tmp_path / "two.txt"]

    def execute(context, _inputs):
        return ExecuteResult(
            candidates=[
                _candidate(context, outputs[0], "one.txt"),
                _candidate(context, outputs[1], "two.txt"),
            ],
            fidelity_coverage=FidelityCoverage.PARTIAL,
        )

    with pytest.raises(StrictFidelityRefused, match="found unassessed") as caught:
        run_pipeline(
            operation="synthetic",
            input_paths=[fixtures_dir / "simple-3page.pdf"],
            execute=execute,
            engine_name="test",
            engine_version="1",
            settings=Settings(strict_fidelity=True, jobs_root=tmp_path / "jobs"),
        )

    assert caught.value.report is not None
    assert caught.value.report.fidelity_status is FidelityStatus.UNASSESSED
    assert caught.value.report.validation is None
    assert not any(path.exists() for path in outputs)


@pytest.mark.parametrize(
    ("impact", "expected_status"),
    [
        (FidelityImpact.REVIEW, FidelityStatus.REVIEW_REQUIRED),
        (FidelityImpact.KNOWN_LOSS, FidelityStatus.KNOWN_LOSS),
    ],
)
def test_strict_fidelity_refuses_each_blocking_warning_impact(
    fixtures_dir: Path,
    tmp_path: Path,
    impact: FidelityImpact,
    expected_status: FidelityStatus,
) -> None:
    output = tmp_path / f"{impact.value}.txt"

    def execute(context, _inputs):
        return ExecuteResult(
            candidates=[_candidate(context, output, "candidate.txt")],
            fidelity_coverage=FidelityCoverage.COMPLETE,
            fidelity_warnings=[
                FidelityWarning(
                    code=f"synthetic-{impact.value}",
                    message="Synthetic blocking observation",
                    basis=(
                        FidelityBasis.HEURISTIC
                        if impact is FidelityImpact.REVIEW
                        else FidelityBasis.STRUCTURAL
                    ),
                    impact=impact,
                )
            ],
        )

    with pytest.raises(StrictFidelityRefused) as caught:
        run_pipeline(
            operation="synthetic",
            input_paths=[fixtures_dir / "simple-3page.pdf"],
            execute=execute,
            engine_name="test",
            engine_version="1",
            settings=Settings(strict_fidelity=True, jobs_root=tmp_path / "jobs"),
        )

    assert caught.value.report is not None
    assert caught.value.report.fidelity_status is expected_status
    assert not output.exists()


def test_candidate_escape_is_rejected_before_strict_fidelity_policy(
    fixtures_dir: Path,
    tmp_path: Path,
) -> None:
    escaped = tmp_path / "escaped.txt"

    def execute(_context, _inputs):
        escaped.write_text("outside workspace", encoding="utf-8")
        return ExecuteResult(
            candidates=[
                CandidateOutput(
                    workspace_path=escaped,
                    destination=tmp_path / "destination.txt",
                    media_type="text/plain",
                )
            ],
            fidelity_coverage=FidelityCoverage.PARTIAL,
        )

    with pytest.raises(PipelineError, match="escapes its allowed directory") as caught:
        run_pipeline(
            operation="synthetic",
            input_paths=[fixtures_dir / "simple-3page.pdf"],
            execute=execute,
            engine_name="test",
            engine_version="1",
            settings=Settings(strict_fidelity=True, jobs_root=tmp_path / "jobs"),
        )
    assert isinstance(caught.value.__cause__, PathSecurityError)


def test_input_alias_is_rejected_before_strict_fidelity_policy(
    fixtures_dir: Path,
    tmp_path: Path,
) -> None:
    source = fixtures_dir / "simple-3page.pdf"
    original = source.read_bytes()

    def execute(context, _inputs):
        return ExecuteResult(
            candidates=[_candidate(context, source, "candidate.txt")],
            fidelity_coverage=FidelityCoverage.PARTIAL,
        )

    with pytest.raises(PipelineError, match="aliases an input file") as caught:
        run_pipeline(
            operation="synthetic",
            input_paths=[source],
            execute=execute,
            engine_name="test",
            engine_version="1",
            settings=Settings(strict_fidelity=True, jobs_root=tmp_path / "jobs"),
        )

    assert not isinstance(caught.value, StrictFidelityRefused)
    assert source.read_bytes() == original


def test_output_limit_is_enforced_before_strict_fidelity_policy(
    fixtures_dir: Path,
    tmp_path: Path,
) -> None:
    output = tmp_path / "limited.txt"

    def execute(context, _inputs):
        candidate = context.workspace / "too-large.txt"
        candidate.write_text("12345", encoding="utf-8")
        return ExecuteResult(
            candidates=[
                CandidateOutput(
                    workspace_path=candidate,
                    destination=output,
                    media_type="text/plain",
                )
            ],
            fidelity_coverage=FidelityCoverage.PARTIAL,
        )

    settings = Settings(strict_fidelity=True, jobs_root=tmp_path / "jobs")
    settings.limits.max_output_bytes = 4
    with pytest.raises(PipelineError, match="Generated outputs total") as caught:
        run_pipeline(
            operation="synthetic",
            input_paths=[fixtures_dir / "simple-3page.pdf"],
            execute=execute,
            engine_name="test",
            engine_version="1",
            settings=settings,
        )

    assert not isinstance(caught.value, StrictFidelityRefused)
    assert not output.exists()


def test_collision_is_rejected_before_strict_fidelity_without_overwrite(
    fixtures_dir: Path,
    tmp_path: Path,
) -> None:
    output = tmp_path / "existing.txt"
    output.write_text("original", encoding="utf-8")

    def execute(context, _inputs):
        return ExecuteResult(
            candidates=[_candidate(context, output, "replacement.txt")],
            fidelity_coverage=FidelityCoverage.PARTIAL,
        )

    with pytest.raises(PipelineError, match="Output already exists") as caught:
        run_pipeline(
            operation="synthetic",
            input_paths=[fixtures_dir / "simple-3page.pdf"],
            execute=execute,
            engine_name="test",
            engine_version="1",
            settings=Settings(strict_fidelity=True, jobs_root=tmp_path / "jobs"),
        )

    assert not isinstance(caught.value, StrictFidelityRefused)
    assert isinstance(caught.value.__cause__, OutputCollisionError)
    assert output.read_text(encoding="utf-8") == "original"


def test_strict_refusal_leaves_overwrite_target_byte_identical(
    fixtures_dir: Path,
    tmp_path: Path,
) -> None:
    output = tmp_path / "existing-overwrite.txt"
    original = b"original bytes\x00\xff"
    output.write_bytes(original)

    def execute(context, _inputs):
        return ExecuteResult(
            candidates=[_candidate(context, output, "replacement.txt")],
            fidelity_coverage=FidelityCoverage.PARTIAL,
        )

    with pytest.raises(StrictFidelityRefused):
        run_pipeline(
            operation="synthetic",
            input_paths=[fixtures_dir / "simple-3page.pdf"],
            execute=execute,
            engine_name="test",
            engine_version="1",
            collision=CollisionPolicy.OVERWRITE,
            settings=Settings(strict_fidelity=True, jobs_root=tmp_path / "jobs"),
        )

    assert output.read_bytes() == original


@pytest.mark.parametrize("collision", [CollisionPolicy.RENAME, CollisionPolicy.OVERWRITE])
def test_directory_destination_is_rejected_before_strict_fidelity_policy(
    fixtures_dir: Path,
    tmp_path: Path,
    collision: CollisionPolicy,
) -> None:
    destination = tmp_path / "existing-directory"
    destination.mkdir()

    def execute(context, _inputs):
        return ExecuteResult(
            candidates=[_candidate(context, destination, "candidate.txt")],
            fidelity_coverage=FidelityCoverage.PARTIAL,
        )

    with pytest.raises(PipelineError, match="Output destination is a directory") as caught:
        run_pipeline(
            operation="synthetic",
            input_paths=[fixtures_dir / "simple-3page.pdf"],
            execute=execute,
            engine_name="test",
            engine_version="1",
            collision=collision,
            settings=Settings(strict_fidelity=True, jobs_root=tmp_path / "jobs"),
        )

    assert not isinstance(caught.value, StrictFidelityRefused)
    assert destination.is_dir()
