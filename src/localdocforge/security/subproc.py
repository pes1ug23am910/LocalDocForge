"""Hardened subprocess execution for external engines.

Rules enforced here (see docs/THREAT_MODEL.md):
- argument arrays only, never a shell;
- executables must come from the allowlist and be resolved to a real path;
- minimal inherited environment;
- bounded runtime and captured output;
- the whole process tree dies on timeout or cancellation.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

from localdocforge.security.paths import is_remote_path

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
_PATHLIKE_ENV_KEYS = frozenset({"HOME", "TEMP", "TMP", "TMPDIR", "SYSTEMROOT", "WINDIR"})
_SAFE_EXTRA_ENV_KEYS = frozenset({"HOME", "TEMP", "TMP", "TMPDIR", "LANG", "LC_ALL"})


class ToolError(Exception):
    """An external tool failed, timed out, or was not allowed to run."""


class ToolTimeout(ToolError):
    pass


@dataclass(frozen=True)
class ToolResult:
    returncode: int
    output: str  # merged stdout+stderr, bounded


def _safe_search_directories() -> list[Path]:
    directories: list[Path] = []
    for raw_directory in os.environ.get("PATH", "").split(os.pathsep):
        if not raw_directory:
            continue
        directory = Path(os.path.expandvars(raw_directory.strip('"'))).expanduser()
        if directory.is_absolute() and not is_remote_path(directory):
            directories.append(directory)
    return directories


def minimal_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {
        key: os.environ[key]
        for key in _SAFE_ENV_KEYS
        if key in os.environ
        and (
            key not in _PATHLIKE_ENV_KEYS
            or (
                Path(os.environ[key]).is_absolute()
                and not is_remote_path(Path(os.environ[key]))
            )
        )
    }
    env["PATH"] = os.pathsep.join(str(path) for path in _safe_search_directories())
    if extra:
        for raw_key, value in extra.items():
            key = raw_key.upper()
            if key == "PATH":
                raise ToolError("Child PATH overrides are forbidden")
            if key not in _SAFE_EXTRA_ENV_KEYS:
                raise ToolError(f"Child environment override {raw_key!r} is not allowed")
            if key in _PATHLIKE_ENV_KEYS:
                path = Path(value)
                if not path.is_absolute() or is_remote_path(path):
                    raise ToolError(
                        f"Refusing non-local filesystem value for child environment {key}"
                    )
            env[key] = value
    return env


def find_executable(tool: str) -> str | None:
    """Resolve an allowlisted tool from absolute PATH entries only.

    ``shutil.which`` may search the current directory on Windows even when it
    is absent from PATH. Document directories are valid working directories,
    so that behavior would allow a planted ``qpdf.exe`` to hijack a probe.
    """
    candidates = EXECUTABLE_ALLOWLIST.get(tool)
    if candidates is None:
        raise ToolError(f"Executable {tool!r} is not on the allowlist")
    if os.name == "nt":
        path_extensions = [
            ext for ext in os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(os.pathsep)
            if ext.startswith(".") and "/" not in ext and "\\" not in ext
        ]
    else:
        path_extensions = [""]
    for directory in _safe_search_directories():
        for candidate in candidates:
            suffixes = [""] if Path(candidate).suffix else path_extensions
            for suffix in suffixes:
                executable = directory / f"{candidate}{suffix}"
                if executable.is_file() and os.access(executable, os.X_OK):
                    with contextlib.suppress(OSError, RuntimeError):
                        resolved = executable.resolve(strict=True)
                        if not is_remote_path(resolved):
                            return str(resolved)
    return None


def kill_process_tree(process: subprocess.Popen[bytes]) -> None:
    """Terminate a process and all of its descendants."""
    if process.poll() is not None:
        return
    try:
        if os.name == "nt":
            system_root = os.environ.get("SYSTEMROOT", r"C:\Windows")
            taskkill = os.path.join(system_root, "System32", "taskkill.exe")
            result = subprocess.run(  # noqa: S603 - fixed absolute executable, pid is an integer
                [taskkill, "/F", "/T", "/PID", str(process.pid)],
                capture_output=True,
                timeout=15,
                check=False,
            )
            if result.returncode != 0 and process.poll() is None:
                process.kill()
        else:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (OSError, subprocess.TimeoutExpired):
        process.kill()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _drain_output(stream, output: bytearray, max_output_bytes: int) -> None:
    """Drain a child pipe without ever retaining more than the configured cap."""
    while chunk := stream.read(64 * 1024):
        remaining = max_output_bytes - len(output)
        if remaining > 0:
            output.extend(chunk[:remaining])


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
    if max_output_bytes < 0:
        raise ValueError("max_output_bytes cannot be negative")
    executable = find_executable(tool)
    if executable is None:
        raise ToolError(f"{tool} is not installed")
    argv = [executable, *args]
    if cwd is not None:
        if not cwd.is_absolute():
            raise ToolError("External tool working directories must be absolute")
        if is_remote_path(cwd):
            raise ToolError("External tools cannot use a network filesystem working directory")
        try:
            child_cwd = cwd.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ToolError("External tool working directory does not exist") from exc
        if not child_cwd.is_dir():
            raise ToolError("External tool working directory is not a directory")
        if is_remote_path(child_cwd):
            raise ToolError("External tools cannot use a network filesystem working directory")
    else:
        child_cwd = Path(executable).parent
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
        cwd=str(child_cwd),
        env=minimal_env(env_extra),
        **popen_kwargs,  # type: ignore[arg-type]
    )
    assert process.stdout is not None
    output = bytearray()
    reader = threading.Thread(
        target=_drain_output,
        args=(process.stdout, output, max_output_bytes),
        name=f"ldf-{tool}-output",
        daemon=True,
    )
    reader.start()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        kill_process_tree(process)
        reader.join(timeout=5)
        raise ToolTimeout(
            f"{tool} exceeded its {timeout:.0f}s time limit and was terminated"
        ) from exc
    except BaseException:
        # KeyboardInterrupt/SystemExit must not orphan an engine or its children.
        kill_process_tree(process)
        reader.join(timeout=5)
        raise
    reader.join(timeout=5)
    if reader.is_alive():
        process.stdout.close()
        reader.join(timeout=1)
    decoded = bytes(output).decode("utf-8", errors="replace")
    return ToolResult(returncode=process.returncode, output=decoded)
