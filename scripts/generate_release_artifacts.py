#!/usr/bin/env python3
"""Generate deterministic offline licensing notices and a CycloneDX SBOM.

The generator performs no network access. It combines exact pins from
requirements-lock.txt with metadata and license evidence already installed in
the current Python environment. Native libraries bundled by the pikepdf,
pypdfium2, and Pillow wheels are recorded separately.
"""

from __future__ import annotations

import argparse
import importlib
import importlib.metadata as metadata
import json
import re
import sys
import tomllib
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
LOCK_PATH = ROOT / "requirements-lock.txt"
PYPROJECT_PATH = ROOT / "pyproject.toml"

LOCK_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s]+)$")
REQUIREMENT_NAME_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")
PILLOW_HEADING_RE = re.compile(r"^===== (.+)-([0-9][^ ]*) =====$")

SPDX_IDS = {
    "Apache-2.0",
    "BSD-2-Clause",
    "BSD-3-Clause",
    "FTL",
    "GPL-2.0-or-later",
    "ISC",
    "MIT",
    "MIT-CMU",
    "MPL-2.0",
    "PSF-2.0",
    "Zlib",
}

CLASSIFIER_LICENSES = {
    "License :: OSI Approved :: Apache Software License": "Apache-2.0",
    "License :: OSI Approved :: BSD License": (
        "BSD license (classifier; exact variant not stated)"
    ),
    "License :: OSI Approved :: ISC License (ISCL)": "ISC",
    "License :: OSI Approved :: MIT License": "MIT",
    "License :: OSI Approved :: Mozilla Public License 2.0 (MPL 2.0)": "MPL-2.0",
}

PILLOW_LICENSE_SUMMARIES = {
    "brotli": "MIT-style terms in the Pillow wheel license bundle",
    "freetype": "FTL OR GPL-2.0-or-later; see the Pillow wheel license bundle",
    "harfbuzz": "MIT-style terms in the Pillow wheel license bundle",
    "lcms2": "MIT-style terms in the Pillow wheel license bundle",
    "libavif": "BSD-style terms in the Pillow wheel license bundle",
    "libjpeg-turbo": (
        "IJG, BSD-style, and zlib terms; see the Pillow wheel license bundle"
    ),
    "libpng": "libpng terms in the Pillow wheel license bundle",
    "libwebp": "BSD-style terms in the Pillow wheel license bundle",
    "openjpeg": "BSD-style terms in the Pillow wheel license bundle",
    "tiff": "libtiff terms in the Pillow wheel license bundle",
    "xz": "Mixed component terms; see the Pillow wheel license bundle",
    "zlib-ng": "zlib terms in the Pillow wheel license bundle",
}

EXTERNAL_ENGINES = (
    ("qpdf CLI", "Apache-2.0"),
    ("Tesseract", "Apache-2.0"),
    ("OCRmyPDF", "MPL-2.0"),
    (
        "Ghostscript",
        "AGPL/commercial dual-licensing reported; exact current terms unverified",
    ),
    ("LibreOffice", "MPL-2.0 and bundled-component terms"),
    ("Pandoc", "GPL-2.0-or-later reported; exact current terms unverified"),
    ("Typst", "Apache-2.0 reported"),
    ("veraPDF", "GPL/MPL dual-licensing reported; exact SPDX choice unverified"),
)


@dataclass(frozen=True)
class LicenseEvidence:
    display: str
    choice: dict[str, Any]
    source: str


def canonicalize(name: str) -> str:
    """Return the normalized Python distribution name used by lock lookups."""
    return re.sub(r"[-_.]+", "-", name).lower()


def pypi_purl(name: str, version: str) -> str:
    normalized = quote(canonicalize(name), safe="-._~")
    encoded_version = quote(version, safe="-._~+")
    return f"pkg:pypi/{normalized}@{encoded_version}"


def generic_purl(name: str, version: str) -> str:
    normalized = quote(canonicalize(name), safe="-._~")
    encoded_version = quote(version, safe="-._~+")
    return f"pkg:generic/{normalized}@{encoded_version}"


def collapse(value: str) -> str:
    return " ".join(value.split())


def markdown_cell(value: str) -> str:
    return collapse(value).replace("|", r"\|")


def load_lock(path: Path = LOCK_PATH) -> dict[str, tuple[str, str]]:
    """Read a strict name==version lock without resolving or downloading."""
    locked: dict[str, tuple[str, str]] = {}
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = LOCK_RE.fullmatch(line)
        if match is None:
            raise ValueError(f"{path}:{line_number}: expected an exact name==version pin")
        source_name, version = match.groups()
        normalized = canonicalize(source_name)
        if normalized in locked:
            raise ValueError(f"{path}:{line_number}: duplicate distribution {source_name!r}")
        locked[normalized] = (source_name, version)
    return locked


def load_pyproject(path: Path = PYPROJECT_PATH) -> dict[str, Any]:
    with path.open("rb") as stream:
        return tomllib.load(stream)


def installed_distributions() -> dict[str, metadata.Distribution]:
    result: dict[str, metadata.Distribution] = {}
    for distribution in metadata.distributions():
        name = distribution.metadata.get("Name")
        if name:
            result[canonicalize(name)] = distribution
    return result


def validate_locked_environment(
    locked: dict[str, tuple[str, str]],
    installed: dict[str, metadata.Distribution],
) -> None:
    problems: list[str] = []
    for name, (source_name, expected_version) in sorted(locked.items()):
        distribution = installed.get(name)
        if distribution is None:
            problems.append(f"{source_name}=={expected_version} is not installed")
        elif distribution.version != expected_version:
            problems.append(
                f"{source_name} is locked at {expected_version} but "
                f"{distribution.version} is installed"
            )
    if problems:
        details = "\n  - ".join(problems)
        raise RuntimeError(
            "Installed distributions do not match requirements-lock.txt; "
            f"license evidence would be unreliable:\n  - {details}"
        )


def requirement_name(requirement: str) -> str | None:
    match = REQUIREMENT_NAME_RE.match(requirement)
    return canonicalize(match.group(1)) if match else None


def dependency_names(
    distribution: metadata.Distribution | None,
    locked: dict[str, tuple[str, str]],
) -> set[str]:
    """Return installed locked base dependencies, excluding optional extras.

    Marker evaluation is intentionally conservative and offline. Requirements
    conditional on an extra are excluded because LocalDocForge does not request
    dependency extras in pyproject.toml. Other conditional packages are included
    only when they are present in this environment's lock.
    """
    dependencies: set[str] = set()
    if distribution is None:
        return dependencies
    for requirement in distribution.requires or ():
        _, separator, marker = requirement.partition(";")
        if separator and re.search(r"\bextra\b", marker, flags=re.IGNORECASE):
            continue
        name = requirement_name(requirement)
        if name in locked:
            dependencies.add(name)
    return dependencies


def dependency_closure(
    seeds: set[str],
    locked: dict[str, tuple[str, str]],
    installed: dict[str, metadata.Distribution],
) -> set[str]:
    found: set[str] = set()
    queue: deque[str] = deque(sorted(seeds))
    while queue:
        name = queue.popleft()
        if name in found or name not in locked:
            continue
        found.add(name)
        queue.extend(sorted(dependency_names(installed.get(name), locked) - found))
    return found


def dependency_categories(
    project: dict[str, Any],
    locked: dict[str, tuple[str, str]],
    installed: dict[str, metadata.Distribution],
) -> tuple[dict[str, str], set[str], set[str]]:
    project_table = project["project"]
    runtime_direct = {
        name
        for requirement in project_table.get("dependencies", ())
        if (name := requirement_name(requirement)) is not None
    }
    dev_direct = {
        name
        for requirement in project_table.get("optional-dependencies", {}).get("dev", ())
        if (name := requirement_name(requirement)) is not None
    }
    runtime = dependency_closure(runtime_direct, locked, installed)
    dev = dependency_closure(dev_direct, locked, installed) - runtime
    categories: dict[str, str] = {}
    for name in locked:
        if name in runtime:
            categories[name] = "runtime"
        elif name in dev:
            categories[name] = "development"
        else:
            categories[name] = "lock-only"
    return categories, runtime_direct, dev_direct


def license_evidence(distribution: metadata.Distribution | None) -> LicenseEvidence:
    if distribution is None:
        text = "NOASSERTION - distribution is not installed in the generation environment"
        return LicenseEvidence(
            text,
            {"license": {"name": text}},
            "installed metadata unavailable",
        )

    license_files = distribution.metadata.get_all("License-File") or []
    entry_word = "entry" if len(license_files) == 1 else "entries"
    file_note = f"; {len(license_files)} License-File {entry_word}"
    expression = distribution.metadata.get("License-Expression")
    if expression:
        normalized = collapse(expression)
        return LicenseEvidence(
            normalized,
            {"expression": normalized},
            "installed METADATA License-Expression" + file_note,
        )

    raw_license = distribution.metadata.get("License")
    if raw_license and collapse(raw_license).upper() not in {"UNKNOWN", "N/A"}:
        normalized = collapse(raw_license)
        choice = (
            {"license": {"id": normalized}}
            if normalized in SPDX_IDS
            else {"license": {"name": normalized}}
        )
        return LicenseEvidence(
            normalized,
            choice,
            "installed METADATA License field" + file_note,
        )

    for classifier in distribution.metadata.get_all("Classifier") or []:
        if classifier in CLASSIFIER_LICENSES:
            normalized = CLASSIFIER_LICENSES[classifier]
            choice = (
                {"license": {"id": normalized}}
                if normalized in SPDX_IDS
                else {"license": {"name": normalized}}
            )
            return LicenseEvidence(
                normalized,
                choice,
                "installed METADATA license classifier" + file_note,
            )

    text = "NOASSERTION - installed metadata has no normalized license field"
    return LicenseEvidence(
        text,
        {"license": {"name": text}},
        "installed METADATA" + file_note,
    )


def distribution_file(
    distribution: metadata.Distribution | None,
    suffix: str,
) -> Path | None:
    if distribution is None:
        return None
    normalized_suffix = suffix.replace("\\", "/").lower()
    for package_path in distribution.files or ():
        if str(package_path).replace("\\", "/").lower().endswith(normalized_suffix):
            return Path(distribution.locate_file(package_path))
    return None


def qpdf_version() -> str:
    try:
        pikepdf = importlib.import_module("pikepdf")
        version = getattr(pikepdf, "__libqpdf_version__", None)
        return str(version) if version else "unknown"
    except Exception:
        return "unknown"


def pdfium_version(distribution: metadata.Distribution | None) -> str:
    version_path = distribution_file(distribution, "pypdfium2_raw/version.json")
    if version_path is None:
        return "unknown"
    try:
        values = json.loads(version_path.read_text(encoding="utf-8"))
        return ".".join(str(values[key]) for key in ("major", "minor", "build", "patch"))
    except (KeyError, OSError, TypeError, ValueError):
        return "unknown"


def pillow_codecs(distribution: metadata.Distribution | None) -> list[tuple[str, str]]:
    license_path = distribution_file(distribution, "licenses/LICENSE")
    if license_path is None:
        return []
    try:
        lines = license_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    codecs: list[tuple[str, str]] = []
    for line in lines:
        match = PILLOW_HEADING_RE.fullmatch(line.strip())
        if match:
            codecs.append((match.group(1), match.group(2)))
    return sorted(set(codecs), key=lambda item: (canonicalize(item[0]), item[1]))


def property_list(values: dict[str, str]) -> list[dict[str, str]]:
    return [{"name": key, "value": values[key]} for key in sorted(values)]


def native_component(
    name: str,
    version: str,
    license_name: str,
    bundled_by: str,
    evidence: str,
) -> dict[str, Any]:
    return {
        "type": "library",
        "bom-ref": generic_purl(name, version),
        "name": name,
        "version": version,
        "scope": "required",
        "licenses": [
            {
                "license": (
                    {"id": license_name}
                    if license_name in SPDX_IDS
                    else {"name": license_name}
                )
            }
        ],
        "purl": generic_purl(name, version),
        "properties": property_list(
            {
                "localdocforge:bundledBy": bundled_by,
                "localdocforge:componentCategory": "bundled-native",
                "localdocforge:licenseEvidence": evidence,
            }
        ),
    }


def locked_component(
    name: str,
    source_name: str,
    version: str,
    category: str,
    distribution: metadata.Distribution | None,
) -> dict[str, Any]:
    license_info = license_evidence(distribution)
    installed_version = distribution.version if distribution is not None else "not installed"
    display_name = distribution.metadata.get("Name", source_name) if distribution else source_name
    return {
        "type": "library",
        "bom-ref": pypi_purl(name, version),
        "name": display_name,
        "version": version,
        "scope": "required" if category == "runtime" else "optional",
        "licenses": [license_info.choice],
        "purl": pypi_purl(name, version),
        "properties": property_list(
            {
                "localdocforge:dependencyCategory": category,
                "localdocforge:installedVersion": installed_version,
                "localdocforge:licenseEvidence": license_info.source,
                "localdocforge:lockEvidence": (
                    "requirements-lock.txt exact version pin; no artifact hash"
                ),
            }
        ),
    }


def build_sbom(
    project: dict[str, Any],
    locked: dict[str, tuple[str, str]],
    installed: dict[str, metadata.Distribution],
    categories: dict[str, str],
    runtime_direct: set[str],
) -> dict[str, Any]:
    components = [
        locked_component(name, source_name, version, categories[name], installed.get(name))
        for name, (source_name, version) in sorted(locked.items())
    ]

    qpdf = native_component(
        "qpdf",
        qpdf_version(),
        "Apache-2.0",
        "pikepdf",
        "pikepdf wheel licenses/licenses-for-wheels.txt",
    )
    pdfium = native_component(
        "PDFium",
        pdfium_version(installed.get("pypdfium2")),
        "BSD-style plus bundled dependency terms",
        "pypdfium2",
        "pypdfium2 wheel BUILD_LICENSES/pdfium.txt and related BUILD_LICENSES files",
    )
    pillow_version = locked.get("pillow", ("Pillow", "unknown"))[1]
    pillow_bundle = native_component(
        "Pillow codec bundle",
        pillow_version,
        "Multiple licenses; see the Pillow wheel license bundle",
        "Pillow",
        "Pillow wheel licenses/LICENSE",
    )
    codec_components = [
        native_component(
            name,
            version,
            PILLOW_LICENSE_SUMMARIES.get(
                canonicalize(name),
                "License text present in the Pillow wheel license bundle",
            ),
            "Pillow codec bundle",
            f"Pillow wheel licenses/LICENSE section {name}-{version}",
        )
        for name, version in pillow_codecs(installed.get("pillow"))
    ]
    components.extend([qpdf, pdfium, pillow_bundle, *codec_components])
    components.sort(key=lambda item: (item["name"].lower(), item["version"], item["bom-ref"]))

    root_name = project["project"]["name"]
    root_version = project["project"]["version"]
    root_ref = pypi_purl(root_name, root_version)
    dependencies: dict[str, set[str]] = {
        component["bom-ref"]: set() for component in components
    }
    dependencies[root_ref] = {
        pypi_purl(name, locked[name][1]) for name in runtime_direct if name in locked
    }
    for name, (_, version) in locked.items():
        distribution = installed.get(name)
        dependencies[pypi_purl(name, version)].update(
            pypi_purl(dependency, locked[dependency][1])
            for dependency in dependency_names(distribution, locked)
        )
    dependencies[pypi_purl("pikepdf", locked["pikepdf"][1])].add(qpdf["bom-ref"])
    dependencies[pypi_purl("pypdfium2", locked["pypdfium2"][1])].add(pdfium["bom-ref"])
    dependencies[pypi_purl("pillow", locked["pillow"][1])].add(pillow_bundle["bom-ref"])
    dependencies[pillow_bundle["bom-ref"]].update(
        component["bom-ref"] for component in codec_components
    )

    return {
        "$schema": "http://cyclonedx.org/schema/bom-1.6.schema.json",
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "version": 1,
        "metadata": {
            "tools": {
                "components": [
                    {
                        "type": "application",
                        "name": "LocalDocForge offline release-artifact generator",
                        "version": "1",
                    }
                ]
            },
            "component": {
                "type": "application",
                "bom-ref": root_ref,
                "name": root_name,
                "version": root_version,
                "licenses": [{"license": {"id": "MIT"}}],
                "purl": root_ref,
                "properties": property_list(
                    {
                        "localdocforge:artifactGeneration": "offline and deterministic",
                        "localdocforge:licenseFile": "LICENSE",
                    }
                ),
            },
            "properties": property_list(
                {
                    "localdocforge:advisoryReview": (
                        "not performed; network permission required"
                    ),
                    "localdocforge:lockHashes": "absent",
                    "localdocforge:profiles": (
                        "Lite/Standard/Full profiles are not implemented"
                    ),
                    "localdocforge:sourceEvidence": (
                        "requirements-lock.txt plus installed importlib.metadata "
                        "and wheel license files"
                    ),
                }
            ),
        },
        "components": components,
        "dependencies": [
            {"ref": reference, "dependsOn": sorted(dependencies[reference])}
            for reference in sorted(dependencies)
        ],
    }


def notice_table(
    names: list[str],
    locked: dict[str, tuple[str, str]],
    installed: dict[str, metadata.Distribution],
) -> list[str]:
    lines = ["| Package | Version | License | Local evidence |", "|---|---:|---|---|"]
    for name in names:
        source_name, version = locked[name]
        distribution = installed.get(name)
        info = license_evidence(distribution)
        display_name = (
            distribution.metadata.get("Name", source_name)
            if distribution
            else source_name
        )
        lines.append(
            "| "
            + " | ".join(
                markdown_cell(value)
                for value in (display_name, version, info.display, info.source)
            )
            + " |"
        )
    return lines


def build_notices(
    project: dict[str, Any],
    locked: dict[str, tuple[str, str]],
    installed: dict[str, metadata.Distribution],
    categories: dict[str, str],
) -> str:
    runtime = sorted(name for name, category in categories.items() if category == "runtime")
    development = sorted(
        name for name, category in categories.items() if category == "development"
    )
    lock_only = sorted(
        name for name, category in categories.items() if category == "lock-only"
    )
    pillow_entries = pillow_codecs(installed.get("pillow"))
    qpdf = qpdf_version()
    pdfium = pdfium_version(installed.get("pypdfium2"))
    build_requirements = project.get("build-system", {}).get("requires", ())

    lines = [
        "# Third-Party Notices",
        "",
        "This file is generated by `scripts/generate_release_artifacts.py`. "
        "Do not edit it by hand.",
        "Generation is offline and deterministic: exact pins come from "
        "`requirements-lock.txt`;",
        "license labels and license-file counts come from installed distribution metadata.",
        "A label is evidence for review, not a substitute for retaining the upstream "
        "license text.",
        "",
        "## Runtime Python distributions",
        "",
        *notice_table(runtime, locked, installed),
        "",
        "## Development and test distributions",
        "",
        "These are not required by the declared runtime dependency graph, but the current "
        "README",
        "quick-start installs the unified lock and therefore installs them.",
        "",
        *notice_table(development, locked, installed),
        "",
        "## Lock-only distributions",
        "",
        "These pins are present in the environment-derived lock but are neither in the "
        "declared",
        "runtime closure nor the declared `dev` closure. They include "
        "Uvicorn-standard extras.",
        "",
        *notice_table(lock_only, locked, installed),
        "",
        "## Build requirements",
        "",
        "The build requirements are not pinned by `requirements-lock.txt` "
        "and were not resolved",
        "or license-verified by this offline generation:",
        "",
        *(f"- `{requirement}`" for requirement in build_requirements),
        "",
        "## Native components bundled by installed wheels",
        "",
        "| Component | Version | Bundled by | License evidence |",
        "|---|---:|---|---|",
        f"| qpdf | {markdown_cell(qpdf)} | pikepdf | Apache-2.0; "
        "pikepdf wheel `licenses-for-wheels.txt` |",
        f"| PDFium | {markdown_cell(pdfium)} | pypdfium2 | BSD-style and bundled "
        "dependency terms; wheel `BUILD_LICENSES` files |",
        f"| Pillow codec bundle | {locked['pillow'][1]} | Pillow | Multiple terms; "
        "Pillow wheel `licenses/LICENSE` |",
    ]
    for name, version in pillow_entries:
        summary = PILLOW_LICENSE_SUMMARIES.get(
            canonicalize(name),
            "See Pillow wheel license bundle",
        )
        lines.append(
            f"| {markdown_cell(name)} | {markdown_cell(version)} | "
            f"Pillow codec bundle | {markdown_cell(summary)} |"
        )
    lines.extend(
        [
            "",
            "The inspected Windows pikepdf wheel also reports bundled libjpeg/IJG terms "
            "and",
            "Microsoft Visual C++ Runtime redistributable terms. Standalone packaging must "
            "preserve",
            "those upstream notices. The SBOM records the specifically versioned native "
            "components",
            "that could be established from local evidence; absence of a version is never "
            "guessed.",
            "",
            "## Optional external engines not bundled by LocalDocForge",
            "",
            "The following are subprocess candidates that users may install separately. "
            "They are not",
            "contained in this repository or its Python dependency lock. The labels below "
            "are expected",
            "upstream licensing only and must be checked against the exact installed "
            "release before an",
            "engine is enabled or redistributed.",
            "",
            "| Engine | Expected upstream licensing; offline verification status |",
            "|---|---|",
        ]
    )
    lines.extend(f"| {name} | {license_name} |" for name, license_name in EXTERNAL_ENGINES)
    lines.extend(
        [
            "",
            "## Known limitations and required release checks",
            "",
            "- No vulnerability or security-advisory lookup was performed. Network access "
            "was not",
            "  authorized; every Python and native component remains advisory-unverified.",
            "- The lock has exact versions but no artifact hashes or platform markers.",
            "- License evidence is from the locally installed Windows/Python 3.14 "
            "environment and may",
            "  differ for wheels built for another operating system or architecture.",
            "- Lite, Standard, and Full installation profiles are not implemented. This "
            "notice uses",
            "  the single combined lock and must not be represented as a profile-specific "
            "SBOM.",
            "- A distributor must preserve applicable upstream license and notice files "
            "from wheels",
            "  and separately review any bundler-generated executable or installer.",
            "",
        ]
    )
    return "\n".join(lines)


def render_artifacts(root: Path = ROOT) -> dict[Path, str]:
    locked = load_lock(root / "requirements-lock.txt")
    project = load_pyproject(root / "pyproject.toml")
    installed = installed_distributions()
    validate_locked_environment(locked, installed)
    categories, runtime_direct, _ = dependency_categories(project, locked, installed)
    notices = build_notices(project, locked, installed, categories)
    sbom = build_sbom(project, locked, installed, categories, runtime_direct)
    return {
        root / "THIRD_PARTY_NOTICES.md": notices,
        root / "docs" / "SBOM.cdx.json": json.dumps(
            sbom,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
    }


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
    args = parser.parse_args(argv)
    outputs = render_artifacts()
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
