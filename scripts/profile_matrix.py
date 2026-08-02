#!/usr/bin/env python3
"""Create clean environments and smoke/uninstall every install profile."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
LOCK_DIR = ROOT / "requirements" / "locks"
MANIFEST_PATH = ROOT / "packaging" / "release-artifact-manifest.json"
UV_VERSION = "0.11.26"
PROFILES = ("base", "lite", "standard", "full")
CHECKSUM_RE = re.compile(r"^([0-9a-fA-F]{64}) (?: |\*)(.+)$")


def _uv_executable() -> str:
    configured = os.environ.get("LDF_UV")
    candidates = [
        configured,
        shutil.which("uv"),
        str(ROOT / ".venv" / ("Scripts/uv.exe" if os.name == "nt" else "bin/uv")),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            result = subprocess.run(  # noqa: S603
                [candidate, "--version"],
                check=True,
                capture_output=True,
                text=True,
            )
            if result.stdout.strip().split()[1] != UV_VERSION:
                raise RuntimeError(f"uv {UV_VERSION} is required: {result.stdout.strip()}")
            return candidate
    raise RuntimeError("uv 0.11.26 is required; see requirements/uv-bootstrap.txt")


def _venv_python(environment: Path) -> Path:
    if os.name == "nt":
        return environment / "Scripts" / "python.exe"
    return environment / "bin" / "python"


def _clean_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment.pop("PYTHONHOME", None)
    environment.pop("PYTHONPATH", None)
    return environment


def _run(
    command: list[str],
    *,
    capture: bool = False,
    environment: dict[str, str] | None = None,
    cwd: Path = ROOT,
) -> subprocess.CompletedProcess[str]:
    display = " ".join(command)
    print(f"+ {display}")
    return subprocess.run(  # noqa: S603
        command,
        cwd=cwd,
        env=environment or _clean_environment(),
        check=True,
        capture_output=capture,
        text=True,
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _package_source_sha256() -> str:
    """Hash the exact root inputs copied by ``_stage_source``."""
    files = [ROOT / name for name in ("LICENSE", "README.md", "pyproject.toml")]
    files.extend(
        sorted(
            path
            for path in (ROOT / "src" / "localdocforge").rglob("*")
            if path.is_file()
            and "__pycache__" not in path.parts
            and path.suffix not in {".pyc", ".pyo"}
            and path.name != ".DS_Store"
        )
    )
    digest = hashlib.sha256()
    for path in files:
        relative = path.relative_to(ROOT).as_posix().encode("utf-8")
        payload = path.read_bytes()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return digest.hexdigest()


def _verify_release_manifest(wheel: Path, source_digest: str) -> None:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    if manifest.get("schema") != 2:
        raise RuntimeError("release artifact manifest schema is stale")
    if manifest.get("source_tree_sha256") != source_digest:
        raise RuntimeError("package source inputs differ from the release artifact manifest")
    artifact = manifest.get("artifacts", {}).get(wheel.name)
    if not isinstance(artifact, dict) or artifact.get("sha256") != _sha256(wheel):
        raise RuntimeError("selected wheel differs from the release artifact manifest")


def _verify_checksum_file(wheel: Path, checksum_file: Path) -> None:
    expected: str | None = None
    for raw_line in checksum_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = CHECKSUM_RE.fullmatch(line)
        if match is None:
            raise RuntimeError(f"malformed checksum line in {checksum_file.name!r}")
        if Path(match.group(2)).name == wheel.name:
            if expected is not None:
                raise RuntimeError(f"duplicate checksum for {wheel.name}")
            expected = match.group(1).lower()
    if expected is None:
        raise RuntimeError(f"checksum file does not name {wheel.name}")
    actual = _sha256(wheel)
    if actual != expected:
        raise RuntimeError(f"checksum mismatch for {wheel.name}: {actual} != {expected}")


def _interpreter_identity(python: Path) -> dict[str, str]:
    code = (
        "import json,platform,sys; "
        "print(json.dumps({'implementation': platform.python_implementation(), "
        "'version': platform.python_version(), 'system': platform.system(), "
        "'platform_release': platform.release(), 'platform_version': platform.version(), "
        "'machine': platform.machine(), 'executable_name': "
        "__import__('pathlib').Path(sys.executable).name}, sort_keys=True))"
    )
    result = _run([str(python), "-I", "-c", code], capture=True)
    identity = json.loads(result.stdout)
    if not isinstance(identity, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in identity.items()
    ):
        raise RuntimeError("selected interpreter returned malformed identity evidence")
    return identity


def _source_state() -> dict[str, str | bool]:
    git = shutil.which("git")
    if git is None:
        return {"revision": "unavailable", "working_tree_changes": True}
    try:
        revision = subprocess.run(  # noqa: S603 - resolved local Git executable
            [git, "rev-parse", "HEAD"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        dirty = bool(
            subprocess.run(  # noqa: S603 - resolved local Git executable
                [git, "status", "--porcelain", "--untracked-files=all"],
                cwd=ROOT,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
        )
    except (OSError, subprocess.CalledProcessError):
        return {"revision": "unavailable", "working_tree_changes": True}
    return {"revision": revision, "working_tree_changes": dirty}


def _wheel(directory: Path) -> Path:
    wheels = sorted(directory.glob("localdocforge-*.whl"))
    if len(wheels) != 1:
        raise ValueError(f"expected exactly one LocalDocForge wheel in {directory}, found {wheels}")
    return wheels[0].resolve()


def _source_requirement(profile: str) -> str:
    return "." if profile == "base" else f".[{profile}]"


def _wheel_requirement(wheel: Path, profile: str) -> str:
    return str(wheel) if profile == "base" else f"{wheel}[{profile}]"


def _stage_source(destination: Path) -> Path:
    destination.mkdir(parents=True, exist_ok=False)
    for name in ("LICENSE", "README.md", "pyproject.toml"):
        shutil.copy2(ROOT / name, destination / name)
    shutil.copytree(
        ROOT / "src" / "localdocforge",
        destination / "src" / "localdocforge",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo", ".DS_Store"),
    )
    return destination


def _assert_uninstalled(python: Path) -> None:
    code = (
        "import importlib.util; "
        "raise SystemExit(1 if importlib.util.find_spec('localdocforge') else 0)"
    )
    _run([str(python), "-I", "-c", code])


def _profile_run(
    uv: str,
    base_python: str,
    wheel: Path,
    profile: str,
    root: Path,
    *,
    install_source: bool,
) -> dict[str, Any]:
    environment = root / profile
    _run([uv, "venv", "--python", base_python, str(environment)])
    python = _venv_python(environment)
    lock_profile = "lite" if profile == "base" else profile
    lock = LOCK_DIR / f"{lock_profile}.txt"
    _run([uv, "pip", "sync", "--python", str(python), "--require-hashes", str(lock)])

    if install_source:
        source = _stage_source(root / f"{profile}-source")
        _run(
            [
                uv,
                "pip",
                "install",
                "--python",
                str(python),
                "--no-deps",
                _source_requirement(profile),
            ],
            cwd=source,
        )
        _run([str(python), "-I", "-c", "import localdocforge"])
        _run([uv, "pip", "uninstall", "--python", str(python), "localdocforge"])
        _assert_uninstalled(python)

    _run(
        [
            uv,
            "pip",
            "install",
            "--python",
            str(python),
            "--no-deps",
            _wheel_requirement(wheel, profile),
        ]
    )
    _run([uv, "pip", "check", "--python", str(python)])
    _run(
        [str(python), "-I", str(ROOT / "scripts" / "profile_smoke.py"), "--profile", profile]
    )
    installed = _run(
        [
            str(python),
            "-I",
            "-c",
            (
                "import importlib.metadata as m, json; "
                "print(json.dumps(sorted((d.metadata['Name'], d.version) "
                "for d in m.distributions())))"
            ),
        ],
        capture=True,
    )
    _run([uv, "pip", "uninstall", "--python", str(python), "localdocforge"])
    _assert_uninstalled(python)
    return {
        "profile": profile,
        "status": "passed",
        "interpreter": _interpreter_identity(python),
        "lock": lock.name,
        "lock_sha256": _sha256(lock),
        "packages": json.loads(installed.stdout),
    }


def _full_test_run(uv: str, base_python: str, wheel: Path, root: Path) -> dict[str, str]:
    environment = root / "full-tests"
    _run([uv, "venv", "--python", base_python, str(environment)])
    python = _venv_python(environment)
    _run(
        [
            uv,
            "pip",
            "sync",
            "--python",
            str(python),
            "--require-hashes",
            str(LOCK_DIR / "dev.txt"),
        ]
    )
    _run([uv, "pip", "install", "--python", str(python), "--no-deps", str(wheel)])
    _run([uv, "pip", "check", "--python", str(python)])
    _run([str(python), "-m", "ruff", "check", "src", "tests", "scripts"])
    _run([str(python), "-m", "mypy"])
    _run([str(python), "-m", "pytest", "tests", "-q"])
    _run([str(python), str(ROOT / "scripts" / "run_blocked_network.py")])
    _run(
        [
            str(python),
            str(ROOT / "scripts" / "generate_release_artifacts.py"),
            "--check",
        ]
    )
    _run([uv, "pip", "uninstall", "--python", str(python), "localdocforge"])
    _assert_uninstalled(python)
    return {"status": "passed"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel-dir", type=Path, required=True)
    parser.add_argument("--python", default=sys.executable, help="base interpreter for clean venvs")
    parser.add_argument("--profiles", nargs="+", choices=PROFILES, default=list(PROFILES))
    parser.add_argument(
        "--install-source",
        action="store_true",
        help="also exercise pip-compatible ., .[lite], .[standard], and .[full] installs",
    )
    parser.add_argument(
        "--full-tests",
        action="store_true",
        help="also run lint, mypy, normal/blocked full tests, and artifact drift in a dev venv",
    )
    parser.add_argument("--evidence", type=Path, help="optional sanitized JSON result path")
    parser.add_argument(
        "--checksum-file",
        type=Path,
        help="optional SHA256SUMS file that must authenticate the selected wheel",
    )
    args = parser.parse_args(argv)

    uv = _uv_executable()
    wheel = _wheel(args.wheel_dir.resolve())
    if args.checksum_file is not None:
        _verify_checksum_file(wheel, args.checksum_file.resolve())
    source_digest = _package_source_sha256()
    _verify_release_manifest(wheel, source_digest)
    results: list[dict[str, Any]] = []
    full_tests: dict[str, str] = {"status": "not-run"}
    with tempfile.TemporaryDirectory(prefix="ldf-profile-matrix-") as temp:
        root = Path(temp)
        for profile in args.profiles:
            results.append(
                _profile_run(
                    uv,
                    args.python,
                    wheel,
                    profile,
                    root,
                    install_source=args.install_source,
                )
            )
        if args.full_tests:
            full_tests = _full_test_run(uv, args.python, wheel, root)

    interpreter = results[0]["interpreter"] if results else None
    if interpreter is not None and any(
        result["interpreter"] != interpreter for result in results
    ):
        raise RuntimeError("profile environments used different interpreter identities")
    lock_digests = {result["lock"]: result["lock_sha256"] for result in results}
    if args.full_tests:
        lock_digests["dev.txt"] = _sha256(LOCK_DIR / "dev.txt")
    evidence = {
        "schema": 2,
        "interpreter": interpreter,
        "python_selector": Path(args.python).name,
        "uv": UV_VERSION,
        "wheel": wheel.name,
        "wheel_sha256": _sha256(wheel),
        "package_source_sha256": source_digest,
        "release_manifest_verified": True,
        "lock_sha256": dict(sorted(lock_digests.items())),
        "checksum_file_verified": args.checksum_file is not None,
        "source": _source_state(),
        "source_install_syntax_tested": args.install_source,
        "results": results,
        "full_tests": full_tests,
    }
    if args.evidence:
        args.evidence.parent.mkdir(parents=True, exist_ok=True)
        args.evidence.write_text(
            json.dumps(evidence, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    print(json.dumps(evidence, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
