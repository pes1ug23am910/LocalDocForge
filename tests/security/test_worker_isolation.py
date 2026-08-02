"""Real-process evidence for the API worker isolation boundary."""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import io
import json
import logging
import multiprocessing
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

import pytest
import uvicorn
from fastapi.testclient import TestClient
from starlette.datastructures import FormData, Headers, UploadFile

import localdocforge.api.app as api_module
import localdocforge.api.worker as worker_module
from localdocforge.api.app import create_app
from localdocforge.api.worker import (
    AdmissionError,
    WorkerJob,
    WorkerJobStatus,
    WorkerManager,
    WorkerOutcome,
    WorkerProcess,
    WorkerRequest,
)
from localdocforge.config.settings import Settings
from localdocforge.domain.models import ConversionReport, ReportStatus, ResourceLimits
from localdocforge.jobs.workspace import make_private_dir
from localdocforge.security import subproc as subproc_module
from localdocforge.security.paths import PathSecurityError
from localdocforge.security.subproc import ToolTimeout

TOKEN = "worker-isolation-test-token"


def _hold_api_session_lease(path: str, ready) -> None:
    lease = api_module._try_acquire_session_lease(Path(path), create=True)
    if lease is None:
        return
    ready.set()
    while True:
        time.sleep(1)


def _auth(**extra: str) -> dict[str, str]:
    return {"X-LDF-Token": TOKEN, **extra}


def _upload(path: Path):
    return ("files", (path.name, io.BytesIO(path.read_bytes()), "application/pdf"))


def _probe_job(
    manager: WorkerManager,
    root: Path,
    probe: str,
    *,
    client: str = "client-a",
    params: dict[str, str] | None = None,
) -> WorkerJob:
    job_id = uuid.uuid4().hex
    job_root = root / job_id
    make_private_dir(job_root, exist_ok=False)
    make_private_dir(job_root / "in", exist_ok=False)
    make_private_dir(job_root / "out", exist_ok=False)
    request = WorkerRequest(
        job_id=job_id,
        operation="internal-probe",
        job_root=str(job_root),
        input_names=(),
        params=dict(params or {}),
        settings_json=manager.settings.model_dump_json(),
        probe=probe,
    )
    job = WorkerJob(
        request=request,
        client_key=client,
        output_dir=job_root / "out",
        max_events=manager.settings.api_max_progress_events,
    )
    admission = manager.reserve(client)
    manager.enqueue(admission, job)
    return job


def _wait_running(job: WorkerJob, timeout: float = 10.0) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with job.lock:
            controller = job._controller
            if job.status is WorkerJobStatus.RUNNING and controller is not None:
                pid = controller.pid
                if pid is not None:
                    return pid
        time.sleep(0.01)
    raise AssertionError("worker did not enter the running state")


def _wait_absent(pid: int, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _process_exists(pid):
            return
        time.sleep(0.05)
    raise AssertionError(f"process {pid} survived worker termination")


def _process_exists(pid: int) -> bool:
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    process_query_limited_information = 0x1000
    still_active = 259
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        return False
    try:
        exit_code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return False
        return exit_code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


def _limits(**updates) -> ResourceLimits:
    values = {
        "timeout_seconds": 10.0,
        "max_memory_bytes": None,
        "max_cpu_seconds": None,
        "max_temporary_bytes": 64 * 1024**2,
        "max_subprocesses": 8,
    }
    values.update(updates)
    return ResourceLimits(**values)


def _run_api_parent_with_hung_tree(jobs_root: str, evidence_connection) -> None:
    """Run a real leased API session whose parent is intentionally hard-killed."""

    async def run() -> None:
        app = create_app(
            Settings(
                jobs_root=Path(jobs_root),
                limits=_limits(timeout_seconds=300),
                api_max_concurrent_jobs=1,
            ),
            token=TOKEN,
        )
        async with app.router.lifespan_context(app):
            state = app.state.ldf
            job = _probe_job(state.manager, state.data_root, "tree")
            worker_pid = await asyncio.to_thread(_wait_running, job)
            child_pid_path = job.output_dir.parent / "probe-child.pid"
            deadline = time.monotonic() + 15
            while not child_pid_path.is_file() and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
            if not child_pid_path.is_file():
                raise RuntimeError("synthetic descendant did not start")
            evidence_connection.send(
                {
                    "session_root": str(state.data_root),
                    "lease_path": str(
                        api_module._session_lease_path(state.api_root, state.data_root)
                    ),
                    "worker_pid": worker_pid,
                    "child_pid": int(child_pid_path.read_text(encoding="ascii")),
                }
            )
            evidence_connection.close()
            await asyncio.Event().wait()

    asyncio.run(run())


class _ObservedRLock:
    """RLock wrapper that exposes when a concurrent publication reader arrives."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.attempted = threading.Event()

    def __enter__(self):
        self.attempted.set()
        self._lock.acquire()
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self._lock.release()


def _unpublished_terminal_job(state) -> tuple[WorkerJob, _ObservedRLock]:
    job_id = uuid.uuid4().hex
    job_root = state.data_root / job_id
    make_private_dir(job_root, exist_ok=False)
    make_private_dir(job_root / "out", exist_ok=False)
    observed_lock = _ObservedRLock()
    request = WorkerRequest(
        job_id=job_id,
        operation="internal-probe",
        job_root=str(job_root),
        input_names=(),
        params={},
        settings_json=state.settings.model_dump_json(),
    )
    job = WorkerJob(
        request=request,
        client_key="publication-race",
        output_dir=job_root / "out",
        max_events=state.settings.api_max_progress_events,
        status=WorkerJobStatus.RUNNING,
        lock=observed_lock,  # type: ignore[arg-type]
        _accounted=False,
    )
    state.jobs[job_id] = job
    return job, observed_lock


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_input_bytes", -1),
        ("max_output_bytes", -1),
        ("max_temporary_bytes", -1),
        ("max_memory_bytes", -1),
        ("max_pages", -1),
        ("max_image_pixels", -1),
        ("max_decompressed_bytes", -1),
        ("max_archive_entries", -1),
        ("max_subprocesses", -1),
        ("max_cpu_seconds", 0),
        ("max_archive_expansion_ratio", 0),
        ("timeout_seconds", 0),
    ],
)
def test_invalid_os_resource_limits_are_rejected(field, value):
    with pytest.raises(ValueError, match=field):
        ResourceLimits(**{field: value})


@pytest.mark.parametrize(
    "field",
    ["max_cpu_seconds", "max_archive_expansion_ratio", "timeout_seconds"],
)
@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_float_resource_limits_are_rejected(field, value):
    with pytest.raises(ValueError, match=field):
        ResourceLimits(**{field: value})


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_api_rate_limit_window_is_rejected(value):
    with pytest.raises(ValueError, match="api_rate_limit_window_seconds"):
        Settings(api_rate_limit_window_seconds=value)


def test_api_upload_remains_bounded_when_job_input_limit_is_disabled(fixtures_dir, tmp_path):
    settings = Settings(
        jobs_root=tmp_path / "jobs",
        limits=_limits(max_input_bytes=None),
        api_max_upload_bytes=128,
    )
    app = create_app(settings, token=TOKEN)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        response = client.post(
            "/api/jobs/extract-pages",
            headers=_auth(),
            files=[_upload(fixtures_dir / "simple-3page.pdf")],
            data={"pages": "1"},
        )
    assert response.status_code == 413
    assert not app.state.ldf.data_root.exists()


def test_parent_transport_budget_is_bounded_by_temporary_limit(tmp_path):
    settings = Settings(
        jobs_root=tmp_path / "jobs",
        limits=_limits(max_input_bytes=1024 * 1024, max_temporary_bytes=1024),
        api_max_upload_bytes=1024 * 1024,
    )
    app = create_app(settings, token=TOKEN)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        response = client.post(
            "/api/jobs/extract-pages",
            headers=_auth(),
            files={"files": ("oversized.pdf", b"%PDF-1.7\n" + b"x" * 2048, "application/pdf")},
            data={"pages": "1"},
        )

        assert response.status_code == 413
        assert response.json() == {"detail": "Request body exceeds the configured limit"}
        assert not any(
            path.name.startswith(".transport-") for path in app.state.ldf.data_root.iterdir()
        )
        assert app.state.ldf.jobs == {}


def test_multipart_spool_uses_session_root_not_remote_ambient_temp(tmp_path, monkeypatch):
    settings = Settings(
        strict_offline=True,
        jobs_root=tmp_path / "jobs",
        limits=_limits(max_input_bytes=1024 * 1024, max_temporary_bytes=1024 * 1024),
    )
    app = create_app(settings, token=TOKEN)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        monkeypatch.setattr(
            tempfile,
            "tempdir",
            r"\\127.0.0.1\ldf-must-not-contact\ambient-temp",
        )
        response = client.post(
            "/api/jobs/extract-pages",
            headers=_auth(),
            files={"files": ("source.pdf", b"%PDF-1.7\n" + b"x" * (128 * 1024), "application/pdf")},
            data={"pages": "1", "unexpected": "reject-after-spooling"},
        )

        assert response.status_code == 422
        assert "Unknown form field" in response.json()["detail"]
        assert not any(
            path.name.startswith(".transport-") for path in app.state.ldf.data_root.iterdir()
        )
        assert app.state.ldf.jobs == {}


def test_malformed_multipart_removes_rolled_contained_spool(tmp_path):
    boundary = "LocalDocForgeMalformedBoundary"
    body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="files"; filename="source.pdf"\r\n'
        "Content-Type: application/pdf\r\n\r\n"
    ).encode() + b"%PDF-1.7\n" + b"x" * (128 * 1024)
    body += (
        f"\r\n--{boundary}\r\n"
        'Content-Disposition: form-data; filename="missing-name.pdf"\r\n'
        "Content-Type: application/pdf\r\n\r\n"
        f"bad\r\n--{boundary}--\r\n"
    ).encode()
    app = create_app(
        Settings(jobs_root=tmp_path / "jobs", limits=_limits(max_temporary_bytes=1024 * 1024)),
        token=TOKEN,
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        response = client.post(
            "/api/jobs/extract-pages",
            headers={
                **_auth(),
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            },
            content=body,
        )

        assert response.status_code == 400
        assert response.json() == {"detail": "Malformed multipart form"}
        assert not any(
            path.name.startswith(".transport-") for path in app.state.ldf.data_root.iterdir()
        )
        assert app.state.ldf.jobs == {}


def test_async_api_job_uses_worker_and_exposes_bounded_progress(fixtures_dir, tmp_path):
    settings = Settings(
        jobs_root=tmp_path / "jobs",
        api_max_progress_events=6,
        limits=_limits(),
    )
    app = create_app(settings, token=TOKEN)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        submitted = client.post(
            "/api/jobs/extract-pages?async=true",
            headers=_auth(Prefer="respond-async"),
            files=[_upload(fixtures_dir / "simple-3page.pdf")],
            data={"pages": "1"},
        )
        assert submitted.status_code == 202, submitted.text
        assert submitted.headers["Preference-Applied"] == "respond-async"
        job_id = submitted.json()["job_id"]

        deadline = time.monotonic() + 15
        detail = None
        while time.monotonic() < deadline:
            detail = client.get(f"/api/jobs/{job_id}", headers=_auth())
            if detail.json()["status"] == "success":
                break
            time.sleep(0.02)
        assert detail is not None and detail.json()["status"] == "success"
        assert detail.json()["containment"]["wall_clock"] == "parent_watchdog"
        assert detail.json()["containment"]["process_tree_exit"] == "verified_empty"
        expected_tree = (
            "windows_job_object_kill_on_close" if os.name == "nt" else "posix_process_group"
        )
        assert detail.json()["containment"]["process_tree"] == expected_tree

        events = client.get(f"/api/jobs/{job_id}/events", headers=_auth()).json()["events"]
        assert 1 <= len(events) <= settings.api_max_progress_events
        assert events[-1]["stage"] == "success"
        assert all(event["job_id"] == job_id for event in events)
        assert (
            client.get(
                f"/api/jobs/{job_id}/outputs/0",
                headers=_auth(),
            ).status_code
            == 200
        )


def test_download_rejects_running_job_even_if_an_output_path_exists(tmp_path):
    app = create_app(
        Settings(jobs_root=tmp_path / "jobs", limits=_limits()),
        token=TOKEN,
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        state = app.state.ldf
        job_id = uuid.uuid4().hex
        job_root = state.data_root / job_id
        make_private_dir(job_root, exist_ok=False)
        make_private_dir(job_root / "out", exist_ok=False)
        output = job_root / "out" / "partial.pdf"
        output.write_bytes(b"partial")
        request = WorkerRequest(
            job_id=job_id,
            operation="internal-probe",
            job_root=str(job_root),
            input_names=(),
            params={},
            settings_json=state.settings.model_dump_json(),
        )
        job = WorkerJob(
            request=request,
            client_key="testclient",
            output_dir=job_root / "out",
            max_events=state.settings.api_max_progress_events,
            status=WorkerJobStatus.RUNNING,
            outputs=[output],
        )
        state.jobs[job_id] = job

        response = client.get(f"/api/jobs/{job_id}/outputs/0", headers=_auth())

        assert response.status_code == 409
        assert response.json()["detail"] == "Job outputs are available only after success"


def test_password_never_crosses_back_in_worker_ipc_or_events(
    fixtures_dir, fixture_password, tmp_path
):
    app = create_app(
        Settings(jobs_root=tmp_path / "jobs", limits=_limits()),
        token=TOKEN,
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        response = client.post(
            "/api/jobs/extract-pages",
            headers=_auth(),
            files=[_upload(fixtures_dir / "encrypted.pdf")],
            data={"pages": "1", "password": fixture_password},
        )
        assert response.status_code == 201, response.text
        job_id = response.json()["job_id"]
        events = client.get(f"/api/jobs/{job_id}/events", headers=_auth()).json()
        serialized = json.dumps({"response": response.json(), "events": events})
        assert fixture_password not in serialized
        assert str(app.state.ldf.data_root.resolve()) not in serialized


def test_progress_ipc_redacts_required_password_and_both_path_forms(tmp_path):
    manager = WorkerManager(
        Settings(jobs_root=tmp_path / "jobs", limits=_limits(), api_max_concurrent_jobs=1)
    )
    manager.start()
    secret = "synthetic-worker-password"
    try:
        job = _probe_job(
            manager,
            tmp_path / "probe-jobs",
            "ipc-leakage",
            params={"password": secret},
        )
        private_root = str(job.output_dir.parent)
        private_posix = job.output_dir.parent.as_posix()
        assert job.done.wait(10)
        serialized = json.dumps(job.events)
        assert secret not in serialized
        assert private_root not in serialized
        assert private_posix not in serialized
        assert "<redacted>" in serialized
    finally:
        manager.shutdown()


def test_worker_python_native_and_descendant_stdio_cannot_reach_logs(tmp_path, capfd, caplog):
    marker = "SYNTHETIC-WORKER-STDIO-SECRET-71b2"
    caplog.set_level(logging.DEBUG)
    manager = WorkerManager(
        Settings(jobs_root=tmp_path / "jobs", limits=_limits(), api_max_concurrent_jobs=1)
    )
    manager.start()
    try:
        job = _probe_job(
            manager,
            tmp_path / "probe-jobs",
            "stdio-leakage",
            params={"password": marker},
        )
        assert job.done.wait(10)
        assert job.status is WorkerJobStatus.SUCCESS
    finally:
        manager.shutdown()
    captured = capfd.readouterr()
    assert marker not in captured.out
    assert marker not in captured.err
    assert marker not in caplog.text


def test_stdio_descendant_command_is_secret_independent():
    marker = "SYNTHETIC-WORKER-ARGV-SECRET-811e"
    command = worker_module._stdio_probe_command()

    assert marker not in json.dumps(command)
    assert "stdin.buffer.read()" in command[-1]


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group regression")
def test_run_tool_inherits_validated_worker_group_without_self_kill(monkeypatch):
    process_group = os.getpgrp()
    monkeypatch.setenv("LDF_WORKER_PROCESS_GROUP", str(process_group))
    monkeypatch.setattr(subproc_module, "find_executable", lambda _tool: sys.executable)

    result = subproc_module.run_tool(
        "typst",
        ["-c", "import os; print(os.getpgrp())"],
        timeout=5,
    )

    assert result.returncode == 0
    assert int(result.output.strip()) == process_group
    with pytest.raises(ToolTimeout):
        subproc_module.run_tool(
            "typst",
            ["-c", "import time; time.sleep(60)"],
            timeout=0.1,
        )


def test_malformed_document_marker_is_absent_from_api_state_events_and_logs(tmp_path, caplog):
    marker = "SYNTHETIC-DOCUMENT-TEXT-MUST-NOT-LEAK-9f17"
    malformed = tmp_path / "marker-malformed.pdf"
    malformed.write_bytes(f"%PDF-1.7\n{marker}\nnot-a-real-pdf".encode())
    app = create_app(
        Settings(jobs_root=tmp_path / "jobs", limits=_limits()),
        token=TOKEN,
    )
    caplog.set_level(logging.DEBUG)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        response = client.post(
            "/api/jobs/extract-pages",
            headers=_auth(),
            files=[_upload(malformed)],
            data={"pages": "1"},
        )
        assert response.status_code == 422
        job = next(reversed(app.state.ldf.jobs.values()))
        detail = client.get(f"/api/jobs/{job.job_id}", headers=_auth()).json()
        events = client.get(f"/api/jobs/{job.job_id}/events", headers=_auth()).json()
        serialized = json.dumps(
            {
                "response": response.json(),
                "detail": detail,
                "events": events,
            }
        )
        assert marker not in serialized
        assert marker not in caplog.text
        assert response.json()["detail"] == "Document processing failed"


def test_real_process_tree_is_killed_through_api_cancellation(tmp_path):
    settings = Settings(jobs_root=tmp_path / "jobs", limits=_limits(timeout_seconds=30))
    app = create_app(settings, token=TOKEN)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        state = app.state.ldf
        job = _probe_job(state.manager, state.data_root, "tree", client="testclient")
        state.jobs[job.job_id] = job
        worker_pid = _wait_running(job)
        child_pid_path = job.output_dir.parent / "probe-child.pid"
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not child_pid_path.is_file():
            time.sleep(0.02)
        assert child_pid_path.is_file()
        child_pid = int(child_pid_path.read_text(encoding="ascii"))
        assert _process_exists(worker_pid)
        assert _process_exists(child_pid)

        cancelled = client.post(f"/api/jobs/{job.job_id}/cancel", headers=_auth())
        assert cancelled.status_code == 202
        assert job.done.wait(10)
        assert job.status is WorkerJobStatus.CANCELLED
        assert job.containment["process_tree_exit"] == "verified_empty"
        assert not _process_exists(worker_pid)
        assert not _process_exists(child_pid)
        _wait_absent(worker_pid)
        _wait_absent(child_pid)
        assert not job.output_dir.parent.exists()


def test_cancelled_sync_request_cancels_queued_job_through_manager(fixtures_dir, tmp_path):
    settings = Settings(
        jobs_root=tmp_path / "jobs",
        limits=_limits(timeout_seconds=30),
        api_max_concurrent_jobs=1,
        api_max_queued_jobs=2,
    )
    app = create_app(settings, token=TOKEN)
    with TestClient(app, base_url="http://127.0.0.1"):
        state = app.state.ldf
        blocker = _probe_job(state.manager, state.data_root, "hang", client="blocker")
        _wait_running(blocker)
        admission = state.manager.reserve("cancelled-request")
        upload = UploadFile(
            file=io.BytesIO((fixtures_dir / "simple-3page.pdf").read_bytes()),
            filename="synthetic.pdf",
            headers=Headers({"content-type": "application/pdf"}),
        )
        form = FormData([("files", upload), ("pages", "1")])

        async def submit_then_cancel() -> WorkerJob:
            task = asyncio.create_task(
                api_module._submit_job_form(
                    "extract-pages",
                    form,
                    state,
                    admission,
                    respond_async=False,
                )
            )
            deadline = time.monotonic() + 5
            queued = None
            while time.monotonic() < deadline:
                queued = next(
                    (
                        job
                        for job in state.jobs.values()
                        if job is not blocker and job.status is WorkerJobStatus.QUEUED
                    ),
                    None,
                )
                if queued is not None:
                    break
                await asyncio.sleep(0.01)
            assert queued is not None
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            return queued

        cancelled = asyncio.run(submit_then_cancel())
        assert cancelled.done.wait(5)
        assert cancelled.status is WorkerJobStatus.CANCELLED
        assert not cancelled.output_dir.parent.exists()
        state.manager.cancel(blocker)
        assert blocker.done.wait(5)


def test_live_uvicorn_sync_disconnect_cancels_and_finalizes_job(
    fixtures_dir, tmp_path, monkeypatch
):
    started_marker = tmp_path / "live-worker-started.txt"
    cancelled_marker = tmp_path / "live-worker-cancelled.txt"

    def synthetic_disconnect_worker(controller, cancel_requested):
        started_marker.write_text(controller.request.job_id, encoding="ascii")
        if not cancel_requested.wait(15):
            return WorkerOutcome(
                status=WorkerJobStatus.CRASHED,
                error="Synthetic disconnect worker was not cancelled",
                http_status=500,
            )
        cancelled_marker.write_text(controller.request.job_id, encoding="ascii")
        return WorkerOutcome(
            status=WorkerJobStatus.CANCELLED,
            error="Synthetic disconnected job was cancelled",
            http_status=409,
        )

    monkeypatch.setattr(WorkerProcess, "run", synthetic_disconnect_worker)
    app = create_app(
        Settings(jobs_root=tmp_path / "jobs", limits=_limits(timeout_seconds=30)),
        token=TOKEN,
    )
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    port = int(listener.getsockname()[1])
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            lifespan="on",
            access_log=False,
            log_config=None,
            timeout_graceful_shutdown=2,
        )
    )
    server_errors: list[BaseException] = []

    def serve() -> None:
        try:
            server.run([listener])
        except BaseException as exc:  # pragma: no cover - asserted below
            server_errors.append(exc)

    server_thread = threading.Thread(target=serve, name="live-uvicorn", daemon=True)
    client_socket: socket.socket | None = None
    server_thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and not server_errors and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started
        assert server_errors == []

        boundary = "----LocalDocForgeDisconnectBoundary"
        pdf_payload = (fixtures_dir / "simple-3page.pdf").read_bytes()
        body = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="files"; filename="source.pdf"\r\n'
            "Content-Type: application/pdf\r\n\r\n"
        ).encode() + pdf_payload
        body += (
            f"\r\n--{boundary}\r\n"
            'Content-Disposition: form-data; name="pages"\r\n\r\n'
            f"1\r\n--{boundary}--\r\n"
        ).encode()
        request_bytes = (
            "POST /api/jobs/extract-pages HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{port}\r\n"
            f"X-LDF-Token: {TOKEN}\r\n"
            f"Content-Type: multipart/form-data; boundary={boundary}\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\n\r\n"
        ).encode() + body
        client_socket = socket.create_connection(("127.0.0.1", port), timeout=5)
        client_socket.sendall(request_bytes)

        deadline = time.monotonic() + 10
        while not started_marker.is_file() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert started_marker.is_file()
        client_socket.shutdown(socket.SHUT_RDWR)
        client_socket.close()
        client_socket = None

        deadline = time.monotonic() + 10
        while not cancelled_marker.is_file() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert cancelled_marker.is_file()
        job_id = started_marker.read_text(encoding="ascii")
        assert cancelled_marker.read_text(encoding="ascii") == job_id
        job = app.state.ldf.jobs[job_id]
        assert job.done.wait(5)
        assert job.status is WorkerJobStatus.CANCELLED
        assert not job._accounted
        assert not job.output_dir.parent.exists()
        assert not any(
            path.name.startswith(".transport-") for path in app.state.ldf.data_root.iterdir()
        )
        assert app.state.ldf.manager.diagnostics()["running"] == 0
        assert app.state.ldf.manager.diagnostics()["queued"] == 0
        with app.state.ldf.manager._lock:
            assert app.state.ldf.manager._active_total == 0
    finally:
        if client_socket is not None:
            with contextlib.suppress(OSError):
                client_socket.close()
        server.should_exit = True
        server_thread.join(10)
        with contextlib.suppress(OSError):
            listener.close()

    assert not server_thread.is_alive()
    assert server_errors == []


def test_cancellation_during_postprocessing_discards_outputs(fixtures_dir, tmp_path, monkeypatch):
    entered_postprocessing = threading.Event()
    release_postprocessing = threading.Event()
    original_ensure_contained = worker_module.ensure_contained

    def pause_worker_output_validation(path, root, *, what="path"):
        if what == "worker output":
            entered_postprocessing.set()
            if not release_postprocessing.wait(10):
                raise TimeoutError("test did not release postprocessing")
        return original_ensure_contained(path, root, what=what)

    monkeypatch.setattr(
        worker_module,
        "ensure_contained",
        pause_worker_output_validation,
    )
    app = create_app(
        Settings(jobs_root=tmp_path / "jobs", limits=_limits()),
        token=TOKEN,
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        try:
            response = client.post(
                "/api/jobs/extract-pages?async=true",
                headers=_auth(Prefer="respond-async"),
                files=[_upload(fixtures_dir / "simple-3page.pdf")],
                data={"pages": "1"},
            )
            assert response.status_code == 202, response.text
            job = app.state.ldf.jobs[response.json()["job_id"]]
            assert entered_postprocessing.wait(15)
            with job.lock:
                controller = job._controller
            assert controller is not None
            assert app.state.ldf.manager.cancel(job)
        finally:
            release_postprocessing.set()

        assert job.done.wait(10)
        assert job.status is WorkerJobStatus.CANCELLED
        assert job.outputs == []
        assert not job.output_dir.parent.exists()
        assert controller.terminate()


def test_public_submit_closes_multipart_form_exactly_once(fixtures_dir, tmp_path, monkeypatch):
    close_counts: dict[int, int] = {}
    original_close = FormData.close

    async def count_and_reject_redundant_close(form):
        identity = id(form)
        close_counts[identity] = close_counts.get(identity, 0) + 1
        if close_counts[identity] > 1:
            raise RuntimeError("synthetic redundant close failure")
        await original_close(form)

    monkeypatch.setattr(FormData, "close", count_and_reject_redundant_close)
    app = create_app(
        Settings(jobs_root=tmp_path / "jobs", limits=_limits()),
        token=TOKEN,
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        response = client.post(
            "/api/jobs/extract-pages",
            headers=_auth(),
            files=[_upload(fixtures_dir / "simple-3page.pdf")],
            data={"pages": "1"},
        )
        assert response.status_code == 201, response.text
        job = app.state.ldf.jobs[response.json()["job_id"]]
        assert job.status is WorkerJobStatus.SUCCESS
    assert close_counts
    assert max(close_counts.values()) == 1


def test_public_submit_outer_close_is_failure_only(fixtures_dir, tmp_path, monkeypatch):
    close_count = 0
    original_close = FormData.close

    async def count_close(form):
        nonlocal close_count
        close_count += 1
        await original_close(form)

    monkeypatch.setattr(FormData, "close", count_close)
    app = create_app(
        Settings(jobs_root=tmp_path / "jobs", limits=_limits()),
        token=TOKEN,
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        response = client.post(
            "/api/jobs/extract-pages",
            headers=_auth(),
            files=[_upload(fixtures_dir / "simple-3page.pdf")],
            data={"unexpected": "value"},
        )

    assert response.status_code == 422
    assert close_count == 1


def test_cancelled_form_close_keeps_single_helper_owner(fixtures_dir, tmp_path, monkeypatch):
    app = create_app(
        Settings(jobs_root=tmp_path / "jobs", limits=_limits()),
        token=TOKEN,
    )
    with TestClient(app, base_url="http://127.0.0.1"):
        state = app.state.ldf
        admission = state.manager.reserve("cancelled-close")
        upload = UploadFile(
            file=io.BytesIO((fixtures_dir / "simple-3page.pdf").read_bytes()),
            filename="synthetic.pdf",
            headers=Headers({"content-type": "application/pdf"}),
        )
        form = FormData([("files", upload), ("pages", "1")])
        close_count = 0
        close_state = [False]

        async def cancelled_close(_form):
            nonlocal close_count
            close_count += 1
            raise asyncio.CancelledError

        monkeypatch.setattr(FormData, "close", cancelled_close)
        try:
            with pytest.raises(asyncio.CancelledError):
                asyncio.run(
                    api_module._submit_job_form(
                        "extract-pages",
                        form,
                        state,
                        admission,
                        respond_async=False,
                        _form_close_state=close_state,
                    )
                )
        finally:
            state.manager.release(admission)

        assert close_state == [True]
        assert close_count == 1
        assert not any(state.data_root.iterdir())


def test_parent_wall_timeout_kills_a_real_hung_worker(tmp_path):
    manager = WorkerManager(
        Settings(
            jobs_root=tmp_path / "jobs",
            limits=_limits(timeout_seconds=0.25),
            api_max_concurrent_jobs=1,
        )
    )
    manager.start()
    try:
        job = _probe_job(manager, tmp_path / "probe-jobs", "hang")
        worker_pid = _wait_running(job)
        assert job.done.wait(10)
        assert job.status is WorkerJobStatus.TIMED_OUT
        _wait_absent(worker_pid)
        assert not (tmp_path / "probe-jobs" / job.job_id).exists()
    finally:
        manager.shutdown()


def test_worker_process_pid_and_terminate_are_safe_after_process_close(tmp_path):
    settings = Settings(jobs_root=tmp_path / "jobs", limits=_limits())
    job_id = uuid.uuid4().hex
    job_root = tmp_path / "probe-jobs" / job_id
    make_private_dir(job_root, exist_ok=False)
    make_private_dir(job_root / "in", exist_ok=False)
    make_private_dir(job_root / "out", exist_ok=False)
    controller = WorkerProcess(
        WorkerRequest(
            job_id=job_id,
            operation="internal-probe",
            job_root=str(job_root),
            input_names=(),
            params={},
            settings_json=settings.model_dump_json(),
            probe="environment",
        )
    )

    outcome = controller.run(threading.Event())
    closed_pid = controller.pid

    assert outcome.status is WorkerJobStatus.SUCCESS
    assert closed_pid is not None
    assert controller.pid == closed_pid
    assert controller.terminate()
    assert controller.terminate()


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object accounting regression")
def test_unverified_windows_job_exit_fails_closed(monkeypatch, tmp_path):
    monkeypatch.setattr(worker_module._WindowsJob, "wait_empty", lambda _job, _timeout: False)
    settings = Settings(jobs_root=tmp_path / "jobs", limits=_limits())
    job_id = uuid.uuid4().hex
    job_root = tmp_path / "probe-jobs" / job_id
    make_private_dir(job_root, exist_ok=False)
    make_private_dir(job_root / "in", exist_ok=False)
    make_private_dir(job_root / "out", exist_ok=False)
    controller = WorkerProcess(
        WorkerRequest(
            job_id=job_id,
            operation="internal-probe",
            job_root=str(job_root),
            input_names=(),
            params={},
            settings_json=settings.model_dump_json(),
            probe="environment",
        )
    )

    outcome = controller.run(threading.Event())

    assert outcome.status is WorkerJobStatus.CRASHED
    assert outcome.error == "Worker process tree exit could not be verified"
    assert outcome.containment["process_tree_exit"] == "unverified; failed closed"


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object API regression")
def test_windows_job_termination_failure_is_checked(monkeypatch):
    class FailingKernel32:
        @staticmethod
        def TerminateJobObject(_handle, _exit_code):
            return 0

    monkeypatch.setattr(ctypes, "WinDLL", lambda *_args, **_kwargs: FailingKernel32())
    job = worker_module._WindowsJob(handle=1, process_handle=0, details={})

    with pytest.raises(OSError, match="TerminateJobObject failed"):
        job.terminate()


@pytest.mark.skipif(os.name != "nt", reason="Windows pre-Job assignment regression")
def test_windows_job_assignment_failure_keeps_gate_closed_and_proves_only_leader_exit(
    monkeypatch, tmp_path
):
    def reject_job_assignment(_pid, _limits, _strict_offline):
        raise OSError("synthetic Job Object assignment failure")

    monkeypatch.setattr(worker_module, "_create_windows_job", reject_job_assignment)
    settings = Settings(jobs_root=tmp_path / "jobs", limits=_limits())
    job_id = uuid.uuid4().hex
    job_root = tmp_path / "probe-jobs" / job_id
    make_private_dir(job_root, exist_ok=False)
    make_private_dir(job_root / "in", exist_ok=False)
    make_private_dir(job_root / "out", exist_ok=False)
    controller = WorkerProcess(
        WorkerRequest(
            job_id=job_id,
            operation="internal-probe",
            job_root=str(job_root),
            input_names=(),
            params={},
            settings_json=settings.model_dump_json(),
            probe="environment",
        )
    )

    outcome = controller.run(threading.Event())
    worker_pid = controller.pid

    assert outcome.status is WorkerJobStatus.CRASHED
    assert outcome.error == "Windows Job Object containment could not be established"
    assert outcome.containment["document_gate"] == "never_opened"
    assert outcome.containment["process_tree"] == "unavailable; worker failed closed"
    assert outcome.containment["process_tree_exit"] == "pre_gate_leader_verified"
    assert outcome.containment["process_tree_exit"] != "verified_empty"
    assert outcome.probe == {}
    assert not (job_root / "worker-temp").exists()
    assert worker_pid is not None
    _wait_absent(worker_pid)


def test_worker_crash_and_malformed_ipc_do_not_wedge_the_slot(monkeypatch, tmp_path):
    manager = WorkerManager(
        Settings(
            jobs_root=tmp_path / "jobs",
            limits=_limits(),
            api_max_concurrent_jobs=1,
            api_max_queued_jobs=2,
        )
    )
    manager.start()
    try:
        crashed = _probe_job(manager, tmp_path / "probe-jobs", "crash")
        assert crashed.done.wait(10)
        assert crashed.status is WorkerJobStatus.CRASHED

        malformed = _probe_job(manager, tmp_path / "probe-jobs", "malformed-ipc")
        assert malformed.done.wait(10)
        assert malformed.status is WorkerJobStatus.CRASHED
        assert malformed.error == "Worker IPC validation failed"

        monkeypatch.setenv("LDF_WORKER_ENV_PROBE", "present-only-in-inherited-environment")
        recovered = _probe_job(manager, tmp_path / "probe-jobs", "environment")
        assert recovered.done.wait(10)
        assert recovered.status is WorkerJobStatus.SUCCESS
        assert recovered.probe == {
            "marker_present": False,
            "temp_root": "worker-temp",
        }
    finally:
        manager.shutdown()


def test_post_worker_path_escape_fails_closed_and_slot_survives(monkeypatch, tmp_path):
    manager = WorkerManager(
        Settings(
            jobs_root=tmp_path / "jobs",
            limits=_limits(),
            api_max_concurrent_jobs=1,
            api_max_queued_jobs=1,
        )
    )
    original_run = WorkerProcess.run
    original_ensure_contained = worker_module.ensure_contained

    def synthetic_escape_outcome(controller, cancel_requested):
        if controller.request.probe != "escaped-output":
            return original_run(controller, cancel_requested)
        output = Path(controller.request.job_root) / "out" / "escaped.pdf"
        output.write_bytes(b"synthetic")
        return WorkerOutcome(
            status=WorkerJobStatus.SUCCESS,
            report=ConversionReport(
                operation="internal-probe",
                status=ReportStatus.SUCCESS,
                job_id=controller.request.job_id,
            ),
            output_names=[output.name],
            http_status=201,
        )

    def reject_worker_output(path, root, *, what="path"):
        if what == "worker output":
            raise PathSecurityError("synthetic worker output escape")
        return original_ensure_contained(path, root, what=what)

    monkeypatch.setattr(WorkerProcess, "run", synthetic_escape_outcome)
    monkeypatch.setattr(worker_module, "ensure_contained", reject_worker_output)
    manager.start()
    try:
        escaped = _probe_job(manager, tmp_path / "probe-jobs", "escaped-output")
        assert escaped.done.wait(10)
        assert escaped.status is WorkerJobStatus.CRASHED
        assert escaped.error == "Worker output containment or cleanup failed"
        assert escaped.outputs == []
        assert not escaped.output_dir.parent.exists()

        recovered = _probe_job(manager, tmp_path / "probe-jobs", "environment")
        assert recovered.done.wait(10)
        assert recovered.status is WorkerJobStatus.SUCCESS
    finally:
        manager.shutdown()


def test_strict_offline_guard_is_installed_inside_spawned_worker(tmp_path):
    manager = WorkerManager(
        Settings(
            strict_offline=True,
            jobs_root=tmp_path / "jobs",
            limits=_limits(),
            api_max_concurrent_jobs=1,
        )
    )
    manager.start()
    try:
        job = _probe_job(manager, tmp_path / "probe-jobs", "network")
        assert job.done.wait(10)
        assert job.status is WorkerJobStatus.SUCCESS
        assert job.probe == {"blocked": True}
        assert "python_socket_guard_only" in str(job.containment["network"])
        assert "not OS-sandboxed" in str(job.containment["network"])
    finally:
        manager.shutdown()


def test_blocked_network_sitecustomize_reaches_spawned_worker(tmp_path):
    manager = WorkerManager(
        Settings(
            jobs_root=tmp_path / "jobs",
            limits=_limits(),
            api_max_concurrent_jobs=1,
        )
    )
    manager.start()
    try:
        job = _probe_job(
            manager,
            tmp_path / "probe-jobs",
            "network-instrumentation",
        )
        assert job.done.wait(10)
        gate_expected = os.environ.get("LDF_BLOCK_NETWORK") == "1"
        assert job.probe["blocked_gate_active"] is gate_expected
        assert job.probe["grandchild_blocked_gate_active"] is gate_expected
        assert job.probe["guard_path_only"] is gate_expected
    finally:
        manager.shutdown()


def test_worker_leader_exit_cannot_orphan_descendant(tmp_path):
    manager = WorkerManager(
        Settings(
            jobs_root=tmp_path / "jobs",
            limits=_limits(timeout_seconds=30),
            api_max_concurrent_jobs=1,
        )
    )
    manager.start()
    try:
        job = _probe_job(manager, tmp_path / "probe-jobs", "orphan-tree")
        assert job.done.wait(10)
        child_event = next(event for event in job.events if event["stage"] == "probe-child")
        child_pid = int(child_event["current"])
        assert job.status is WorkerJobStatus.CRASHED
        assert not _process_exists(child_pid)
        _wait_absent(child_pid)
    finally:
        manager.shutdown()


def test_failed_private_cleanup_is_visible_and_reported(fixtures_dir, tmp_path, monkeypatch):
    monkeypatch.setattr(worker_module, "remove_tree_with_retries", lambda _path: False)
    app = create_app(
        Settings(jobs_root=tmp_path / "jobs", limits=_limits()),
        token=TOKEN,
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        response = client.post(
            "/api/jobs/extract-pages",
            headers=_auth(),
            files=[_upload(fixtures_dir / "garbage.pdf")],
            data={"pages": "1"},
        )
        assert response.status_code == 500
        report = response.json()["report"]
        warning_codes = {warning["code"] for warning in report["security_warnings"]}
        assert "api-private-cleanup-incomplete" in warning_codes
        job = next(reversed(app.state.ldf.jobs.values()))
        assert job.status is WorkerJobStatus.CRASHED
        assert job.containment["private_cleanup"] == "incomplete"


def test_temporary_disk_watchdog_terminates_real_worker_and_cleans(tmp_path):
    manager = WorkerManager(
        Settings(
            jobs_root=tmp_path / "jobs",
            limits=_limits(max_temporary_bytes=256 * 1024),
            api_max_concurrent_jobs=1,
        )
    )
    manager.start()
    try:
        job = _probe_job(manager, tmp_path / "probe-jobs", "disk")
        worker_pid = _wait_running(job)
        assert job.done.wait(10)
        assert job.status is WorkerJobStatus.LIMIT_EXCEEDED
        assert "temporary-disk limit" in job.error
        _wait_absent(worker_pid)
        assert not (tmp_path / "probe-jobs" / job.job_id).exists()
    finally:
        manager.shutdown()


def test_aggregate_output_watchdog_terminates_real_worker_and_cleans(tmp_path):
    manager = WorkerManager(
        Settings(
            jobs_root=tmp_path / "jobs",
            limits=_limits(
                max_output_bytes=256 * 1024,
                max_temporary_bytes=64 * 1024**2,
            ),
            api_max_concurrent_jobs=1,
        )
    )
    manager.start()
    try:
        job = _probe_job(manager, tmp_path / "probe-jobs", "output")
        worker_pid = _wait_running(job)
        assert job.done.wait(10)
        assert job.status is WorkerJobStatus.LIMIT_EXCEEDED
        assert "aggregate output limit" in job.error
        assert job.containment["private_cleanup"] == "complete"
        _wait_absent(worker_pid)
        assert not (tmp_path / "probe-jobs" / job.job_id).exists()
    finally:
        manager.shutdown()


@pytest.mark.parametrize(
    ("probe", "limit_updates", "containment_key"),
    [
        ("cpu", {"max_cpu_seconds": 0.2}, "cpu"),
        ("memory", {"max_memory_bytes": 192 * 1024**2}, "memory"),
        ("tree", {"max_subprocesses": 0}, "child_processes"),
    ],
)
def test_os_resource_limits_stop_real_workers(probe, limit_updates, containment_key, tmp_path):
    if sys.platform == "darwin" and probe == "memory":
        pytest.skip("RLIMIT_AS is not a reliable memory ceiling on macOS")
    manager = WorkerManager(
        Settings(
            jobs_root=tmp_path / "jobs",
            limits=_limits(timeout_seconds=5, **limit_updates),
            api_max_concurrent_jobs=1,
        )
    )
    manager.start()
    try:
        job = _probe_job(manager, tmp_path / "probe-jobs", probe)
        worker_pid = _wait_running(job)
        assert job.done.wait(10)
        assert job.status in {
            WorkerJobStatus.CRASHED,
            WorkerJobStatus.LIMIT_EXCEEDED,
        }
        _wait_absent(worker_pid)
        assert job.containment[containment_key] not in {"disabled", "unsupported"}
    finally:
        manager.shutdown()


def test_cancelled_queue_tombstone_releases_physical_capacity(tmp_path):
    manager = WorkerManager(
        Settings(
            jobs_root=tmp_path / "jobs",
            limits=_limits(timeout_seconds=30),
            api_max_concurrent_jobs=1,
            api_max_queued_jobs=1,
            api_max_active_jobs_per_client=3,
        )
    )
    manager.start()
    try:
        running = _probe_job(manager, tmp_path / "probe-jobs", "hang", client="running")
        _wait_running(running)
        cancelled = _probe_job(
            manager,
            tmp_path / "probe-jobs",
            "hang",
            client="cancelled",
        )
        assert cancelled.status is WorkerJobStatus.QUEUED
        assert manager.cancel(cancelled)
        assert cancelled.done.wait(5)

        replacement = _probe_job(
            manager,
            tmp_path / "probe-jobs",
            "hang",
            client="replacement",
        )

        assert replacement.status is WorkerJobStatus.QUEUED
        assert manager.diagnostics()["queued"] == 1
    finally:
        manager.shutdown()


def test_dequeued_job_cancellation_owns_cleanup_and_terminal_publication(tmp_path, monkeypatch):
    manager = WorkerManager(
        Settings(
            jobs_root=tmp_path / "jobs",
            limits=_limits(timeout_seconds=30),
            api_max_concurrent_jobs=1,
            api_max_queued_jobs=1,
        )
    )
    dispatcher_dequeued = threading.Event()
    release_dispatcher = threading.Event()
    dispatcher_returned = threading.Event()
    cleanup_started = threading.Event()
    release_cleanup = threading.Event()
    target_root: Path | None = None
    cancel_results: list[bool] = []
    cancel_errors: list[BaseException] = []
    cancel_thread: threading.Thread | None = None
    original_run_job = manager._run_job
    original_cleanup = worker_module._remove_tree_safely

    def paused_dispatch(job):
        dispatcher_dequeued.set()
        if not release_dispatcher.wait(10):
            raise TimeoutError("test did not release dispatcher")
        try:
            original_run_job(job)
        finally:
            dispatcher_returned.set()

    def paused_cleanup(path):
        if target_root is not None and path == target_root:
            cleanup_started.set()
            if not release_cleanup.wait(10):
                return False
        return original_cleanup(path)

    monkeypatch.setattr(manager, "_run_job", paused_dispatch)
    monkeypatch.setattr(worker_module, "_remove_tree_safely", paused_cleanup)
    manager.start()
    try:
        job = _probe_job(
            manager,
            tmp_path / "probe-jobs",
            "hang",
            client="forced-race",
        )
        target_root = job.output_dir.parent
        assert dispatcher_dequeued.wait(5)

        def cancel_job() -> None:
            try:
                cancel_results.append(manager.cancel(job))
            except BaseException as exc:  # pragma: no cover - asserted below
                cancel_errors.append(exc)

        cancel_thread = threading.Thread(target=cancel_job, name="forced-race-canceller")
        cancel_thread.start()
        assert cleanup_started.wait(5)

        with job.lock:
            assert job.status is WorkerJobStatus.QUEUED
            assert not job.done.is_set()
            assert job._accounted
            assert job.request is None
            assert {event["stage"] for event in job.events}.isdisjoint(
                {"cancelled", "crashed", "running"}
            )

        release_dispatcher.set()
        assert dispatcher_returned.wait(5)
        with job.lock:
            assert job.status is WorkerJobStatus.QUEUED
            assert not job.done.is_set()
            assert job._accounted

        release_cleanup.set()
        cancel_thread.join(5)
        assert not cancel_thread.is_alive()
        assert cancel_errors == []
        assert cancel_results == [True]
        assert job.done.is_set()
        assert job.status is WorkerJobStatus.CANCELLED
        assert not job._accounted
        stages = [event["stage"] for event in job.events]
        assert stages.count("cancelled") == 1
        assert "crashed" not in stages
        assert "running" not in stages
        assert not target_root.exists()
    finally:
        release_dispatcher.set()
        release_cleanup.set()
        if cancel_thread is not None:
            cancel_thread.join(5)
        manager.shutdown()


def test_queue_per_client_rate_and_concurrency_admission_are_bounded(tmp_path):
    settings = Settings(
        jobs_root=tmp_path / "jobs",
        limits=_limits(timeout_seconds=30),
        api_max_concurrent_jobs=1,
        api_max_queued_jobs=1,
        api_max_active_jobs_per_client=2,
        api_rate_limit_jobs=10,
    )
    manager = WorkerManager(settings)
    manager.start()
    try:
        running = _probe_job(manager, tmp_path / "probe-jobs", "hang", client="same-client")
        _wait_running(running)
        queued = _probe_job(manager, tmp_path / "probe-jobs", "hang", client="same-client")
        time.sleep(0.1)
        assert queued.status is WorkerJobStatus.QUEUED

        with pytest.raises(AdmissionError) as per_client:
            manager.reserve("same-client")
        assert per_client.value.status_code == 429
        with pytest.raises(AdmissionError) as saturated:
            manager.reserve("different-client")
        assert saturated.value.status_code == 503
    finally:
        manager.shutdown()
    assert running.status is WorkerJobStatus.CANCELLED
    assert queued.status is WorkerJobStatus.CANCELLED

    rate_manager = WorkerManager(
        settings.model_copy(
            update={
                "api_rate_limit_jobs": 1,
                "api_max_active_jobs_per_client": 3,
            }
        )
    )
    rate_manager.start()
    try:
        reservation = rate_manager.reserve("rate-client")
        rate_manager.release(reservation)
        with pytest.raises(AdmissionError) as limited:
            rate_manager.reserve("rate-client")
        assert limited.value.status_code == 429
        assert limited.value.retry_after is not None
    finally:
        rate_manager.shutdown()


def test_shutdown_terminates_running_worker_and_discards_queue(tmp_path):
    manager = WorkerManager(
        Settings(
            jobs_root=tmp_path / "jobs",
            limits=_limits(timeout_seconds=30),
            api_max_concurrent_jobs=1,
            api_max_queued_jobs=1,
        )
    )
    manager.start()
    running = _probe_job(manager, tmp_path / "probe-jobs", "hang", client="client-a")
    worker_pid = _wait_running(running)
    queued = _probe_job(manager, tmp_path / "probe-jobs", "hang", client="client-b")
    manager.shutdown()

    assert running.done.is_set() and running.status is WorkerJobStatus.CANCELLED
    assert queued.done.is_set() and queued.status is WorkerJobStatus.CANCELLED
    _wait_absent(worker_pid)
    assert not (tmp_path / "probe-jobs" / running.job_id).exists()
    assert not (tmp_path / "probe-jobs" / queued.job_id).exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows console-control lifecycle regression")
def test_windows_console_control_gracefully_stops_real_cli_server(tmp_path):
    jobs_root = tmp_path / "console-jobs"
    log_path = tmp_path / "console-server.log"
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    port = int(listener.getsockname()[1])
    listener.close()

    environment = os.environ.copy()
    source_root = Path(worker_module.__file__).resolve().parents[2]
    prior_pythonpath = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        str(source_root)
        if not prior_pythonpath
        else f"{source_root}{os.pathsep}{prior_pythonpath}"
    )
    environment["PYTHONUNBUFFERED"] = "1"
    environment["LDF_JOBS_ROOT"] = str(jobs_root)
    command = [
        sys.executable,
        "-m",
        "localdocforge.cli.main",
        "web",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
    ]

    server = None
    with log_path.open("wb") as log_stream:
        server = subprocess.Popen(  # noqa: S603 - repository-owned CLI under test
            command,
            cwd=Path(__file__).resolve().parents[2],
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=log_stream,
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )
        try:
            deadline = time.monotonic() + 20
            session_roots: list[Path] = []
            while time.monotonic() < deadline:
                if server.poll() is not None:
                    break
                session_roots = list((jobs_root / "api-data").glob("ldf-api-*"))
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                        if len(session_roots) == 1:
                            break
                except OSError:
                    pass
                time.sleep(0.05)
            assert server.poll() is None
            assert len(session_roots) == 1
            lease_path = api_module._session_lease_path(
                jobs_root / "api-data", session_roots[0]
            )
            assert lease_path.is_file()

            # CTRL_BREAK is the targetable Windows console-control equivalent:
            # unlike CTRL_C it can safely address only this new process group.
            server.send_signal(signal.CTRL_BREAK_EVENT)
            assert server.wait(timeout=20) == 0
        finally:
            if server.poll() is None:
                server.kill()
                server.wait(timeout=10)

    assert not (jobs_root / "api-data").exists(), log_path.read_text(
        encoding="utf-8", errors="replace"
    )


def test_incomplete_manager_shutdown_retains_jobs_and_session_root(tmp_path, monkeypatch, caplog):
    app = create_app(
        Settings(jobs_root=tmp_path / "jobs", limits=_limits()),
        token=TOKEN,
    )
    caplog.set_level(logging.ERROR)
    with TestClient(app, base_url="http://127.0.0.1"):
        state = app.state.ldf
        retained_job, _observed = _unpublished_terminal_job(state)
        retained_root = state.data_root
        marker = retained_root / "incomplete-shutdown-marker"
        marker.write_text("retain until stale cleanup", encoding="utf-8")
        original_shutdown = state.manager.shutdown

        def forced_incomplete_shutdown():
            diagnostics = original_shutdown()
            diagnostics["shutdown_complete"] = False
            diagnostics["shutdown_incomplete_workers"] = 1
            return diagnostics

        monkeypatch.setattr(state.manager, "shutdown", forced_incomplete_shutdown)

    assert retained_root.is_dir()
    assert marker.is_file()
    assert retained_job.job_id in state.jobs
    assert state.shutdown_diagnostics["shutdown_complete"] is False
    assert state.shutdown_diagnostics["session_cleanup_complete"] is False
    assert state.shutdown_diagnostics["cleanup_failed_closed"] is True
    assert "Worker shutdown incomplete; private session cleanup deferred" in caplog.text
    assert str(retained_root) not in caplog.text


def test_eviction_waits_for_publication_lock_and_requires_done(tmp_path, monkeypatch):
    app = create_app(
        Settings(jobs_root=tmp_path / "jobs", limits=_limits()),
        token=TOKEN,
    )
    with TestClient(app, base_url="http://127.0.0.1"):
        state = app.state.ldf
        state.max_recent_jobs = 0
        job, observed_lock = _unpublished_terminal_job(state)
        partial_status = threading.Event()
        release_publication = threading.Event()
        remove_called = threading.Event()
        original_remove = api_module._remove_private_job

        def observed_remove(path):
            remove_called.set()
            original_remove(path)

        def partial_publish() -> None:
            with job.lock:
                job.status = WorkerJobStatus.SUCCESS
                partial_status.set()
                release_publication.wait(10)

        monkeypatch.setattr(api_module, "_remove_private_job", observed_remove)
        publisher = threading.Thread(target=partial_publish, name="partial-publisher")
        publisher.start()
        assert partial_status.wait(5)
        observed_lock.attempted.clear()
        evictor = threading.Thread(
            target=api_module._evict_completed_jobs,
            args=(state,),
            name="concurrent-evictor",
        )
        evictor.start()
        try:
            assert observed_lock.attempted.wait(5)
            assert evictor.is_alive()
            assert not remove_called.is_set()
            assert job.output_dir.parent.is_dir()
        finally:
            release_publication.set()
            publisher.join(5)
            evictor.join(5)

        assert not publisher.is_alive()
        assert not evictor.is_alive()
        assert not job.done.is_set()
        assert job.job_id in state.jobs
        assert job.output_dir.parent.is_dir()
        assert not remove_called.is_set()

        with job.lock:
            job.done.set()
        api_module._evict_completed_jobs(state)
        assert remove_called.is_set()
        assert job.job_id not in state.jobs
        assert not job.output_dir.parent.exists()


def test_delete_waits_for_publication_lock_and_requires_done(tmp_path, monkeypatch):
    app = create_app(
        Settings(jobs_root=tmp_path / "jobs", limits=_limits()),
        token=TOKEN,
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        state = app.state.ldf
        job, observed_lock = _unpublished_terminal_job(state)
        partial_status = threading.Event()
        release_publication = threading.Event()
        remove_called = threading.Event()
        responses = []
        request_errors: list[BaseException] = []
        original_remove = api_module._remove_private_job

        def observed_remove(path):
            remove_called.set()
            original_remove(path)

        def partial_publish() -> None:
            with job.lock:
                job.status = WorkerJobStatus.SUCCESS
                partial_status.set()
                release_publication.wait(10)

        def delete_job() -> None:
            try:
                responses.append(client.delete(f"/api/jobs/{job.job_id}", headers=_auth()))
            except BaseException as exc:  # pragma: no cover - asserted below
                request_errors.append(exc)

        monkeypatch.setattr(api_module, "_remove_private_job", observed_remove)
        publisher = threading.Thread(target=partial_publish, name="partial-publisher")
        publisher.start()
        assert partial_status.wait(5)
        observed_lock.attempted.clear()
        deleter = threading.Thread(target=delete_job, name="concurrent-deleter")
        deleter.start()
        try:
            assert observed_lock.attempted.wait(5)
            assert deleter.is_alive()
            assert not remove_called.is_set()
            assert job.output_dir.parent.is_dir()
        finally:
            release_publication.set()
            publisher.join(5)
            deleter.join(5)

        assert request_errors == []
        assert len(responses) == 1
        assert responses[0].status_code == 409
        assert not job.done.is_set()
        assert job.job_id in state.jobs
        assert job.output_dir.parent.is_dir()
        assert not remove_called.is_set()

        with job.lock:
            job.done.set()
        completed_delete = client.delete(f"/api/jobs/{job.job_id}", headers=_auth())
        assert completed_delete.status_code == 200
        assert remove_called.is_set()
        assert job.job_id not in state.jobs
        assert not job.output_dir.parent.exists()


def test_active_download_lease_blocks_delete_until_stream_finishes(tmp_path, monkeypatch):
    app = create_app(
        Settings(jobs_root=tmp_path / "jobs", limits=_limits()),
        token=TOKEN,
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        state = app.state.ldf
        job, _observed_lock = _unpublished_terminal_job(state)
        output = job.output_dir / "leased-output.pdf"
        payload = b"synthetic-download-payload"
        output.write_bytes(payload)
        with job.lock:
            job.outputs = [output]
            job.status = WorkerJobStatus.SUCCESS
            job.done.set()

        stream_started = threading.Event()
        release_stream = threading.Event()
        responses = []
        request_errors: list[BaseException] = []
        original_file_response = api_module.FileResponse.__call__

        async def paused_file_response(response, scope, receive, send):
            stream_started.set()
            await asyncio.to_thread(release_stream.wait, 10)
            return await original_file_response(response, scope, receive, send)

        def download() -> None:
            try:
                responses.append(
                    client.get(f"/api/jobs/{job.job_id}/outputs/0", headers=_auth())
                )
            except BaseException as exc:  # pragma: no cover - asserted below
                request_errors.append(exc)

        monkeypatch.setattr(api_module.FileResponse, "__call__", paused_file_response)
        downloader = threading.Thread(target=download, name="leased-output-downloader")
        downloader.start()
        try:
            assert stream_started.wait(5)
            with job.lock:
                assert job.active_downloads == 1

            refused = client.delete(f"/api/jobs/{job.job_id}", headers=_auth())
            assert refused.status_code == 409
            assert "active output downloads" in refused.json()["detail"]
            assert job.output_dir.parent.is_dir()
        finally:
            release_stream.set()
            downloader.join(10)

        assert not downloader.is_alive()
        assert request_errors == []
        assert len(responses) == 1
        assert responses[0].status_code == 200
        assert responses[0].content == payload
        with job.lock:
            assert job.active_downloads == 0

        deleted = client.delete(f"/api/jobs/{job.job_id}", headers=_auth())
        assert deleted.status_code == 200
        assert not job.output_dir.parent.exists()


@pytest.mark.parametrize("endpoint_kind", ["list", "events"])
def test_job_snapshots_wait_for_publication_lock(endpoint_kind, tmp_path):
    app = create_app(
        Settings(jobs_root=tmp_path / "jobs", limits=_limits()),
        token=TOKEN,
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        state = app.state.ldf
        job, observed_lock = _unpublished_terminal_job(state)
        job.add_event("running", message="job running")
        partial_status = threading.Event()
        release_publication = threading.Event()
        responses = []
        request_errors: list[BaseException] = []

        def partial_publish() -> None:
            with job.lock:
                job.status = WorkerJobStatus.SUCCESS
                job.add_event("success", message="job success")
                partial_status.set()
                release_publication.wait(10)
                job.done.set()

        endpoint = "/api/jobs"
        if endpoint_kind == "events":
            endpoint = f"/api/jobs/{job.job_id}/events"

        def read_snapshot() -> None:
            try:
                responses.append(client.get(endpoint, headers=_auth()))
            except BaseException as exc:  # pragma: no cover - asserted below
                request_errors.append(exc)

        publisher = threading.Thread(target=partial_publish, name="partial-publisher")
        publisher.start()
        assert partial_status.wait(5)
        observed_lock.attempted.clear()
        reader = threading.Thread(target=read_snapshot, name=f"{endpoint_kind}-reader")
        reader.start()
        try:
            assert observed_lock.attempted.wait(5)
            assert reader.is_alive()
        finally:
            release_publication.set()
            publisher.join(5)
            reader.join(5)

        assert not publisher.is_alive()
        assert not reader.is_alive()
        assert request_errors == []
        assert len(responses) == 1
        assert responses[0].status_code == 200
        payload = responses[0].json()
        if endpoint_kind == "list":
            row = next(item for item in payload["jobs"] if item["job_id"] == job.job_id)
            assert row["status"] == "success"
        else:
            assert payload["status"] == "success"
            assert payload["events"][-1]["stage"] == "success"


def test_stale_session_cleanup_requires_an_unlocked_owner_lease(tmp_path):
    api_root = tmp_path / "api-data"
    current = api_root / f"ldf-api-{uuid.uuid4().hex}"
    stale = api_root / f"ldf-api-{uuid.uuid4().hex}"
    unleased = api_root / f"ldf-api-{uuid.uuid4().hex}"
    malformed = api_root / "ldf-api-not-a-session-id"
    unrelated = api_root / "unrelated"
    for path in (current, stale, unleased, malformed, unrelated):
        path.mkdir(parents=True)
        (path / "marker").write_text(path.name, encoding="utf-8")
    lease = api_module._try_acquire_session_lease(
        api_module._session_lease_path(api_root, stale),
        create=True,
    )
    assert lease is not None
    lease.close()

    api_module._cleanup_stale_api_sessions(
        api_root,
        current,
        max_age=0,
    )

    assert not stale.exists()
    assert current.is_dir()
    assert unleased.is_dir()
    assert malformed.is_dir()
    assert unrelated.is_dir()


def test_live_session_lease_survives_cleanup_and_crash_release_is_collected(tmp_path):
    api_root = tmp_path / "api-data"
    live = api_root / f"ldf-api-{uuid.uuid4().hex}"
    current = api_root / f"ldf-api-{uuid.uuid4().hex}"
    live.mkdir(parents=True)
    (live / "marker").write_text("live-session", encoding="utf-8")
    lease_path = api_module._session_lease_path(api_root, live)
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    holder = context.Process(
        target=_hold_api_session_lease,
        args=(str(lease_path), ready),
        name="ldf-test-session-lease-holder",
    )
    holder.start()
    try:
        assert ready.wait(10), f"lease holder exited early with {holder.exitcode}"

        api_module._cleanup_stale_api_sessions(api_root, current, max_age=0)
        assert live.is_dir()
        assert lease_path.is_file()

        holder.kill()
        holder.join(10)
        assert not holder.is_alive()

        api_module._cleanup_stale_api_sessions(api_root, current, max_age=24 * 3600)
        assert not live.exists()
        assert not lease_path.exists()
    finally:
        if holder.is_alive():
            holder.kill()
            holder.join(10)


@pytest.mark.skipif(os.name != "nt", reason="Windows parent-death Job Object regression")
def test_windows_parent_death_kills_worker_tree_and_restart_collects_session(tmp_path):
    jobs_root = tmp_path / "restart-jobs"
    context = multiprocessing.get_context("spawn")
    evidence_reader, evidence_writer = context.Pipe(duplex=False)
    parent = context.Process(
        target=_run_api_parent_with_hung_tree,
        args=(str(jobs_root), evidence_writer),
        name="ldf-test-api-parent",
    )
    parent.start()
    evidence_writer.close()
    try:
        assert evidence_reader.poll(25), f"API parent exited early with {parent.exitcode}"
        evidence = evidence_reader.recv()
        session_root = Path(evidence["session_root"])
        lease_path = Path(evidence["lease_path"])
        assert session_root.is_dir()
        assert lease_path.is_file()
        assert _process_exists(evidence["worker_pid"])
        assert _process_exists(evidence["child_pid"])

        parent.kill()
        parent.join(10)
        assert not parent.is_alive()
        _wait_absent(evidence["worker_pid"], timeout=15)
        _wait_absent(evidence["child_pid"], timeout=15)

        # A fresh API lifespan is the restart boundary. It may collect only
        # after the dead parent's kernel lease has been released.
        restarted = create_app(
            Settings(jobs_root=jobs_root, limits=_limits()),
            token=TOKEN,
        )
        with TestClient(restarted, base_url="http://127.0.0.1"):
            assert not session_root.exists()
            assert not lease_path.exists()
    finally:
        evidence_reader.close()
        if parent.is_alive():
            parent.kill()
            parent.join(10)


def test_api_rate_limit_rejects_before_second_upload_is_saved(fixtures_dir, tmp_path):
    settings = Settings(
        jobs_root=tmp_path / "jobs",
        limits=_limits(),
        api_rate_limit_jobs=1,
        api_rate_limit_window_seconds=60,
    )
    app = create_app(settings, token=TOKEN)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        first = client.post(
            "/api/jobs/extract-pages",
            headers=_auth(),
            files=[_upload(fixtures_dir / "simple-3page.pdf")],
            data={"pages": "1"},
        )
        assert first.status_code == 201
        job_count = len(app.state.ldf.jobs)
        second = client.post(
            "/api/jobs/extract-pages",
            headers=_auth(),
            files=[_upload(fixtures_dir / "simple-3page.pdf")],
            data={"pages": "1"},
        )
        assert second.status_code == 429
        assert second.headers["Retry-After"]
        assert len(app.state.ldf.jobs) == job_count
