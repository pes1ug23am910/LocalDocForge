#!/usr/bin/env python3
"""Focused smoke checks for a clean installed LocalDocForge profile."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import secrets
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

CORE_CAPABILITIES = {
    "crop",
    "extract-pages",
    "images-to-pdf",
    "inspect",
    "merge",
    "organize",
    "pdf-to-images",
    "remove-pages",
    "rotate",
    "split",
}
STANDARD_HINT = "pip install 'localdocforge[standard]'"


def _run_cli(
    *arguments: str, check: bool = True, timeout: int = 30
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["LDF_STRICT_OFFLINE"] = "true"
    environment.pop("PYTHONHOME", None)
    environment.pop("PYTHONPATH", None)
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-I", "-m", "localdocforge.cli.main", *arguments],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=environment,
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"ldf {' '.join(arguments)} failed with {result.returncode}:\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def _doctor() -> dict[str, Any]:
    result = _run_cli("--json", "doctor")
    payload = json.loads(result.stdout)
    if payload["outbound_network_client"] is not False:
        raise AssertionError("doctor unexpectedly reports an outbound network client")
    if payload["strict_offline"] is not True:
        raise AssertionError("strict-offline environment state was not preserved")
    available = {item["id"] for item in payload["capabilities"] if item["available"] is True}
    if not CORE_CAPABILITIES <= available:
        raise AssertionError(
            f"core capabilities unavailable: {sorted(CORE_CAPABILITIES - available)}"
        )
    return payload


def _core_operation_smoke(root: Path) -> None:
    import pikepdf
    from PIL import Image

    source_pdf = root / "profile-input.pdf"
    with pikepdf.Pdf.new() as document:
        document.add_blank_page(page_size=(72, 72))
        document.save(source_pdf)

    inspected = _run_cli("--json", "inspect", str(source_pdf))
    inventory = json.loads(inspected.stdout)
    if inventory["page_count"] != 1:
        raise AssertionError(f"inspect returned unexpected page inventory: {inventory!r}")

    source_image = root / "profile-input.png"
    Image.new("RGB", (32, 24), (20, 80, 160)).save(source_image)
    output_pdf = root / "profile-output.pdf"
    _run_cli("--quiet", "images-to-pdf", str(source_image), "-o", str(output_pdf))
    with pikepdf.Pdf.open(output_pdf) as converted:
        if len(converted.pages) != 1:
            raise AssertionError("images-to-pdf smoke produced the wrong page count")


def _profile_specific_smoke(profile: str, payload: dict[str, Any], root: Path) -> None:
    effective = "lite" if profile == "base" else profile
    has_fastapi = importlib.util.find_spec("fastapi") is not None
    has_uvicorn = importlib.util.find_spec("uvicorn") is not None
    has_multipart = importlib.util.find_spec("python_multipart") is not None
    has_pypdf = importlib.util.find_spec("pypdf") is not None

    if effective == "lite":
        if has_fastapi or has_uvicorn or has_multipart or has_pypdf:
            raise AssertionError("lite environment contains Standard/Full-only Python packages")
        result = _run_cli("web", "--port", "8477", check=False, timeout=10)
        combined = result.stdout + result.stderr
        if result.returncode != 2:
            raise AssertionError(f"lite 'ldf web' returned {result.returncode}, expected 2")
        if STANDARD_HINT not in combined or "Traceback" in combined:
            raise AssertionError(
                f"lite web failure was not actionable and traceback-free:\n{combined}"
            )
    else:
        if not (has_fastapi and has_uvicorn and has_multipart):
            raise AssertionError(f"{profile} environment is missing the localhost API stack")
        from localdocforge.api.app import create_app
        from localdocforge.config.settings import Settings

        application = create_app(
            Settings(jobs_root=root / "jobs"),
            token=secrets.token_urlsafe(16),
        )
        if application.title != "LocalDocForge":
            raise AssertionError("localhost API app factory returned unexpected metadata")
        if effective == "standard" and has_pypdf:
            raise AssertionError("standard environment unexpectedly contains Full-only pypdf")
        if effective == "full" and not has_pypdf:
            raise AssertionError("full environment is missing the pypdf diagnostic adapter")

    engines = {item["name"]: item for item in payload["engines"]}
    expected_pypdf = effective == "full"
    if bool(engines["pypdf"]["available"]) != expected_pypdf:
        raise AssertionError("doctor pypdf probe does not match the installed profile")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True, choices=("base", "lite", "standard", "full"))
    args = parser.parse_args(argv)

    payload = _doctor()
    with tempfile.TemporaryDirectory(prefix=f"ldf-{args.profile}-smoke-") as temp:
        root = Path(temp)
        _core_operation_smoke(root)
        _profile_specific_smoke(args.profile, payload, root)
    print(f"profile smoke passed: {args.profile}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
