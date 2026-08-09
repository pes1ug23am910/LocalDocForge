"""Localhost-only HTTP API.

Security model (docs/THREAT_MODEL.md §T7):
- Loopback only: the runner refuses non-loopback binds; the app additionally
  rejects any request whose Host header is not a loopback name.
- Auth: a per-session random token. GET / sets it as an HttpOnly
  SameSite=Strict cookie for the browser; every /api request must present it
  in the ``X-LDF-Token`` header (double-submit — the cookie alone is never
  enough), which also defeats CSRF because cross-origin pages cannot set
  custom headers without a CORS preflight we never grant.
- No CORS middleware is installed: no cross-origin grants exist at all.
- CSP and hardening headers on every response.
- Browser payloads never carry filesystem paths: files are uploaded
  (multipart) and results are downloaded by job id + artifact index. Job ids
  are unguessable (uuid4). Outputs are served only from the owning job's
  directory after containment checks.
- Upload size limits enforced while streaming to disk, not after.
- Job history is in memory only and dies with the process (privacy default).
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import math
import os
import re
import secrets
import tempfile
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response

# Raw request.form() yields Starlette's UploadFile (FastAPI's subclasses it),
# so containment/auth code must test against the Starlette base class.
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser
from starlette.types import Receive, Scope, Send

from localdocforge import __version__
from localdocforge.api.worker import (
    Admission,
    AdmissionError,
    WorkerJob,
    WorkerJobStatus,
    WorkerManager,
    WorkerRequest,
)
from localdocforge.config.settings import Settings, get_settings
from localdocforge.domain.models import ConversionReport
from localdocforge.domain.pages import PageRange, PageRangeError
from localdocforge.engines.base import EngineUnavailableError
from localdocforge.engines.registry import default_registry
from localdocforge.jobs.workspace import (
    CollisionPolicy,
    default_jobs_root,
    make_private_dir,
    remove_tree_with_retries,
)
from localdocforge.operations import images as image_ops
from localdocforge.operations import markdown as markdown_ops
from localdocforge.operations import optimize as optimize_ops
from localdocforge.operations import organize as organize_ops
from localdocforge.operations import text as text_ops
from localdocforge.pipelines.runner import PipelineError
from localdocforge.security.filenames import sanitize_filename
from localdocforge.security.paths import ensure_contained

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})
_TOKEN_HEADER = "X-LDF-Token"  # noqa: S105 - header name, not a credential
_TOKEN_COOKIE = "ldf_token"  # noqa: S105 - cookie name, not a credential
_API_SESSION_PREFIX = "ldf-api-"
_API_SESSION_PATTERN = re.compile(r"^ldf-api-[0-9a-f]{32}$")
_API_SESSION_LEASE_DIR = ".leases"
_MULTIPART_OVERHEAD_BYTES = 2 * 1024 * 1024
_MULTIPART_SPOOL_MEMORY_BYTES = 64 * 1024
_MAX_MULTIPART_FILES = 256
_MAX_FORM_FIELDS = 100
_MAX_FORM_FIELD_BYTES = 64 * 1024
_DISCONNECT_POLL_SECONDS = 0.05
_DISCONNECT_FINALIZE_SECONDS = 5.0
_LEGACY_API_JOB = re.compile(r"^[0-9a-f]{32}$")
_LOG = logging.getLogger(__name__)
_TERMINAL_JOB_STATUSES = frozenset(
    {
        WorkerJobStatus.SUCCESS,
        WorkerJobStatus.FAILED,
        WorkerJobStatus.CANCELLED,
        WorkerJobStatus.TIMED_OUT,
        WorkerJobStatus.CRASHED,
        WorkerJobStatus.LIMIT_EXCEEDED,
    }
)


@dataclass
class _SessionLease:
    path: Path
    stream: BinaryIO

    def close(self) -> None:
        self.stream.close()


@dataclass
class ApiState:
    token: str
    settings: Settings
    api_root: Path
    data_root: Path
    manager: WorkerManager
    allow_nonlocal: bool = False
    jobs: dict[str, WorkerJob] = field(default_factory=dict)
    max_recent_jobs: int = 50
    shutdown_diagnostics: dict[str, int | bool] = field(default_factory=dict)
    session_lease: _SessionLease | None = None


class _JobFileResponse(FileResponse):
    """Keep a job output leased until streaming finishes or disconnects."""

    def __init__(self, path: Path, *, filename: str, job: WorkerJob) -> None:
        super().__init__(path, filename=filename)
        self._job = job

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            with self._job.lock:
                self._job.active_downloads = max(0, self._job.active_downloads - 1)


def _hardening_headers(response: Response) -> None:
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
        "script-src 'self'; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; "
        "base-uri 'none'; form-action 'self'"
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"


def _host_is_loopback(request: Request) -> bool:
    host = request.headers.get("host", "")
    hostname = host.rsplit(":", 1)[0] if not host.startswith("[") else host.split("]")[0] + "]"
    return hostname.lower() in _LOOPBACK_HOSTS


def _client_token(request: Request) -> str | None:
    return request.headers.get(_TOKEN_HEADER)


class _RequestTooLarge(OSError):
    """Abort multipart parsing while still triggering Starlette's spool cleanup."""

    pass


class _ContainedMultiPartParser(MultiPartParser):
    """Starlette multipart parser with request-scoped disk and aggregate bounds."""

    spool_max_size = _MULTIPART_SPOOL_MEMORY_BYTES

    def __init__(
        self,
        *args,
        spool_directory: Path,
        max_file_bytes: int | None,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._spool_directory = spool_directory
        self._max_file_bytes = max_file_bytes
        self._file_bytes = 0

    def on_headers_finished(self) -> None:
        super().on_headers_finished()
        upload = self._current_part.file
        if upload is None:
            return
        ambient_spool = self._files_to_close_on_error[-1]
        contained_spool = tempfile.SpooledTemporaryFile(
            max_size=self.spool_max_size,
            mode="w+b",
            dir=str(self._spool_directory),
        )
        upload.file = contained_spool
        self._files_to_close_on_error[-1] = contained_spool
        ambient_spool.close()

    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        if self._current_part.file is not None:
            self._file_bytes += end - start
            if self._max_file_bytes is not None and self._file_bytes > self._max_file_bytes:
                raise _RequestTooLarge
        super().on_part_data(data, start, end)


def _error_response(status: int, message: str) -> JSONResponse:
    response = JSONResponse({"detail": message}, status_code=status)
    _hardening_headers(response)
    return response


def _contains_request_too_large(exc: BaseException) -> bool:
    if isinstance(exc, _RequestTooLarge):
        return True
    if isinstance(exc, BaseExceptionGroup):
        return any(_contains_request_too_large(child) for child in exc.exceptions)
    return (exc.__cause__ is not None and _contains_request_too_large(exc.__cause__)) or (
        exc.__context__ is not None and _contains_request_too_large(exc.__context__)
    )


def _public_report(report: ConversionReport, private_roots: tuple[Path, ...] = ()) -> dict:
    """Return an API-safe report without server-side filesystem paths."""
    payload = json.loads(report.model_dump_json())
    replacements: dict[str, str] = {}
    for collection in ("inputs", "outputs"):
        for artifact in payload.get(collection, []):
            private_path = str(artifact.get("path", ""))
            public_name = Path(private_path).name
            if private_path:
                replacements[private_path] = public_name
            artifact["path"] = public_name

    def scrub(value):
        if isinstance(value, str):
            for private_path, public_name in replacements.items():
                value = value.replace(private_path, public_name)
            for root in private_roots:
                value = value.replace(str(root), "<private-job-root>")
                value = value.replace(root.as_posix(), "<private-job-root>")
            return value
        if isinstance(value, list):
            return [scrub(item) for item in value]
        if isinstance(value, dict):
            return {key: scrub(item) for key, item in value.items()}
        return value

    return scrub(payload)


def _public_error_message(
    message: str,
    report: ConversionReport | None,
    private_roots: tuple[Path, ...] = (),
) -> str:
    if report is not None:
        for artifact in (*report.inputs, *report.outputs):
            message = message.replace(str(artifact.path), artifact.path.name)
    for root in private_roots:
        message = message.replace(str(root), "<private-job-root>")
        message = message.replace(root.as_posix(), "<private-job-root>")
    return message


def _session_lease_path(root: Path, session: Path) -> Path:
    return root / _API_SESSION_LEASE_DIR / f"{session.name}.lock"


def _try_acquire_session_lease(path: Path, *, create: bool = False) -> _SessionLease | None:
    """Acquire an OS-released exclusive lease, or return ``None`` fail-closed."""
    stream: BinaryIO | None = None
    try:
        if create:
            make_private_dir(path.parent)
            stream = path.open("a+b")
        else:
            if not path.is_file():
                return None
            stream = path.open("r+b")
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return _SessionLease(path=path, stream=stream)
    except (ImportError, OSError):
        if stream is not None:
            stream.close()
        return None


def _cleanup_stale_api_sessions(root: Path, current: Path, *, max_age: float = 24 * 3600) -> None:
    """Remove only sessions whose external lease proves that no owner remains."""
    del max_age  # retained for call compatibility; age is not proof of owner death
    if not root.is_dir():
        return
    for entry in root.iterdir():
        if entry == current or not entry.is_dir():
            continue
        if entry.name == _API_SESSION_LEASE_DIR:
            continue
        if not _API_SESSION_PATTERN.fullmatch(entry.name):
            continue
        lease_path = _session_lease_path(root, entry)
        lease = _try_acquire_session_lease(lease_path)
        if lease is None:
            # A locked lease means a live owner. A missing/unreadable lease has
            # no liveness proof, so preserve the session rather than guessing.
            continue
        try:
            removed = remove_tree_with_retries(entry)
        finally:
            lease.close()
        if removed:
            with contextlib.suppress(OSError):
                lease_path.unlink()


def _remove_private_job(path: Path) -> None:
    if not remove_tree_with_retries(path):
        raise _ApiError(500, "Unable to remove private job data; close open output files and retry")


def _client_key(request: Request) -> str:
    """Stable admission key without trusting proxy-controlled headers."""
    return request.client.host if request.client is not None else "unknown-client"


def _api_input_limit(settings: Settings) -> int:
    job_limit = settings.limits.max_input_bytes
    if job_limit is None:
        return settings.api_max_upload_bytes
    return min(job_limit, settings.api_max_upload_bytes)


def _api_transport_limit(settings: Settings) -> int:
    """Bound parent-process upload spooling by both input and temp budgets."""
    limit = _api_input_limit(settings)
    temporary_limit = settings.limits.max_temporary_bytes
    return limit if temporary_limit is None else min(limit, temporary_limit)


def _async_requested(request: Request) -> bool:
    raw = request.query_params.get("async")
    if raw is not None:
        normalized = raw.strip().casefold()
        if normalized in {"1", "true", "yes"}:
            return True
        if normalized not in {"0", "false", "no", ""}:
            raise _ApiError(422, "'async' must be true or false")
    preferences = {item.strip().casefold() for item in request.headers.get("prefer", "").split(",")}
    return "respond-async" in preferences


def _job_state_payload(job: WorkerJob, private_roots: tuple[Path, ...]) -> dict:
    with job.lock:
        if job.report is not None:
            payload = _public_report(job.report, private_roots)
            payload["report_status"] = payload.get("status")
            payload["status"] = job.status.value
            payload["api_status"] = job.status.value
        else:
            payload = {
                "job_id": job.job_id,
                "operation": job.operation,
                "status": job.status.value,
            }
            if job.error:
                payload["detail"] = (
                    "Internal worker error" if job.error_status >= 500 else job.error
                )
        payload["created_at"] = job.created_at.isoformat()
        payload["started_at"] = job.started_at.isoformat() if job.started_at else None
        payload["finished_at"] = job.finished_at.isoformat() if job.finished_at else None
        payload["containment"] = dict(job.containment)
        if job.events:
            payload["progress"] = dict(job.events[-1])
        return payload


def _submission_payload(job: WorkerJob, private_roots: tuple[Path, ...]) -> dict:
    with job.lock:
        payload = {
            "job_id": job.job_id,
            "operation": job.operation,
            "status": job.status.value,
            "status_url": f"/api/jobs/{job.job_id}",
            "events_url": f"/api/jobs/{job.job_id}/events",
            "cancel_url": f"/api/jobs/{job.job_id}/cancel",
        }
        if job.report is not None:
            payload["report"] = _public_report(job.report, private_roots)
        payload["containment"] = dict(job.containment)
        payload["outputs"] = [
            {"index": index, "name": path.name, "size_bytes": path.stat().st_size}
            for index, path in enumerate(job.outputs)
            if path.is_file()
        ]
        return payload


def _sync_job_response(job: WorkerJob, private_roots: tuple[Path, ...]) -> JSONResponse:
    if job.status is WorkerJobStatus.SUCCESS:
        return JSONResponse(_submission_payload(job, private_roots), status_code=201)
    detail = "Internal server error" if job.error_status >= 500 else job.error
    payload: dict[str, object] = {"detail": detail or "Job failed"}
    if job.report is not None:
        payload["report"] = _public_report(job.report, private_roots)
    return JSONResponse(payload, status_code=job.error_status)


def _evict_completed_jobs(state: ApiState) -> None:
    while len(state.jobs) > state.max_recent_jobs:
        oldest = None
        for candidate in state.jobs.values():
            with candidate.lock:
                completed = (
                    candidate.done.is_set()
                    and candidate.status in _TERMINAL_JOB_STATUSES
                    and candidate.active_downloads == 0
                    and not candidate.deleting
                )
                if completed:
                    candidate.deleting = True
                    oldest = candidate
                    break
        if oldest is None:
            return
        # Do not hold job.lock while taking the manager lock in forget().
        try:
            _remove_private_job(oldest.output_dir.parent)
        except BaseException:
            with oldest.lock:
                oldest.deleting = False
            raise
        state.manager.forget(oldest)
        state.jobs.pop(oldest.job_id, None)


def _save_uploads(
    uploads: list[UploadFile], directory: Path, *, max_bytes: int | None
) -> list[Path]:
    saved: list[Path] = []
    total_written = 0
    for index, upload in enumerate(uploads):
        name = sanitize_filename(upload.filename or f"upload-{index}", fallback=f"upload-{index}")
        target = ensure_contained(directory / f"{index:02d}-{name}", directory, what="upload")
        written = 0
        with target.open("wb") as sink:
            while True:
                chunk = upload.file.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                total_written += len(chunk)
                if max_bytes is not None and total_written > max_bytes:
                    sink.close()
                    target.unlink(missing_ok=True)
                    raise _ApiError(
                        413,
                        f"Uploads exceed the configured per-job limit of {max_bytes:,} bytes",
                    )
                sink.write(chunk)
        saved.append(target)
    return saved


class _ApiError(Exception):
    def __init__(self, status: int, message: str) -> None:
        self.status = status
        self.message = message
        super().__init__(message)


def _range_or_none(value: str | None, *, what: str) -> PageRange | None:
    if value is None or value == "":
        return None
    try:
        return PageRange(spec=value)
    except (PageRangeError, ValueError) as exc:
        raise _ApiError(422, f"Invalid {what}: {exc}") from exc


def _int_param(
    params: dict[str, str],
    key: str,
    *,
    default: int | None = None,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int | None:
    raw = params.get(key)
    if raw in (None, ""):
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise _ApiError(422, f"'{key}' must be an integer") from None
    if minimum is not None and value < minimum:
        raise _ApiError(422, f"'{key}' must be at least {minimum}")
    if maximum is not None and value > maximum:
        raise _ApiError(422, f"'{key}' must be at most {maximum}")
    return value


def _float_param(
    params: dict[str, str],
    key: str,
    *,
    default: float | None = None,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float | None:
    raw = params.get(key)
    if raw in (None, ""):
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise _ApiError(422, f"'{key}' must be a number") from None
    if not math.isfinite(value):
        raise _ApiError(422, f"'{key}' must be finite")
    if minimum is not None and value < minimum:
        raise _ApiError(422, f"'{key}' must be at least {minimum:g}")
    if maximum is not None and value > maximum:
        raise _ApiError(422, f"'{key}' must be at most {maximum:g}")
    return value


def _strict_bool_param(
    params: dict[str, str],
    key: str,
    *,
    default: bool,
) -> bool:
    raw = params.get(key)
    if raw is None:
        return default
    normalized = raw.strip().casefold()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    raise _ApiError(422, f"'{key}' must be true or false")


def _one_input(paths: list[Path], operation: str) -> Path:
    if len(paths) != 1:
        raise _ApiError(422, f"{operation} needs exactly one file")
    return paths[0]


def create_app(
    settings: Settings | None = None,
    *,
    token: str | None = None,
    allow_nonlocal: bool = False,
) -> FastAPI:
    settings = settings or get_settings()
    if settings.strict_offline and allow_nonlocal:
        raise ValueError("strict-offline mode forbids non-loopback web access")
    api_root = (settings.jobs_root or default_jobs_root()) / "api-data"
    state = ApiState(
        token=token or secrets.token_urlsafe(32),
        settings=settings,
        api_root=api_root,
        data_root=api_root / f"{_API_SESSION_PREFIX}{uuid.uuid4().hex}",
        manager=WorkerManager(settings),
        allow_nonlocal=allow_nonlocal,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        make_private_dir(state.api_root)
        make_private_dir(state.api_root / _API_SESSION_LEASE_DIR)
        _cleanup_stale_api_sessions(state.api_root, state.data_root)
        lease_path = _session_lease_path(state.api_root, state.data_root)
        state.session_lease = _try_acquire_session_lease(lease_path, create=True)
        if state.session_lease is None:
            raise RuntimeError("Unable to establish the private API session lease")
        try:
            make_private_dir(state.data_root, exist_ok=False)
            state.manager.start()
        except BaseException:
            state.session_lease.close()
            state.session_lease = None
            with contextlib.suppress(OSError):
                lease_path.unlink()
            raise
        try:
            yield
        finally:
            state.shutdown_diagnostics = state.manager.shutdown()
            if not state.shutdown_diagnostics.get("shutdown_complete", False):
                state.shutdown_diagnostics["session_cleanup_complete"] = False
                state.shutdown_diagnostics["cleanup_failed_closed"] = True
                _LOG.error("Worker shutdown incomplete; private session cleanup deferred")
            else:
                cleanup_complete = remove_tree_with_retries(state.data_root)
                state.shutdown_diagnostics["session_cleanup_complete"] = cleanup_complete
                state.shutdown_diagnostics["cleanup_failed_closed"] = not cleanup_complete
                if cleanup_complete:
                    state.jobs.clear()
                    assert state.session_lease is not None
                    state.session_lease.close()
                    state.session_lease = None
                    with contextlib.suppress(OSError):
                        lease_path.unlink()
                    with contextlib.suppress(OSError):
                        (state.api_root / _API_SESSION_LEASE_DIR).rmdir()
                    with contextlib.suppress(OSError):
                        state.api_root.rmdir()
                else:
                    # The process no longer owns workers. Leave the unlocked
                    # lease as proof that the next startup may retry cleanup.
                    assert state.session_lease is not None
                    state.session_lease.close()
                    state.session_lease = None
                    _LOG.error("Private API session cleanup failed and was deferred")

    app = FastAPI(
        title="LocalDocForge",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.ldf = state

    # ------------------------------------------------------------- middleware

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if not state.allow_nonlocal and not _host_is_loopback(request):
            return _error_response(400, "LocalDocForge only answers loopback requests")
        if request.url.path.startswith("/api"):
            presented = _client_token(request)
            if presented is None or not hmac.compare_digest(presented, state.token):
                return _error_response(401, f"Missing or invalid {_TOKEN_HEADER} header")

        body_limit = _api_transport_limit(state.settings)
        if request.method == "POST" and request.url.path.startswith("/api/jobs/"):
            request_limit = body_limit + _MULTIPART_OVERHEAD_BYTES
            content_length = request.headers.get("content-length")
            if content_length is not None:
                try:
                    if int(content_length) > request_limit:
                        return _error_response(413, "Request body exceeds the configured limit")
                except ValueError:
                    return _error_response(400, "Invalid Content-Length header")

            original_receive = request._receive
            received = 0

            async def limited_receive():
                nonlocal received
                message = await original_receive()
                if message["type"] == "http.request":
                    received += len(message.get("body", b""))
                    if received > request_limit:
                        raise _RequestTooLarge
                return message

            request._receive = limited_receive

        try:
            response = await call_next(request)
        except _RequestTooLarge:
            return _error_response(413, "Request body exceeds the configured limit")
        except Exception as exc:
            if _contains_request_too_large(exc):
                return _error_response(413, "Request body exceeds the configured limit")
            # Keep exception values (which may contain private paths or parser
            # data) out of uvicorn's default traceback logging surface.
            return _error_response(500, "Internal server error")
        _hardening_headers(response)
        return response

    @app.exception_handler(_ApiError)
    async def api_error_handler(_request: Request, exc: _ApiError):
        return _error_response(exc.status, exc.message)

    @app.exception_handler(AdmissionError)
    async def admission_error_handler(_request: Request, exc: AdmissionError):
        response = _error_response(exc.status_code, exc.message)
        if exc.retry_after is not None:
            response.headers["Retry-After"] = str(max(1, math.ceil(exc.retry_after)))
        return response

    @app.exception_handler(PipelineError)
    async def pipeline_error_handler(_request: Request, exc: PipelineError):
        private_roots = (state.api_root.parent,)
        payload: dict = {"detail": _public_error_message(str(exc), exc.report, private_roots)}
        if exc.report is not None:
            payload["report"] = _public_report(exc.report, private_roots)
        response = JSONResponse(payload, status_code=422)
        _hardening_headers(response)
        return response

    @app.exception_handler(Exception)
    async def unhandled_error_handler(_request: Request, _exc: Exception):
        # Do not expose tracebacks, local paths, form values, or parser details.
        return _error_response(500, "Internal server error")

    # ------------------------------------------------------------------ pages

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        capabilities = default_registry().capabilities()
        available = [c for c in capabilities if c.available]
        pending = [c for c in capabilities if not c.available]
        items = "".join(f"<li>{c.title}</li>" for c in available)
        pending_items = "".join(f"<li>{c.title} — {c.notes or 'planned'}</li>" for c in pending)
        if state.settings.strict_offline and not state.allow_nonlocal:
            privacy_status = (
                "Strict-offline mode active — local application policy and Python network "
                "guards are enabled as defense in depth; native code is not OS-sandboxed."
            )
        elif state.allow_nonlocal:
            privacy_status = "Non-loopback access is enabled; remote clients may reach this server."
        else:
            privacy_status = "Local-only engine set active; strict-offline mode is not enabled."
        body = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>LocalDocForge</title>
<style>body{{font-family:system-ui;margin:3rem auto;max-width:42rem;line-height:1.5}}
h1{{margin-bottom:.2rem}} .muted{{color:#666}}</style></head>
<body>
<h1>LocalDocForge</h1>
<p class="muted">v{__version__} — {privacy_status}</p>
<p>The browser interface is under construction. These capabilities are
implemented in this build; interface coverage varies, so consult the CLI/API
reference for the available entry point.</p>
<h2>Available on this machine</h2><ul>{items}</ul>
<h2>Not available yet</h2><ul class="muted">{pending_items}</ul>
<p class="muted">API authentication: the <code>{_TOKEN_HEADER}</code> header
printed when the server started. This page never runs remote code.</p>
</body></html>"""
        response = HTMLResponse(body)
        response.set_cookie(
            _TOKEN_COOKIE,
            state.token,
            httponly=True,
            samesite="strict",
            secure=False,
            path="/",
        )
        return response

    # -------------------------------------------------------------------- api

    @app.get("/api/health")
    async def health():
        workers = state.manager.diagnostics()
        return {
            "status": "ok",
            "version": __version__,
            "strict_offline": state.settings.strict_offline,
            "loopback_only": not state.allow_nonlocal,
            "workers": workers,
        }

    @app.get("/api/capabilities")
    async def capabilities():
        registry = default_registry()
        engines = [json.loads(engine.model_dump_json()) for engine in registry.all_infos()]
        for engine in engines:
            if engine.get("path"):
                engine["path"] = Path(engine["path"]).name
        return {
            "engines": engines,
            "capabilities": [json.loads(c.model_dump_json()) for c in registry.capabilities()],
        }

    @app.get("/api/jobs")
    async def list_jobs():
        _evict_completed_jobs(state)
        jobs = []
        for job in list(state.jobs.values()):
            with job.lock:
                jobs.append(
                    {
                        "job_id": job.job_id,
                        "operation": job.operation,
                        "status": job.status.value,
                    }
                )
        return {"jobs": jobs}

    @app.get("/api/jobs/{job_id}")
    async def job_detail(job_id: str):
        job = state.jobs.get(job_id)
        if job is None:
            raise _ApiError(404, "Unknown job")
        return JSONResponse(_job_state_payload(job, (state.api_root.parent,)))

    @app.get("/api/jobs/{job_id}/events")
    async def job_events(job_id: str, after: int = 0):
        if after < 0:
            raise _ApiError(422, "'after' cannot be negative")
        job = state.jobs.get(job_id)
        if job is None:
            raise _ApiError(404, "Unknown job")
        with job.lock:
            return {
                "job_id": job_id,
                "status": job.status.value,
                "events": job.event_snapshot(after),
            }

    @app.get("/api/jobs/{job_id}/outputs/{index}")
    async def job_output(job_id: str, index: int):
        job = state.jobs.get(job_id)
        if job is None:
            raise _ApiError(404, "Unknown job")
        with job.lock:
            if job.deleting:
                raise _ApiError(409, "Job output deletion is in progress")
            if job.status is not WorkerJobStatus.SUCCESS:
                raise _ApiError(409, "Job outputs are available only after success")
            if not 0 <= index < len(job.outputs):
                raise _ApiError(404, "No such output")
            path = ensure_contained(job.outputs[index], job.output_dir, what="job output")
            if not path.is_file():
                raise _ApiError(404, "Output no longer exists")
            job.active_downloads += 1
        try:
            return _JobFileResponse(path, filename=path.name, job=job)
        except BaseException:
            with job.lock:
                job.active_downloads = max(0, job.active_downloads - 1)
            raise

    @app.post("/api/jobs/{job_id}/cancel")
    async def job_cancel(job_id: str):
        job = state.jobs.get(job_id)
        if job is None:
            raise _ApiError(404, "Unknown job")
        if not state.manager.cancel(job):
            raise _ApiError(409, "Job is already in a terminal state")
        return JSONResponse(
            {"job_id": job_id, "status": "cancelling"},
            status_code=202,
        )

    @app.delete("/api/jobs/{job_id}")
    async def job_delete(job_id: str):
        job = state.jobs.get(job_id)
        if job is None:
            raise _ApiError(404, "Unknown job")
        with job.lock:
            completed = job.done.is_set() and job.status in _TERMINAL_JOB_STATUSES
            if not completed:
                raise _ApiError(409, "Cancel the active job before deleting it")
            if job.active_downloads:
                raise _ApiError(409, "Wait for active output downloads before deleting the job")
            if job.deleting:
                raise _ApiError(409, "Job deletion is already in progress")
            job.deleting = True
        try:
            _remove_private_job(job.output_dir.parent)
        except BaseException:
            with job.lock:
                job.deleting = False
            raise
        # job.lock is intentionally released before manager.forget().
        state.manager.forget(job)
        state.jobs.pop(job_id, None)
        return {"deleted": job_id}

    @app.post("/api/jobs/{operation}")
    async def submit_job(operation: str, request: Request):
        if operation not in _OPERATIONS:
            raise _ApiError(
                404,
                f"Unknown or unavailable operation {operation!r}. "
                f"Available: {', '.join(sorted(_OPERATIONS))}",
            )
        admission = state.manager.reserve(_client_key(request))
        form = None
        form_close_state = [False]
        transport_root = ensure_contained(
            state.data_root / f".transport-{admission.token}",
            state.data_root,
            what="API transport spool",
        )
        try:
            make_private_dir(transport_root, exist_ok=False)
            if not request.headers.get("content-type", "").lower().startswith(
                "multipart/form-data"
            ):
                raise _ApiError(422, "Job submissions require multipart/form-data")
            parser = _ContainedMultiPartParser(
                request.headers,
                request.stream(),
                max_files=_MAX_MULTIPART_FILES,
                max_fields=_MAX_FORM_FIELDS,
                max_part_size=_MAX_FORM_FIELD_BYTES,
                spool_directory=transport_root,
                max_file_bytes=_api_transport_limit(state.settings),
            )
            try:
                form = await parser.parse()
            except MultiPartException as exc:
                raise _ApiError(400, "Malformed multipart form") from exc
            try:
                # The helper owns the form after parsing and closes it before
                # enqueue. The route is only the failure-path fallback.
                return await _submit_job_form(
                    operation,
                    form,
                    state,
                    admission,
                    respond_async=_async_requested(request),
                    _form_close_state=form_close_state,
                    _disconnect_receive=request.receive,
                    _transport_root=transport_root,
                )
            except BaseException:
                # Suppress ordinary cleanup failures without swallowing task
                # cancellation, which derives from BaseException.
                if not form_close_state[0]:
                    form_close_state[0] = True
                    with contextlib.suppress(Exception):
                        await form.close()
                raise
        finally:
            state.manager.release(admission)
            if transport_root.exists():
                _remove_private_job(transport_root)
            if state.session_lease is None and not state.jobs:
                # Some ASGI harnesses omit lifespan events. Do not leave the
                # provisional transport hierarchy created for a rejected body.
                for empty in (state.data_root, state.api_root, state.api_root.parent):
                    with contextlib.suppress(OSError):
                        empty.rmdir()

    return app


async def _wait_for_sync_job(
    job: WorkerJob,
    state: ApiState,
    disconnect_receive: Receive | None,
) -> None:
    """Wait without losing ownership when a synchronous HTTP client disappears."""

    async def receive_disconnect() -> None:
        assert disconnect_receive is not None
        while True:
            message = await disconnect_receive()
            if message["type"] == "http.disconnect":
                return

    disconnect_task = (
        asyncio.create_task(receive_disconnect()) if disconnect_receive is not None else None
    )
    try:
        while not job.done.is_set():
            if disconnect_task is not None and disconnect_task.done():
                # A receive failure after the complete form body is also treated
                # as a lost client; either way no synchronous response is owned.
                with contextlib.suppress(Exception):
                    disconnect_task.result()
                await asyncio.to_thread(state.manager.cancel, job)
                finalized = await asyncio.to_thread(
                    state.manager.wait,
                    job,
                    _DISCONNECT_FINALIZE_SECONDS,
                )
                if not finalized:
                    with job.lock:
                        controller = job._controller
                    if controller is not None:
                        controller.terminate()
                    await asyncio.to_thread(state.manager.wait, job, 2.0)
                return
            await asyncio.sleep(_DISCONNECT_POLL_SECONDS)
    finally:
        if disconnect_task is not None:
            if not disconnect_task.done():
                disconnect_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await disconnect_task


async def _submit_job_form(
    operation: str,
    form,
    state: ApiState,
    admission: Admission,
    *,
    respond_async: bool,
    _form_close_state: list[bool] | None = None,
    _disconnect_receive: Receive | None = None,
    _transport_root: Path | None = None,
) -> JSONResponse:
    job: WorkerJob | None = None
    enqueued = False
    form_close_state = _form_close_state if _form_close_state is not None else [False]
    allowed_params = _OPERATION_PARAMS.get(operation, frozenset())
    unknown = sorted(set(form.keys()) - {"files"} - allowed_params)
    if unknown:
        raise _ApiError(422, f"Unknown form field(s): {', '.join(unknown)}")
    raw_uploads = form.getlist("files")
    uploads = [value for value in raw_uploads if isinstance(value, UploadFile)]
    if len(uploads) != len(raw_uploads):
        raise _ApiError(422, "Every 'files' value must be a file upload")
    if not uploads:
        raise _ApiError(422, "At least one file must be uploaded as 'files'")
    params: dict[str, str] = {}
    for key in allowed_params:
        values = form.getlist(key)
        if not values:
            continue
        if len(values) != 1 or not isinstance(values[0], str):
            raise _ApiError(422, f"'{key}' must be supplied exactly once as text")
        params[key] = values[0]

    job_id = uuid.uuid4().hex
    job_root = ensure_contained(state.data_root / job_id, state.data_root, what="API job")
    input_dir = job_root / "in"
    output_dir = job_root / "out"
    try:
        make_private_dir(job_root, exist_ok=False)
        make_private_dir(input_dir, exist_ok=False)
        make_private_dir(output_dir, exist_ok=False)
        saved = _save_uploads(uploads, input_dir, max_bytes=_api_input_limit(state.settings))
        # UploadFile handles are no longer needed after bounded transport.
        # Close them before worker ownership begins so a close failure cannot
        # leave an unreported queued/running job behind.
        form_close_state[0] = True
        await form.close()
        if _transport_root is not None:
            _remove_private_job(_transport_root)
        request = WorkerRequest(
            job_id=job_id,
            operation=operation,
            job_root=str(job_root),
            input_names=tuple(path.name for path in saved),
            params=params,
            settings_json=state.settings.model_dump_json(),
        )
        job = WorkerJob(
            request=request,
            client_key=admission.client_key,
            output_dir=output_dir,
            max_events=state.settings.api_max_progress_events,
        )
        state.jobs[job_id] = job
        state.manager.enqueue(admission, job)
        enqueued = True
        _evict_completed_jobs(state)
        if respond_async:
            response = JSONResponse(
                _submission_payload(job, (state.api_root.parent,)),
                status_code=202,
            )
            response.headers["Preference-Applied"] = "respond-async"
            return response
        await _wait_for_sync_job(job, state, _disconnect_receive)
        return _sync_job_response(job, (state.api_root.parent,))
    except BaseException:
        if enqueued and job is not None:
            state.manager.cancel(job)
            await asyncio.to_thread(state.manager.wait, job, 5.0)
        else:
            state.jobs.pop(job_id, None)
            _remove_private_job(job_root)
        raise


# ------------------------------------------------------------------ operations
# Each runner: (input_paths, output_dir, form_params, settings, progress)
# -> ConversionReport.
# Outputs always land inside the server-managed job output dir; browser
# payloads never name filesystem paths.


def _organize_options(
    settings: Settings,
    params: dict[str, str],
    progress=None,
) -> organize_ops.OrganizeOptions:
    return organize_ops.OrganizeOptions(
        collision=CollisionPolicy.RENAME,
        settings=settings,
        password=params.get("password") or None,
        progress=progress,
    )


def _run_merge(paths, output_dir, params, settings, progress=None):
    if len(paths) < 2:
        raise _ApiError(422, "merge needs at least two files")
    ranges = None
    if params.get("pages"):
        try:
            specs = json.loads(params["pages"])
        except json.JSONDecodeError as exc:
            raise _ApiError(422, "'pages' must be valid JSON") from exc
        if not isinstance(specs, list) or len(specs) != len(paths):
            raise _ApiError(422, "'pages' must be a JSON list with one entry per file")
        if any(spec is not None and not isinstance(spec, str) for spec in specs):
            raise _ApiError(422, "Each 'pages' entry must be a string or null")
        ranges = [_range_or_none(spec, what="pages") for spec in specs]
    return organize_ops.merge_pdfs(
        paths,
        output_dir / "merged.pdf",
        page_ranges=ranges,
        options=_organize_options(settings, params, progress),
    )


def _run_split(paths, output_dir, params, settings, progress=None):
    source = _one_input(paths, "split")
    every = _int_param(params, "every", minimum=1)
    return organize_ops.split_pdf(
        source,
        output_dir,
        pages=_range_or_none(params.get("pages"), what="pages"),
        every=every,
        options=_organize_options(settings, params, progress),
    )


def _single_input_range_op(fn, key):
    def runner(paths, output_dir, params, settings, progress=None):
        source = _one_input(paths, fn.__name__.replace("_", "-"))
        page_range = _range_or_none(params.get(key), what=key)
        if page_range is None:
            raise _ApiError(422, f"'{key}' is required")
        return fn(
            source,
            output_dir / "result.pdf",
            page_range,
            options=_organize_options(settings, params, progress),
        )

    return runner


def _run_rotate(paths, output_dir, params, settings, progress=None):
    source = _one_input(paths, "rotate")
    degrees = _int_param(params, "degrees")
    if degrees is None:
        raise _ApiError(422, "'degrees' is required")
    return organize_ops.rotate_pages(
        source,
        output_dir / "rotated.pdf",
        degrees=degrees,
        pages=_range_or_none(params.get("pages"), what="pages"),
        options=_organize_options(settings, params, progress),
    )


def _run_crop(paths, output_dir, params, settings, progress=None):
    source = _one_input(paths, "crop")
    try:
        box = tuple(float(v) for v in params.get("box", "").split(","))
        if len(box) != 4 or not all(math.isfinite(value) for value in box):
            raise ValueError
    except ValueError:
        raise _ApiError(422, "'box' must be 'x0,y0,x1,y1' in points") from None
    return organize_ops.crop_pages(
        source,
        output_dir / "cropped.pdf",
        box=box,  # type: ignore[arg-type]
        pages=_range_or_none(params.get("pages"), what="pages"),
        options=_organize_options(settings, params, progress),
    )


def _run_compress(paths, output_dir, params, settings, progress=None):
    source = _one_input(paths, "compress")
    preset = params.get("preset", "lossless")
    if preset not in optimize_ops.COMPRESS_PRESETS:
        raise _ApiError(
            422,
            "'preset' must be one of: " + ", ".join(optimize_ops.COMPRESS_PRESETS),
        )
    return optimize_ops.compress_pdf(
        source,
        output_dir / "compressed.pdf",
        preset=preset,
        options=_organize_options(settings, params, progress),
    )


def _run_images_to_pdf(paths, output_dir, params, settings, progress=None):
    margin = _float_param(params, "margin", default=24.0, minimum=0)
    dpi = _int_param(params, "dpi", default=200, minimum=36, maximum=600)
    quality = _int_param(params, "quality", default=95, minimum=1, maximum=100)
    assert margin is not None and dpi is not None and quality is not None
    options = image_ops.ImagesToPdfOptions(
        page_size=params.get("page_size", "A4"),
        fit=params.get("fit", "fit"),
        margin_pt=margin,
        background=params.get("background", "white"),
        dpi=dpi,
        jpeg_quality=quality,
        collision=CollisionPolicy.RENAME,
        settings=settings,
        progress=progress,
    )
    return image_ops.images_to_pdf(paths, output_dir / "images.pdf", options=options)


def _run_pdf_to_images(paths, output_dir, params, settings, progress=None):
    source = _one_input(paths, "pdf-to-images")
    image_format = params.get("format") or None
    if image_format is not None and image_format.lower() not in image_ops.OUTPUT_IMAGE_FORMATS:
        raise _ApiError(422, "'format' must be one of: png, jpeg, webp, tiff")
    preset = params.get("preset") or None
    if preset is not None and preset not in image_ops.CONVERT_PRESETS:
        raise _ApiError(
            422, "'preset' must be one of: " + ", ".join(sorted(image_ops.CONVERT_PRESETS))
        )
    dpi = _int_param(params, "dpi", minimum=18, maximum=1200)
    quality = _int_param(params, "quality", minimum=1, maximum=100)
    options = image_ops.PdfToImagesOptions(
        pages=_range_or_none(params.get("pages"), what="pages"),
        preset=preset,
        collision=CollisionPolicy.RENAME,
        settings=settings,
        password=params.get("password") or None,
        progress=progress,
    )
    if image_format is not None:
        options.image_format = image_format
    if dpi is not None:
        options.dpi = dpi
    if quality is not None:
        options.jpeg_quality = quality
    return image_ops.pdf_to_images(source, output_dir, options=options)


def _run_pdf_to_md(paths, output_dir, params, settings, progress=None):
    source = _one_input(paths, "pdf-to-md")
    output_format = (params.get("format") or "md").strip().lower()
    if output_format not in text_ops.TEXT_OUTPUT_FORMATS:
        raise _ApiError(
            422,
            "'format' must be one of: " + ", ".join(text_ops.TEXT_OUTPUT_FORMATS),
        )
    tables = _strict_bool_param(params, "tables", default=False)
    if tables and output_format != "md":
        raise _ApiError(422, "'tables' requires 'format' to be 'md'")
    options = text_ops.PdfToMdOptions(
        output_format=output_format,
        pages=_range_or_none(params.get("pages"), what="pages"),
        page_anchors=_strict_bool_param(
            params,
            "page_anchors",
            default=True,
        ),
        tables=tables,
        collision=CollisionPolicy.RENAME,
        settings=settings,
        progress=progress,
        password=params.get("password") or None,
    )
    return text_ops.pdf_to_md(
        source,
        output_dir / f"document.{output_format}",
        options=options,
    )


def _transport_upload_alias(path: Path) -> str:
    """Undo the private numeric transport prefix while retaining sanitization."""
    prefix, separator, alias = path.name.partition("-")
    if separator and prefix.isdecimal() and alias:
        return alias
    return path.name


def _run_md_to_pdf(paths, output_dir, params, settings, progress=None):
    aliases = {_transport_upload_alias(path): path for path in paths}
    if len(aliases) != len(paths):
        raise _ApiError(422, "Markdown uploads must have distinct sanitized basenames")
    markdown_names = [
        name for name in aliases if Path(name).suffix.casefold() in {".md", ".markdown"}
    ]
    if len(markdown_names) != 1:
        raise _ApiError(422, "md-to-pdf needs exactly one .md or .markdown upload")
    source_name = markdown_names[0]
    source = aliases.pop(source_name)
    margin = _float_param(params, "margin", default=20.0, minimum=0)
    assert margin is not None
    try:
        paper = markdown_ops.normalize_paper(params.get("paper", "A4"))
        markdown_ops.validate_margin(margin, paper)
    except PipelineError as exc:
        raise _ApiError(422, str(exc)) from exc
    options = markdown_ops.MdToPdfOptions(
        paper=paper[0],
        margin_mm=margin,
        toc=_strict_bool_param(params, "toc", default=False),
        collision=CollisionPolicy.RENAME,
        settings=settings,
        progress=progress,
    )
    try:
        return markdown_ops.md_to_pdf(
            source,
            output_dir / "document.pdf",
            options=options,
            image_inputs=aliases,
        )
    except EngineUnavailableError as exc:
        raise _ApiError(503, str(exc)) from exc


def _run_convert_images(paths, output_dir, params, settings, progress=None):
    image_format = params.get("format") or None
    if image_format is not None and image_format.lower() not in image_ops.OUTPUT_IMAGE_FORMATS:
        raise _ApiError(422, "'format' must be one of: png, jpeg, webp, tiff")
    preset = params.get("preset") or None
    if preset is not None and preset not in image_ops.CONVERT_PRESETS:
        raise _ApiError(
            422, "'preset' must be one of: " + ", ".join(sorted(image_ops.CONVERT_PRESETS))
        )
    keep_raw = params.get("keep_metadata", "false").strip().lower()
    if keep_raw not in ("true", "false", "1", "0"):
        raise _ApiError(422, "'keep_metadata' must be true or false")
    options = image_ops.ConvertImagesOptions(
        image_format=image_format,
        quality=_int_param(params, "quality", minimum=1, maximum=100),
        max_dimension=_int_param(params, "max_dimension", minimum=16, maximum=30000),
        preset=preset,
        keep_metadata=keep_raw in ("true", "1"),
        background=params.get("background", "white"),
        collision=CollisionPolicy.RENAME,
        settings=settings,
        progress=progress,
    )
    return image_ops.convert_images(paths, output_dir, options=options)


_OPERATIONS = {
    "merge": _run_merge,
    "split": _run_split,
    "remove-pages": _single_input_range_op(organize_ops.remove_pages, "pages"),
    "extract-pages": _single_input_range_op(organize_ops.extract_pages, "pages"),
    "organize": _single_input_range_op(organize_ops.organize_pdf, "order"),
    "rotate": _run_rotate,
    "crop": _run_crop,
    "compress": _run_compress,
    "images-to-pdf": _run_images_to_pdf,
    "pdf-to-images": _run_pdf_to_images,
    "pdf-to-md": _run_pdf_to_md,
    "md-to-pdf": _run_md_to_pdf,
    "convert-images": _run_convert_images,
}

_OPERATION_PARAMS: dict[str, frozenset[str]] = {
    "merge": frozenset({"pages", "password"}),
    "split": frozenset({"pages", "every", "password"}),
    "remove-pages": frozenset({"pages", "password"}),
    "extract-pages": frozenset({"pages", "password"}),
    "organize": frozenset({"order", "password"}),
    "rotate": frozenset({"degrees", "pages", "password"}),
    "crop": frozenset({"box", "pages", "password"}),
    "compress": frozenset({"preset", "password"}),
    "images-to-pdf": frozenset({"page_size", "fit", "margin", "background", "dpi", "quality"}),
    "pdf-to-images": frozenset(
        {"format", "dpi", "pages", "quality", "preset", "password"}
    ),
    "pdf-to-md": frozenset({"pages", "format", "page_anchors", "tables", "password"}),
    "md-to-pdf": frozenset({"paper", "margin", "toc"}),
    "convert-images": frozenset(
        {"format", "quality", "max_dimension", "preset", "keep_metadata", "background"}
    ),
}
