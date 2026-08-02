#!/usr/bin/env python3
"""Regenerate and verify LocalDocForge's universal profile lock exports."""

from __future__ import annotations

import argparse
import difflib
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCK_DIR = ROOT / "requirements" / "locks"
UV_VERSION = "0.11.26"
PROFILES = ("lite", "standard", "full", "dev")
RUNTIME_PROFILES = ("lite", "standard", "full")
REQUIREMENT_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^ ;\\]+)")
HASH_RE = re.compile(r"--hash=sha256:([0-9a-f]{64})(?:\s|$)")


def _canonicalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


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
            actual = result.stdout.strip().split()
            if len(actual) < 2 or actual[1] != UV_VERSION:
                raise RuntimeError(
                    f"uv {UV_VERSION} is required; {candidate!r} reported {result.stdout.strip()!r}"
                )
            return candidate
    raise RuntimeError(
        "uv is not installed. Bootstrap it with "
        "'python -m pip install --require-hashes -r requirements/uv-bootstrap.txt'."
    )


def _export_command(uv: str, profile: str, output: Path) -> list[str]:
    return [
        uv,
        "export",
        "--quiet",
        "--locked",
        "--no-default-groups",
        "--extra",
        profile,
        "--no-emit-project",
        "--no-header",
        "--format",
        "requirements.txt",
        "--output-file",
        str(output),
    ]


def _requirements(text: str, source: Path) -> set[str]:
    names: set[str] = set()
    starts: list[tuple[int, re.Match[str]]] = []
    lines = text.splitlines()
    for index, line in enumerate(lines):
        match = REQUIREMENT_RE.match(line)
        if match:
            starts.append((index, match))
    if not starts:
        raise ValueError(f"{source} contains no locked requirements")

    for position, (start, match) in enumerate(starts):
        end = starts[position + 1][0] if position + 1 < len(starts) else len(lines)
        block = "\n".join(lines[start:end])
        name = _canonicalize(match.group(1))
        if name in names:
            raise ValueError(f"{source} contains duplicate requirement {name!r}")
        if not HASH_RE.search(block):
            raise ValueError(f"{source} requirement {name!r} has no SHA-256 artifact hash")
        if "implementation_name == 'cpython'" not in lines[start]:
            raise ValueError(f"{source} requirement {name!r} lacks a CPython platform marker")
        names.add(name)

    forbidden = ("--trusted-host", "--allow-insecure-host", "-e ", "--editable", " @ http:")
    for token in forbidden:
        if token in text:
            raise ValueError(f"{source} contains forbidden lock token {token!r}")
    return names


def _validate_profile_relationships(contents: dict[str, str]) -> None:
    sets = {
        profile: _requirements(contents[profile], LOCK_DIR / f"{profile}.txt")
        for profile in PROFILES
    }
    if not sets["lite"] < sets["standard"]:
        raise ValueError("standard lock must be a strict superset of lite")
    if not sets["standard"] < sets["full"]:
        raise ValueError("full lock must be a strict superset of standard")
    if sets["full"] - sets["standard"] != {"pypdf"}:
        raise ValueError("full must add only the optional pypdf diagnostic adapter")
    expected_standard = {
        "anyio",
        "click",
        "fastapi",
        "h11",
        "idna",
        "python-multipart",
        "starlette",
        "uvicorn",
    }
    if sets["standard"] - sets["lite"] != expected_standard:
        raise ValueError("standard dependency delta no longer matches the localhost API stack")
    if not sets["full"] < sets["dev"]:
        raise ValueError("dev lock must contain the full runtime closure plus development tools")
    required_dev = {"build", "httpx", "mypy", "pytest", "reportlab", "ruff", "twine"}
    if not required_dev <= sets["dev"]:
        missing = sorted(required_dev - sets["dev"])
        raise ValueError(f"dev lock is missing required release tools: {missing}")


def _validate_compatibility_include() -> None:
    expected = (
        "# Compatibility entry point for the historical development bootstrap command.\n"
        "# New installs should select an explicit runtime profile from requirements/locks.\n"
        "# Hash checking is mandatory even when this compatibility include is used.\n"
        "--require-hashes\n"
        "-r requirements/locks/dev.txt\n"
    )
    path = ROOT / "requirements-lock.txt"
    if path.read_text(encoding="utf-8") != expected:
        raise ValueError(f"{path} is not the audited hash-enforcing compatibility include")


def _run_lock(uv: str, *, check: bool) -> None:
    command = [uv, "lock", "--check"] if check else [uv, "lock"]
    environment = os.environ.copy()
    if check:
        environment["UV_OFFLINE"] = "1"
    subprocess.run(command, cwd=ROOT, env=environment, check=True)  # noqa: S603


def _write(uv: str) -> None:
    _run_lock(uv, check=False)
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    for profile in PROFILES:
        subprocess.run(  # noqa: S603
            _export_command(uv, profile, LOCK_DIR / f"{profile}.txt"),
            cwd=ROOT,
            check=True,
        )


def _check(uv: str) -> None:
    _run_lock(uv, check=True)
    checked_in = {
        profile: (LOCK_DIR / f"{profile}.txt").read_text(encoding="utf-8") for profile in PROFILES
    }
    with tempfile.TemporaryDirectory(prefix="ldf-lock-check-") as temp:
        temp_root = Path(temp)
        environment = os.environ.copy()
        environment["UV_OFFLINE"] = "1"
        for profile in PROFILES:
            candidate = temp_root / f"{profile}.txt"
            subprocess.run(  # noqa: S603
                _export_command(uv, profile, candidate),
                cwd=ROOT,
                env=environment,
                check=True,
            )
            actual = candidate.read_text(encoding="utf-8")
            expected = checked_in[profile]
            if actual != expected:
                diff = "".join(
                    difflib.unified_diff(
                        expected.splitlines(keepends=True),
                        actual.splitlines(keepends=True),
                        fromfile=str(LOCK_DIR / f"{profile}.txt"),
                        tofile=f"regenerated/{profile}.txt",
                    )
                )
                raise RuntimeError(f"{profile} lock export drifted:\n{diff}")
    _validate_profile_relationships(checked_in)
    _validate_compatibility_include()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--write", action="store_true", help="resolve and rewrite uv.lock and exports"
    )
    mode.add_argument("--check", action="store_true", help="verify lock and export drift (default)")
    args = parser.parse_args(argv)

    uv = _uv_executable()
    if args.write:
        _write(uv)
    else:
        _check(uv)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
