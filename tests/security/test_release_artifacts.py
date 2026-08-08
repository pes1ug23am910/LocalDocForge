"""Release licensing artifacts must be deterministic, current, and honest."""

from __future__ import annotations

import importlib.util
import json
import sys
import tomllib
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GENERATOR_PATH = ROOT / "scripts" / "generate_release_artifacts.py"
REPORT_PATH = ROOT / "docs" / "ADVISORY_REPORT.json"
PROFILES = ("lite", "standard", "full")
PYTHON_COUNTS = {"lite": 21, "standard": 29, "full": 30}
NATIVE_COUNT = 18
UNVERSIONED_COUNT = 18
REVIEW_DATES = {"2026-07-19", "2026-08-08"}
BASE_DIRECT = {
    "pkg:pypi/pi-heif@1.4.0",
    "pkg:pypi/pikepdf@10.10.0",
    "pkg:pypi/pillow@12.3.0",
    "pkg:pypi/pydantic@2.13.4",
    "pkg:pypi/pydantic-settings@2.14.2",
    "pkg:pypi/pypdfium2@5.12.1",
    "pkg:pypi/typer@0.27.0",
}
STANDARD_DIRECT = BASE_DIRECT | {
    "pkg:pypi/fastapi@0.139.2",
    "pkg:pypi/python-multipart@0.0.32",
    "pkg:pypi/uvicorn@0.51.0",
}
DIRECT_REFS = {
    "lite": BASE_DIRECT,
    "standard": STANDARD_DIRECT,
    "full": STANDARD_DIRECT | {"pkg:pypi/pypdf@6.14.2"},
}


def _load_generator():
    spec = importlib.util.spec_from_file_location("ldf_release_artifacts", GENERATOR_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _properties(item: dict) -> dict[str, str]:
    return {entry["name"]: entry["value"] for entry in item.get("properties", [])}


def test_generator_is_deterministic_and_artifacts_are_current():
    generator = _load_generator()
    first = generator.render_artifacts(ROOT)
    second = generator.render_artifacts(ROOT)
    assert first == second
    for path, expected in first.items():
        assert path.read_text(encoding="utf-8") == expected
    assert generator.main(["--check"]) == 0
    assert generator.main(["--check", "--profile", "lite"]) == 0


def test_hash_locks_and_report_profile_membership_agree_exactly():
    generator = _load_generator()
    report = json.loads(REPORT_PATH.read_text(encoding="utf-8"))
    reviewed = {
        generator.canonicalize(component["name"]): set(component["profiles"])
        for component in report["components"]
        if component["kind"] == "runtime-python"
    }
    actual: dict[str, set[str]] = {name: set() for name in reviewed}
    for profile in PROFILES:
        lock = generator.load_lock(ROOT / "requirements" / "locks" / f"{profile}.txt")
        assert len(lock) == PYTHON_COUNTS[profile]
        assert all(requirement.hashes for requirement in lock.values())
        assert all(
            all(len(digest) == 64 for digest in requirement.hashes)
            for requirement in lock.values()
        )
        for name in lock:
            actual[name].add(profile)
    assert actual == reviewed
    assert actual["click"] == {"standard", "full"}
    assert actual["annotated-doc"] == set(PROFILES)


def test_cyclonedx_16_profile_shape_scope_and_findings():
    for profile in PROFILES:
        path = ROOT / "docs" / f"SBOM.{profile}.cdx.json"
        sbom = json.loads(path.read_text(encoding="utf-8"))
        assert sbom["$schema"] == "http://cyclonedx.org/schema/bom-1.6.schema.json"
        assert sbom["bomFormat"] == "CycloneDX"
        assert sbom["specVersion"] == "1.6"
        assert sbom["version"] == 1
        metadata_properties = _properties(sbom["metadata"])
        assert metadata_properties["localdocforge:profile"] == profile
        assert metadata_properties["localdocforge:advisoryAccessDate"] == "2026-07-19"
        assert metadata_properties["localdocforge:advisoryAmendedDate"] == "2026-08-08"
        assert sbom["metadata"]["timestamp"] == "2026-08-08T00:00:00Z"
        assert "SHA-256 artifact hashes" in metadata_properties[
            "localdocforge:lockEvidence"
        ]
        assert "universal-profile SBOM" in metadata_properties[
            "localdocforge:wheelEvidencePlatform"
        ]
        assert "Windows x86-64" in metadata_properties[
            "localdocforge:wheelEvidencePlatform"
        ]
        assert "incomplete" in metadata_properties[
            "localdocforge:nativeComposition"
        ]
        assert sbom["compositions"] == [
            {
                "aggregate": "incomplete",
                "assemblies": [sbom["metadata"]["component"]["bom-ref"]],
            }
        ]

        components = sbom["components"]
        assert len(components) == PYTHON_COUNTS[profile] + NATIVE_COUNT + UNVERSIONED_COUNT
        references = {component["bom-ref"] for component in components}
        assert len(references) == len(components)
        python_components = [
            component
            for component in components
            if _properties(component)["localdocforge:componentKind"]
            == "runtime-python"
        ]
        assert len(python_components) == PYTHON_COUNTS[profile]
        assert sum(
            _properties(component)["localdocforge:componentKind"] == "bundled-native"
            for component in components
        ) == NATIVE_COUNT
        assert sum(
            _properties(component)["localdocforge:componentKind"]
            == "bundled-native-unversioned"
            for component in components
        ) == UNVERSIONED_COUNT
        for component in components:
            properties = _properties(component)
            assert properties["localdocforge:advisoryDisposition"]
            assert properties["localdocforge:advisoryReviewDate"] in REVIEW_DATES
            assert properties["localdocforge:licenseConclusion"]
            assert properties["localdocforge:licenseVerification"]
            if properties["localdocforge:componentKind"] == "runtime-python":
                assert int(properties["localdocforge:lockHashCount"]) > 0
                assert "sha256:" in properties["localdocforge:lockHashes"]

        root_ref = sbom["metadata"]["component"]["bom-ref"]
        valid_dependency_refs = references | {root_ref}
        dependency_map = {
            relationship["ref"]: set(relationship["dependsOn"])
            for relationship in sbom["dependencies"]
        }
        assert dependency_map[root_ref] == DIRECT_REFS[profile]
        for relationship in sbom["dependencies"]:
            assert relationship["ref"] in valid_dependency_refs
            assert set(relationship["dependsOn"]) <= references
        assert dependency_map["pkg:pypi/pydantic@2.13.4"] == {
            "pkg:pypi/annotated-types@0.7.0",
            "pkg:pypi/pydantic-core@2.46.4",
            "pkg:pypi/typing-extensions@4.16.0",
            "pkg:pypi/typing-inspection@0.4.2",
        }
        assert {
            "pkg:pypi/annotated-doc@0.0.4",
            "pkg:pypi/rich@15.0.0",
            "pkg:pypi/shellingham@1.5.4",
        } <= dependency_map["pkg:pypi/typer@0.27.0"]
        assert {
            "pkg:pypi/lxml@6.1.1",
            "pkg:pypi/packaging@26.2",
            "pkg:pypi/pillow@12.3.0",
            "pkg:generic/qpdf@12.3.2",
            "pkg:generic/microsoft-visual-cpp-runtime@14.44.35211.0",
        } == dependency_map["pkg:pypi/pikepdf@10.10.0"]
        assert dependency_map["pkg:pypi/pypdfium2@5.12.1"] == {
            "pkg:generic/pdfium@152.0.7947.0"
        }
        assert len(dependency_map["pkg:generic/pdfium@152.0.7947.0"]) == 14
        assert len(dependency_map["pkg:generic/libavif@1.4.2"]) == 4
        assert dependency_map["pkg:pypi/pillow@12.3.0"] == {
            "pkg:generic/pillow%20codec%20bundle@12.3.0"
        }
        assert dependency_map["pkg:pypi/pi-heif@1.4.0"] == {
            "pkg:generic/libheif@1.23.0",
            "pkg:pypi/pillow@12.3.0",
        }
        assert dependency_map["pkg:generic/libheif@1.23.0"] == {
            "pkg:generic/libde265@1.1.1"
        }
        assert "pkg:generic/libheif@1.23.0" not in dependency_map[
            "pkg:generic/pillow%20codec%20bundle@12.3.0"
        ]
        assert "pkg:generic/libde265@1.1.1" not in dependency_map[
            "pkg:generic/pillow%20codec%20bundle@12.3.0"
        ]

        vulnerabilities = {item["id"]: item for item in sbom["vulnerabilities"]}
        assert set(vulnerabilities) == {
            "OSV-2020-2308",
            "OSV-2023-1129",
            "OSV-2025-219",
        }
        vulnerability = vulnerabilities["OSV-2025-219"]
        assert vulnerability["ratings"] == [{"severity": "high"}]
        assert {item["ref"] for item in vulnerability["affects"]} == {
            "pkg:generic/openjpeg@2.5.4",
            "pkg:generic/pillow%20codec%20bundle@12.3.0",
            "pkg:pypi/pillow@12.3.0",
        }
        for heif_advisory in ("OSV-2020-2308", "OSV-2023-1129"):
            entry = vulnerabilities[heif_advisory]
            assert entry["ratings"] == [{"severity": "medium"}]
            assert {item["ref"] for item in entry["affects"]} == {
                "pkg:generic/libheif@1.23.0",
                "pkg:pypi/pi-heif@1.4.0",
            }
            assert entry["recommendation"]

        by_ref = {component["bom-ref"]: component for component in components}
        assert _properties(by_ref["pkg:generic/openjpeg@2.5.4"])[
            "localdocforge:advisoryDisposition"
        ] == "affected"
        assert _properties(by_ref["pkg:generic/libheif@1.23.0"])[
            "localdocforge:advisoryDisposition"
        ] == "affected"
        assert _properties(by_ref["pkg:pypi/pi-heif@1.4.0"])[
            "localdocforge:advisoryDisposition"
        ] == "contains-affected-component"
        assert _properties(by_ref["pkg:generic/libde265@1.1.1"])[
            "localdocforge:advisoryDisposition"
        ] == "no-known-applicable-advisory"
        assert _properties(by_ref["pkg:generic/pdfium@152.0.7947.0"])[
            "localdocforge:advisoryDisposition"
        ] == "unknown"
        msvc = by_ref[
            "pkg:generic/microsoft-visual-cpp-runtime@14.44.35211.0"
        ]
        assert msvc["version"] == "14.44.35211.0"
        assert "msvcp140" in _properties(msvc)[
            "localdocforge:licenseLocalVersionEvidence"
        ]
        for reference in dependency_map["pkg:generic/pdfium@152.0.7947.0"]:
            properties = _properties(by_ref[reference])
            assert properties["localdocforge:versionStatus"] == "unknown"
            assert properties["localdocforge:advisoryDisposition"] == "unknown"

    assert (ROOT / "docs" / "SBOM.cdx.json").read_bytes() == (
        ROOT / "docs" / "SBOM.full.cdx.json"
    ).read_bytes()


def test_machine_readable_review_is_complete_precise_and_source_attributed():
    report = json.loads(REPORT_PATH.read_text(encoding="utf-8"))
    assert report["schemaVersion"] == 1
    assert report["accessDate"] == "2026-07-19"
    assert report["amendedDate"] == "2026-08-08"
    refresh = report["verificationRuns"][-1]
    assert refresh["accessDate"] == "2026-08-08"
    assert refresh["releaseDisposition"] == "not-cleared"
    assert all(source["url"].startswith("https://") for source in refresh["sources"])
    assert all(source["exactVersions"] for source in refresh["sources"])
    assert all(source["conclusion"] for source in refresh["sources"])
    assert all(source["disposition"] for source in refresh["sources"])
    assert report["scope"]["versionedReviewRecordCount"] == 48
    assert report["scope"]["versionedBundledNativeComponents"] == NATIVE_COUNT
    assert report["scope"]["unversionedNestedNativeComponents"] == UNVERSIONED_COUNT
    assert report["scope"]["optionalEngines"]["reviewed"] is False
    components = report["components"]
    assert len({component["bomRef"] for component in components}) == 48
    assert Counter(component["kind"] for component in components) == {
        "runtime-python": 30,
        "bundled-native": NATIVE_COUNT,
    }
    assert Counter(
        component["security"]["disposition"] for component in components
    ) == report["summary"]["dispositionCounts"]

    allowed_dispositions = set(report["method"]["conclusionVocabulary"])
    for component in components:
        assert component["version"] and component["version"] != "unknown"
        assert component["license"]["concluded"]
        assert component["license"]["status"].startswith("verified")
        assert component["license"]["upstreamText"].startswith("https://")
        security = component["security"]
        assert security["disposition"] in allowed_dispositions
        assert security["sourceRefs"]
        assert security["upstream"].startswith("https://")
        assert security["applicability"]
        assert security["remediation"]
        assert security["residualRisk"]

    moving_license_sources = [
        component
        for component in components
        if any(
            token in component["license"]["upstreamText"]
            for token in ("/blob/main/", "/blob/master/", "/refs/heads/main/")
        )
    ]
    assert [component["name"] for component in moving_license_sources] == ["PDFium"]
    assert moving_license_sources[0]["license"]["upstreamTextLimitation"]

    by_ref = {component["bomRef"]: component for component in components}
    openjpeg = by_ref["pkg:generic/openjpeg@2.5.4"]
    assert openjpeg["security"]["disposition"] == "affected"
    assert openjpeg["security"]["affectedVersionRanges"]
    advisory = openjpeg["security"]["advisories"][0]
    assert advisory["id"] == "OSV-2025-219"
    assert advisory["severity"] == "HIGH"
    assert "depends/install_openjpeg.sh" in " ".join(advisory["sources"])
    assert "winbuild/build_prepare.py" in " ".join(advisory["sources"])
    assert "No checksum evidence" in advisory["provenanceConclusion"]

    pdfium = by_ref["pkg:generic/pdfium@152.0.7947.0"]
    assert pdfium["security"]["disposition"] == "unknown"
    assert pdfium["security"]["affectedVersionRanges"] is None
    assert "hash=null" in pdfium["license"]["localVersionEvidence"]

    libheif = by_ref["pkg:generic/libheif@1.23.0"]
    assert libheif["security"]["disposition"] == "affected"
    assert libheif["reviewDate"] == "2026-08-08"
    assert [advisory["id"] for advisory in libheif["security"]["advisories"]] == [
        "OSV-2020-2308",
        "OSV-2023-1129",
    ]
    for advisory in libheif["security"]["advisories"]:
        assert advisory["severity"] == "MEDIUM"
        assert advisory["provenanceConclusion"]
        assert any("osv.dev" in url for url in advisory["sources"])
    assert libheif["license"]["concluded"] == "LGPL-3.0-or-later"

    pi_heif_record = by_ref["pkg:pypi/pi-heif@1.4.0"]
    assert pi_heif_record["security"]["disposition"] == "contains-affected-component"
    assert pi_heif_record["bomRef"] in {
        f"pkg:pypi/pi-heif@{pi_heif_record['version']}"
    }
    assert "no x265 encoder" in pi_heif_record["license"]["localVersionEvidence"]

    libde265 = by_ref["pkg:generic/libde265@1.1.1"]
    assert libde265["security"]["disposition"] == "no-known-applicable-advisory"
    assert "OSV-2020-2308" in libde265["security"]["applicability"]
    assert libde265["license"]["concluded"] == "LGPL-3.0-or-later"

    msvc = by_ref["pkg:generic/microsoft-visual-cpp-runtime@14.44.35211.0"]
    assert msvc["bundledBy"] == "pikepdf"
    assert msvc["security"]["disposition"] == "no-known-applicable-advisory"
    assert msvc["security"]["sourceRefs"] == ["microsoft-vc-runtime-exact"]
    assert "0f885b509a685d2b" in msvc["license"]["localVersionEvidence"]
    assert "vs2022-cruntime" in msvc["license"]["upstreamText"]

    unversioned = report["unversionedNestedComponents"]
    assert len({component["bomRef"] for component in unversioned}) == 18
    assert sum(
        component["parentBomRef"] == "pkg:generic/pdfium@152.0.7947.0"
        for component in unversioned
    ) == 14
    assert sum(
        component["parentBomRef"] == "pkg:generic/libavif@1.4.2"
        for component in unversioned
    ) == 4
    assert all(
        component["advisoryDisposition"] == "unknown"
        and component["licenseNotice"]
        and component["policyRef"] == "method.unversionedComponentPolicy"
        for component in unversioned
    )
    assert {
        component["name"]
        for component in unversioned
        if component["versionEvidence"] == "LOCAL"
    } == {
        "AOM (libavif child)",
        "dav1d (libavif child)",
        "libsharpyuv (libavif child)",
        "libyuv (libavif child)",
    }


def test_notice_index_and_profile_notices_disclose_required_uncertainty():
    index = (ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    for profile in PROFILES:
        assert f"THIRD_PARTY_NOTICES.{profile}.md" in index
        assert f"docs/SBOM.{profile}.cdx.json" in index
        notices = (ROOT / f"THIRD_PARTY_NOTICES.{profile}.md").read_text(
            encoding="utf-8"
        )
        assert f"# Third-Party Notices — {profile.title()} profile" in notices
        assert "## Runtime Python distributions" in notices
        assert "## Versioned native review records from inspected Windows wheels" in notices
        assert "## Known version-unknown nested native children" in notices
        assert "Microsoft Visual C++ Runtime (msvcp140.dll)" in notices
        assert "AOM (libavif child)" in notices
        assert "universal-profile SBOM" in notices
        assert "CycloneDX composition is explicitly `incomplete`" in notices
        assert "OpenJPEG 2.5.4 is affected" in notices
        assert "libheif 1.23.0 is affected" in notices
        assert "pi-heif wheels are decode-only builds" in notices
        assert "no GPLv2 x265" in notices
        assert "PDFium 152.0.7947.0 is advisory-unknown" in notices
        assert "not a safety guarantee" in notices
        assert "No vulnerability or security-advisory lookup was performed" not in notices
        assert "profiles are not implemented" not in notices
    assert "byte-identical compatibility alias" in index


def test_project_license_metadata_and_standard_mit_text():
    with (ROOT / "pyproject.toml").open("rb") as stream:
        project = tomllib.load(stream)["project"]
    assert project["license"] == "MIT"
    assert project["license-files"] == ["LICENSE"]

    license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert license_text.startswith("MIT License\n")
    assert "Permission is hereby granted, free of charge" in license_text
    assert 'THE SOFTWARE IS PROVIDED "AS IS"' in license_text
