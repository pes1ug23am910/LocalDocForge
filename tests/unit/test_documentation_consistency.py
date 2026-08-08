"""Regression checks for release-critical capability documentation."""

import json
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


def test_heic_convert_images_slice_is_documented_consistently() -> None:
    feature = (ROOT / "docs" / "FEATURE_MATRIX.md").read_text(encoding="utf-8")
    assert "| Convert images (HEIC/JPG/PNG/TIFF/BMP/WebP → PNG/JPEG/WebP/TIFF) | ✅ |" in feature
    assert "HEIC/auto-crop/deskew unavailable" not in feature  # HEIC input shipped
    assert "HEIF is decode-only (no HEIC output)" in feature

    cli = (ROOT / "docs" / "CLI.md").read_text(encoding="utf-8")
    assert "ldf convert-images" in cli
    assert "`convert-images`" in cli  # listed as an API job operation
    assert "keep_metadata" in cli
    assert "location-metadata-retained" in cli

    fidelity = (ROOT / "docs" / "CONVERSION_FIDELITY.md").read_text(encoding="utf-8")
    for code in (
        "image-reencoded",
        "image-downscaled",
        "alpha-flattened",
        "metadata-stripped",
        "xmp-metadata-dropped",
        "color-profile-converted",
        "color-profile-retained",
        "location-metadata-retained",
    ):
        assert code in fidelity, code

    status = (ROOT / "docs" / "STATUS.md").read_text(encoding="utf-8")
    assert "convert-images slice" in status
    assert "libheif 1.23.0" in status  # the advisory finding is not hidden

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "ldf convert-images" in readme

    engines = (ROOT / "docs" / "ENGINE_DECISIONS.md").read_text(encoding="utf-8")
    assert "pi-heif" in engines
    assert "decode-only" in engines


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


def test_password_stdin_slice_is_documented_consistently() -> None:
    cli = (ROOT / "docs" / "CLI.md").read_text(encoding="utf-8")
    cli_flat = " ".join(cli.split())
    assert "[--password-stdin]" in cli
    assert "--password-stdin" in cli
    assert "LDF_PASSWORD" in cli
    assert "LDF_PASSWORD=" in cli
    assert "supplies the empty password" in cli_flat
    assert "One password is tried for every encrypted input" in cli_flat
    precedence_start = cli_flat.index("Encrypted CLI-input precedence")
    precedence = cli_flat[precedence_start : precedence_start + 500]
    assert precedence.index("--password-stdin") < precedence.index("LDF_PASSWORD")
    assert precedence.index("LDF_PASSWORD") < precedence.index("hidden interactive prompt")

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "--password-stdin" in readme
    assert "LDF_PASSWORD" in readme
    assert "test suite (464 tests)" in readme

    threat = (ROOT / "docs" / "THREAT_MODEL.md").read_text(encoding="utf-8")
    assert "strict-UTF-8 line" in threat
    assert "environment-secret exposure" in threat
    assert "empty `LDF_PASSWORD` suppresses prompt fallback" in threat

    feature = (ROOT / "docs" / "FEATURE_MATRIX.md").read_text(encoding="utf-8")
    assert "test_cli.py::TestPasswordSources" in feature
    assert "differing passwords fail clearly" in feature

    fidelity = (ROOT / "docs" / "CONVERSION_FIDELITY.md").read_text(encoding="utf-8")
    assert "CLI credential source" in fidelity
    assert "argv value" in fidelity

    technical = (ROOT / "docs" / "TECHNICAL_REFERENCE.md").read_text(encoding="utf-8")
    getting_started = (ROOT / "docs" / "GETTING_STARTED_WINDOWS.md").read_text(
        encoding="utf-8"
    )
    architecture = (ROOT / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")
    active_docs = "\n".join((cli, readme, threat, feature, technical, getting_started))
    assert "passwords prompt-only" not in active_docs
    assert "never in argv/env/reports" not in active_docs
    architecture_flat = " ".join(architecture.split()).lower()
    assert (
        "missing credentials in a non-interactive invocation map to usage exit 2"
        in architecture_flat
    )

    status = (ROOT / "docs" / "STATUS.md").read_text(encoding="utf-8")
    assert "non-interactive PDF passwords, S1" in status
    assert "464 outcomes: 462 passed and two expected" in status

    packaging = (ROOT / "docs" / "PACKAGING.md").read_text(encoding="utf-8")
    assert "2026-08-08 S1 manifest identity" in packaging
    assert "remain honest historical" in packaging
    manifest = json.loads(
        (ROOT / "packaging" / "release-artifact-manifest.json").read_text(
            encoding="utf-8"
        )
    )
    windows_identity = manifest["platforms"]["Windows-AMD64"]
    assert windows_identity["source_tree_sha256"] in packaging
    for artifact in windows_identity["artifacts"].values():
        assert artifact["sha256"] in packaging
