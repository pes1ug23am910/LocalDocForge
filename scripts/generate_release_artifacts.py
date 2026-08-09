#!/usr/bin/env python3
"""Generate deterministic, profile-specific licensing notices and CycloneDX SBOMs.

The generator performs no network access. Exact Python versions, environment markers,
and artifact hashes come from the committed profile exports. License and advisory
conclusions come from the curated, source-attributed advisory report.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PYPROJECT_PATH = ROOT / "pyproject.toml"
REPORT_PATH = ROOT / "docs" / "ADVISORY_REPORT.json"
UV_LOCK_PATH = ROOT / "uv.lock"
PROFILES = ("lite", "standard", "full")
PROFILE_LOCK_PATHS = {
    profile: ROOT / "requirements" / "locks" / f"{profile}.txt"
    for profile in PROFILES
}
PROFILE_SBOM_PATHS = {
    profile: ROOT / "docs" / f"SBOM.{profile}.cdx.json" for profile in PROFILES
}
PROFILE_NOTICE_PATHS = {
    profile: ROOT / f"THIRD_PARTY_NOTICES.{profile}.md" for profile in PROFILES
}
LEGACY_SBOM_PATH = ROOT / "docs" / "SBOM.cdx.json"
COMBINED_NOTICE_PATH = ROOT / "THIRD_PARTY_NOTICES.md"

LOCK_RE = re.compile(
    r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;\\]+)"
    r"(?:\s*;\s*(.*?))?\s*\\?$"
)
HASH_RE = re.compile(r"--hash=sha256:([0-9a-f]{64})")

SPDX_LICENSE_IDS = {
    "0BSD",
    "Apache-2.0",
    "BSD-2-Clause",
    "BSD-3-Clause",
    "FTL",
    "GPL-2.0-or-later",
    "ISC",
    "LGPL-3.0-or-later",
    "MIT",
    "MIT-CMU",
    "MPL-2.0",
    "PSF-2.0",
    "Zlib",
    "libpng-2.0",
    "libtiff",
}
SPDX_EXPRESSIONS = {
    "Apache-2.0 OR BSD-2-Clause",
    "FTL OR GPL-2.0-or-later",
}


@dataclass(frozen=True)
class LockedRequirement:
    source_name: str
    version: str
    marker: str
    hashes: tuple[str, ...]


def canonicalize(name: str) -> str:
    """Return the PEP 503 normalized distribution name."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _flush_lock_entry(
    locked: dict[str, LockedRequirement],
    current: tuple[str, str, str] | None,
    hashes: list[str],
    path: Path,
) -> None:
    if current is None:
        return
    source_name, version, marker = current
    name = canonicalize(source_name)
    if name in locked:
        raise ValueError(f"duplicate package {source_name!r} in {path}")
    unique_hashes = tuple(sorted(set(hashes)))
    if not unique_hashes:
        raise ValueError(f"{source_name}=={version} has no SHA-256 hashes in {path}")
    locked[name] = LockedRequirement(source_name, version, marker, unique_hashes)


def load_lock(path: Path) -> dict[str, LockedRequirement]:
    """Parse a hash-bearing uv/pip requirements export."""
    locked: dict[str, LockedRequirement] = {}
    current: tuple[str, str, str] | None = None
    hashes: list[str] = []
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        match = LOCK_RE.match(raw_line)
        if match:
            _flush_lock_entry(locked, current, hashes, path)
            marker = (match.group(3) or "").rstrip().removesuffix("\\").rstrip()
            current = (match.group(1), match.group(2), marker)
            hashes = HASH_RE.findall(raw_line)
            continue
        if current is not None:
            hashes.extend(HASH_RE.findall(raw_line))
        elif raw_line.strip() and not raw_line.lstrip().startswith("#"):
            raise ValueError(f"unsupported lock syntax at {path}:{line_number}")
    _flush_lock_entry(locked, current, hashes, path)
    if not locked:
        raise ValueError(f"no locked requirements found in {path}")
    return locked


def load_pyproject(path: Path) -> dict[str, Any]:
    with path.open("rb") as stream:
        return tomllib.load(stream)


def load_uv_lock(path: Path) -> dict[str, Any]:
    with path.open("rb") as stream:
        lock = tomllib.load(stream)
    if lock.get("version") != 1 or not isinstance(lock.get("package"), list):
        raise ValueError("unsupported uv.lock structure")
    return lock


def load_report(path: Path) -> dict[str, Any]:
    report = json.loads(path.read_text(encoding="utf-8"))
    if report.get("schemaVersion") != 1:
        raise ValueError("unsupported advisory report schema")
    if report.get("accessDate") != "2026-07-19":
        raise ValueError("advisory report access date must be 2026-07-19")
    if report.get("amendedDate") != "2026-08-09":
        raise ValueError("advisory report amended date must be 2026-08-09")
    components = report.get("components")
    if not isinstance(components, list) or len(components) != 48:
        raise ValueError("advisory report must contain 48 versioned review records")
    unversioned = report.get("unversionedNestedComponents")
    if not isinstance(unversioned, list) or len(unversioned) != 18:
        raise ValueError("advisory report must enumerate 18 unversioned native children")
    return report


def report_components(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    for component in report["components"]:
        reference = component["bomRef"]
        if reference in indexed:
            raise ValueError(f"duplicate advisory component reference: {reference}")
        indexed[reference] = component
    return indexed


def runtime_report_by_name(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        canonicalize(component["name"]): component
        for component in report["components"]
        if component["kind"] == "runtime-python"
    }


def validate_profile_scope(
    profile: str,
    locked: dict[str, LockedRequirement],
    report: dict[str, Any],
) -> None:
    expected = {
        canonicalize(component["name"])
        for component in report["components"]
        if component["kind"] == "runtime-python" and profile in component["profiles"]
    }
    actual = set(locked)
    if actual != expected:
        missing = ", ".join(sorted(expected - actual)) or "none"
        extra = ", ".join(sorted(actual - expected)) or "none"
        raise ValueError(
            f"{profile} profile disagrees with advisory report; missing={missing}; extra={extra}"
        )
    by_name = runtime_report_by_name(report)
    for name, requirement in locked.items():
        reviewed = by_name[name]
        if reviewed["version"] != requirement.version:
            raise ValueError(
                f"{profile} version mismatch for {name}: "
                f"lock={requirement.version}; report={reviewed['version']}"
            )


def uv_dependency_edges(
    uv_lock: dict[str, Any],
    profile: str,
    locked: dict[str, LockedRequirement],
) -> tuple[set[str], dict[str, set[str]]]:
    """Return direct root names and per-package transitive edges from uv.lock."""
    packages = uv_lock["package"]
    root_records = [
        package
        for package in packages
        if canonicalize(package["name"]) == "localdocforge"
        and package.get("source", {}).get("editable") == "."
    ]
    if len(root_records) != 1:
        raise ValueError("uv.lock must contain one editable localdocforge record")
    root_record = root_records[0]
    root_requirements = list(root_record.get("dependencies", []))
    root_requirements.extend(
        root_record.get("optional-dependencies", {}).get(profile, [])
    )
    direct = {
        canonicalize(requirement["name"])
        for requirement in root_requirements
        if canonicalize(requirement["name"]) in locked
    }

    records: dict[str, dict[str, Any]] = {}
    for package in packages:
        name = canonicalize(package["name"])
        if name not in locked or package.get("version") != locked[name].version:
            continue
        if name in records:
            raise ValueError(f"ambiguous uv.lock record for {name}")
        records[name] = package
    if set(records) != set(locked):
        missing = ", ".join(sorted(set(locked) - set(records)))
        raise ValueError(f"uv.lock is missing exact profile records: {missing}")

    edges = {
        name: {
            canonicalize(dependency["name"])
            for dependency in record.get("dependencies", [])
            if canonicalize(dependency["name"]) in locked
        }
        for name, record in records.items()
    }
    reachable = set(direct)
    pending = list(direct)
    while pending:
        name = pending.pop()
        for dependency in edges[name] - reachable:
            reachable.add(dependency)
            pending.append(dependency)
    if reachable != set(locked):
        missing = ", ".join(sorted(set(locked) - reachable)) or "none"
        extra = ", ".join(sorted(reachable - set(locked))) or "none"
        raise ValueError(
            f"{profile} uv dependency closure mismatch; missing={missing}; extra={extra}"
        )
    return direct, edges


def property_list(values: dict[str, str]) -> list[dict[str, str]]:
    return [{"name": name, "value": values[name]} for name in sorted(values)]


def license_list(conclusion: str) -> list[dict[str, Any]]:
    if conclusion in SPDX_LICENSE_IDS:
        return [{"license": {"id": conclusion}}]
    if conclusion in SPDX_EXPRESSIONS:
        return [{"expression": conclusion}]
    return [{"license": {"name": conclusion}}]


def external_references(component: dict[str, Any]) -> list[dict[str, str]]:
    references: list[dict[str, str]] = []
    license_info = component["license"]
    if license_info.get("upstreamText"):
        references.append({"type": "license", "url": license_info["upstreamText"]})
    if license_info.get("exactReleaseMetadata"):
        references.append(
            {"type": "distribution", "url": license_info["exactReleaseMetadata"]}
        )
    security = component["security"]
    if security.get("upstream"):
        references.append({"type": "advisories", "url": security["upstream"]})
    for advisory in security.get("advisories", []):
        for url in advisory.get("sources", []):
            references.append({"type": "advisories", "url": url})
    unique = {(item["type"], item["url"]): item for item in references}
    return [unique[key] for key in sorted(unique)]


def common_component_properties(
    component: dict[str, Any], profile: str
) -> dict[str, str]:
    security = component["security"]
    license_info = component["license"]
    values = {
        "localdocforge:advisoryApplicability": security["applicability"],
        "localdocforge:advisoryDisposition": security["disposition"],
        "localdocforge:advisoryRemediation": security["remediation"],
        "localdocforge:advisoryReviewDate": component.get("reviewDate", "2026-07-19"),
        "localdocforge:componentKind": component["kind"],
        "localdocforge:licenseConclusion": license_info["concluded"],
        "localdocforge:licenseVerification": license_info["status"],
        "localdocforge:profile": profile,
        "localdocforge:residualRisk": security["residualRisk"],
        "localdocforge:role": component["role"],
    }
    if license_info.get("localVersionEvidence"):
        values["localdocforge:licenseLocalVersionEvidence"] = license_info[
            "localVersionEvidence"
        ]
    if license_info.get("exactVersionEvidence"):
        values["localdocforge:licenseExactVersionEvidence"] = license_info[
            "exactVersionEvidence"
        ]
    if license_info.get("upstreamTextLimitation"):
        values["localdocforge:licenseSourceLimitation"] = license_info[
            "upstreamTextLimitation"
        ]
    if license_info.get("vendorLifecycleEvidence"):
        values["localdocforge:vendorLifecycleEvidence"] = license_info[
            "vendorLifecycleEvidence"
        ]
    if component.get("evidencePlatform"):
        values["localdocforge:evidencePlatform"] = component["evidencePlatform"]
    return values


def python_component(
    reviewed: dict[str, Any],
    requirement: LockedRequirement,
    profile: str,
    lock_path: Path,
    root: Path,
) -> dict[str, Any]:
    properties = common_component_properties(reviewed, profile)
    properties.update(
        {
            "localdocforge:lockHashCount": str(len(requirement.hashes)),
            "localdocforge:lockHashes": ",".join(
                f"sha256:{value}" for value in requirement.hashes
            ),
            "localdocforge:lockMarker": requirement.marker or "unconditional",
            "localdocforge:lockPath": lock_path.relative_to(root).as_posix(),
        }
    )
    result: dict[str, Any] = {
        "type": "library",
        "bom-ref": reviewed["bomRef"],
        "name": requirement.source_name,
        "version": requirement.version,
        "purl": reviewed["bomRef"],
        "licenses": license_list(reviewed["license"]["concluded"]),
        "properties": property_list(properties),
    }
    references = external_references(reviewed)
    if references:
        result["externalReferences"] = references
    return result


def native_component(reviewed: dict[str, Any], profile: str) -> dict[str, Any]:
    properties = common_component_properties(reviewed, profile)
    properties["localdocforge:bundledBy"] = reviewed.get("bundledBy", "unknown")
    result: dict[str, Any] = {
        "type": "library",
        "bom-ref": reviewed["bomRef"],
        "name": reviewed["name"],
        "version": reviewed["version"],
        "purl": reviewed["bomRef"],
        "licenses": license_list(reviewed["license"]["concluded"]),
        "properties": property_list(properties),
    }
    references = external_references(reviewed)
    if references:
        result["externalReferences"] = references
    return result


def unversioned_native_component(
    reviewed: dict[str, Any], profile: str, policy: dict[str, Any]
) -> dict[str, Any]:
    properties = {
        "localdocforge:advisoryApplicability": policy["applicability"],
        "localdocforge:advisoryDisposition": reviewed["advisoryDisposition"],
        "localdocforge:advisoryRemediation": policy["remediation"],
        "localdocforge:advisoryReviewDate": "2026-07-19",
        "localdocforge:bundledBy": reviewed["parentBomRef"],
        "localdocforge:componentKind": "bundled-native-unversioned",
        "localdocforge:evidencePlatform": reviewed["evidencePlatform"],
        "localdocforge:licenseConclusion": reviewed["licenseConclusion"],
        "localdocforge:licenseNotice": reviewed["licenseNotice"],
        "localdocforge:licenseVerification": (
            "aggregate notice preserved; exact child version unavailable"
        ),
        "localdocforge:profile": profile,
        "localdocforge:residualRisk": policy["residualRisk"],
        "localdocforge:versionEvidence": reviewed["versionEvidence"],
        "localdocforge:versionStatus": "unknown",
    }
    return {
        "type": "library",
        "bom-ref": reviewed["bomRef"],
        "name": reviewed["name"],
        "licenses": license_list(reviewed["licenseConclusion"]),
        "properties": property_list(properties),
    }


def build_dependencies(
    root_ref: str,
    locked: dict[str, LockedRequirement],
    direct_names: set[str],
    python_edges: dict[str, set[str]],
    python_components: list[dict[str, Any]],
    native_components: list[dict[str, Any]],
    unversioned_components: list[dict[str, Any]],
    unversioned_records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    all_components = python_components + native_components + unversioned_components
    all_refs = {item["bom-ref"] for item in all_components}
    relationships: dict[str, set[str]] = {reference: set() for reference in all_refs}
    python_refs = {
        name: f"pkg:pypi/{name}@{requirement.version}"
        for name, requirement in locked.items()
    }
    relationships[root_ref] = {python_refs[name] for name in direct_names}
    for name, dependencies in python_edges.items():
        relationships[python_refs[name]].update(
            python_refs[dependency] for dependency in dependencies
        )
    nested = {
        "pkg:pypi/pikepdf@10.10.0": {
            "pkg:generic/qpdf@12.3.2",
            "pkg:generic/microsoft-visual-cpp-runtime@14.44.35211.0",
        },
        "pkg:pypi/pypdfium2@5.12.1": {"pkg:generic/pdfium@152.0.7947.0"},
        "pkg:pypi/pillow@12.3.0": {
            "pkg:generic/pillow%20codec%20bundle@12.3.0"
        },
        "pkg:pypi/pi-heif@1.4.0": {"pkg:generic/libheif@1.23.0"},
        # libde265 ships as its own DLL in the pi-heif wheel but is loaded
        # and driven exclusively by libheif's HEVC decode path.
        "pkg:generic/libheif@1.23.0": {"pkg:generic/libde265@1.1.1"},
        "pkg:generic/pillow%20codec%20bundle@12.3.0": {
            reference
            for reference in all_refs
            if reference.startswith("pkg:generic/")
            and reference
            not in {
                "pkg:generic/pdfium@152.0.7947.0",
                "pkg:generic/microsoft-visual-cpp-runtime@14.44.35211.0",
                "pkg:generic/pillow%20codec%20bundle@12.3.0",
                "pkg:generic/qpdf@12.3.2",
                "pkg:generic/libheif@1.23.0",
                "pkg:generic/libde265@1.1.1",
            }
        },
    }
    for parent, children in nested.items():
        if parent in relationships:
            relationships[parent].update(children & all_refs)
    for record in unversioned_records:
        parent = record["parentBomRef"]
        child = record["bomRef"]
        if parent in relationships and child in relationships:
            relationships[parent].add(child)
    return [
        {"ref": reference, "dependsOn": sorted(relationships[reference])}
        for reference in sorted(relationships)
    ]


def build_vulnerabilities(indexed: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """One SBOM entry per applicable advisory, derived from the curated report.

    The component with disposition ``affected`` that names an advisory id owns
    that advisory's description, severity, applicability, and remediation;
    every component whose record lists the same id (aggregates and bundling
    wheels) joins the ``affects`` list.
    """
    owners: dict[str, dict[str, Any]] = {}
    affects: dict[str, set[str]] = {}
    for reference, component in indexed.items():
        security = component["security"]
        for advisory in security.get("advisories", []):
            advisory_id = advisory["id"]
            affects.setdefault(advisory_id, set()).add(reference)
            if security["disposition"] == "affected":
                if advisory_id in owners:
                    raise ValueError(
                        f"advisory {advisory_id} has more than one affected owner"
                    )
                owners[advisory_id] = {"component": component, "advisory": advisory}
    if set(owners) != set(affects):
        unowned = ", ".join(sorted(set(affects) - set(owners)))
        raise ValueError(f"advisories without an affected owner record: {unowned}")
    entries = []
    for advisory_id in sorted(owners):
        component = owners[advisory_id]["component"]
        advisory = owners[advisory_id]["advisory"]
        entries.append(
            {
                "id": advisory_id,
                "source": {
                    "name": "OSV",
                    "url": f"https://osv.dev/vulnerability/{advisory_id}",
                },
                "ratings": [{"severity": advisory["severity"].lower()}],
                "description": advisory["summary"],
                "analysis": {
                    "state": "in_triage",
                    "detail": component["security"]["applicability"],
                },
                "affects": [
                    {"ref": reference}
                    for reference in sorted(affects[advisory_id])
                ],
                "recommendation": component["security"]["remediation"],
            }
        )
    return entries


def build_sbom(
    root: Path,
    project: dict[str, Any],
    uv_lock: dict[str, Any],
    report: dict[str, Any],
    profile: str,
    locked: dict[str, LockedRequirement],
) -> dict[str, Any]:
    indexed = report_components(report)
    by_name = runtime_report_by_name(report)
    lock_path = root / "requirements" / "locks" / f"{profile}.txt"
    python_components = [
        python_component(by_name[name], requirement, profile, lock_path, root)
        for name, requirement in sorted(locked.items())
    ]
    native_components = [
        native_component(component, profile)
        for component in report["components"]
        if component["kind"] == "bundled-native" and profile in component["profiles"]
    ]
    native_components.sort(key=lambda item: item["bom-ref"])
    unversioned_records = [
        component
        for component in report["unversionedNestedComponents"]
        if profile in component["profiles"]
    ]
    unversioned_components = [
        unversioned_native_component(
            component, profile, report["method"]["unversionedComponentPolicy"]
        )
        for component in unversioned_records
    ]
    unversioned_components.sort(key=lambda item: item["bom-ref"])
    direct_names, python_edges = uv_dependency_edges(uv_lock, profile, locked)
    versioned_native_count = sum(
        1 for component in report["components"] if component["kind"] == "bundled-native"
    )
    unversioned_count = len(report["unversionedNestedComponents"])
    root_name = canonicalize(project["project"]["name"])
    root_version = project["project"]["version"]
    root_ref = f"pkg:pypi/{root_name}@{root_version}"
    report_sha256 = hashlib.sha256(
        (root / "docs" / "ADVISORY_REPORT.json").read_bytes()
    ).hexdigest()
    return {
        "$schema": "http://cyclonedx.org/schema/bom-1.6.schema.json",
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "version": 1,
        "metadata": {
            "timestamp": "2026-08-09T00:00:00Z",
            "tools": {
                "components": [
                    {
                        "type": "application",
                        "name": "LocalDocForge offline release-artifact generator",
                        "version": "2",
                    }
                ]
            },
            "component": {
                "type": "application",
                "bom-ref": root_ref,
                "name": root_name,
                "version": root_version,
                "purl": root_ref,
                "licenses": [{"license": {"id": "MIT"}}],
            },
            "properties": property_list(
                {
                    "localdocforge:advisoryAccessDate": report["accessDate"],
                    "localdocforge:advisoryAmendedDate": report["amendedDate"],
                    "localdocforge:advisoryReport": "docs/ADVISORY_REPORT.json",
                    "localdocforge:advisoryReportSha256": report_sha256,
                    "localdocforge:advisoryReview": (
                        "authoritative upstream/vendor sources with OSV and GitHub "
                        "reviewed-advisory corroboration; no-finding is not a safety claim"
                    ),
                    "localdocforge:lockEvidence": (
                        f"requirements/locks/{profile}.txt; exact pins, markers, "
                        "and SHA-256 artifact hashes"
                    ),
                    "localdocforge:profile": profile,
                    "localdocforge:releaseDisposition": report["summary"][
                        "releaseDisposition"
                    ],
                    "localdocforge:wheelEvidencePlatform": (
                        "This universal-profile SBOM uses Windows x86-64 / CPython "
                        "3.14 evidence for every native component. It does not claim "
                        "the same native composition on Linux or macOS; every other "
                        "target wheel requires equivalent re-inventory."
                    ),
                    "localdocforge:nativeComposition": (
                        f"incomplete: {versioned_native_count} versioned records "
                        f"plus {unversioned_count} known unversioned children; "
                        "additional statically linked or platform-specific "
                        "children may exist"
                    ),
                }
            ),
        },
        "components": python_components + native_components + unversioned_components,
        "dependencies": build_dependencies(
            root_ref,
            locked,
            direct_names,
            python_edges,
            python_components,
            native_components,
            unversioned_components,
            unversioned_records,
        ),
        "compositions": [{"aggregate": "incomplete", "assemblies": [root_ref]}],
        "vulnerabilities": build_vulnerabilities(indexed),
    }


def markdown_cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def component_source_links(component: dict[str, Any]) -> str:
    links = [f"[license]({component['license']['upstreamText']})"]
    release = component["license"].get("exactReleaseMetadata")
    if release:
        links.append(f"[release]({release})")
    links.append(f"[advisories]({component['security']['upstream']})")
    return ", ".join(links)


def build_profile_notices(
    report: dict[str, Any], profile: str, locked: dict[str, LockedRequirement]
) -> str:
    runtime = [
        component
        for component in report["components"]
        if component["kind"] == "runtime-python" and profile in component["profiles"]
    ]
    native = [
        component
        for component in report["components"]
        if component["kind"] == "bundled-native" and profile in component["profiles"]
    ]
    unversioned = [
        component
        for component in report["unversionedNestedComponents"]
        if profile in component["profiles"]
    ]
    runtime.sort(key=lambda item: canonicalize(item["name"]))
    native.sort(key=lambda item: canonicalize(item["name"]))
    unversioned.sort(key=lambda item: item["bomRef"])
    lines = [
        f"# Third-Party Notices — {profile.title()} profile",
        "",
        "Generated offline by `scripts/generate_release_artifacts.py` from the "
        f"hash-bearing `{profile}` lock and `docs/ADVISORY_REPORT.json`.",
        f"License/advisory sources were accessed {report['accessDate']}; verification "
        f"sources were refreshed {report['amendedDate']}. A no-finding disposition is "
        "not a safety guarantee or legal advice.",
        "",
        "## Runtime Python distributions",
        "",
        "| Package | Version | License conclusion | Advisory disposition | Sources |",
        "|---|---:|---|---|---|",
    ]
    for component in runtime:
        requirement = locked[canonicalize(component["name"])]
        lines.append(
            "| "
            + " | ".join(
                markdown_cell(value)
                for value in (
                    requirement.source_name,
                    requirement.version,
                    component["license"]["concluded"],
                    component["security"]["disposition"],
                    component_source_links(component),
                )
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "## Versioned native review records from inspected Windows wheels",
            "",
            "| Component | Version | Bundled by | License conclusion | "
            "Advisory disposition | Sources |",
            "|---|---:|---|---|---|---|",
        ]
    )
    for component in native:
        lines.append(
            "| "
            + " | ".join(
                markdown_cell(value)
                for value in (
                    component["name"],
                    component["version"],
                    component.get("bundledBy", "—"),
                    component["license"]["concluded"],
                    component["security"]["disposition"],
                    component_source_links(component),
                )
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "## Known version-unknown nested native children",
            "",
            "These children are enumerated because aggregate wheel evidence names or "
            "links them, but no trustworthy exact binary/source version is available. "
            "Their advisory disposition is `unknown`; preserve the parent aggregate "
            "license notices in full.",
            "",
            "| Component | Parent | Version evidence | Aggregate license evidence | "
            "Advisory disposition |",
            "|---|---|---|---|---|",
        ]
    )
    for component in unversioned:
        lines.append(
            "| "
            + " | ".join(
                markdown_cell(value)
                for value in (
                    component["name"],
                    component["parentBomRef"],
                    component["versionEvidence"],
                    f"{component['licenseConclusion']}; {component['licenseNotice']}",
                    component["advisoryDisposition"],
                )
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "## Security dispositions requiring release action",
            "",
            "- **OpenJPEG 2.5.4 is affected by "
            "[OSV-2025-219](https://osv.dev/vulnerability/OSV-2025-219).** "
            "Current media gates reject JP2/J2K, but the vulnerable codec remains "
            "bundled. Replace it with a build containing upstream fix "
            "`d33cbecc148d3affcdf403211fddc2cc5d442379` or a later fixed release.",
            "- **libheif 1.23.0 is affected by the OSS-Fuzz records "
            "[OSV-2020-2308](https://osv.dev/vulnerability/OSV-2020-2308) and "
            "[OSV-2023-1129](https://osv.dev/vulnerability/OSV-2023-1129)** "
            "(MEDIUM read-class memory-safety crashes with no fixed release "
            "enumerated), and untrusted HEIC/HEIF inputs reach this decoder by "
            "design of the HEIF input feature. Adopt a pi-heif build that "
            "resolves both records; contained workers and resource limits bound "
            "the exposure meanwhile.",
            "- **PDFium 152.0.7947.0 is advisory-unknown.** The wheel records no "
            "source commit, and public Chromium/PDFium information cannot prove which "
            "private security fixes this build contains. It directly parses untrusted "
            "PDFs and must not be represented as cleared.",
            "- **All 18 enumerated version-unknown native children remain "
            "advisory-unknown.** Their aggregate notices are retained, but no "
            "affected/not-affected conclusion is possible without exact provenance.",
            "",
            "## Redistribution and platform notes",
            "",
            "- Preserve complete license/notice directories from pikepdf, pypdfium2, "
            "Pillow, pi-heif, and all other redistributed wheels; summary labels "
            "here do not replace those texts.",
            "- pi-heif wheels are decode-only builds of pillow-heif with an LGPLv3 "
            "license ceiling (libheif + libde265). They bundle no GPLv2 x265 "
            "encoder; the full pillow-heif package is a dev-profile test-fixture "
            "tool only and ships in no runtime profile.",
            "- This notice and its universal-profile SBOM use native evidence from "
            "Windows x86-64 / CPython 3.14 wheels only. They do not assert identical "
            "native composition on Linux or macOS; re-inventory every target wheel.",
            f"- The CycloneDX composition is explicitly `incomplete`: {len(native)} "
            f"native records have versions, {len(unversioned)} known children do "
            "not, and additional static or platform-specific children may exist.",
            "- Typst 0.15.1 is an enabled, separately installed subprocess engine for "
            "Markdown-to-PDF, but it is not distributed by any Python profile and "
            "remains outside this report's component inventory. qpdf CLI, Tesseract, "
            "OCRmyPDF, Ghostscript, LibreOffice, Pandoc, and veraPDF are not shipped "
            "by these profiles.",
            "",
        ]
    )
    return "\n".join(lines)


def build_combined_notices(report: dict[str, Any]) -> str:
    counts = {
        profile: sum(
            1
            for component in report["components"]
            if component["kind"] == "runtime-python"
            and profile in component["profiles"]
        )
        for profile in PROFILES
    }
    native_count = sum(
        1 for component in report["components"] if component["kind"] == "bundled-native"
    )
    unversioned_count = len(report["unversionedNestedComponents"])
    lines = [
        "# Third-Party Notices",
        "",
        "This is the profile index. Use the notice and SBOM matching the installed "
        "LocalDocForge profile.",
        "",
        "| Profile | Python components | Versioned native records | "
        "Known unversioned children | Notice | SBOM |",
        "|---|---:|---:|---:|---|---|",
    ]
    for profile in PROFILES:
        lines.append(
            f"| {profile.title()} | {counts[profile]} | {native_count} | "
            f"{unversioned_count} | "
            f"[notices](THIRD_PARTY_NOTICES.{profile}.md) | "
            f"[SBOM](docs/SBOM.{profile}.cdx.json) |"
        )
    lines.extend(
        [
            "",
            "`docs/SBOM.cdx.json` is a byte-identical compatibility alias of the Full "
            "profile SBOM.",
            "",
            "All profiles currently bundle affected OpenJPEG 2.5.4 "
            "([OSV-2025-219](https://osv.dev/vulnerability/OSV-2025-219)) through "
            "Pillow, affected libheif 1.23.0 "
            "([OSV-2020-2308](https://osv.dev/vulnerability/OSV-2020-2308), "
            "[OSV-2023-1129](https://osv.dev/vulnerability/OSV-2023-1129)) through "
            "pi-heif, and advisory-unknown PDFium 152.0.7947.0 through pypdfium2. "
            "See the profile notice and `docs/ADVISORY_REPORT.json` for applicability, "
            "remediation, residual risk, and authoritative sources.",
            f"The {native_count} versioned native records are not exhaustive: "
            f"{unversioned_count} known unversioned "
            "PDFium/libavif children remain advisory-unknown, and each SBOM's "
            "CycloneDX composition is `incomplete`.",
            "",
            "These summaries are not legal advice. Redistributors must retain the full "
            "upstream license and notice texts shipped in wheel metadata.",
            "",
        ]
    )
    return "\n".join(lines)


def render_artifacts(
    root: Path = ROOT,
    profiles: tuple[str, ...] = PROFILES,
    *,
    include_combined: bool = True,
) -> dict[Path, str]:
    project = load_pyproject(root / "pyproject.toml")
    uv_lock = load_uv_lock(root / "uv.lock")
    report = load_report(root / "docs" / "ADVISORY_REPORT.json")
    outputs: dict[Path, str] = {}
    rendered_sboms: dict[str, str] = {}
    for profile in profiles:
        if profile not in PROFILES:
            raise ValueError(f"unknown profile: {profile}")
        lock_path = root / "requirements" / "locks" / f"{profile}.txt"
        locked = load_lock(lock_path)
        validate_profile_scope(profile, locked, report)
        sbom = build_sbom(root, project, uv_lock, report, profile, locked)
        rendered = json.dumps(sbom, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        rendered_sboms[profile] = rendered
        outputs[root / "docs" / f"SBOM.{profile}.cdx.json"] = rendered
        outputs[root / f"THIRD_PARTY_NOTICES.{profile}.md"] = build_profile_notices(
            report, profile, locked
        )
    if "full" in rendered_sboms:
        outputs[root / "docs" / "SBOM.cdx.json"] = rendered_sboms["full"]
    if include_combined:
        outputs[root / "THIRD_PARTY_NOTICES.md"] = build_combined_notices(report)
    return outputs


def write_or_check(outputs: dict[Path, str], *, check: bool) -> list[Path]:
    changed: list[Path] = []
    for path, content in outputs.items():
        current = path.read_text(encoding="utf-8") if path.exists() else None
        if current != content:
            changed.append(path)
            if not check:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8", newline="\n")
    return changed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Fail if committed artifacts differ; do not write files.",
    )
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--profile", choices=PROFILES)
    selection.add_argument(
        "--all-profiles",
        action="store_true",
        help="Generate all profiles and the combined notice index (default).",
    )
    args = parser.parse_args(argv)
    profiles = (args.profile,) if args.profile else PROFILES
    outputs = render_artifacts(
        profiles=profiles,
        include_combined=args.profile is None,
    )
    changed = write_or_check(outputs, check=args.check)
    if args.check and changed:
        for path in changed:
            print(f"out of date: {path.relative_to(ROOT)}", file=sys.stderr)
        return 1
    if not args.check:
        for path in sorted(outputs):
            print(f"generated {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
