"""Regression checks for release-critical capability documentation."""

import json
from pathlib import Path

from localdocforge.cli.agent_brief import USAGE_BY_CAPABILITY_ID
from localdocforge.engines.adapters import OP_PDF_TO_MD
from localdocforge.engines.registry import CAPABILITY_SPECS
from localdocforge.operations.text import WARNING_CODE_ORDER

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


def test_pdf_to_images_llm_preset_is_documented_consistently() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "ldf pdf-to-images scanned.pdf -d vision/ --preset llm" in readme
    assert "per-page JPEG q85 renders with long edge ≤ 1568 px" in readme

    cli = (ROOT / "docs" / "CLI.md").read_text(encoding="utf-8")
    cli_flat = " ".join(cli.split())
    assert "ldf pdf-to-images scan.pdf -d vision/ --preset llm" in cli
    assert "separate render scale for every page" in cli_flat
    assert "Explicit `--dpi` selects fixed-DPI rendering and disables" in cli_flat
    api_row = next(line for line in cli.splitlines() if line.startswith("| pdf-to-images |"))
    assert "`preset` (`llm`)" in api_row

    feature = (ROOT / "docs" / "FEATURE_MATRIX.md").read_text(encoding="utf-8")
    feature_row = next(
        line for line in feature.splitlines() if line.startswith("| PDF → images")
    )
    assert "`llm` preset" in feature_row
    assert "explicit DPI disables the cap" in feature_row

    fidelity = (ROOT / "docs" / "CONVERSION_FIDELITY.md").read_text(
        encoding="utf-8"
    )
    pdf_images = fidelity[fidelity.index("## pdf-to-images") : fidelity.index("## convert-images")]
    assert "JPEG quality 85" in pdf_images
    assert "1568-px long-edge bound" in pdf_images
    assert "image-downscaled" in pdf_images
    assert "effective DPI" in pdf_images

    status = (ROOT / "docs" / "STATUS.md").read_text(encoding="utf-8")
    assert "pdf-to-images LLM preset, S2" in status
    assert "fractional-size page" in status


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
    assert "504 tests as of" in status and "2026-08-08" in status

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


def test_agent_brief_slice_is_documented_consistently() -> None:
    implemented_ids = {spec.id for spec in CAPABILITY_SPECS if spec.implemented}
    assert set(USAGE_BY_CAPABILITY_ID) == implemented_ids

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "ldf agent-brief" in readme
    assert "ldf --json agent-brief" in readme

    cli = (ROOT / "docs" / "CLI.md").read_text(encoding="utf-8")
    for phrase in (
        "CAPABILITY_SPECS",
        "EngineRegistry.capabilities()",
        "implemented=False",
        "warnings[]",
        "security_warnings[]",
        "fidelity_warnings[]",
        "verify` -> `fallback` -> `review",
        "docs/AGENT_FEEDBACK.md",
        "stdout-only",
        "no local API job endpoint",
        "standalone wheel or direct VCS install",
    ):
        assert phrase in cli, phrase

    feature = (ROOT / "docs" / "FEATURE_MATRIX.md").read_text(encoding="utf-8")
    assert "| Registry-derived agent brief | ✅ |" in feature
    assert "ldf doctor --json" not in feature
    assert "every implemented command appears with live availability" in feature
    assert "requires a discoverable source checkout" in feature

    status = (ROOT / "docs" / "STATUS.md").read_text(encoding="utf-8")
    assert "registry-derived agent brief, S3" in status
    assert "implemented-but-unavailable state" in status
    assert "504-outcome suites" in status  # dated S3 evidence remains historical

    fidelity = (ROOT / "docs" / "CONVERSION_FIDELITY.md").read_text(
        encoding="utf-8"
    )
    assert "agent-brief (read-only diagnostics)" in fidelity
    assert "opens and converts no document" in fidelity
    assert "introduces no fidelity warning code" in fidelity


def test_pdf_to_md_slice_is_documented_consistently() -> None:
    matching = [spec for spec in CAPABILITY_SPECS if spec.id == "pdf-to-markdown"]
    assert len(matching) == 1
    assert matching[0].implemented is True
    assert matching[0].operation == OP_PDF_TO_MD
    assert all(spec.id != "pdf-to-md" for spec in CAPABILITY_SPECS)
    assert "ldf pdf-to-md" in USAGE_BY_CAPABILITY_ID["pdf-to-markdown"]

    expected_codes = (
        "no-text-layer",
        "headings-inferred",
        "reading-order-uncertain",
        "tables-flattened",
    )
    assert WARNING_CODE_ORDER == expected_codes

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "ldf pdf-to-md input.pdf -o content.md" in readme
    assert "strict UTF-8" in readme
    assert "not a text-fidelity channel" in readme
    assert all(code in readme for code in expected_codes)

    cli = (ROOT / "docs" / "CLI.md").read_text(encoding="utf-8")
    assert "ldf pdf-to-md INPUT.pdf -o OUTPUT" in cli
    assert "[--format md|txt|jsonl] [--no-page-anchors]" in cli
    assert "<!-- ldf:page N -->" in cli
    assert "--- ldf:page N ---" in cli
    api_row = next(line for line in cli.splitlines() if line.startswith("| pdf-to-md |"))
    for field in ("`pages`", "`format`", "`page_anchors`", "`password`"):
        assert field in api_row
    planned = cli[cli.index("## Planned") :]
    assert "`pdf-to-md`" not in planned
    assert "`md-to-pdf`" in planned

    feature = (ROOT / "docs" / "FEATURE_MATRIX.md").read_text(encoding="utf-8")
    row = next(
        line for line in feature.splitlines() if line.startswith("| PDF → Markdown/text/JSONL")
    )
    assert "| ✅ | pdfium text API | Lib, CLI, API |" in row
    assert "No OCR, bidi repair, or silent dehyphenation" in row

    fidelity = (ROOT / "docs" / "CONVERSION_FIDELITY.md").read_text(
        encoding="utf-8"
    )
    text_section = fidelity[fidelity.index("## pdf-to-md") :]
    text_section_flat = " ".join(text_section.split())
    assert all(f"`{code}`" in text_section for code in expected_codes)
    for phrase in (
        "Unicode is normalized to NFC",
        "one selected page at a time",
        "per-page wall time scales with PDFium text-rectangle count",
        "details.coverage.per_page[]",
        '"warning_codes": [...]',
        "pdf-to-images --preset llm",
    ):
        assert phrase in text_section_flat

    getting_started = (ROOT / "docs" / "GETTING_STARTED_WINDOWS.md").read_text(
        encoding="utf-8"
    )
    assert "fast extraction-vs-render decision signals" not in getting_started
    assert "not a cheap probe" in getting_started

    engines = (ROOT / "docs" / "ENGINE_DECISIONS.md").read_text(encoding="utf-8")
    assert "| pypdfium2 (PDFium) | 5.12.1 / PDFium 152.0.7947.0 |" in engines
    assert "Chrome's widely deployed PDF engine" in engines
    assert "Chrome's PDF engine: fast" not in engines
    assert "PyMuPDF and" in engines and "pymupdf4llm are banned" in engines

    architecture = (ROOT / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")
    architecture_flat = " ".join(architecture.split())
    for field in (
        "pages_total",
        "pages_with_text",
        "pages_with_text_layer",
        "char_count",
        "has_text_layer",
        "warning_codes",
    ):
        assert field in architecture
    assert "deterministic, non-mutating candidate validator" in architecture_flat

    threat = (ROOT / "docs" / "THREAT_MODEL.md").read_text(encoding="utf-8")
    threat_flat = " ".join(threat.split())
    assert "either reserved" in threat_flat
    assert "JSONL preserves them as ordinary framed" in threat_flat
    assert "stdout/console encoding is not a fidelity boundary" in threat_flat

    library = (ROOT / "docs" / "LIBRARY_API.md").read_text(encoding="utf-8")
    for symbol in ("PdfToMdOptions", "pdf_to_md", "page_text_stats", "text_coverage"):
        assert symbol in library

    status = (ROOT / "docs" / "STATUS.md").read_text(encoding="utf-8")
    assert "`pdf-to-md` text extraction, S4" in status
    assert "no new runtime dependency" in status
