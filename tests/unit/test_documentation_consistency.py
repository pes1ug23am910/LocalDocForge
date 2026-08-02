"""Regression checks for release-critical capability documentation."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_implementation_plan_records_shipped_api_and_pending_react_ui() -> None:
    plan = (ROOT / "docs" / "IMPLEMENTATION_PLAN.md").read_text(encoding="utf-8")

    assert "API/UI pending" not in plan
    assert "core + CLI + local API; React UI pending" in plan
    assert "full React browser UI" in plan


def test_optional_engine_heading_does_not_claim_present_typst_is_absent() -> None:
    decisions = (ROOT / "docs" / "ENGINE_DECISIONS.md").read_text(encoding="utf-8")

    assert "Probed but absent on this machine" not in decisions
    assert "Optional executable probes (features gated off; availability varies)" in decisions
    assert "**Present on this machine**" in decisions
    assert "AGPL obligations do not attach" not in decisions
    assert "MPL-2.0 (pikepdf) / Apache-2.0 (qpdf)" in decisions


def test_live_docs_describe_worker_and_packaging_state_consistently() -> None:
    paths = [
        ROOT / "README.md",
        ROOT / "docs" / "STATUS.md",
        ROOT / "docs" / "ARCHITECTURE.md",
        ROOT / "docs" / "THREAT_MODEL.md",
        ROOT / "docs" / "CLI.md",
        ROOT / "docs" / "FEATURE_MATRIX.md",
        ROOT / "docs" / "IMPLEMENTATION_PLAN.md",
        ROOT / "docs" / "PACKAGING.md",
    ]
    live_docs = "\n".join(path.read_text(encoding="utf-8") for path in paths)

    stale_claims = (
        "Worker-process isolation is not implemented",
        "API jobs execute synchronously in the request process",
        "There is no background queue",
        "profiles are not implemented",
        "advisory verification was not performed",
    )
    assert all(claim not in live_docs for claim in stale_claims)
    assert "not an OS network sandbox" in live_docs
    assert "before publishing a terminal state" in live_docs
    assert "require `success`" in live_docs
    assert "arbitrary same-user code can create a new session" in live_docs


def test_compression_slice_is_documented_consistently() -> None:
    feature = (ROOT / "docs" / "FEATURE_MATRIX.md").read_text(encoding="utf-8")
    assert "| Compress PDF | ✅ lossless preset |" in feature
    assert "| Compress PDF | ❌" not in feature

    cli = (ROOT / "docs" / "CLI.md").read_text(encoding="utf-8")
    assert "ldf compress" in cli
    assert "`compress`, `repair`" not in cli  # removed from the planned list

    status = (ROOT / "docs" / "STATUS.md").read_text(encoding="utf-8")
    assert "No Phase 2 document feature was started" not in status
    assert "lossless compression slice" in status

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "ldf compress" in readme

    fidelity = (ROOT / "docs" / "CONVERSION_FIDELITY.md").read_text(encoding="utf-8")
    assert "compress-no-reduction" in fidelity
    assert "resource-cleanup-skipped" in fidelity

    threat = (ROOT / "docs" / "THREAT_MODEL.md").read_text(encoding="utf-8")
    assert "Compression, repair, OCR" not in threat  # stale unavailable-list form
    assert "Lossy compression presets, repair" in threat
