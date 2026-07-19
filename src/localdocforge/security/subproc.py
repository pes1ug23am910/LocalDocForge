"""Hardened subprocess execution for external engines.

Rules enforced here (see docs/THREAT_MODEL.md):
- argument arrays only, never a shell;
- executables must come from the allowlist and be resolved to a real path;
- minimal inherited environment;
- bounded runtime and captured output;
- the whole process tree dies on timeout or cancellation.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path

# The only external executables LocalDocForge will ever launch. Adding an
# engine means adding it here; nothing else may reach subprocess.
EXECUTABLE_ALLOWLIST: dict[str, tuple[str, ...]] = {
    "qpdf": ("qpdf",),
    "tesseract": ("tesseract",),
    "ocrmypdf": ("ocrmypdf",),
    "ghostscript": ("gswin64c", "gswin32c", "gs"),
    "libreoffice": ("soffice",),
    "pandoc": ("pandoc",),
    "typst": ("typst",),
    "verapdf": ("verapdf", "verapdf.bat"),
}

_SAFE_ENV_KEYS = (
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "WINDIR",
    "TEMP",
    "TMP",
    "TMPDIR",
    "HOME",
    "LANG",
    "LC_ALL",
)


class ToolError(Exception):
    """An external tool failed, timed out, or was not allowed to run."""


class ToolTimeout(ToolError):
    pass


@dataclass(frozen=True)
class ToolResult:
    returncode: int
    output: str  # merged stdout+stderr, bounded


def minimal_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {key: os.environ[key] for key in _SAFE_ENV_KEYS if key in os.environ}
    if extra:
        env.update(extra)
    return env


def find_executable(tool: str) -> str | None:
    """Resolve an allowlisted tool name to a concrete executable path."""
    candidates = EXECUTABLE_ALLOWLIST.get(tool)
    if candidates is None:
        raise ToolError(f"Executable {tool!r} is not on the allowlist")
    for candidate in candidates:
        found = shutil.which(candidate)
        if found:
            return found
    return None


def kill_process_tree(process: subprocess.Popen[bytes]) -> None:
    """Terminate a process and all of its descendants."""
    if process.poll() is not None:
        return
    try:
        if os.name == "nt":
            system_root = os.environ.get("SYSTEMROOT", r"C:\Windows")
            taskkill = os.path.join(system_root, "System32", "taskkill.exe")
            subprocess.run(  # noqa: S603 - fixed absolute executable, pid is an integer
                [taskkill, "/F", "/T", "/PID", str(process.pid)],
                capture_output=True,
                timeout=15,
                check=False,
            )
        else:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (OSError, subprocess.TimeoutExpired):
        process.kill()


def run_tool(
    tool: str,
    args: list[str],
    *,
    timeout: float = 30.0,
    cwd: Path | None = None,
    max_output_bytes: int = 1_000_000,
    env_extra: dict[str, str] | None = None,
) -> ToolResult:
    """Run an allowlisted external tool with hard bounds. Raises ToolError/ToolTimeout."""
    executable = find_executable(tool)
    if executable is None:
        raise ToolError(f"{tool} is not installed")
    argv = [executable, *args]
    popen_kwargs: dict[str, object] = {}
    if os.name == "nt":
        popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    else:
        popen_kwargs["start_new_session"] = True  # own process group for killpg
    process = subprocess.Popen(  # noqa: S603 - allowlisted executable, argv list, no shell
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        cwd=str(cwd) if cwd else None,
        env=minimal_env(env_extra),
        **popen_kwargs,  # type: ignore[arg-type]
    )
    try:
        raw, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        kill_process_tree(process)
        process.communicate()
        raise ToolTimeout(
            f"{tool} exceeded its {timeout:.0f}s time limit and was terminated"
        ) from exc
    output = raw[:max_output_bytes].decode("utf-8", errors="replace")
    return ToolResult(returncode=process.returncode, output=output)
