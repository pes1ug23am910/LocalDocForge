"""Machine-readable fidelity-contract semantics."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from localdocforge.domain.models import (
    ConversionReport,
    FidelityBasis,
    FidelityCoverage,
    FidelityImpact,
    FidelityStatus,
    FidelityWarning,
    OutputArtifact,
    ReportStatus,
    derive_fidelity_status,
)


def _warning(
    impact: FidelityImpact,
    *,
    basis: FidelityBasis = FidelityBasis.DECLARED,
) -> FidelityWarning:
    return FidelityWarning(
        code=f"test-{impact.value}",
        message="Synthetic fidelity observation",
        basis=basis,
        impact=impact,
    )


@pytest.mark.parametrize(
    ("coverage", "warnings", "expected"),
    [
        (FidelityCoverage.NONE, [], FidelityStatus.UNASSESSED),
        (FidelityCoverage.PARTIAL, [], FidelityStatus.UNASSESSED),
        (FidelityCoverage.COMPLETE, [], FidelityStatus.NO_KNOWN_LOSS),
        (
            FidelityCoverage.COMPLETE,
            [_warning(FidelityImpact.ADVISORY)],
            FidelityStatus.NO_KNOWN_LOSS,
        ),
        (
            FidelityCoverage.NONE,
            [_warning(FidelityImpact.REVIEW)],
            FidelityStatus.REVIEW_REQUIRED,
        ),
        (
            FidelityCoverage.COMPLETE,
            [_warning(FidelityImpact.REVIEW), _warning(FidelityImpact.KNOWN_LOSS)],
            FidelityStatus.KNOWN_LOSS,
        ),
    ],
)
def test_status_is_derived_from_coverage_and_worst_impact(
    coverage: FidelityCoverage,
    warnings: list[FidelityWarning],
    expected: FidelityStatus,
) -> None:
    assert derive_fidelity_status(coverage, warnings) is expected


def test_heuristic_observation_cannot_claim_known_loss() -> None:
    with pytest.raises(ValidationError, match="heuristic fidelity warnings cannot claim"):
        _warning(FidelityImpact.KNOWN_LOSS, basis=FidelityBasis.HEURISTIC)


def test_legacy_report_payload_defaults_to_unassessed() -> None:
    report = ConversionReport.model_validate(
        {
            "operation": "legacy",
            "status": ReportStatus.SUCCESS,
            "job_id": "legacy-report",
            "fidelity_warnings": [
                {
                    "code": "legacy-warning",
                    "message": "Written before the fidelity contract",
                }
            ],
        }
    )

    assert report.fidelity_coverage is FidelityCoverage.NONE
    assert report.fidelity_status is FidelityStatus.REVIEW_REQUIRED
    assert report.fidelity_warnings[0].basis is FidelityBasis.DECLARED
    assert report.fidelity_warnings[0].impact is FidelityImpact.REVIEW


def test_report_rejects_a_caller_supplied_inconsistent_status() -> None:
    with pytest.raises(ValidationError, match="fidelity_status must be derived"):
        ConversionReport.model_validate(
            {
                "operation": "forged",
                "status": ReportStatus.SUCCESS,
                "job_id": "forged-status",
                "fidelity_coverage": "none",
                "fidelity_status": "no-known-loss",
            }
        )


def test_report_rejects_an_inconsistent_output_artifact_status() -> None:
    with pytest.raises(ValidationError, match="output fidelity_status must match"):
        ConversionReport(
            operation="forged-output",
            status=ReportStatus.SUCCESS,
            job_id="forged-output-status",
            fidelity_coverage=FidelityCoverage.COMPLETE,
            outputs=[
                OutputArtifact(
                    path="output.pdf",
                    media_type="application/pdf",
                    size_bytes=1,
                    fidelity_status=FidelityStatus.KNOWN_LOSS,
                )
            ],
        )


def test_report_rederives_status_when_unpublished_evidence_is_replaced() -> None:
    report = ConversionReport(
        operation="mutable-assessment",
        status=ReportStatus.FAILED,
        job_id="mutable-assessment",
        fidelity_coverage=FidelityCoverage.COMPLETE,
    )

    report.set_fidelity_assessment(
        coverage=FidelityCoverage.COMPLETE,
        warnings=[_warning(FidelityImpact.KNOWN_LOSS)],
    )

    assert report.fidelity_status is FidelityStatus.KNOWN_LOSS
    assert report.model_dump(mode="json")["fidelity_status"] == "known-loss"


def test_finalized_fidelity_evidence_and_output_verdict_cannot_be_mutated() -> None:
    warning = _warning(FidelityImpact.ADVISORY)
    output = OutputArtifact(
        path="output.pdf",
        media_type="application/pdf",
        size_bytes=1,
    )
    report = ConversionReport(
        operation="finalized-assessment",
        status=ReportStatus.SUCCESS,
        job_id="finalized-assessment",
        fidelity_coverage=FidelityCoverage.COMPLETE,
        fidelity_warnings=[warning],
        outputs=[output],
    )

    with pytest.raises(ValidationError, match="Instance is frozen"):
        warning.impact = FidelityImpact.KNOWN_LOSS
    with pytest.raises(ValidationError, match="Instance is frozen"):
        warning.message = "Changed after publication"
    with pytest.raises(ValidationError, match="Instance is frozen"):
        report.outputs[0].fidelity_status = FidelityStatus.KNOWN_LOSS
    with pytest.raises(ValidationError, match="Instance is frozen"):
        report.outputs[0].path = "changed.pdf"
    with pytest.raises(ValidationError, match="Field is frozen"):
        report.fidelity_warnings = (_warning(FidelityImpact.KNOWN_LOSS),)
    with pytest.raises(ValueError, match="immutable after outputs are finalized"):
        report.set_fidelity_assessment(
            coverage=FidelityCoverage.COMPLETE,
            warnings=[_warning(FidelityImpact.KNOWN_LOSS)],
        )

    assert report.fidelity_status is FidelityStatus.NO_KNOWN_LOSS
    assert report.outputs[0].fidelity_status is FidelityStatus.NO_KNOWN_LOSS
    assert output.fidelity_status is FidelityStatus.UNASSESSED
    assert report.model_dump(mode="json")["fidelity_status"] == "no-known-loss"


def test_report_model_copy_revalidates_fidelity_updates() -> None:
    report = ConversionReport(
        operation="copy-invariant",
        status=ReportStatus.SUCCESS,
        job_id="copy-invariant",
        fidelity_coverage=FidelityCoverage.COMPLETE,
    )

    with pytest.raises(ValidationError, match="fidelity_status must be derived"):
        report.model_copy(
            update={"fidelity_warnings": (_warning(FidelityImpact.KNOWN_LOSS),)}
        )

    copied = report.model_copy(deep=True)
    assert copied is not report
    assert copied.fidelity_status is FidelityStatus.NO_KNOWN_LOSS
