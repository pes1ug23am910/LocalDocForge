"""Release licensing artifacts must be deterministic, current, and honest."""

from __future__ import annotations

import importlib.util
import json
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GENERATOR_PATH = ROOT / "scripts" / "generate_release_artifacts.py"
SBOM_PATH = ROOT / "docs" / "SBOM.cdx.json"


def _load_generator():
    spec = importlib.util.spec_from_file_location("ldf_release_artifacts", GENERATOR_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _locked_versions() -> dict[str, str]:
    locked: dict[str, str] = {}
    for line in (ROOT / "requirements-lock.txt").read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#"):
            name, version = line.split("==", 1)
            locked[re.sub(r"[-_.]+", "-", name).lower()] = version
    return locked


def test_generator_is_deterministic_and_artifacts_are_current():
    generator = _load_generator()
    first = generator.render_artifacts(ROOT)
    second = generator.render_artifacts(ROOT)
    assert first == second
    for path, expected in first.items():
        assert path.read_text(encoding="utf-8") == expected
    assert generator.main(["--check"]) == 0


def test_cyclonedx_16_shape_and_locked_components():
    sbom = json.loads(SBOM_PATH.read_text(encoding="utf-8"))
    assert sbom["$schema"] == "http://cyclonedx.org/schema/bom-1.6.schema.json"
    assert sbom["bomFormat"] == "CycloneDX"
    assert sbom["specVersion"] == "1.6"
    assert sbom["version"] == 1
    assert sbom["metadata"]["component"]["name"] == "localdocforge"
    assert sbom["metadata"]["tools"]["components"]

    components = sbom["components"]
    references = {component["bom-ref"] for component in components}
    assert len(references) == len(components)
    by_purl = {component.get("purl"): component for component in components}
    for name, version in _locked_versions().items():
        purl = f"pkg:pypi/{name}@{version}"
        component = by_purl[purl]
        assert component["version"] == version
        assert component["licenses"]
        properties = {item["name"]: item["value"] for item in component["properties"]}
        assert properties["localdocforge:licenseEvidence"]
        assert "no artifact hash" in properties["localdocforge:lockEvidence"]
        assert properties["localdocforge:installedVersion"] == version

    native_names = {component["name"] for component in components}
    assert {"qpdf", "PDFium", "Pillow codec bundle", "libjpeg-turbo"} <= native_names
    native_by_name = {component["name"]: component for component in components}
    assert native_by_name["qpdf"]["version"] != "unknown"
    assert native_by_name["PDFium"]["version"] != "unknown"

    valid_dependency_refs = references | {sbom["metadata"]["component"]["bom-ref"]}
    for relationship in sbom["dependencies"]:
        assert relationship["ref"] in valid_dependency_refs
        assert set(relationship["dependsOn"]) <= references


def test_notice_sections_and_required_uncertainty_disclosures():
    notices = (ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    for heading in (
        "## Runtime Python distributions",
        "## Development and test distributions",
        "## Lock-only distributions",
        "## Build requirements",
        "## Native components bundled by installed wheels",
        "## Optional external engines not bundled by LocalDocForge",
    ):
        assert heading in notices
    assert "No vulnerability or security-advisory lookup was performed" in notices
    assert "Lite, Standard, and Full installation profiles are not implemented" in notices
    assert "Ghostscript" in notices and "commercial" in notices


def test_project_license_metadata_and_standard_mit_text():
    with (ROOT / "pyproject.toml").open("rb") as stream:
        project = tomllib.load(stream)["project"]
    assert project["license"] == "MIT"
    assert project["license-files"] == ["LICENSE"]

    license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert license_text.startswith("MIT License\n")
    assert "Permission is hereby granted, free of charge" in license_text
    assert 'THE SOFTWARE IS PROVIDED "AS IS"' in license_text
