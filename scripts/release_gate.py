#!/usr/bin/env python3
"""Run LocalDocForge's local release-hardening gate.

Cross-platform CI uses the same commands. A local pass is evidence only for the
current platform and interpreter; this script never infers other runner results.
"""

from __future__ import annotations

import argparse
import copy
import gzip
import hashlib
import io
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import urllib.request
import zipfile
from email.parser import Parser
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from packaging.requirements import Requirement

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "packaging" / "release-artifact-manifest.json"
BUILD_BACKEND_REQUIREMENTS = ROOT / "requirements" / "build-backend.txt"
BUILD_BACKEND_WHEEL_NAME = "setuptools-83.0.0-py3-none-any.whl"
BUILD_BACKEND_WHEEL_URL = (
    "https://files.pythonhosted.org/packages/5d/40/"
    "e1e72872c6354b306daef1703549e8e83b4d43cfea356311bf722a043752/"
    + BUILD_BACKEND_WHEEL_NAME
)
BUILD_BACKEND_WHEEL_SHA256 = "29b23c360f22f414dc7336bb39178cc7bcbf6021ed2733cde173f09dba19abb3"
BUILD_BACKEND_RELEASE_HASHES = frozenset(
    {
        BUILD_BACKEND_WHEEL_SHA256,
        "025bccbbf0fa05b6192bc64ae1e7b16e001fd6d6d4d5de03c97b1c1ade523bef",
    }
)
BUILD_BACKEND_MAX_BYTES = 2 * 1024 * 1024
SOURCE_DATE_EPOCH = "1704067200"
ALL_STEPS = ("locks", "quality", "artifacts", "tests", "blocked-network", "build", "profiles")
EXPECTED_BASE_DEPENDENCIES = frozenset(
    {
        "pydantic",
        "pydantic-settings",
        "typer",
        "markdown-it-py",
        "pikepdf",
        "pypdfium2",
        "pdfplumber",
        "cryptography",
        "pillow",
        "pi-heif",
    }
)


def _run(
    command: list[str],
    *,
    environment: dict[str, str] | None = None,
    cwd: Path = ROOT,
) -> None:
    print(f"+ {' '.join(command)}", flush=True)
    subprocess.run(  # noqa: S603
        command,
        cwd=cwd,
        env=environment,
        check=True,
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_build_backend_lock() -> None:
    text = BUILD_BACKEND_REQUIREMENTS.read_text(encoding="utf-8")
    requirements = re.findall(
        r"(?m)^([A-Za-z0-9][A-Za-z0-9._-]*==[^\s\\]+)",
        text,
    )
    if requirements != ["setuptools==83.0.0"]:
        raise RuntimeError("build-backend lock must contain only setuptools==83.0.0")
    hashes = frozenset(re.findall(r"--hash=sha256:([0-9a-f]{64})(?:\s|$)", text))
    if hashes != BUILD_BACKEND_RELEASE_HASHES:
        raise RuntimeError("build-backend lock does not contain the audited PyPI hashes")
    if "--index-url https://pypi.org/simple" not in text:
        raise RuntimeError("build-backend lock must use the official PyPI simple index")
    if any(
        token in text
        for token in ("--extra-index-url", "--trusted-host", "--find-links", "http://")
    ):
        raise RuntimeError("build-backend lock contains an unapproved package source")
    with (ROOT / "pyproject.toml").open("rb") as stream:
        build_requires = tomllib.load(stream)["build-system"]["requires"]
    if build_requires != ["setuptools==83.0.0"]:
        raise RuntimeError("pyproject build backend and hash lock disagree")


def _prepare_build_backend_wheelhouse(wheelhouse: Path) -> Path:
    """Download the audited backend wheel and verify it before isolated builds."""
    _validate_build_backend_lock()
    parsed = urlparse(BUILD_BACKEND_WHEEL_URL)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "files.pythonhosted.org"
        or Path(parsed.path).name != BUILD_BACKEND_WHEEL_NAME
    ):
        raise RuntimeError("build-backend wheel URL is not the audited official PyPI file")

    wheelhouse.mkdir(parents=True, exist_ok=False)
    destination = wheelhouse / BUILD_BACKEND_WHEEL_NAME
    request = urllib.request.Request(  # noqa: S310 - validated HTTPS PyPI URL above
        BUILD_BACKEND_WHEEL_URL,
        headers={"User-Agent": "LocalDocForge/0.1 release-gate"},
    )
    print(f"+ download {BUILD_BACKEND_WHEEL_URL}", flush=True)
    digest = hashlib.sha256()
    total = 0
    try:
        with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
            final = urlparse(response.geturl())
            if final.scheme != "https" or final.hostname != "files.pythonhosted.org":
                raise RuntimeError("build-backend download redirected outside official PyPI")
            declared = response.headers.get("Content-Length")
            if declared is not None and int(declared) > BUILD_BACKEND_MAX_BYTES:
                raise RuntimeError("build-backend wheel exceeds the download limit")
            with destination.open("xb") as output:
                while chunk := response.read(64 * 1024):
                    total += len(chunk)
                    if total > BUILD_BACKEND_MAX_BYTES:
                        raise RuntimeError("build-backend wheel exceeds the download limit")
                    digest.update(chunk)
                    output.write(chunk)
        if digest.hexdigest() != BUILD_BACKEND_WHEEL_SHA256:
            raise RuntimeError("build-backend wheel SHA-256 does not match the audited lock")
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    return destination


def _source_tree_sha256(root: Path) -> str:
    """Hash only the package inputs copied by ``_stage_source``.

    Build backends may add ``build/`` and ``*.egg-info`` beneath the staged
    tree. Those derived files must not contaminate the source identity that the
    clean-install gate recomputes from the checkout.
    """
    files = [root / name for name in ("LICENSE", "README.md", "pyproject.toml")]
    files.extend(
        sorted(
            path
            for path in (root / "src" / "localdocforge").rglob("*")
            if path.is_file()
            and "__pycache__" not in path.parts
            and path.suffix not in {".pyc", ".pyo"}
            and path.name != ".DS_Store"
        )
    )
    digest = hashlib.sha256()
    for path in files:
        relative = path.relative_to(root).as_posix().encode("utf-8")
        payload = path.read_bytes()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return digest.hexdigest()


def _build_environment(build_backend_wheelhouse: Path) -> dict[str, str]:
    if not build_backend_wheelhouse.is_dir():
        raise RuntimeError("verified build-backend wheelhouse is missing")
    environment = os.environ.copy()
    for name in tuple(environment):
        if name.startswith("PIP_"):
            environment.pop(name)
    environment["SOURCE_DATE_EPOCH"] = SOURCE_DATE_EPOCH
    environment["PYTHONHASHSEED"] = "0"
    environment["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    environment["PIP_CONFIG_FILE"] = os.devnull
    environment["PIP_NO_INDEX"] = "1"
    environment["PIP_FIND_LINKS"] = str(build_backend_wheelhouse.resolve())
    environment["PIP_NO_CACHE_DIR"] = "1"
    environment["PIP_ONLY_BINARY"] = ":all:"
    return environment


def _find_artifacts(directory: Path) -> tuple[Path, Path]:
    wheels = sorted(directory.glob("localdocforge-*.whl"))
    sdists = sorted(directory.glob("localdocforge-*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise RuntimeError(
            f"expected one wheel and one sdist in {directory}; "
            f"found wheels={wheels}, sdists={sdists}"
        )
    return wheels[0], sdists[0]


def _canonicalize_sdist(path: Path) -> None:
    """Rewrite a valid sdist with stable ordering, metadata, and gzip time."""
    records: list[tuple[tarfile.TarInfo, bytes | None]] = []
    with tarfile.open(path, "r:gz") as source:
        for member in source.getmembers():
            payload: bytes | None = None
            if member.isfile():
                stream = source.extractfile(member)
                if stream is None:
                    raise RuntimeError(f"could not read regular sdist member {member.name!r}")
                payload = stream.read()
            records.append((copy.copy(member), payload))

    temporary = path.with_name(f".{path.name}.canonical.tmp")
    temporary.unlink(missing_ok=True)
    try:
        with temporary.open("wb") as raw:
            with gzip.GzipFile(
                filename="",
                mode="wb",
                compresslevel=9,
                fileobj=raw,
                mtime=int(SOURCE_DATE_EPOCH),
            ) as compressed:
                with tarfile.open(
                    fileobj=compressed,
                    mode="w",
                    format=tarfile.PAX_FORMAT,
                ) as target:
                    for member, payload in sorted(records, key=lambda record: record[0].name):
                        member.uid = 0
                        member.gid = 0
                        member.uname = ""
                        member.gname = ""
                        member.mtime = int(SOURCE_DATE_EPOCH)
                        member.pax_headers = {}
                        member.mode = 0o755 if member.isdir() else 0o644
                        if member.isfile():
                            if payload is None:
                                raise AssertionError("regular sdist member lost its payload")
                            member.size = len(payload)
                            target.addfile(member, io.BytesIO(payload))
                        else:
                            member.size = 0
                            target.addfile(member)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _stage_source(destination: Path) -> Path:
    """Copy only package inputs into a new build workspace.

    Building directly from the checkout lets setuptools reuse and mutate
    ``build/`` and ``*.egg-info`` state. A deliberately small stage also keeps
    virtual environments, prompt material, and unrelated untracked files out
    of the build context.
    """
    destination.mkdir(parents=True, exist_ok=False)
    for name in ("LICENSE", "README.md", "pyproject.toml"):
        shutil.copy2(ROOT / name, destination / name)
    shutil.copytree(
        ROOT / "src" / "localdocforge",
        destination / "src" / "localdocforge",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo", ".DS_Store"),
    )
    return destination


def _build(
    output: Path,
    source: Path = ROOT,
    *,
    build_backend_wheelhouse: Path,
) -> tuple[Path, Path]:
    output.mkdir(parents=True, exist_ok=False)
    for mode in ("--sdist", "--wheel"):
        _run(
            [
                sys.executable,
                "-m",
                "build",
                "--installer",
                "pip",
                mode,
                "--outdir",
                str(output),
                str(source),
            ],
            environment=_build_environment(build_backend_wheelhouse),
        )
    wheel, sdist = _find_artifacts(output)
    _canonicalize_sdist(sdist)
    _run([sys.executable, "-m", "twine", "check", str(wheel), str(sdist)])
    return wheel, sdist


def _wheel_metadata(wheel: Path) -> tuple[Any, list[str]]:
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        metadata_names = [name for name in names if name.endswith(".dist-info/METADATA")]
        wheel_names = [name for name in names if name.endswith(".dist-info/WHEEL")]
        if len(metadata_names) != 1 or len(wheel_names) != 1:
            raise RuntimeError("wheel has ambiguous dist-info metadata")
        metadata = Parser().parsestr(archive.read(metadata_names[0]).decode("utf-8"))
        wheel_text = archive.read(wheel_names[0]).decode("utf-8")
    if "Tag: py3-none-any" not in wheel_text:
        raise RuntimeError("wheel is not tagged as the expected pure Python py3-none-any artifact")
    if any(name.startswith(("tests/", "docs/", "LocalDocForge_")) for name in names):
        raise RuntimeError("wheel contains repository-only tests, docs, or prompt material")
    return metadata, names


def _validate_metadata(wheel: Path) -> None:
    metadata, _ = _wheel_metadata(wheel)
    if metadata["Requires-Python"] not in {">=3.12,<3.15", "<3.15,>=3.12"}:
        raise RuntimeError(f"unexpected Requires-Python: {metadata['Requires-Python']!r}")
    if set(metadata.get_all("Provides-Extra") or ()) != {"dev", "full", "lite", "standard"}:
        raise RuntimeError("wheel does not declare the audited install-profile extras")

    requirements = [Requirement(value) for value in metadata.get_all("Requires-Dist") or ()]
    base = {item.name.lower() for item in requirements if item.marker is None}
    if base != EXPECTED_BASE_DEPENDENCIES:
        raise RuntimeError(f"wheel base dependency drift: {sorted(base)}")

    by_extra: dict[str, set[str]] = {name: set() for name in ("lite", "standard", "full", "dev")}
    for extra in by_extra:
        environment = {"extra": extra}
        by_extra[extra] = {
            item.name.lower()
            for item in requirements
            if item.marker is not None and item.marker.evaluate(environment)
        }
    if by_extra["lite"]:
        raise RuntimeError("lite extra must remain an explicit alias for the base dependency set")
    standard_direct = {"fastapi", "uvicorn", "python-multipart"}
    if by_extra["standard"] != standard_direct:
        raise RuntimeError(f"standard direct dependencies drifted: {sorted(by_extra['standard'])}")
    if by_extra["full"] != standard_direct | {"pypdf"}:
        raise RuntimeError(f"full direct dependencies drifted: {sorted(by_extra['full'])}")
    if (
        not (by_extra["full"] | {"pytest", "reportlab", "ruff", "httpx", "mypy", "build", "twine"})
        <= by_extra["dev"]
    ):
        raise RuntimeError("dev extra no longer includes Full plus all release/test tools")


def _sdist_members(sdist: Path) -> list[str]:
    with tarfile.open(sdist, "r:gz") as archive:
        return sorted(member.name for member in archive.getmembers() if member.isfile())


def _platform_key() -> str:
    """Identity key for artifacts built on this host.

    Byte-identical artifacts are platform-scoped: package METADATA newlines,
    zip external attributes, and deflate output all legitimately differ
    between build hosts, so each platform records its own canonical hashes.
    """
    return f"{platform.system()}-{platform.machine()}"


def _artifact_record(wheel: Path, sdist: Path, source: Path) -> dict[str, Any]:
    _, wheel_members = _wheel_metadata(wheel)
    return {
        "source_date_epoch": int(SOURCE_DATE_EPOCH),
        "source_tree_sha256": _source_tree_sha256(source),
        "artifacts": {
            wheel.name: {
                "sha256": _sha256(wheel),
                "size": wheel.stat().st_size,
                "members": sorted(name for name in wheel_members if not name.endswith("/")),
            },
            sdist.name: {
                "sha256": _sha256(sdist),
                "size": sdist.stat().st_size,
                "members": _sdist_members(sdist),
            },
        },
    }


def _check_or_update_manifest(
    record: dict[str, Any],
    update: bool,
    *,
    allow_unrecorded: bool = False,
) -> None:
    key = _platform_key()
    if update:
        platforms: dict[str, Any] = {}
        if MANIFEST_PATH.is_file():
            loaded = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
            if loaded.get("schema") == 3 and isinstance(loaded.get("platforms"), dict):
                platforms = loaded["platforms"]
            elif loaded.get("schema") == 2:
                # The flat schema-2 manifest was only ever generated on the
                # Windows x64 release workstation; carry it under that key.
                platforms = {
                    "Windows-AMD64": {
                        field: loaded[field]
                        for field in ("source_date_epoch", "source_tree_sha256", "artifacts")
                        if field in loaded
                    }
                }
        platforms[key] = record
        rendered = (
            json.dumps({"schema": 3, "platforms": platforms}, indent=2, sort_keys=True) + "\n"
        )
        MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
        MANIFEST_PATH.write_text(rendered, encoding="utf-8")
        try:
            shown = MANIFEST_PATH.relative_to(ROOT)
        except ValueError:  # tests point MANIFEST_PATH outside the repository
            shown = MANIFEST_PATH
        print(f"updated {shown} for {key}")
        return
    if not MANIFEST_PATH.is_file():
        raise RuntimeError(
            f"{MANIFEST_PATH} is missing; run the gate once with --update-artifact-manifest"
        )
    loaded = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    if loaded.get("schema") != 3 or not isinstance(loaded.get("platforms"), dict):
        raise RuntimeError(
            "release artifact manifest schema is stale; regenerate it with "
            "--update-artifact-manifest"
        )
    recorded = loaded["platforms"].get(key)
    if recorded is None:
        message = (
            f"no recorded release identity for platform {key}; byte-identical artifacts "
            "are platform-scoped (package metadata newlines, archive attributes, and "
            "deflate output differ across build hosts)"
        )
        if allow_unrecorded:
            print(f"NOTICE: {message}; comparison skipped (--allow-unrecorded-platform)")
            return
        raise RuntimeError(
            message
            + "; run --update-artifact-manifest on this platform or pass "
            "--allow-unrecorded-platform"
        )
    expected = json.dumps(recorded, indent=2, sort_keys=True)
    rendered = json.dumps(record, indent=2, sort_keys=True)
    if expected != rendered:
        raise RuntimeError(
            "release artifact drift detected; inspect the package changes and, if intentional, "
            "rerun with --update-artifact-manifest"
        )


def _rebuild_sdist(
    sdist: Path,
    output: Path,
    *,
    build_backend_wheelhouse: Path,
) -> Path:
    extract_root = output.parent / "sdist-source"
    extract_root.mkdir()
    with tarfile.open(sdist, "r:gz") as archive:
        archive.extractall(extract_root, filter="data")  # noqa: S202
    roots = [path for path in extract_root.iterdir() if path.is_dir()]
    if len(roots) != 1:
        raise RuntimeError(f"sdist did not contain one source root: {roots}")
    wheel_output = output
    wheel_output.mkdir()
    _run(
        [
            sys.executable,
            "-m",
            "build",
            "--installer",
            "pip",
            "--wheel",
            "--outdir",
            str(wheel_output),
            str(roots[0]),
        ],
        environment=_build_environment(build_backend_wheelhouse),
    )
    wheels = sorted(wheel_output.glob("localdocforge-*.whl"))
    if len(wheels) != 1:
        raise RuntimeError("sdist wheel rebuild did not produce exactly one wheel")
    return wheels[0]


def _build_gate(
    update_manifest: bool,
    dist_directory: Path | None = None,
    allow_unrecorded: bool = False,
) -> Path:
    with tempfile.TemporaryDirectory(prefix="ldf-release-build-") as temp:
        root = Path(temp)
        build_backend_wheelhouse = root / "build-backend-wheelhouse"
        _prepare_build_backend_wheelhouse(build_backend_wheelhouse)
        first_source = _stage_source(root / "source-first")
        second_source = _stage_source(root / "source-second")
        first_wheel, first_sdist = _build(
            root / "first",
            first_source,
            build_backend_wheelhouse=build_backend_wheelhouse,
        )
        second_wheel, second_sdist = _build(
            root / "second",
            second_source,
            build_backend_wheelhouse=build_backend_wheelhouse,
        )
        if _sha256(first_wheel) != _sha256(second_wheel):
            raise RuntimeError("two clean wheel builds were not byte-for-byte reproducible")
        if _sha256(first_sdist) != _sha256(second_sdist):
            raise RuntimeError("two clean sdist builds were not byte-for-byte reproducible")
        rebuilt_wheel = _rebuild_sdist(
            first_sdist,
            root / "from-sdist",
            build_backend_wheelhouse=build_backend_wheelhouse,
        )
        if _sha256(first_wheel) != _sha256(rebuilt_wheel):
            raise RuntimeError("wheel rebuilt from the sdist differs from the direct clean wheel")
        _validate_metadata(first_wheel)
        _check_or_update_manifest(
            _artifact_record(first_wheel, first_sdist, first_source),
            update_manifest,
            allow_unrecorded=allow_unrecorded,
        )

        if dist_directory is None:
            retained = Path(tempfile.mkdtemp(prefix="ldf-release-artifacts-"))
        else:
            retained = dist_directory.resolve()
            retained.mkdir(parents=True, exist_ok=True)
            if any(retained.iterdir()):
                raise RuntimeError(f"refusing to overwrite non-empty dist directory: {retained}")
        retained_wheel = retained / first_wheel.name
        retained_sdist = retained / first_sdist.name
        shutil.copy2(first_wheel, retained_wheel)
        shutil.copy2(first_sdist, retained_sdist)
        return retained_wheel


def _quality_gate() -> None:
    _run([sys.executable, "-m", "ruff", "check", "src", "tests", "scripts"])
    _run([sys.executable, "-m", "mypy"])
    # Static portability: analyze the same sources as the other supported
    # platforms see them, so a Windows-only attribute cannot reach CI unseen.
    _run([sys.executable, "-m", "mypy", "--platform", "linux"])
    _run([sys.executable, "-m", "mypy", "--platform", "darwin"])
    _run(["git", "diff", "--check"])
    _run([sys.executable, "-m", "pip", "check"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--steps", nargs="+", choices=ALL_STEPS, default=list(ALL_STEPS))
    parser.add_argument("--update-artifact-manifest", action="store_true")
    parser.add_argument(
        "--allow-unrecorded-platform",
        action="store_true",
        help="Tolerate a manifest with no identity recorded for this build platform "
        "(comparison is skipped with a printed notice; reproducibility checks still run).",
    )
    parser.add_argument("--profile-evidence", type=Path)
    parser.add_argument(
        "--dist-dir",
        type=Path,
        help="retain the canonical wheel and sdist in this empty directory",
    )
    args = parser.parse_args(argv)
    steps = set(args.steps)
    full_release_gate = steps == set(ALL_STEPS)
    if args.dist_dir is not None and not ({"build", "profiles"} & steps):
        parser.error("--dist-dir requires the build or profiles step")

    if "locks" in steps:
        _run([sys.executable, str(ROOT / "scripts" / "lock_profiles.py"), "--check"])
    if "quality" in steps:
        _quality_gate()
    if "artifacts" in steps:
        _run([sys.executable, str(ROOT / "scripts" / "generate_release_artifacts.py"), "--check"])
    if "tests" in steps:
        _run([sys.executable, "-m", "pytest", "tests", "-q"])
    if "blocked-network" in steps:
        _run([sys.executable, str(ROOT / "scripts" / "run_blocked_network.py")])

    wheel: Path | None = None
    try:
        if "build" in steps or "profiles" in steps:
            wheel = _build_gate(
                args.update_artifact_manifest,
                args.dist_dir,
                args.allow_unrecorded_platform,
            )
        if "profiles" in steps:
            if wheel is None:
                raise AssertionError("profile step requires a built wheel")
            command = [
                sys.executable,
                str(ROOT / "scripts" / "profile_matrix.py"),
                "--wheel-dir",
                str(wheel.parent),
                "--install-source",
            ]
            if args.allow_unrecorded_platform:
                command.append("--allow-unrecorded-platform")
            if full_release_gate:
                command.append("--full-tests")
            if args.profile_evidence:
                command.extend(["--evidence", str(args.profile_evidence)])
            _run(command)
    finally:
        if wheel is not None and args.dist_dir is None:
            shutil.rmtree(wheel.parent, ignore_errors=True)

    print("release gate passed for this local platform/interpreter only")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
