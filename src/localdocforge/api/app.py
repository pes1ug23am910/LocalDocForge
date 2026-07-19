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

import contextlib
import hmac
import json
import math
import re
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response

# Raw request.form() yields Starlette's UploadFile (FastAPI's subclasses it),
# so containment/auth code must test against the Starlette base class.
from starlette.datastructures import UploadFile

from localdocforge import __version__
from localdocforge.config.settings import Settings, get_settings
from localdocforge.domain.models import ConversionReport
from localdocforge.domain.pages import PageRange, PageRangeError
from localdocforge.engines.registry import default_registry
from localdocforge.jobs.workspace import (
    CollisionPolicy,
    default_jobs_root,
    make_private_dir,
    remove_tree_with_retries,
)
from localdocforge.operations import images as image_ops
from localdocforge.operations import organize as organize_ops
from localdocforge.pipelines.runner import PipelineError
from localdocforge.security.filenames import sanitize_filename
from localdocforge.security.paths import ensure_contained

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})
_TOKEN_HEADER = "X-LDF-Token"  # noqa: S105 - header name, not a credential
_TOKEN_COOKIE = "ldf_token"  # noqa: S105 - cookie name, not a credential
_API_SESSION_PREFIX = "ldf-api-"
_MULTIPART_OVERHEAD_BYTES = 2 * 1024 * 1024
_MAX_MULTIPART_FILES = 256
_MAX_FORM_FIELDS = 100
_MAX_FORM_FIELD_BYTES = 64 * 1024
_LEGACY_API_JOB = re.compile(r"^[0-9a-f]{32}$")


@dataclass
class ApiJob:
    job_id: str
    operation: str
    report: ConversionReport
    output_dir: Path
    outputs: list[Path] = field(default_factory=list)


@dataclass
class ApiState:
    token: str
    settings: Settings
    api_root: Path
    data_root: Path
    allow_nonlocal: bool = False
    jobs: dict[str, ApiJob] = field(default_factory=dict)
    max_recent_jobs: int = 50


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


def _error_response(status: int, message: str) -> JSONResponse:
    response = JSONResponse({"detail": message}, status_code=status)
    _hardening_headers(response)
    return response


def _contains_request_too_large(exc: BaseException) -> bool:
    if isinstance(exc, _RequestTooLarge):
        return True
    if isinstance(exc, BaseExceptionGroup):
        return any(_contains_request_too_large(child) for child in exc.exceptions)
    return (
        exc.__cause__ is not None and _contains_request_too_large(exc.__cause__)
    ) or (
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


def _cleanup_stale_api_sessions(root: Path, current: Path, *, max_age: float = 24 * 3600) -> None:
    """Best-effort cleanup of crashed API sessions, never unrelated directories."""
    if not root.is_dir():
        return
    cutoff = time.time() - max_age
    for entry in root.iterdir():
        if entry == current or not entry.is_dir():
            continue
        if _LEGACY_API_JOB.fullmatch(entry.name):
            # One-time migration cleanup for the pre-session-root API layout.
            remove_tree_with_retries(entry)
            continue
        if not entry.name.startswith(_API_SESSION_PREFIX):
            continue
        with contextlib.suppress(OSError):
            if entry.stat().st_mtime < cutoff:
                remove_tree_with_retries(entry)


def _remove_private_job(path: Path) -> None:
    if not remove_tree_with_retries(path):
        raise _ApiError(500, "Unable to remove private job data; close open output files and retry")


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
        allow_nonlocal=allow_nonlocal,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        make_private_dir(state.api_root)
        _cleanup_stale_api_sessions(state.api_root, state.data_root)
        make_private_dir(state.data_root, exist_ok=False)
        try:
            yield
        finally:
            state.jobs.clear()
            remove_tree_with_retries(state.data_root)
            with contextlib.suppress(OSError):
                state.api_root.rmdir()

    app = FastAPI(title="LocalDocForge", version=__version__, docs_url=None, redoc_url=None,
                  openapi_url=None, lifespan=lifespan)
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

        body_limit = state.settings.limits.max_input_bytes
        if (
            request.method == "POST"
            and request.url.path.startswith("/api/jobs/")
            and body_limit is not None
        ):
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

    @app.exception_handler(PipelineError)
    async def pipeline_error_handler(_request: Request, exc: PipelineError):
        private_roots = (state.api_root.parent,)
        payload: dict = {
            "detail": _public_error_message(str(exc), exc.report, private_roots)
        }
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
                "Strict-offline mode active — processed locally; nothing leaves this machine."
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
            _TOKEN_COOKIE, state.token, httponly=True, samesite="strict", secure=False,
            path="/",
        )
        return response

    # -------------------------------------------------------------------- api

    @app.get("/api/health")
    async def health():
        return {
            "status": "ok",
            "version": __version__,
            "strict_offline": state.settings.strict_offline,
            "loopback_only": not state.allow_nonlocal,
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
        return {
            "jobs": [
                {"job_id": job.job_id, "operation": job.operation,
                 "status": job.report.status.value}
                for job in state.jobs.values()
            ]
        }

    @app.get("/api/jobs/{job_id}")
    async def job_detail(job_id: str):
        job = state.jobs.get(job_id)
        if job is None:
            raise _ApiError(404, "Unknown job")
        return JSONResponse(_public_report(job.report, (state.api_root.parent,)))

    @app.get("/api/jobs/{job_id}/outputs/{index}")
    async def job_output(job_id: str, index: int):
        job = state.jobs.get(job_id)
        if job is None:
            raise _ApiError(404, "Unknown job")
        if not 0 <= index < len(job.outputs):
            raise _ApiError(404, "No such output")
        path = ensure_contained(job.outputs[index], job.output_dir, what="job output")
        if not path.is_file():
            raise _ApiError(404, "Output no longer exists")
        return FileResponse(path, filename=path.name)

    @app.delete("/api/jobs/{job_id}")
    async def job_delete(job_id: str):
        job = state.jobs.get(job_id)
        if job is None:
            raise _ApiError(404, "Unknown job")
        _remove_private_job(job.output_dir.parent)
        state.jobs.pop(job_id, None)
        return {"deleted": job_id}

    @app.post("/api/jobs/{operation}")
    async def submit_job(operation: str, request: Request):
        runner = _OPERATIONS.get(operation)
        if runner is None:
            raise _ApiError(
                404,
                f"Unknown or unavailable operation {operation!r}. "
                f"Available: {', '.join(sorted(_OPERATIONS))}",
            )
        form = await request.form(
            max_files=_MAX_MULTIPART_FILES,
            max_fields=_MAX_FORM_FIELDS,
            max_part_size=_MAX_FORM_FIELD_BYTES,
        )
        try:
            return _submit_job_form(operation, runner, form, state)
        finally:
            await form.close()

    return app


def _submit_job_form(operation, runner, form, state: ApiState) -> JSONResponse:
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
        saved = _save_uploads(
            uploads, input_dir, max_bytes=state.settings.limits.max_input_bytes
        )
        report = runner(saved, output_dir, params, state.settings)
        _remove_private_job(input_dir)

        outputs = [artifact.path for artifact in report.outputs]
        job = ApiJob(
            job_id=job_id,
            operation=operation,
            report=report,
            output_dir=output_dir,
            outputs=outputs,
        )
        state.jobs[job_id] = job
        while len(state.jobs) > state.max_recent_jobs:
            oldest_id = next(iter(state.jobs))
            oldest = state.jobs[oldest_id]
            _remove_private_job(oldest.output_dir.parent)
            state.jobs.pop(oldest_id)
        return JSONResponse(
            {
                "job_id": job_id,
                "operation": operation,
                "report": _public_report(report, (state.api_root.parent,)),
                "outputs": [
                    {"index": i, "name": path.name, "size_bytes": path.stat().st_size}
                    for i, path in enumerate(outputs)
                ],
            },
            status_code=201,
        )
    except BaseException:
        state.jobs.pop(job_id, None)
        _remove_private_job(job_root)
        raise


# ------------------------------------------------------------------ operations
# Each runner: (input_paths, output_dir, form_params, settings) -> ConversionReport.
# Outputs always land inside the server-managed job output dir; browser
# payloads never name filesystem paths.


def _organize_options(settings: Settings, params: dict[str, str]) -> organize_ops.OrganizeOptions:
    return organize_ops.OrganizeOptions(
        collision=CollisionPolicy.RENAME,
        settings=settings,
        password=params.get("password") or None,
    )


def _run_merge(paths, output_dir, params, settings):
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
        paths, output_dir / "merged.pdf", page_ranges=ranges,
        options=_organize_options(settings, params),
    )


def _run_split(paths, output_dir, params, settings):
    source = _one_input(paths, "split")
    every = _int_param(params, "every", minimum=1)
    return organize_ops.split_pdf(
        source, output_dir,
        pages=_range_or_none(params.get("pages"), what="pages"),
        every=every, options=_organize_options(settings, params),
    )


def _single_input_range_op(fn, key):
    def runner(paths, output_dir, params, settings):
        source = _one_input(paths, fn.__name__.replace("_", "-"))
        page_range = _range_or_none(params.get(key), what=key)
        if page_range is None:
            raise _ApiError(422, f"'{key}' is required")
        return fn(
            source, output_dir / "result.pdf", page_range,
            options=_organize_options(settings, params),
        )

    return runner


def _run_rotate(paths, output_dir, params, settings):
    source = _one_input(paths, "rotate")
    degrees = _int_param(params, "degrees")
    if degrees is None:
        raise _ApiError(422, "'degrees' is required")
    return organize_ops.rotate_pages(
        source, output_dir / "rotated.pdf", degrees=degrees,
        pages=_range_or_none(params.get("pages"), what="pages"),
        options=_organize_options(settings, params),
    )


def _run_crop(paths, output_dir, params, settings):
    source = _one_input(paths, "crop")
    try:
        box = tuple(float(v) for v in params.get("box", "").split(","))
        if len(box) != 4 or not all(math.isfinite(value) for value in box):
            raise ValueError
    except ValueError:
        raise _ApiError(422, "'box' must be 'x0,y0,x1,y1' in points") from None
    return organize_ops.crop_pages(
        source, output_dir / "cropped.pdf", box=box,  # type: ignore[arg-type]
        pages=_range_or_none(params.get("pages"), what="pages"),
        options=_organize_options(settings, params),
    )


def _run_images_to_pdf(paths, output_dir, params, settings):
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
    )
    return image_ops.images_to_pdf(paths, output_dir / "images.pdf", options=options)


def _run_pdf_to_images(paths, output_dir, params, settings):
    source = _one_input(paths, "pdf-to-images")
    dpi = _int_param(params, "dpi", default=150, minimum=18, maximum=1200)
    quality = _int_param(params, "quality", default=90, minimum=1, maximum=100)
    assert dpi is not None and quality is not None
    options = image_ops.PdfToImagesOptions(
        image_format=params.get("format", "png"),
        dpi=dpi,
        pages=_range_or_none(params.get("pages"), what="pages"),
        jpeg_quality=quality,
        collision=CollisionPolicy.RENAME,
        settings=settings,
        password=params.get("password") or None,
    )
    return image_ops.pdf_to_images(source, output_dir, options=options)


_OPERATIONS = {
    "merge": _run_merge,
    "split": _run_split,
    "remove-pages": _single_input_range_op(organize_ops.remove_pages, "pages"),
    "extract-pages": _single_input_range_op(organize_ops.extract_pages, "pages"),
    "organize": _single_input_range_op(organize_ops.organize_pdf, "order"),
    "rotate": _run_rotate,
    "crop": _run_crop,
    "images-to-pdf": _run_images_to_pdf,
    "pdf-to-images": _run_pdf_to_images,
}

_OPERATION_PARAMS: dict[str, frozenset[str]] = {
    "merge": frozenset({"pages", "password"}),
    "split": frozenset({"pages", "every", "password"}),
    "remove-pages": frozenset({"pages", "password"}),
    "extract-pages": frozenset({"pages", "password"}),
    "organize": frozenset({"order", "password"}),
    "rotate": frozenset({"degrees", "pages", "password"}),
    "crop": frozenset({"box", "pages", "password"}),
    "images-to-pdf": frozenset(
        {"page_size", "fit", "margin", "background", "dpi", "quality"}
    ),
    "pdf-to-images": frozenset({"format", "dpi", "pages", "quality", "password"}),
}
