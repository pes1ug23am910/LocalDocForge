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
import sys
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from localdocforge.security.paths import is_remote_path

# The only external executables LocalDocForge will ever launch. Adding an
# engine means adding it here; nothing else may reach subprocess.
EXECUTABLE_ALLOWLIST: dict[str, tuple[str, ...]] = {
    "qpdf": ("qpdf",),
    "tesseract": ("tesseract.exe",) if os.name == "nt" else ("tesseract",),
    "ocrmypdf": ("ocrmypdf",),
    "libreoffice": ("soffice",),
    "pandoc": ("pandoc",),
    "typst": ("typst",),
    "verapdf": ("verapdf", "verapdf.bat"),
}
# Ghostscript is AGPL and may only run as an OCRmyPDF child. LocalDocForge may
# discover it for capability gating, but ``run_tool("ghostscript", ...)`` is
# intentionally impossible.
DISCOVERY_ONLY_EXECUTABLES: dict[str, tuple[str, ...]] = {
    # OCRmyPDF 17.x supports only the 64-bit console executable on Windows.
    "ghostscript": ("gswin64c.exe",) if os.name == "nt" else ("gs",),
}
_TRUSTED_CHILD_EXECUTABLES: dict[str, frozenset[str]] = {
    # OCRmyPDF is the only executable that may launch these descendants. The
    # resolved directories exclusively form its child PATH so selection is
    # identical to LocalDocForge's probes and optional tools stay unreachable.
    "ocrmypdf": frozenset({"tesseract", "ghostscript"}),
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
_PATHLIKE_ENV_KEYS = frozenset(
    {"HOME", "USERPROFILE", "TEMP", "TMP", "TMPDIR", "SYSTEMROOT", "WINDIR"}
)
_SAFE_EXTRA_ENV_KEYS = frozenset(
    {"HOME", "USERPROFILE", "TEMP", "TMP", "TMPDIR", "LANG", "LC_ALL"}
)
_WORKER_PROCESS_GROUP_ENV = "LDF_WORKER_PROCESS_GROUP"


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


def _local_directory(path: Path) -> Path | None:
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if not resolved.is_dir() or is_remote_path(resolved):
        return None
    return resolved


def _windows_vendor_directories(tool: str) -> list[Path]:
    """Return narrow machine-level vendor install locations without scanning.

    The approved Windows installers register these locations but do not
    reliably update the current process PATH. HKLM is used deliberately; a
    same-user document must not be able to steer engine discovery through a
    user-writable registry key.
    """
    if os.name != "nt" or tool not in {"tesseract", "ghostscript"}:
        return []
    try:
        import winreg
    except ImportError:  # pragma: no cover - Windows-only module
        return []

    access = winreg.KEY_READ | getattr(winreg, "KEY_WOW64_64KEY", 0)
    raw_directories: list[Path] = []
    versioned_ghostscript_directories: list[tuple[tuple[int, ...], Path]] = []
    try:
        if tool == "tesseract":
            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SOFTWARE\Tesseract-OCR",
                0,
                access,
            ) as key:
                install_dir, _ = winreg.QueryValueEx(key, "InstallDir")
                if isinstance(install_dir, str):
                    raw_directories.append(Path(install_dir))
        else:
            roots = (
                r"SOFTWARE\Artifex\GPL Ghostscript",
                r"SOFTWARE\GPL Ghostscript",
            )

            def version_key(value: str) -> tuple[int, ...]:
                try:
                    return tuple(int(part) for part in value.split("."))
                except ValueError:
                    return (0,)

            for root_name in roots:
                try:
                    with winreg.OpenKey(
                        winreg.HKEY_LOCAL_MACHINE,
                        root_name,
                        0,
                        access,
                    ) as root:
                        versions: list[str] = []
                        index = 0
                        while True:
                            try:
                                versions.append(winreg.EnumKey(root, index))
                            except OSError:
                                break
                            index += 1
                    for version in versions:
                        with winreg.OpenKey(
                            winreg.HKEY_LOCAL_MACHINE,
                            f"{root_name}\\{version}",
                            0,
                            access,
                        ) as key:
                            value_index = 0
                            while True:
                                try:
                                    _, raw_path, _ = winreg.EnumValue(key, value_index)
                                except OSError:
                                    break
                                value_index += 1
                                if not isinstance(raw_path, str):
                                    continue
                                path = Path(raw_path)
                                versioned_ghostscript_directories.append(
                                    (
                                        version_key(version),
                                        path.parent
                                        if path.suffix.casefold() == ".dll"
                                        else path / "bin",
                                    )
                                )
                except OSError:
                    continue
    except OSError:
        return []

    raw_directories.extend(
        path
        for _, path in sorted(
            versioned_ghostscript_directories,
            key=lambda item: item[0],
            reverse=True,
        )
    )
    directories: list[Path] = []
    for raw in raw_directories:
        directory = _local_directory(raw)
        if directory is not None and directory not in directories:
            directories.append(directory)
    return directories


def _trusted_search_directories(tool: str) -> list[Path]:
    directories: list[Path] = []
    if tool == "ocrmypdf":
        # Console entry points installed with LocalDocForge live beside the
        # active venv Python even when ldf.exe is invoked by absolute path and
        # that Scripts/bin directory is absent from PATH.
        for raw in (
            Path(sys.executable).parent,
            Path(sys.prefix) / ("Scripts" if os.name == "nt" else "bin"),
        ):
            directory = _local_directory(raw)
            if directory is not None and directory not in directories:
                directories.append(directory)
    for directory in _windows_vendor_directories(tool):
        if directory not in directories:
            directories.append(directory)
    return directories


def minimal_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {
        key: os.environ[key]
        for key in _SAFE_ENV_KEYS
        if key in os.environ
        and (
            key not in _PATHLIKE_ENV_KEYS
            or (Path(os.environ[key]).is_absolute() and not is_remote_path(Path(os.environ[key])))
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


def _tool_environment(
    tool: str,
    extra: dict[str, str] | None,
    child_path_tools: tuple[str, ...],
    expected_child_executables: Mapping[str, str] | None,
) -> dict[str, str]:
    env = minimal_env(extra)
    if not child_path_tools:
        if expected_child_executables:
            raise ToolError("Expected child executables require child PATH tools")
        return env
    allowed = _TRUSTED_CHILD_EXECUTABLES.get(tool, frozenset())
    if any(child not in allowed for child in child_path_tools):
        raise ToolError(f"Executable {tool!r} may not receive the requested child PATH entries")
    expected_children = dict(expected_child_executables or {})
    if expected_child_executables is None or set(expected_children) != set(child_path_tools):
        raise ToolError("Expected child executable paths must match requested PATH tools")
    directories: list[Path] = []
    bound_children: dict[str, Path] = {}
    for child in child_path_tools:
        executable = _resolve_bound_executable_path(child, expected_children[child])
        bound_children[child] = Path(executable)
        directory = _local_directory(Path(executable).parent)
        if directory is None:
            raise ToolError(f"{child} required by {tool} has no trusted local directory")
        if directory not in directories:
            directories.append(directory)
    for child, expected_path in bound_children.items():
        matches = _executable_matches(child, directories)
        if len(matches) != 1 or not _same_local_path(matches[0], expected_path):
            raise ToolError(f"{child} child PATH does not uniquely select its probed executable")
    # OCRmyPDF's descendant surface is intentionally closed to these exact
    # directories. Optional tools and inherited PATH entries are not needed
    # for LocalDocForge's optimize=0 paths.
    env["PATH"] = os.pathsep.join(str(path) for path in directories)
    if os.name == "nt":
        # Python's Windows executable lookup may otherwise prepend the child
        # working directory even when it is absent from PATH. OCRmyPDF uses
        # extensionless shutil.which() calls for these descendants, so bind it
        # to the closed PATH and the required native .exe spelling.
        env["NoDefaultCurrentDirectoryInExePath"] = "1"
        env["PATHEXT"] = ".EXE"
    return env


def _executable_matches(tool: str, directories: list[Path]) -> list[Path]:
    candidates = EXECUTABLE_ALLOWLIST.get(tool) or DISCOVERY_ONLY_EXECUTABLES.get(tool)
    if candidates is None:
        raise ToolError(f"Executable {tool!r} is not on the allowlist")
    if os.name == "nt":
        path_extensions = [
            ext
            for ext in os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(os.pathsep)
            if ext.startswith(".") and "/" not in ext and "\\" not in ext
        ]
    else:
        path_extensions = [""]
    seen: set[Path] = set()
    matches: list[Path] = []
    for directory in directories:
        resolved_directory = _local_directory(directory)
        if resolved_directory is None or resolved_directory in seen:
            continue
        seen.add(resolved_directory)
        for candidate in candidates:
            suffixes = [""] if Path(candidate).suffix else path_extensions
            for suffix in suffixes:
                executable = resolved_directory / f"{candidate}{suffix}"
                if executable.is_file() and os.access(executable, os.X_OK):
                    with contextlib.suppress(OSError, RuntimeError):
                        resolved = executable.resolve(strict=True)
                        if not is_remote_path(resolved) and resolved not in matches:
                            matches.append(resolved)
    return matches


def find_executable(tool: str) -> str | None:
    """Resolve an allowlisted tool from trusted local directories.

    The active environment and narrow machine-vendor registry locations are
    checked where applicable, then absolute PATH entries. ``shutil.which`` is
    not used because it may search the current directory on Windows even when
    it is absent from PATH; document directories are valid working directories.
    """
    directories = [*_trusted_search_directories(tool), *_safe_search_directories()]
    matches = _executable_matches(tool, directories)
    return str(matches[0]) if matches else None


def _same_local_path(left: Path, right: Path) -> bool:
    return os.path.normcase(str(left)) == os.path.normcase(str(right))


def _resolve_bound_executable_path(tool: str, expected: str | None = None) -> str:
    """Resolve ``tool`` and optionally require the exact path seen by a probe."""
    discovered = find_executable(tool)
    if discovered is None:
        raise ToolError(f"{tool} is not installed")
    if expected is None:
        return discovered
    expected_path = Path(expected)
    if not expected_path.is_absolute() or is_remote_path(expected_path):
        raise ToolError(f"Expected {tool} executable is not a trusted local path")
    try:
        expected_resolved = expected_path.resolve(strict=True)
        discovered_resolved = Path(discovered).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ToolError(f"Expected {tool} executable is no longer available") from exc
    if (
        not expected_resolved.is_file()
        or not os.access(expected_resolved, os.X_OK)
        or is_remote_path(expected_resolved)
    ):
        raise ToolError(f"Expected {tool} executable is not a trusted local file")
    if not _same_local_path(expected_resolved, discovered_resolved):
        raise ToolError(f"{tool} executable path changed after its runtime probe")
    return str(expected_resolved)


def _validated_worker_process_group() -> int | None:
    """Return the inherited worker process group only when it matches this process."""
    if os.name == "nt":
        return None
    raw_group = os.environ.get(_WORKER_PROCESS_GROUP_ENV, "")
    if not raw_group.isascii() or not raw_group.isdecimal():
        return None
    try:
        process_group = int(raw_group)
    except ValueError:
        return None
    if process_group <= 0:
        return None
    try:
        return process_group if process_group == os.getpgrp() else None
    except OSError:
        return None


def kill_process_tree(
    process: subprocess.Popen[bytes], *, shared_worker_group: bool = False
) -> None:
    """Terminate a process tree without signalling a shared worker group."""
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
            process_group = os.getpgid(process.pid)
            if shared_worker_group or process_group == os.getpgrp():
                # A worker's tool processes deliberately inherit the worker group so
                # the outer supervisor can terminate every descendant. Signalling
                # that group here would kill this wrapper before it can report the
                # timeout. Kill the direct tool and leave group-wide cleanup to the
                # worker supervisor.
                process.kill()
            else:
                os.killpg(process_group, signal.SIGKILL)
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
    child_path_tools: tuple[str, ...] = (),
    expected_executable: str | None = None,
    expected_child_executables: Mapping[str, str] | None = None,
) -> ToolResult:
    """Run an allowlisted external tool with hard bounds. Raises ToolError/ToolTimeout."""
    if max_output_bytes < 0:
        raise ValueError("max_output_bytes cannot be negative")
    if tool not in EXECUTABLE_ALLOWLIST:
        if tool in DISCOVERY_ONLY_EXECUTABLES:
            raise ToolError(f"{tool} is discovery-only and cannot be launched directly")
        raise ToolError(f"Executable {tool!r} is not on the allowlist")
    executable = _resolve_bound_executable_path(tool, expected_executable)
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
    worker_process_group = _validated_worker_process_group()
    if os.name == "nt":
        popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    else:
        # Standalone CLI tools get a private session for killpg. Worker tools
        # instead stay in the validated worker group so the worker supervisor owns
        # their complete descendant tree.
        popen_kwargs["start_new_session"] = worker_process_group is None
    child_env = _tool_environment(
        tool,
        env_extra,
        child_path_tools,
        expected_child_executables,
    )
    try:
        process = subprocess.Popen(  # noqa: S603 - allowlisted path, argv list, no shell
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            cwd=str(child_cwd),
            env=child_env,
            **popen_kwargs,  # type: ignore[arg-type]
        )
    except OSError as exc:
        # Executables can disappear or become inaccessible after resolution.
        # Keep probes and operations on the typed, path-redacting error surface.
        raise ToolError(f"{tool} could not be launched safely") from exc
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
        if worker_process_group is None:
            kill_process_tree(process)
        else:
            kill_process_tree(process, shared_worker_group=True)
        reader.join(timeout=5)
        raise ToolTimeout(
            f"{tool} exceeded its {timeout:.0f}s time limit and was terminated"
        ) from exc
    except BaseException:
        # KeyboardInterrupt/SystemExit must not orphan an engine or its children.
        if worker_process_group is None:
            kill_process_tree(process)
        else:
            kill_process_tree(process, shared_worker_group=True)
        reader.join(timeout=5)
        raise
    reader.join(timeout=5)
    if reader.is_alive():
        process.stdout.close()
        reader.join(timeout=1)
    decoded = bytes(output).decode("utf-8", errors="replace")
    return ToolResult(returncode=process.returncode, output=decoded)
