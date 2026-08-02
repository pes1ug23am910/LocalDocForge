"""Spawned API workers, hard cancellation, and bounded job admission.

The API process only transports bounded uploads and validates filenames/form
shape. Every conversion, including the legacy synchronous HTTP flow, executes
in a fresh multiprocessing spawn child. The parent owns wall-clock and
directory-size watchdogs and can terminate the complete worker process tree.

Containment is deliberately reported per job. Windows uses a Job Object and
fails closed if the worker cannot be attached. POSIX uses a new process group
and the resource limits exposed by the host; limits without a portable kernel
primitive are labelled as monitored or unsupported rather than implied.
"""

from __future__ import annotations

import contextlib
import ctypes
import json
import math
import multiprocessing
import os
import queue
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from localdocforge.config.settings import Settings
from localdocforge.domain.models import (
    ConversionReport,
    ProgressEvent,
    ResourceLimits,
    SecurityWarning,
    WarningSeverity,
)
from localdocforge.jobs.workspace import remove_tree_with_retries
from localdocforge.security.paths import ensure_contained

_MAX_IPC_BYTES = 1024 * 1024
_WATCHDOG_INTERVAL_SECONDS = 0.05
_WORKER_START_TIMEOUT_SECONDS = 15.0
_WORKER_EXIT_GRACE_SECONDS = 2.0
_WORKER_PROCESS_GROUP_ENV = "LDF_WORKER_PROCESS_GROUP"
_BLOCK_NETWORK_ENV = "LDF_BLOCK_NETWORK"
_BLOCK_NETWORK_GUARD_ENV = "LDF_BLOCK_NETWORK_GUARD_DIR"


class WorkerJobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    CRASHED = "crashed"
    LIMIT_EXCEEDED = "limit_exceeded"


class _ProcessExitProof(StrEnum):
    """What the parent actually proved while finalizing a worker."""

    BOUNDARY_EMPTY = "boundary_empty"
    PRE_GATE_LEADER_EXIT = "pre_gate_leader_exit"
    UNVERIFIED = "unverified"


_TERMINAL_STATES = frozenset(
    {
        WorkerJobStatus.SUCCESS,
        WorkerJobStatus.FAILED,
        WorkerJobStatus.CANCELLED,
        WorkerJobStatus.TIMED_OUT,
        WorkerJobStatus.CRASHED,
        WorkerJobStatus.LIMIT_EXCEEDED,
    }
)


class AdmissionError(Exception):
    """A bounded-queue, per-client, or rate admission refusal."""

    def __init__(self, status_code: int, message: str, *, retry_after: float | None = None):
        self.status_code = status_code
        self.message = message
        self.retry_after = retry_after
        super().__init__(message)


@dataclass(frozen=True)
class Admission:
    token: str
    client_key: str


@dataclass
class WorkerRequest:
    """Minimal request sent to a spawned worker.

    The job root is an internal path required to locate the transported files.
    Params may contain an input password when the selected operation strictly
    requires it; it is cleared from the parent record on completion and is
    never returned in worker messages.
    """

    job_id: str
    operation: str
    job_root: str
    input_names: tuple[str, ...]
    params: dict[str, str]
    settings_json: str
    probe: str | None = None  # internal real-process test hook; never API-controlled


@dataclass
class WorkerOutcome:
    status: WorkerJobStatus
    report: ConversionReport | None = None
    output_names: list[str] = field(default_factory=list)
    error: str = ""
    http_status: int = 500
    containment: dict[str, str | int | float | bool | None] = field(default_factory=dict)
    probe: dict[str, Any] = field(default_factory=dict)


@dataclass
class WorkerJob:
    request: WorkerRequest | None
    client_key: str
    output_dir: Path
    max_events: int
    status: WorkerJobStatus = WorkerJobStatus.QUEUED
    report: ConversionReport | None = None
    outputs: list[Path] = field(default_factory=list)
    error: str = ""
    error_status: int = 500
    containment: dict[str, str | int | float | bool | None] = field(default_factory=dict)
    probe: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    started_at: datetime | None = None
    finished_at: datetime | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    done: threading.Event = field(default_factory=threading.Event)
    cancel_requested: threading.Event = field(default_factory=threading.Event)
    lock: threading.RLock = field(default_factory=threading.RLock)
    active_downloads: int = 0
    deleting: bool = False
    _next_event_id: int = 1
    _accounted: bool = True
    _controller: WorkerProcess | None = None

    @property
    def job_id(self) -> str:
        if self.request is not None:
            return self.request.job_id
        # Request is cleared only after the terminal event records the id.
        return str(self.events[-1]["job_id"])

    @property
    def operation(self) -> str:
        if self.request is not None:
            return self.request.operation
        return str(self.events[-1]["operation"])

    def add_event(
        self,
        stage: str,
        *,
        current: int = 0,
        total: int = 0,
        message: str = "",
    ) -> None:
        with self.lock:
            request = self.request
            job_id = request.job_id if request is not None else str(self.events[-1]["job_id"])
            operation = (
                request.operation if request is not None else str(self.events[-1]["operation"])
            )
            event = {
                "id": self._next_event_id,
                "job_id": job_id,
                "operation": operation,
                "stage": stage,
                "current": max(0, int(current)),
                "total": max(0, int(total)),
                "message": _bounded_text(message, 512),
                "timestamp": datetime.now(UTC).isoformat(),
            }
            self._next_event_id += 1
            self.events.append(event)
            if len(self.events) > self.max_events:
                del self.events[: len(self.events) - self.max_events]

    def event_snapshot(self, after: int = 0) -> list[dict[str, Any]]:
        with self.lock:
            return [dict(event) for event in self.events if int(event["id"]) > after]


@dataclass
class _WindowsJob:
    handle: int
    process_handle: int
    details: dict[str, str | int | float | bool | None]

    def terminate(self) -> None:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        if not kernel32.TerminateJobObject(ctypes.c_void_p(self.handle), 1):
            raise OSError(ctypes.get_last_error(), "TerminateJobObject failed")

    def _accounting(self):
        from ctypes import wintypes

        class LargeInteger(ctypes.Structure):
            _fields_ = [("QuadPart", ctypes.c_longlong)]

        class BasicAccountingInformation(ctypes.Structure):
            _fields_ = [
                ("TotalUserTime", LargeInteger),
                ("TotalKernelTime", LargeInteger),
                ("ThisPeriodTotalUserTime", LargeInteger),
                ("ThisPeriodTotalKernelTime", LargeInteger),
                ("TotalPageFaultCount", wintypes.DWORD),
                ("TotalProcesses", wintypes.DWORD),
                ("ActiveProcesses", wintypes.DWORD),
                ("TotalTerminatedProcesses", wintypes.DWORD),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        info = BasicAccountingInformation()
        if not kernel32.QueryInformationJobObject(
            ctypes.c_void_p(self.handle),
            1,  # JobObjectBasicAccountingInformation
            ctypes.byref(info),
            ctypes.sizeof(info),
            None,
        ):
            raise OSError(ctypes.get_last_error(), "QueryInformationJobObject failed")
        return info

    def cpu_seconds(self) -> float | None:
        try:
            info = self._accounting()
        except OSError:
            return None
        ticks = info.TotalUserTime.QuadPart + info.TotalKernelTime.QuadPart
        return ticks / 10_000_000

    def wait_empty(self, timeout: float) -> bool:
        """Wait until Windows reports that no process remains in the job."""
        deadline = time.monotonic() + timeout
        while True:
            try:
                if self._accounting().ActiveProcesses == 0:
                    return True
            except OSError:
                return False
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(0.01, remaining))

    def close(self) -> None:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        if self.process_handle:
            kernel32.CloseHandle(ctypes.c_void_p(self.process_handle))
            self.process_handle = 0
        if self.handle:
            kernel32.CloseHandle(ctypes.c_void_p(self.handle))
            self.handle = 0


def _bounded_text(value: object, limit: int = 1024) -> str:
    text = str(value).replace("\x00", "")
    return text[:limit]


def _tree_size(root: Path, *, stop_after: int | None = None) -> int:
    total = 0
    try:
        for directory, _subdirs, files in os.walk(root):
            for name in files:
                with contextlib.suppress(OSError):
                    total += (Path(directory) / name).stat().st_size
                    if stop_after is not None and total > stop_after:
                        return total
    except OSError:
        return total
    return total


def _linux_descendant_count(pid: int) -> int | None:
    if not sys.platform.startswith("linux") or not Path("/proc").is_dir():
        return None
    parents: dict[int, int] = {}
    with contextlib.suppress(OSError):
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                stat_line = (entry / "stat").read_text(encoding="ascii", errors="replace")
                tail = stat_line[stat_line.rfind(")") + 2 :].split()
                parents[int(entry.name)] = int(tail[1])
            except (IndexError, OSError, ValueError):
                continue
    descendants = {pid}
    changed = True
    while changed:
        changed = False
        for child, parent in parents.items():
            if parent in descendants and child not in descendants:
                descendants.add(child)
                changed = True
    return max(0, len(descendants) - 1)


def _send_message(connection, payload: dict[str, Any]) -> None:
    try:
        encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
        if len(encoded) > _MAX_IPC_BYTES:
            encoded = b'{"kind":"fatal","error":"Worker message exceeded the IPC limit"}'
        connection.send_bytes(encoded)
    except (BrokenPipeError, EOFError, OSError):
        return


def _receive_message(connection, timeout: float) -> dict[str, Any] | None:
    if not connection.poll(timeout):
        return None
    try:
        raw = connection.recv_bytes(_MAX_IPC_BYTES)
        payload = json.loads(raw)
    except (EOFError, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Worker sent malformed IPC") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("kind"), str):
        raise ValueError("Worker sent malformed IPC")
    return payload


def _install_python_offline_guard() -> None:
    """Deny Python socket/DNS entry points inside a strict-offline worker.

    This is an additional application control, not an OS network sandbox:
    native code can bypass Python's socket module.
    """

    def blocked(*_args, **_kwargs):
        raise PermissionError("strict-offline worker denied a network operation")

    class BlockedSocket:
        def __init__(self, *_args, **_kwargs):
            blocked()

    socket.socket = BlockedSocket  # type: ignore[assignment]
    socket.create_connection = blocked
    socket.getaddrinfo = blocked
    socket.gethostbyname = blocked
    socket.gethostbyname_ex = blocked
    socket.gethostbyaddr = blocked


def _scrub_worker_environment(temporary_root: Path) -> None:
    """Drop API-process secrets and contain library/native temporary paths."""
    inherited = os.environ
    preserve = (
        "COMSPEC",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "PATH",
        "PATHEXT",
        "SYSTEMDRIVE",
        "SYSTEMROOT",
        "TZ",
        "WINDIR",
    )
    sanitized = {key: inherited[key] for key in preserve if key in inherited}

    # Keep the worker/group marker only when it describes this process's real
    # group.  The subprocess wrapper uses it to avoid creating a session that
    # would escape the parent supervisor's kill boundary.
    if os.name == "posix":
        expected_group = str(os.getpgrp())
        if inherited.get(_WORKER_PROCESS_GROUP_ENV) == expected_group:
            sanitized[_WORKER_PROCESS_GROUP_ENV] = expected_group

    # The blocked-network release harness injects sitecustomize at interpreter
    # startup. Preserve only the explicitly marked directory that supplied the
    # module already loaded in this worker; never forward an arbitrary inherited
    # PYTHONPATH to worker grandchildren.
    guard_marker = inherited.get(_BLOCK_NETWORK_GUARD_ENV)
    loaded_guard = sys.modules.get("sitecustomize")
    loaded_guard_file = getattr(loaded_guard, "__file__", None)
    if inherited.get(_BLOCK_NETWORK_ENV) == "1" and guard_marker and loaded_guard_file:
        try:
            guard_root = Path(guard_marker)
            if not guard_root.is_absolute():
                raise ValueError("network guard path was not absolute")
            guard_root = guard_root.resolve(strict=True)
            expected_guard = (guard_root / "sitecustomize.py").resolve(strict=True)
            actual_guard = Path(loaded_guard_file).resolve(strict=True)
            if actual_guard != expected_guard:
                raise ValueError("network guard marker did not match loaded sitecustomize")
        except (OSError, ValueError):
            pass
        else:
            sanitized[_BLOCK_NETWORK_ENV] = "1"
            sanitized[_BLOCK_NETWORK_GUARD_ENV] = str(guard_root)
            sanitized["PYTHONPATH"] = str(guard_root)
    contained_temp = str(temporary_root)
    for key in (
        "APPDATA",
        "HOME",
        "LOCALAPPDATA",
        "TEMP",
        "TMP",
        "TMPDIR",
        "USERPROFILE",
    ):
        sanitized[key] = contained_temp
    inherited.clear()
    inherited.update(sanitized)
    tempfile.tempdir = contained_temp


def _silence_worker_output() -> None:
    """Prevent parser/native stdout or stderr from crossing the API log boundary."""
    descriptor = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(descriptor, 1)
        os.dup2(descriptor, 2)
    finally:
        os.close(descriptor)
    sink = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115
    sys.stdout = sink
    sys.stderr = sink
    sys.__stdout__ = sink
    sys.__stderr__ = sink


def _sanitize_value(value: Any, replacements: tuple[str, ...]) -> Any:
    if isinstance(value, str):
        sanitized = value
        for secret in replacements:
            if secret:
                sanitized = sanitized.replace(secret, "<redacted>")
        return _bounded_text(sanitized, 4096)
    if isinstance(value, list):
        return [_sanitize_value(item, replacements) for item in value]
    if isinstance(value, dict):
        return {str(key): _sanitize_value(item, replacements) for key, item in value.items()}
    return value


def _sanitized_report(
    report: ConversionReport,
    *,
    api_job_id: str,
    job_root: Path,
    secrets: tuple[str, ...],
) -> dict[str, Any]:
    payload = report.model_dump(mode="json")
    payload["job_id"] = api_job_id
    for collection in ("inputs", "outputs"):
        for artifact in payload.get(collection, []):
            artifact["path"] = Path(str(artifact.get("path", ""))).name
    replacements = (str(job_root), job_root.as_posix(), *secrets)
    return _sanitize_value(payload, replacements)


def _public_pipeline_error(message: str) -> str:
    """Keep controlled policy errors useful; genericize parser-derived text."""
    safe_prefixes = (
        "Generated outputs total ",
        "Inputs total ",
        "Job ",
        "Multiple outputs resolve ",
        "Output already exists:",
        "Output path aliases ",
        "Output path is outside ",
        "strict-offline mode forbids ",
    )
    if message.startswith(safe_prefixes):
        return _bounded_text(message, 1024)
    return "Document processing failed"


def _progress_payload(
    event: ProgressEvent,
    replacements: tuple[str, ...],
    *,
    job_id: str,
) -> dict[str, Any]:
    stage = _sanitize_value(event.stage, replacements)
    message = _sanitize_value(event.message, replacements)
    return {
        "job_id": job_id,
        "stage": _bounded_text(stage, 128),
        "current": event.current,
        "total": event.total,
        "message": _bounded_text(message, 512),
        "timestamp": event.timestamp.isoformat(),
    }


def _base_containment(strict_offline: bool) -> dict[str, str | int | float | bool | None]:
    return {
        "platform": sys.platform,
        "document_gate": "never_opened",
        "wall_clock": "parent_watchdog",
        "temporary_disk": "parent_directory_monitor",
        "aggregate_output": "pipeline_and_parent_directory_monitor",
        "network": (
            "python_socket_guard_only; native code is not OS-sandboxed"
            if strict_offline
            else "application_policy_only; no OS network sandbox"
        ),
    }


def _prepare_posix(limits: ResourceLimits, strict_offline: bool) -> dict[str, Any]:
    details = _base_containment(strict_offline)
    try:
        os.setsid()
    except OSError as exc:
        raise RuntimeError("Unable to create the worker process group") from exc
    os.environ[_WORKER_PROCESS_GROUP_ENV] = str(os.getpgrp())
    details["process_tree"] = "posix_process_group"
    details["process_tree_boundary"] = (
        "wrapper children inherit the worker group; arbitrary same-user descendants "
        "can still create a new session"
    )

    try:
        import resource
    except ImportError:
        details.update(
            {
                "memory": "unsupported",
                "cpu": "unsupported",
                "single_output_file": "unsupported",
            }
        )
        return details

    def set_limit(
        resource_name: str,
        value: int | None,
        detail_key: str,
        detail_value: str,
        *,
        hard_extra: int = 0,
    ) -> None:
        identifier = getattr(resource, resource_name, None)
        if value is None:
            details[detail_key] = "disabled"
        elif identifier is None:
            details[detail_key] = "unsupported"
        else:
            try:
                resource.setrlimit(identifier, (value, value + hard_extra))
                details[detail_key] = detail_value
            except (OSError, ValueError):
                details[detail_key] = "unsupported_by_host"

    set_limit("RLIMIT_AS", limits.max_memory_bytes, "memory", "rlimit_as")
    cpu_seconds = (
        max(1, math.ceil(limits.max_cpu_seconds)) if limits.max_cpu_seconds is not None else None
    )
    set_limit(
        "RLIMIT_CPU",
        cpu_seconds,
        "cpu",
        "rlimit_cpu",
        hard_extra=1 if cpu_seconds is not None else 0,
    )
    set_limit(
        "RLIMIT_FSIZE",
        limits.max_output_bytes,
        "single_output_file",
        "rlimit_fsize",
    )
    details["child_processes"] = (
        "parent_procfs_descendant_monitor"
        if sys.platform.startswith("linux")
        else "unsupported_portably_on_this_posix_host"
    )
    return details


def _create_windows_job(
    pid: int,
    limits: ResourceLimits,
    strict_offline: bool,
) -> _WindowsJob:
    from ctypes import wintypes

    class LargeInteger(ctypes.Structure):
        _fields_ = [("QuadPart", ctypes.c_longlong)]

    class IoCounters(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class BasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", LargeInteger),
            ("PerJobUserTimeLimit", LargeInteger),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class ExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BasicLimitInformation),
            ("IoInfo", IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    job_object_extended_limit_information = 9
    job_object_limit_job_time = 0x00000004
    job_object_limit_active_process = 0x00000008
    job_object_limit_job_memory = 0x00000200
    job_object_limit_kill_on_close = 0x00002000
    process_terminate = 0x0001
    process_set_quota = 0x0100
    process_query_limited_information = 0x1000

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    )
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL

    handle = kernel32.CreateJobObjectW(None, None)
    if not handle:
        raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")

    process_handle = None
    try:
        info = ExtendedLimitInformation()
        flags = job_object_limit_kill_on_close
        details = _base_containment(strict_offline)
        details["process_tree"] = "windows_job_object_kill_on_close"

        if limits.max_memory_bytes is not None:
            info.JobMemoryLimit = limits.max_memory_bytes
            flags |= job_object_limit_job_memory
            details["memory"] = "windows_job_object_job_memory"
        else:
            details["memory"] = "disabled"

        if limits.max_cpu_seconds is not None:
            info.BasicLimitInformation.PerJobUserTimeLimit.QuadPart = int(
                limits.max_cpu_seconds * 10_000_000
            )
            flags |= job_object_limit_job_time
            details["cpu"] = "windows_job_object_time_and_parent_accounting_watchdog"
        else:
            details["cpu"] = "disabled"

        if limits.max_subprocesses is not None:
            info.BasicLimitInformation.ActiveProcessLimit = limits.max_subprocesses + 1
            flags |= job_object_limit_active_process
            details["child_processes"] = "windows_job_object_active_process_limit"
        else:
            details["child_processes"] = "disabled"
        details["single_output_file"] = "unsupported; aggregate directory monitor active"

        info.BasicLimitInformation.LimitFlags = flags
        if not kernel32.SetInformationJobObject(
            handle,
            job_object_extended_limit_information,
            ctypes.byref(info),
            ctypes.sizeof(info),
        ):
            raise OSError(ctypes.get_last_error(), "SetInformationJobObject failed")

        access = process_terminate | process_set_quota | process_query_limited_information
        process_handle = kernel32.OpenProcess(access, False, pid)
        if not process_handle:
            raise OSError(ctypes.get_last_error(), "OpenProcess for worker containment failed")
        if not kernel32.AssignProcessToJobObject(handle, process_handle):
            raise OSError(ctypes.get_last_error(), "AssignProcessToJobObject failed")
        return _WindowsJob(int(handle), int(process_handle), details)
    except BaseException:
        if process_handle:
            kernel32.CloseHandle(process_handle)
        kernel32.CloseHandle(handle)
        raise


def _stdio_probe_command() -> list[str]:
    """Return a fixed argv; the synthetic marker is supplied only over stdin."""
    return [
        sys.executable,
        "-c",
        "import sys; sys.stderr.buffer.write(sys.stdin.buffer.read())",
    ]


def _network_grandchild_probe_command() -> list[str]:
    """Return fixed argv that verifies startup guard inheritance."""
    return [
        sys.executable,
        "-c",
        (
            "import socket; "
            "r=socket.getaddrinfo; "
            "active=(getattr(r,'__module__','')=='sitecustomize' and "
            "getattr(r,'__name__','').startswith('_guarded_')); "
            "blocked=False; "
            "\ntry: socket.getaddrinfo('example.invalid',443)"
            "\nexcept OSError as e: blocked='blocked-network gate denied' in str(e)"
            "\nraise SystemExit(0 if active and blocked else 7)"
        ),
    ]


def _run_probe(
    request: WorkerRequest,
    connection,
    replacements: tuple[str, ...],
) -> None:
    root = Path(request.job_root)
    if request.probe == "hang":
        while True:
            time.sleep(1)
    if request.probe == "crash":
        os._exit(23)
    if request.probe == "tree":
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        child = subprocess.Popen(  # noqa: S603 - repository-owned synthetic helper
            [sys.executable, "-c", "import time; time.sleep(300)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )
        (root / "probe-child.pid").write_text(str(child.pid), encoding="ascii")
        while True:
            time.sleep(1)
    if request.probe == "orphan-tree":
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        child = subprocess.Popen(  # noqa: S603 - repository-owned synthetic helper
            [sys.executable, "-c", "import time; time.sleep(300)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )
        (root / "probe-child.pid").write_text(str(child.pid), encoding="ascii")
        event = ProgressEvent(
            job_id=request.job_id,
            stage="probe-child",
            current=child.pid,
            message="synthetic descendant started",
        )
        _send_message(
            connection,
            {
                "kind": "progress",
                "event": _progress_payload(event, replacements, job_id=request.job_id),
            },
        )
        os._exit(0)
    if request.probe == "disk":
        target = root / "probe-disk.bin"
        with target.open("wb") as stream:
            for _index in range(1024):
                stream.write(b"x" * 64 * 1024)
                stream.flush()
                time.sleep(0.005)
        while True:
            time.sleep(1)
    if request.probe == "output":
        work = root / "work"
        work.mkdir(exist_ok=True)
        target = work / "probe-output.bin"
        with target.open("wb") as stream:
            for _index in range(1024):
                stream.write(b"o" * 64 * 1024)
                stream.flush()
                time.sleep(0.005)
        while True:
            time.sleep(1)
    if request.probe == "cpu":
        value = 1
        while True:
            value = (value * 3 + 1) % 1_000_003
    if request.probe == "memory":
        allocations = []
        for _index in range(128):
            allocations.append(bytearray(2 * 1024 * 1024))
            time.sleep(0.005)
        while True:
            time.sleep(1)
    if request.probe == "malformed-ipc":
        connection.send_bytes(b"not-json")
        return
    if request.probe == "ipc-leakage":
        secret = next((value for value in request.params.values() if value), "probe-secret")
        event = ProgressEvent(
            job_id=request.job_id,
            stage=f"stage-{secret}",
            message=f"{request.job_root}/{secret}",
        )
        _send_message(
            connection,
            {
                "kind": "progress",
                "event": _progress_payload(
                    event,
                    replacements,
                    job_id=request.job_id,
                ),
            },
        )
        _send_message(connection, {"kind": "probe_result", "probe": {"completed": True}})
        return
    if request.probe == "stdio-leakage":
        marker = next((value for value in request.params.values() if value), "probe-marker")
        print(marker, flush=True)
        os.write(2, marker.encode("utf-8"))
        subprocess.run(  # noqa: S603 - repository-owned synthetic helper
            _stdio_probe_command(),
            input=marker.encode("utf-8"),
            check=False,
        )
        _send_message(connection, {"kind": "probe_result", "probe": {"completed": True}})
        return
    if request.probe == "network":
        blocked = False
        try:
            candidate = socket.socket()
            close = getattr(candidate, "close", None)
            if close is not None:
                close()
        except PermissionError:
            blocked = True
        _send_message(connection, {"kind": "probe_result", "probe": {"blocked": blocked}})
        return
    if request.probe == "network-instrumentation":
        resolver = socket.getaddrinfo
        blocked_gate_expected = os.environ.get(_BLOCK_NETWORK_ENV) == "1"
        grandchild_guard_active = False
        if blocked_gate_expected:
            creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            grandchild = subprocess.run(  # noqa: S603 - fixed repository probe argv
                _network_grandchild_probe_command(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
                creationflags=creationflags,
            )
            grandchild_guard_active = grandchild.returncode == 0
        _send_message(
            connection,
            {
                "kind": "probe_result",
                "probe": {
                    "blocked_gate_active": (
                        getattr(resolver, "__module__", "") == "sitecustomize"
                        and getattr(resolver, "__name__", "").startswith("_guarded_")
                    ),
                    "grandchild_blocked_gate_active": grandchild_guard_active,
                    "guard_path_only": blocked_gate_expected
                    and os.environ.get("PYTHONPATH") == os.environ.get(_BLOCK_NETWORK_GUARD_ENV),
                },
            },
        )
        return
    if request.probe == "environment":
        _send_message(
            connection,
            {
                "kind": "probe_result",
                "probe": {
                    "marker_present": "LDF_WORKER_ENV_PROBE" in os.environ,
                    "temp_root": Path(tempfile.gettempdir()).name,
                },
            },
        )
        return
    raise RuntimeError("Unknown internal worker probe")


def _worker_process_entry(request: WorkerRequest, start_gate, connection) -> None:
    try:
        settings = Settings.model_validate_json(request.settings_json)
        if os.name == "posix":
            containment = _prepare_posix(settings.limits, settings.strict_offline)
        else:
            containment = _base_containment(settings.strict_offline)
        _send_message(connection, {"kind": "ready", "containment": containment})
        if not start_gate.wait(_WORKER_START_TIMEOUT_SECONDS):
            return
        job_root = Path(request.job_root).resolve(strict=True)
        temporary_root = ensure_contained(
            job_root / "worker-temp",
            job_root,
            what="worker temporary directory",
        )
        temporary_root.mkdir(mode=0o700, exist_ok=False)
        _scrub_worker_environment(temporary_root)
        _silence_worker_output()
        if settings.strict_offline:
            _install_python_offline_guard()
        secrets = tuple(
            value for key, value in request.params.items() if "password" in key.casefold() and value
        )
        replacements = (str(Path(request.job_root)), Path(request.job_root).as_posix(), *secrets)
        if request.probe is not None:
            _run_probe(request, connection, replacements)
            return

        input_dir = ensure_contained(job_root / "in", job_root, what="worker input directory")
        output_dir = ensure_contained(job_root / "out", job_root, what="worker output directory")
        work_dir = ensure_contained(job_root / "work", job_root, what="worker scratch directory")
        input_paths: list[Path] = []
        for name in request.input_names:
            if not name or Path(name).name != name or "/" in name or "\\" in name:
                raise ValueError("Worker input name was not a contained basename")
            input_paths.append(ensure_contained(input_dir / name, input_dir, what="worker input"))

        worker_settings_data = settings.model_dump(mode="json")
        worker_settings_data["jobs_root"] = str(work_dir)
        worker_settings_data["allowed_output_roots"] = [str(output_dir)]
        worker_settings = Settings.model_validate(worker_settings_data)

        # Delayed import keeps PDF/native parsers out of the long-lived API
        # process and avoids importing the operation table until containment is
        # active and the parent has opened the start gate.
        from localdocforge.api import app as api_module
        from localdocforge.pipelines.runner import PipelineError

        runner = api_module._OPERATIONS.get(request.operation)
        if runner is None:
            _send_message(
                connection,
                {"kind": "fatal", "error": "Worker operation is unavailable", "http_status": 500},
            )
            return

        def progress(event: ProgressEvent) -> None:
            _send_message(
                connection,
                {
                    "kind": "progress",
                    "event": _progress_payload(
                        event,
                        replacements,
                        job_id=request.job_id,
                    ),
                },
            )

        try:
            report = runner(
                input_paths,
                output_dir,
                request.params,
                worker_settings,
                progress,
            )
        except api_module._ApiError as exc:
            _send_message(
                connection,
                {
                    "kind": "failure",
                    "error": _sanitize_value(exc.message, replacements),
                    "http_status": exc.status,
                },
            )
            return
        except PipelineError as exc:
            public_error = _public_pipeline_error(str(exc))
            payload: dict[str, Any] = {
                "kind": "failure",
                "error": _sanitize_value(public_error, replacements),
                "http_status": 422,
            }
            if exc.report is not None:
                report_payload = _sanitized_report(
                    exc.report,
                    api_job_id=request.job_id,
                    job_root=job_root,
                    secrets=secrets,
                )
                if report_payload.get("errors"):
                    report_payload["errors"] = [public_error]
                validation = report_payload.get("validation")
                if isinstance(validation, dict):
                    for check in validation.get("checks", []):
                        if isinstance(check, dict) and not check.get("passed", False):
                            check["detail"] = "failed"
                payload["report"] = report_payload
            _send_message(connection, payload)
            return
        except Exception:
            # Unexpected parser/runner values can contain document fragments,
            # passwords, or private paths. Keep them out of IPC and logs.
            _send_message(
                connection,
                {"kind": "fatal", "error": "Internal worker error", "http_status": 500},
            )
            return

        output_names = [artifact.path.name for artifact in report.outputs]
        _send_message(
            connection,
            {
                "kind": "result",
                "report": _sanitized_report(
                    report,
                    api_job_id=request.job_id,
                    job_root=job_root,
                    secrets=secrets,
                ),
                "outputs": output_names,
            },
        )
    except Exception:
        _send_message(
            connection,
            {"kind": "fatal", "error": "Worker setup failed", "http_status": 500},
        )
    finally:
        with contextlib.suppress(OSError):
            connection.close()


class WorkerProcess:
    """One spawned worker plus the parent-side containment/watchdog."""

    def __init__(
        self,
        request: WorkerRequest,
        *,
        on_progress: Callable[[ProgressEvent], None] | None = None,
    ) -> None:
        self.request = request
        self.settings = Settings.model_validate_json(request.settings_json)
        self.limits = self.settings.limits
        self.on_progress = on_progress
        self._process = None
        self._pid: int | None = None
        self._windows_job: _WindowsJob | None = None
        self._tree_ready = False
        self._document_gate_opened = False
        self._tree_finalized = False
        self._terminate_lock = threading.Lock()

    @property
    def pid(self) -> int | None:
        # multiprocessing.Process.pid raises ValueError after Process.close().
        # Keep the immutable identifier separately for diagnostics and tests.
        return self._pid

    @staticmethod
    def _process_is_alive(process) -> bool:
        try:
            return bool(process.is_alive())
        except (AssertionError, ValueError):
            return False

    def terminate(self) -> bool:
        """Kill the complete contained process tree; safe to call repeatedly."""
        with self._terminate_lock:
            if self._tree_finalized:
                return True
            process = self._process
            pid = self._pid
            try:
                if self._windows_job is not None:
                    self._windows_job.terminate()
                    return True
                if os.name == "posix" and self._tree_ready and pid is not None:
                    os.killpg(pid, signal.SIGKILL)
                    return True
                if process is not None and self._process_is_alive(process):
                    process.kill()
                return True
            except ProcessLookupError:
                return True
            except (OSError, ValueError):
                if process is not None and self._process_is_alive(process):
                    with contextlib.suppress(OSError, ValueError):
                        process.kill()
                return False

    @staticmethod
    def _wait_posix_group_empty(process_group: int, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while True:
            try:
                os.killpg(process_group, 0)
            except ProcessLookupError:
                return True
            except PermissionError:
                return False
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(0.01, remaining))

    def _finalize_process_tree(self, process) -> _ProcessExitProof:
        """Reap the worker and return only the process-exit proof actually obtained."""
        pid = self._pid
        if pid is None:
            return (
                _ProcessExitProof.PRE_GATE_LEADER_EXIT
                if not self._document_gate_opened and not self._process_is_alive(process)
                else _ProcessExitProof.UNVERIFIED
            )

        with contextlib.suppress(AssertionError, OSError, ValueError):
            process.join(timeout=_WORKER_EXIT_GRACE_SECONDS)
        if self._process_is_alive(process):
            self.terminate()
            with contextlib.suppress(AssertionError, OSError, ValueError):
                process.join(timeout=_WORKER_EXIT_GRACE_SECONDS)

        if not self._tree_ready:
            # Before the document gate opens the child is trusted bootstrap code
            # and cannot start document processing. With no Job Object/process
            # group established, however, leader exit is not a process-tree proof.
            if not self._document_gate_opened and not self._process_is_alive(process):
                return _ProcessExitProof.PRE_GATE_LEADER_EXIT
            return _ProcessExitProof.UNVERIFIED

        # Leader exit is not evidence that descendants exited. Always signal
        # the established containment boundary once more and then query it.
        termination_ok = self.terminate()
        if self._windows_job is not None:
            tree_empty = self._windows_job.wait_empty(_WORKER_EXIT_GRACE_SECONDS)
            return (
                _ProcessExitProof.BOUNDARY_EMPTY
                if termination_ok and tree_empty
                else _ProcessExitProof.UNVERIFIED
            )
        if os.name == "posix" and self._tree_ready:
            tree_empty = self._wait_posix_group_empty(pid, _WORKER_EXIT_GRACE_SECONDS)
            return (
                _ProcessExitProof.BOUNDARY_EMPTY
                if termination_ok and tree_empty
                else _ProcessExitProof.UNVERIFIED
            )
        return _ProcessExitProof.UNVERIFIED

    @staticmethod
    def _fail_closed_unverified_tree(outcome: WorkerOutcome) -> None:
        outcome.status = WorkerJobStatus.CRASHED
        outcome.report = None
        outcome.output_names = []
        outcome.error = "Worker process tree exit could not be verified"
        outcome.http_status = 500
        outcome.probe = {}
        outcome.containment["process_tree_exit"] = "unverified; failed closed"

    def _progress_message(self, payload: dict[str, Any]) -> None:
        event_payload = payload.get("event")
        if not isinstance(event_payload, dict):
            raise ValueError("Worker sent malformed progress IPC")
        event = ProgressEvent.model_validate(event_payload)
        if event.job_id != self.request.job_id:
            raise ValueError("Worker progress job id mismatch")
        if self.on_progress is not None:
            self.on_progress(event)

    def _terminal_message(
        self,
        payload: dict[str, Any],
        containment: dict[str, str | int | float | bool | None],
    ) -> WorkerOutcome | None:
        kind = payload["kind"]
        if kind == "progress":
            self._progress_message(payload)
            return None
        if kind == "result":
            raw_report = payload.get("report")
            raw_outputs = payload.get("outputs")
            if not isinstance(raw_report, dict) or not isinstance(raw_outputs, list):
                raise ValueError("Worker sent malformed result IPC")
            report = ConversionReport.model_validate(raw_report)
            if report.job_id != self.request.job_id:
                raise ValueError("Worker result job id mismatch")
            output_names: list[str] = []
            for raw_name in raw_outputs:
                if not isinstance(raw_name, str):
                    raise ValueError("Worker output name was not text")
                name = Path(raw_name).name
                if not name or name != raw_name or "/" in name or "\\" in name:
                    raise ValueError("Worker output name was not a basename")
                output_names.append(name)
            return WorkerOutcome(
                status=WorkerJobStatus.SUCCESS,
                report=report,
                output_names=output_names,
                http_status=201,
                containment=containment,
            )
        if kind == "failure":
            status = payload.get("http_status", 422)
            if not isinstance(status, int) or not 400 <= status <= 599:
                raise ValueError("Worker failure status was invalid")
            report_payload = payload.get("report")
            report = (
                ConversionReport.model_validate(report_payload)
                if isinstance(report_payload, dict)
                else None
            )
            if report is not None and report.job_id != self.request.job_id:
                raise ValueError("Worker failure report job id mismatch")
            return WorkerOutcome(
                status=WorkerJobStatus.FAILED,
                report=report,
                error=_bounded_text(payload.get("error", "Worker rejected the job")),
                http_status=status,
                containment=containment,
            )
        if kind == "fatal":
            return WorkerOutcome(
                status=WorkerJobStatus.CRASHED,
                error="Internal worker error",
                http_status=500,
                containment=containment,
            )
        if kind == "probe_result":
            probe = payload.get("probe")
            if not isinstance(probe, dict):
                raise ValueError("Worker probe result was invalid")
            return WorkerOutcome(
                status=WorkerJobStatus.SUCCESS,
                http_status=200,
                containment=containment,
                probe=probe,
            )
        raise ValueError("Worker sent an unexpected IPC message")

    def run(self, cancel_requested: threading.Event) -> WorkerOutcome:
        context = multiprocessing.get_context("spawn")
        receive_connection, send_connection = context.Pipe(duplex=False)
        start_gate = context.Event()
        process = context.Process(
            target=_worker_process_entry,
            args=(self.request, start_gate, send_connection),
            name=f"ldf-worker-{self.request.job_id[:12]}",
            daemon=False,
        )
        self._process = process
        containment = _base_containment(self.settings.strict_offline)
        outcome: WorkerOutcome | None = None
        try:
            process.start()
            self._pid = process.pid
            send_connection.close()
            ready = None
            ready_deadline = time.monotonic() + _WORKER_START_TIMEOUT_SECONDS
            while ready is None and time.monotonic() < ready_deadline:
                if cancel_requested.is_set():
                    self.terminate()
                    outcome = WorkerOutcome(
                        status=WorkerJobStatus.CANCELLED,
                        error="Job was cancelled while its worker was starting",
                        http_status=409,
                        containment=containment,
                    )
                    return outcome
                try:
                    ready = _receive_message(
                        receive_connection,
                        min(0.05, max(0.0, ready_deadline - time.monotonic())),
                    )
                except ValueError:
                    self.terminate()
                    status = (
                        WorkerJobStatus.CANCELLED
                        if cancel_requested.is_set()
                        else WorkerJobStatus.CRASHED
                    )
                    outcome = WorkerOutcome(
                        status=status,
                        error=(
                            "Job was cancelled while its worker was starting"
                            if status is WorkerJobStatus.CANCELLED
                            else "Worker startup protocol failed"
                        ),
                        http_status=409 if status is WorkerJobStatus.CANCELLED else 500,
                        containment=containment,
                    )
                    return outcome
                if process.exitcode is not None and ready is None:
                    break
            if ready is None or ready.get("kind") != "ready":
                self.terminate()
                if cancel_requested.is_set():
                    outcome = WorkerOutcome(
                        status=WorkerJobStatus.CANCELLED,
                        error="Job was cancelled while its worker was starting",
                        http_status=409,
                        containment=containment,
                    )
                    return outcome
                outcome = WorkerOutcome(
                    status=WorkerJobStatus.CRASHED,
                    error="Worker did not become ready",
                    containment=containment,
                )
                return outcome
            child_containment = ready.get("containment")
            if isinstance(child_containment, dict):
                containment.update(
                    {
                        str(key): value
                        for key, value in child_containment.items()
                        if isinstance(value, (str, int, float, bool)) or value is None
                    }
                )

            if os.name == "nt":
                try:
                    assert process.pid is not None
                    self._windows_job = _create_windows_job(
                        process.pid,
                        self.limits,
                        self.settings.strict_offline,
                    )
                    containment.update(self._windows_job.details)
                except OSError:
                    self.terminate()
                    outcome = WorkerOutcome(
                        status=WorkerJobStatus.CRASHED,
                        error="Windows Job Object containment could not be established",
                        containment={
                            **containment,
                            "process_tree": "unavailable; worker failed closed",
                        },
                    )
                    return outcome
            self._tree_ready = True
            self._document_gate_opened = True
            containment["document_gate"] = "opened_after_containment"
            start_gate.set()
            started = time.monotonic()
            root = Path(self.request.job_root)

            while outcome is None:
                if cancel_requested.is_set():
                    self.terminate()
                    outcome = WorkerOutcome(
                        status=WorkerJobStatus.CANCELLED,
                        error="Job was cancelled and its worker process tree was terminated",
                        http_status=409,
                        containment=containment,
                    )
                    break

                elapsed = time.monotonic() - started
                wall_limit = self.limits.timeout_seconds
                if wall_limit is not None and elapsed > wall_limit:
                    self.terminate()
                    outcome = WorkerOutcome(
                        status=WorkerJobStatus.TIMED_OUT,
                        error=f"Job exceeded its {wall_limit:g}s wall-clock limit",
                        http_status=408,
                        containment=containment,
                    )
                    break

                cpu_limit = self.limits.max_cpu_seconds
                if cpu_limit is not None and self._windows_job is not None:
                    cpu_used = self._windows_job.cpu_seconds()
                    if cpu_used is not None and cpu_used > cpu_limit:
                        self.terminate()
                        outcome = WorkerOutcome(
                            status=WorkerJobStatus.LIMIT_EXCEEDED,
                            error="Job exceeded its configured CPU-time limit",
                            http_status=422,
                            containment=containment,
                        )
                        break

                temporary_limit = self.limits.max_temporary_bytes
                if temporary_limit is not None:
                    used = _tree_size(root, stop_after=temporary_limit)
                    if used > temporary_limit:
                        self.terminate()
                        outcome = WorkerOutcome(
                            status=WorkerJobStatus.LIMIT_EXCEEDED,
                            error=(
                                "Job exceeded its configured temporary-disk limit; "
                                "the worker tree was terminated"
                            ),
                            http_status=422,
                            containment=containment,
                        )
                        break

                output_limit = self.limits.max_output_bytes
                if output_limit is not None:
                    generated = _tree_size(root / "work", stop_after=output_limit)
                    if generated <= output_limit:
                        generated += _tree_size(
                            root / "out",
                            stop_after=max(0, output_limit - generated),
                        )
                    if generated > output_limit:
                        self.terminate()
                        outcome = WorkerOutcome(
                            status=WorkerJobStatus.LIMIT_EXCEEDED,
                            error=(
                                "Job exceeded its configured aggregate output limit; "
                                "the worker tree was terminated"
                            ),
                            http_status=422,
                            containment=containment,
                        )
                        break

                child_limit = self.limits.max_subprocesses
                if (
                    child_limit is not None
                    and process.pid is not None
                    and (descendants := _linux_descendant_count(process.pid)) is not None
                    and descendants > child_limit
                ):
                    self.terminate()
                    outcome = WorkerOutcome(
                        status=WorkerJobStatus.LIMIT_EXCEEDED,
                        error="Job exceeded its configured child-process limit",
                        http_status=422,
                        containment=containment,
                    )
                    break

                try:
                    message = _receive_message(
                        receive_connection,
                        _WATCHDOG_INTERVAL_SECONDS,
                    )
                    if message is not None:
                        outcome = self._terminal_message(message, containment)
                except ValueError:
                    self.terminate()
                    if cancel_requested.is_set():
                        outcome = WorkerOutcome(
                            status=WorkerJobStatus.CANCELLED,
                            error=("Job was cancelled and its worker process tree was terminated"),
                            http_status=409,
                            containment=containment,
                        )
                        break
                    outcome = WorkerOutcome(
                        status=WorkerJobStatus.CRASHED,
                        error="Worker IPC validation failed",
                        http_status=500,
                        containment=containment,
                    )
                    break

                if process.exitcode is not None and outcome is None:
                    # The pipe feeder may trail process exit very briefly.
                    with contextlib.suppress(ValueError):
                        message = _receive_message(receive_connection, 0.1)
                        if message is not None:
                            outcome = self._terminal_message(message, containment)
                    if outcome is None:
                        outcome = WorkerOutcome(
                            status=WorkerJobStatus.CRASHED,
                            error=(
                                "Worker exited unexpectedly; an enforced OS resource limit "
                                "or parser crash may have terminated it"
                            ),
                            http_status=500,
                            containment=containment,
                        )

            return outcome
        finally:
            try:
                exit_proof = self._finalize_process_tree(process)
            except Exception:
                exit_proof = _ProcessExitProof.UNVERIFIED
            if outcome is not None:
                if exit_proof is _ProcessExitProof.BOUNDARY_EMPTY:
                    outcome.containment["process_tree_exit"] = "verified_empty"
                elif exit_proof is _ProcessExitProof.PRE_GATE_LEADER_EXIT:
                    outcome.containment["document_gate"] = "never_opened"
                    outcome.containment["process_tree_exit"] = "pre_gate_leader_verified"
                else:
                    self._fail_closed_unverified_tree(outcome)
            with contextlib.suppress(OSError, ValueError):
                receive_connection.close()
            with contextlib.suppress(OSError, ValueError):
                send_connection.close()
            with self._terminate_lock:
                self._tree_finalized = True
                self._tree_ready = False
                self._document_gate_opened = False
                self._process = None
                with contextlib.suppress(OSError, ValueError):
                    process.close()
                if self._windows_job is not None:
                    self._windows_job.close()
                    self._windows_job = None


def _cleanup_failure(outcome: WorkerOutcome) -> WorkerOutcome:
    outcome.containment["private_cleanup"] = "incomplete"
    if outcome.report is not None and not any(
        warning.code == "api-private-cleanup-incomplete"
        for warning in outcome.report.security_warnings
    ):
        outcome.report.security_warnings.append(
            SecurityWarning(
                code="api-private-cleanup-incomplete",
                message=(
                    "Private API job cleanup was incomplete; retry deletion after "
                    "closing any open output files."
                ),
                severity=WarningSeverity.CRITICAL,
            )
        )
    outcome.status = WorkerJobStatus.CRASHED
    outcome.error = "Private API job cleanup was incomplete"
    outcome.http_status = 500
    return outcome


def _remove_tree_safely(path: Path) -> bool:
    """Contain cleanup implementation failures at the worker-manager boundary."""
    try:
        return remove_tree_with_retries(path)
    except Exception:
        return False


class WorkerManager:
    """Threaded dispatcher for a bounded number of one-shot worker processes."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        capacity = settings.api_max_concurrent_jobs + settings.api_max_queued_jobs
        self._queue: queue.Queue[WorkerJob | None] = queue.Queue(maxsize=max(1, capacity))
        self._lock = threading.RLock()
        self._threads: list[threading.Thread] = []
        self._started = False
        self._shutting_down = False
        self._reservations: dict[str, str] = {}
        self._reserved_by_client: defaultdict[str, int] = defaultdict(int)
        self._active_by_client: defaultdict[str, int] = defaultdict(int)
        self._active_total = 0
        self._rate_history: defaultdict[str, deque[float]] = defaultdict(deque)
        self._tracked: dict[str, WorkerJob] = {}
        self._shutdown_complete: bool | None = None
        self._shutdown_incomplete_workers = 0

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            if self._shutting_down:
                raise RuntimeError("Worker manager is shutting down")
            self._started = True
            for index in range(self.settings.api_max_concurrent_jobs):
                thread = threading.Thread(
                    target=self._worker_loop,
                    name=f"ldf-worker-slot-{index + 1}",
                    daemon=True,
                )
                self._threads.append(thread)
                thread.start()

    def reserve(self, client_key: str) -> Admission:
        """Reserve queue capacity before multipart parsing or filesystem writes."""
        # ASGI test harnesses may invoke the app without lifespan events. Lazy
        # start preserves request-size enforcement in that environment; normal
        # servers still start and stop the manager through the app lifespan.
        self.start()
        now = time.monotonic()
        with self._lock:
            if self._shutting_down:
                raise AdmissionError(503, "Worker service is not accepting jobs")

            history = self._rate_history[client_key]
            window = self.settings.api_rate_limit_window_seconds
            while history and now - history[0] >= window:
                history.popleft()
            if len(history) >= self.settings.api_rate_limit_jobs:
                retry_after = max(0.1, window - (now - history[0]))
                raise AdmissionError(
                    429,
                    "Client job submission rate limit exceeded",
                    retry_after=retry_after,
                )

            client_active = (
                self._active_by_client[client_key] + self._reserved_by_client[client_key]
            )
            if client_active >= self.settings.api_max_active_jobs_per_client:
                raise AdmissionError(
                    429,
                    "Client already has the maximum number of queued or running jobs",
                    retry_after=1.0,
                )

            capacity = self.settings.api_max_concurrent_jobs + self.settings.api_max_queued_jobs
            if self._active_total + len(self._reservations) >= capacity:
                raise AdmissionError(503, "Worker queue is full", retry_after=1.0)

            token = uuid.uuid4().hex
            self._reservations[token] = client_key
            self._reserved_by_client[client_key] += 1
            history.append(now)
            return Admission(token=token, client_key=client_key)

    def release(self, admission: Admission) -> None:
        with self._lock:
            client = self._reservations.pop(admission.token, None)
            if client is None:
                return
            self._reserved_by_client[client] -= 1
            if self._reserved_by_client[client] <= 0:
                self._reserved_by_client.pop(client, None)

    def enqueue(self, admission: Admission, job: WorkerJob) -> None:
        with self._lock:
            client = self._reservations.get(admission.token)
            if client != admission.client_key or job.client_key != client:
                raise RuntimeError("Invalid worker admission reservation")
            if self._shutting_down:
                self.release(admission)
                raise AdmissionError(503, "Worker service is shutting down")

            self._reservations.pop(admission.token)
            self._reserved_by_client[client] -= 1
            if self._reserved_by_client[client] <= 0:
                self._reserved_by_client.pop(client, None)
            self._active_by_client[client] += 1
            self._active_total += 1
            self._tracked[job.job_id] = job
            job._accounted = True
            job.add_event("queued", message="job admitted to the bounded worker queue")
            try:
                self._queue.put_nowait(job)
            except queue.Full as exc:
                self._tracked.pop(job.job_id, None)
                self._release_accounting_locked(job)
                raise AdmissionError(503, "Worker queue is full", retry_after=1.0) from exc

    def wait(self, job: WorkerJob, timeout: float | None = None) -> bool:
        return job.done.wait(timeout)

    def cancel(self, job: WorkerJob) -> bool:
        controller: WorkerProcess | None = None
        cleanup_root: Path | None = None
        queued_cancel = False
        with self._lock, job.lock:
            if job.status in _TERMINAL_STATES or job.cancel_requested.is_set():
                return False
            job.cancel_requested.set()
            if job.status is WorkerJobStatus.QUEUED:
                queued_cancel = True
                if job.request is not None:
                    cleanup_root = Path(job.request.job_root)
                    job.request.params.clear()
                    job.request = None
            else:
                controller = job._controller
                job.add_event("cancelling", message="terminating the worker process tree")
        if queued_cancel:
            self._discard_queued_tombstone(job)
        if controller is not None:
            controller.terminate()
        cleanup_ok = cleanup_root is None or _remove_tree_safely(cleanup_root)
        if queued_cancel:
            with self._lock, job.lock:
                if not cleanup_ok:
                    job.status = WorkerJobStatus.CRASHED
                    job.error = "Private API job cleanup was incomplete"
                    job.error_status = 500
                    job.containment["private_cleanup"] = "incomplete"
                    job.add_event("crashed", message=job.error)
                else:
                    job.status = WorkerJobStatus.CANCELLED
                    job.error = "Job was cancelled before its worker started"
                    job.error_status = 409
                    job.containment["private_cleanup"] = "complete"
                    job.add_event("cancelled", message=job.error)
                job.finished_at = datetime.now(UTC)
                job.done.set()
                self._release_accounting_locked(job)
        return True

    def _discard_queued_tombstone(self, job: WorkerJob) -> None:
        """Remove a cancelled queued item so physical capacity matches admission."""
        # Queue has no public targeted-remove operation. Its documented task
        # accounting must be updated under the queue mutex when a consumer has
        # not already taken this exact object.
        with self._queue.mutex:
            removed = False
            for index, candidate in enumerate(self._queue.queue):
                if candidate is job:
                    del self._queue.queue[index]
                    removed = True
                    break
            if not removed:
                return
            self._queue.unfinished_tasks = max(0, self._queue.unfinished_tasks - 1)
            if self._queue.unfinished_tasks == 0:
                self._queue.all_tasks_done.notify_all()
            self._queue.not_full.notify()

    def forget(self, job: WorkerJob) -> None:
        with self._lock:
            if job.status not in _TERMINAL_STATES:
                raise RuntimeError("Cannot forget an active worker job")
            self._tracked.pop(job.job_id, None)

    def diagnostics(self) -> dict[str, int | bool]:
        with self._lock:
            running = sum(
                1 for job in self._tracked.values() if job.status is WorkerJobStatus.RUNNING
            )
            queued = sum(
                1 for job in self._tracked.values() if job.status is WorkerJobStatus.QUEUED
            )
            return {
                "started": self._started,
                "shutting_down": self._shutting_down,
                "shutdown_complete": bool(self._shutdown_complete),
                "shutdown_incomplete_workers": self._shutdown_incomplete_workers,
                "running": running,
                "queued": queued,
                "max_concurrent": self.settings.api_max_concurrent_jobs,
                "max_queued": self.settings.api_max_queued_jobs,
            }

    def shutdown(self) -> dict[str, int | bool]:
        with self._lock:
            if self._shutting_down:
                return self.diagnostics()
            self._shutting_down = True
            jobs = list(self._tracked.values())
        for job in jobs:
            self.cancel(job)

        for _thread in self._threads:
            while True:
                try:
                    self._queue.put(None, timeout=0.1)
                    break
                except queue.Full:
                    continue
        for thread in self._threads:
            thread.join(timeout=10)
        # A controller that ignored the normal cancellation path is killed
        # again after joins; repeated termination is intentionally safe.
        with self._lock:
            remaining = list(self._tracked.values())
        for job in remaining:
            if job._controller is not None:
                job._controller.terminate()
        for thread in self._threads:
            thread.join(timeout=2)
        with self._lock:
            self._shutdown_incomplete_workers = sum(
                1 for thread in self._threads if thread.is_alive()
            )
            self._shutdown_complete = self._shutdown_incomplete_workers == 0
        return self.diagnostics()

    def _release_accounting_locked(self, job: WorkerJob) -> None:
        if not job._accounted:
            return
        job._accounted = False
        self._active_total = max(0, self._active_total - 1)
        remaining = self._active_by_client.get(job.client_key, 0) - 1
        if remaining <= 0:
            self._active_by_client.pop(job.client_key, None)
        else:
            self._active_by_client[job.client_key] = remaining

    def _worker_loop(self) -> None:
        while True:
            job = self._queue.get()
            try:
                if job is None:
                    return
                if job.done.is_set():
                    continue
                self._run_job(job)
            finally:
                self._queue.task_done()

    def _run_job(self, job: WorkerJob) -> None:
        # Claim a queued job and capture its request in the same critical
        # section used by cancellation. If cancellation already owns it, only
        # the cancelling thread may clean up, publish terminal state, and
        # release accounting.
        with self._lock, job.lock:
            if job.done.is_set() or (
                job.status is WorkerJobStatus.QUEUED and job.cancel_requested.is_set()
            ):
                return
            request = job.request
            if request is None:
                job.status = WorkerJobStatus.CRASHED
                job.error = "Worker request state was unavailable"
                job.error_status = 500
                job.outputs = []
                job.finished_at = datetime.now(UTC)
                job._controller = None
                job.add_event("crashed", message=job.error)
                job.done.set()
                self._release_accounting_locked(job)
                return
            if job.status is not WorkerJobStatus.QUEUED:
                return
            job.status = WorkerJobStatus.RUNNING
            job.started_at = datetime.now(UTC)
            job.add_event("running", message="spawned worker is starting")

        job_root = Path(request.job_root)
        try:

            def progress(event: ProgressEvent) -> None:
                job.add_event(
                    event.stage,
                    current=event.current,
                    total=event.total,
                    message=event.message,
                )

            controller = WorkerProcess(request, on_progress=progress)
            with job.lock:
                job._controller = controller
            try:
                outcome = controller.run(job.cancel_requested)
            except Exception:
                outcome = WorkerOutcome(
                    status=WorkerJobStatus.CRASHED,
                    error="Worker supervisor failed safely",
                    http_status=500,
                )

            validated_outputs: list[Path] = []
            if outcome.status is WorkerJobStatus.SUCCESS and outcome.report is not None:
                try:
                    candidates = [
                        ensure_contained(
                            job.output_dir / name,
                            job.output_dir,
                            what="worker output",
                        )
                        for name in outcome.output_names
                    ]
                    if any(not output.is_file() for output in candidates):
                        raise FileNotFoundError("Worker output was missing")
                    cleanup_results = (
                        _remove_tree_safely(job_root / "in"),
                        _remove_tree_safely(job_root / "work"),
                        _remove_tree_safely(job_root / "worker-temp"),
                    )
                    if not all(cleanup_results):
                        raise OSError("Private worker input cleanup was incomplete")
                    validated_outputs = candidates
                    outcome.containment["private_cleanup"] = "inputs_removed; outputs_retained"
                except Exception:
                    outcome = WorkerOutcome(
                        status=WorkerJobStatus.CRASHED,
                        report=outcome.report,
                        error="Worker output containment or cleanup failed",
                        http_status=500,
                        containment=outcome.containment,
                    )

            if outcome.status is not WorkerJobStatus.SUCCESS:
                if _remove_tree_safely(job_root):
                    outcome.containment["private_cleanup"] = "complete"
                else:
                    outcome = _cleanup_failure(outcome)

            # This lock is the sole publication point for validated outputs.
            # Cancellation that wins it first discards the root, then the loop
            # publishes CANCELLED on its next pass.
            while True:
                with self._lock, job.lock:
                    cancel_success = (
                        outcome.status is WorkerJobStatus.SUCCESS and job.cancel_requested.is_set()
                    )
                    if not cancel_success:
                        job.outputs = (
                            list(validated_outputs)
                            if outcome.status is WorkerJobStatus.SUCCESS
                            else []
                        )
                        job.status = outcome.status
                        job.report = outcome.report
                        job.error = _bounded_text(outcome.error)
                        job.error_status = outcome.http_status
                        job.containment = dict(outcome.containment)
                        job.probe = dict(outcome.probe)
                        job.finished_at = datetime.now(UTC)
                        stage = outcome.status.value
                        job.add_event(stage, message=job.error or f"job {stage}")
                        job._controller = None
                        request.params.clear()
                        job.request = None
                        job.done.set()
                        self._release_accounting_locked(job)
                        return

                outcome = WorkerOutcome(
                    status=WorkerJobStatus.CANCELLED,
                    report=outcome.report,
                    error="Job was cancelled and its output was discarded",
                    http_status=409,
                    containment=outcome.containment,
                )
                validated_outputs = []
                if _remove_tree_safely(job_root):
                    outcome.containment["private_cleanup"] = "complete"
                else:
                    outcome = _cleanup_failure(outcome)
        except Exception:
            cleanup_ok = _remove_tree_safely(job_root)
            with self._lock, job.lock:
                job.outputs = []
                job.status = WorkerJobStatus.CRASHED
                job.report = None
                job.error = (
                    "Worker finalization failed safely"
                    if cleanup_ok
                    else "Private API job cleanup was incomplete"
                )
                job.error_status = 500
                job.containment["private_cleanup"] = "complete" if cleanup_ok else "incomplete"
                job.probe = {}
                job.finished_at = datetime.now(UTC)
                job._controller = None
                request.params.clear()
                job.request = None
                job.add_event("crashed", message=job.error)
                job.done.set()
                self._release_accounting_locked(job)
        finally:
            # No post-worker exception may strand a slot or a waiter.
            if not job.done.is_set():
                cleanup_ok = _remove_tree_safely(job_root)
                with self._lock, job.lock:
                    job.outputs = []
                    job.status = WorkerJobStatus.CRASHED
                    job.report = None
                    job.error = (
                        "Worker finalization failed safely"
                        if cleanup_ok
                        else "Private API job cleanup was incomplete"
                    )
                    job.error_status = 500
                    job.containment["private_cleanup"] = "complete" if cleanup_ok else "incomplete"
                    job.probe = {}
                    job.finished_at = datetime.now(UTC)
                    job._controller = None
                    request.params.clear()
                    job.request = None
                    job.done.set()
                    self._release_accounting_locked(job)
