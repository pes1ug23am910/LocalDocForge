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

import hmac
import json
import secrets
import shutil
import uuid
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
from localdocforge.jobs.workspace import CollisionPolicy, default_jobs_root
from localdocforge.operations import images as image_ops
from localdocforge.operations import organize as organize_ops
from localdocforge.pipelines.runner import PipelineError
from localdocforge.security.filenames import sanitize_filename
from localdocforge.security.paths import ensure_contained

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})
_TOKEN_HEADER = "X-LDF-Token"  # noqa: S105 - header name, not a credential
_TOKEN_COOKIE = "ldf_token"  # noqa: S105 - cookie name, not a credential


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
    data_root: Path
    jobs: dict[str, ApiJob] = field(default_factory=dict)
    max_recent_jobs: int = 50


def _hardening_headers(response: Response) -> None:
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
        "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store"


def _host_is_loopback(request: Request) -> bool:
    host = request.headers.get("host", "")
    hostname = host.rsplit(":", 1)[0] if not host.startswith("[") else host.split("]")[0] + "]"
    return hostname.lower() in _LOOPBACK_HOSTS


def _client_token(request: Request) -> str | None:
    return request.headers.get(_TOKEN_HEADER)


def _save_uploads(
    uploads: list[UploadFile], directory: Path, *, max_bytes: int | None
) -> list[Path]:
    saved: list[Path] = []
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
                if max_bytes is not None and written > max_bytes:
                    sink.close()
                    target.unlink(missing_ok=True)
                    raise _ApiError(413, f"Upload {name!r} exceeds the size limit")
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


def create_app(settings: Settings | None = None, *, token: str | None = None) -> FastAPI:
    settings = settings or get_settings()
    state = ApiState(
        token=token or secrets.token_urlsafe(32),
        settings=settings,
        data_root=(settings.jobs_root or default_jobs_root()) / "api-data",
    )
    app = FastAPI(title="LocalDocForge", version=__version__, docs_url=None, redoc_url=None,
                  openapi_url=None)
    app.state.ldf = state

    # ------------------------------------------------------------- middleware

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if not _host_is_loopback(request):
            response = JSONResponse(
                {"detail": "LocalDocForge only answers loopback requests"}, status_code=400
            )
            _hardening_headers(response)
            return response
        if request.url.path.startswith("/api"):
            presented = _client_token(request)
            if presented is None or not hmac.compare_digest(presented, state.token):
                response = JSONResponse(
                    {"detail": f"Missing or invalid {_TOKEN_HEADER} header"}, status_code=401
                )
                _hardening_headers(response)
                return response
        response = await call_next(request)
        _hardening_headers(response)
        return response

    @app.exception_handler(_ApiError)
    async def api_error_handler(_request: Request, exc: _ApiError):
        return JSONResponse({"detail": exc.message}, status_code=exc.status)

    @app.exception_handler(PipelineError)
    async def pipeline_error_handler(_request: Request, exc: PipelineError):
        payload: dict = {"detail": str(exc)}
        if exc.report is not None:
            payload["report"] = json.loads(exc.report.model_dump_json())
        return JSONResponse(payload, status_code=422)

    # ------------------------------------------------------------------ pages

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        capabilities = default_registry().capabilities()
        available = [c for c in capabilities if c.available]
        pending = [c for c in capabilities if not c.available]
        items = "".join(f"<li>{c.title}</li>" for c in available)
        pending_items = "".join(f"<li>{c.title} — {c.notes or 'planned'}</li>" for c in pending)
        body = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>LocalDocForge</title>
<style>body{{font-family:system-ui;margin:3rem auto;max-width:42rem;line-height:1.5}}
h1{{margin-bottom:.2rem}} .muted{{color:#666}}</style></head>
<body>
<h1>LocalDocForge</h1>
<p class="muted">v{__version__} — processed locally; nothing leaves this machine.</p>
<p>The browser interface is under construction. Everything below is usable
today through the <code>ldf</code> CLI and this local API.</p>
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
        return {"status": "ok", "version": __version__}

    @app.get("/api/capabilities")
    async def capabilities():
        registry = default_registry()
        return {
            "engines": [json.loads(e.model_dump_json()) for e in registry.all_infos()],
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
        return JSONResponse(json.loads(job.report.model_dump_json()))

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
        job = state.jobs.pop(job_id, None)
        if job is None:
            raise _ApiError(404, "Unknown job")
        shutil.rmtree(job.output_dir.parent, ignore_errors=True)
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
        form = await request.form()
        uploads = [value for value in form.getlist("files") if isinstance(value, UploadFile)]
        if not uploads:
            raise _ApiError(422, "At least one file must be uploaded as 'files'")
        params = {key: value for key, value in form.items() if isinstance(value, str)}

        job_id = uuid.uuid4().hex
        job_root = state.data_root / job_id
        input_dir = job_root / "in"
        output_dir = job_root / "out"
        input_dir.mkdir(parents=True)
        output_dir.mkdir(parents=True)
        try:
            saved = _save_uploads(
                uploads, input_dir, max_bytes=state.settings.limits.max_input_bytes
            )
            report = runner(saved, output_dir, params, state.settings)
        except _ApiError:
            shutil.rmtree(job_root, ignore_errors=True)
            raise
        except PipelineError:
            shutil.rmtree(job_root, ignore_errors=True)
            raise
        finally:
            for upload in uploads:
                await upload.close()
        shutil.rmtree(input_dir, ignore_errors=True)

        outputs = [artifact.path for artifact in report.outputs]
        job = ApiJob(
            job_id=job_id, operation=operation, report=report,
            output_dir=output_dir, outputs=outputs,
        )
        state.jobs[job_id] = job
        while len(state.jobs) > state.max_recent_jobs:
            oldest_id = next(iter(state.jobs))
            oldest = state.jobs.pop(oldest_id)
            shutil.rmtree(oldest.output_dir.parent, ignore_errors=True)
        return JSONResponse(
            {
                "job_id": job_id,
                "operation": operation,
                "report": json.loads(report.model_dump_json()),
                "outputs": [
                    {"index": i, "name": path.name, "size_bytes": path.stat().st_size}
                    for i, path in enumerate(outputs)
                ],
            },
            status_code=201,
        )

    return app


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
        specs = json.loads(params["pages"])
        if not isinstance(specs, list) or len(specs) != len(paths):
            raise _ApiError(422, "'pages' must be a JSON list with one entry per file")
        ranges = [_range_or_none(spec, what="pages") for spec in specs]
    return organize_ops.merge_pdfs(
        paths, output_dir / "merged.pdf", page_ranges=ranges,
        options=_organize_options(settings, params),
    )


def _run_split(paths, output_dir, params, settings):
    every = int(params["every"]) if params.get("every") else None
    return organize_ops.split_pdf(
        paths[0], output_dir,
        pages=_range_or_none(params.get("pages"), what="pages"),
        every=every, options=_organize_options(settings, params),
    )


def _single_input_range_op(fn, key):
    def runner(paths, output_dir, params, settings):
        page_range = _range_or_none(params.get(key), what=key)
        if page_range is None:
            raise _ApiError(422, f"'{key}' is required")
        return fn(
            paths[0], output_dir / "result.pdf", page_range,
            options=_organize_options(settings, params),
        )

    return runner


def _run_rotate(paths, output_dir, params, settings):
    try:
        degrees = int(params.get("degrees", ""))
    except ValueError:
        raise _ApiError(422, "'degrees' must be an integer multiple of 90") from None
    return organize_ops.rotate_pages(
        paths[0], output_dir / "rotated.pdf", degrees=degrees,
        pages=_range_or_none(params.get("pages"), what="pages"),
        options=_organize_options(settings, params),
    )


def _run_crop(paths, output_dir, params, settings):
    try:
        box = tuple(float(v) for v in params.get("box", "").split(","))
        if len(box) != 4:
            raise ValueError
    except ValueError:
        raise _ApiError(422, "'box' must be 'x0,y0,x1,y1' in points") from None
    return organize_ops.crop_pages(
        paths[0], output_dir / "cropped.pdf", box=box,  # type: ignore[arg-type]
        pages=_range_or_none(params.get("pages"), what="pages"),
        options=_organize_options(settings, params),
    )


def _run_images_to_pdf(paths, output_dir, params, settings):
    options = image_ops.ImagesToPdfOptions(
        page_size=params.get("page_size", "A4"),
        fit=params.get("fit", "fit"),
        collision=CollisionPolicy.RENAME,
        settings=settings,
    )
    return image_ops.images_to_pdf(paths, output_dir / "images.pdf", options=options)


def _run_pdf_to_images(paths, output_dir, params, settings):
    options = image_ops.PdfToImagesOptions(
        image_format=params.get("format", "png"),
        dpi=int(params.get("dpi", "150")),
        pages=_range_or_none(params.get("pages"), what="pages"),
        collision=CollisionPolicy.RENAME,
        settings=settings,
        password=params.get("password") or None,
    )
    return image_ops.pdf_to_images(paths[0], output_dir, options=options)


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
