"""``ldf`` — the LocalDocForge CLI.

Exit codes (stable, documented in docs/CLI.md):
    0  success
    1  operation failed
    2  usage error (bad arguments)
    3  no engine available for the operation
    4  output validation failed (nothing was written)
    5  output already exists and collision policy is 'fail'
    130  cancelled
"""

from __future__ import annotations

import glob as _glob
import json
import os
import sys
from pathlib import Path
from typing import Annotated, NoReturn

import typer

from localdocforge import __version__
from localdocforge.cli.agent_brief import AgentBriefError, build_agent_brief, render_markdown
from localdocforge.config.settings import Settings, get_settings, set_settings
from localdocforge.domain.models import ConversionReport
from localdocforge.domain.pages import PageRange, PageRangeError
from localdocforge.engines.base import EngineUnavailableError
from localdocforge.engines.registry import default_registry
from localdocforge.jobs.workspace import (
    CollisionPolicy,
    OutputCollisionError,
    cleanup_stale_workspaces,
)
from localdocforge.operations import images as image_ops
from localdocforge.operations import optimize as optimize_ops
from localdocforge.operations import organize as organize_ops
from localdocforge.pipelines.runner import PipelineError
from localdocforge.reporting.writers import write_report_files
from localdocforge.security.paths import is_remote_path
from localdocforge.security.sniff import ContentTypeError

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_NO_ENGINE = 3
EXIT_VALIDATION = 4
EXIT_COLLISION = 5
EXIT_CANCELLED = 130

app = typer.Typer(
    name="ldf",
    help="LocalDocForge: private, fully local PDF and document tools.",
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
    pretty_exceptions_show_locals=False,
)

_PASSWORD_ENV = "LDF_PASSWORD"  # noqa: S105 - variable name, not a credential

_state: dict[str, object] = {
    "json": False,
    "quiet": False,
    "report_dir": None,
    "password": None,
    "password_supplied": False,
    "password_stdin_requested": False,
}


def _clear_password_state() -> None:
    _state["password"] = None
    _state["password_supplied"] = False
    _state["password_stdin_requested"] = False


def _password_stdin_error(message: str, *, cause: Exception | None = None) -> NoReturn:
    typer.secho(f"Error: {message}", fg=typer.colors.RED, err=True)
    raise typer.Exit(EXIT_USAGE) from cause


def _read_password_stdin() -> str:
    """Read exactly one explicitly UTF-8 password line without trimming spaces."""
    try:
        raw = typer.get_binary_stream("stdin").readline()
    except (OSError, ValueError) as exc:
        _password_stdin_error("could not read a password line from stdin", cause=exc)
    if raw == b"":
        _password_stdin_error("--password-stdin requires one UTF-8 line on stdin")
    try:
        password = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        _password_stdin_error("--password-stdin input must be valid UTF-8", cause=exc)
    if password.endswith("\r\n"):
        return password[:-2]
    if password.endswith(("\n", "\r")):
        return password[:-1]
    return password


def _ensure_password_resolved() -> None:
    if _state["password_stdin_requested"]:
        _state["password"] = _read_password_stdin()
        _state["password_supplied"] = True
        _state["password_stdin_requested"] = False


def _configured_password() -> tuple[str | None, bool]:
    _ensure_password_resolved()
    if not _state["password_supplied"]:
        return None, False
    password = _state["password"]
    if not isinstance(password, str):
        raise RuntimeError("configured CLI password state is invalid")
    return password, True


def _password_value() -> str | None:
    return _configured_password()[0]


def _windows_file_descriptor_is_console(file_descriptor: int) -> bool:
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        import msvcrt
        from ctypes import wintypes

        handle = msvcrt.get_osfhandle(file_descriptor)
        if handle == -1:
            return False
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetConsoleMode.argtypes = (
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
        )
        kernel32.GetConsoleMode.restype = wintypes.BOOL
        mode = wintypes.DWORD()
        return bool(
            kernel32.GetConsoleMode(
                wintypes.HANDLE(handle),
                ctypes.byref(mode),
            )
        )
    except (AttributeError, OSError, OverflowError, TypeError, ValueError):
        # Fail closed: pipes, files, NUL, missing handles, and probe failures
        # must never enter a hidden prompt that no caller can answer.
        return False


def _stdin_is_tty() -> bool:
    try:
        if not sys.stdin.isatty():
            return False
    except (AttributeError, OSError, ValueError):
        return False
    if sys.platform != "win32":
        return True
    try:
        file_descriptor = sys.stdin.fileno()
    except (AttributeError, OSError, ValueError):
        return False
    return _windows_file_descriptor_is_console(file_descriptor)


def _missing_noninteractive_password() -> NoReturn:
    typer.secho(
        "Error: encrypted input requires a password in non-interactive mode; "
        "use global --password-stdin before the command or set LDF_PASSWORD.",
        fg=typer.colors.RED,
        err=True,
    )
    raise typer.Exit(EXIT_USAGE)


def _fail_one_password(exc: organize_ops.EncryptedInputError) -> NoReturn:
    typer.secho(
        f"Error: {exc} One password is used for all encrypted inputs "
        "in an invocation.",
        fg=typer.colors.RED,
        err=True,
    )
    raise typer.Exit(EXIT_FAILED) from exc


def _password_retry_value(exc: organize_ops.EncryptedInputError) -> str:
    _, supplied = _configured_password()
    if supplied:
        _fail_one_password(exc)
    if not _stdin_is_tty():
        _missing_noninteractive_password()
    return typer.prompt("PDF password", hide_input=True, err=True)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"LocalDocForge {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    ctx: typer.Context,
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit machine-readable JSON on stdout.")
    ] = False,
    quiet: Annotated[
        bool, typer.Option("--quiet", "-q", help="Suppress the report summary.")
    ] = False,
    password_stdin: Annotated[
        bool,
        typer.Option(
            "--password-stdin",
            help="Read one UTF-8 password line from stdin; overrides LDF_PASSWORD.",
        ),
    ] = False,
    strict_offline: Annotated[
        bool | None,
        typer.Option(
            "--strict-offline",
            help="Reject recognized network filesystem paths and non-loopback serving; "
            "this application policy is recorded in reports but is not an OS firewall.",
        ),
    ] = None,
    report_dir: Annotated[
        Path | None,
        typer.Option("--report-dir", help="Also write JSON + text reports into this directory."),
    ] = None,
    version: Annotated[
        bool | None,
        typer.Option("--version", callback=_version_callback, is_eager=True),
    ] = None,
) -> None:
    _clear_password_state()
    ctx.call_on_close(_clear_password_state)
    environment_password = os.environ.pop(_PASSWORD_ENV, None)
    if password_stdin:
        _state["password_stdin_requested"] = True
    elif environment_password is not None:
        _state["password"] = environment_password
        _state["password_supplied"] = True
    _state["json"] = json_output
    _state["quiet"] = quiet
    _state["report_dir"] = report_dir
    # Preserve LDF_STRICT_OFFLINE when the flag was not supplied. Passing a
    # concrete False here would incorrectly outrank the environment setting.
    settings = Settings() if strict_offline is None else Settings(strict_offline=strict_offline)
    if report_dir is not None and settings.strict_offline and is_remote_path(report_dir):
        raise typer.BadParameter("strict-offline mode forbids a network report directory")
    set_settings(settings)
    # agent-brief is contractually read-only; even startup cleanup would make
    # it mutate the jobs tree. Conversion and serving commands retain the sweep.
    if ctx.invoked_subcommand != "agent-brief":
        cleanup_stale_workspaces(settings.jobs_root)


def _emit_report(report: ConversionReport, *, failed: bool = False) -> None:
    if _state["report_dir"] is not None:
        basename = f"{report.operation}-{report.job_id}"
        write_report_files(report, Path(str(_state["report_dir"])), basename)
    if _state["json"]:
        typer.echo(report.model_dump_json(indent=2))
    elif not _state["quiet"] or failed:
        typer.echo(report.to_human())


def _run(operation_fn, *args, password_retry: bool = True, **kwargs) -> None:
    """Execute an operation function, handling errors and report output."""
    try:
        report = operation_fn(*args, **kwargs)
    except organize_ops.EncryptedInputError as exc:
        options = kwargs.get("options")
        if password_retry and options is not None and hasattr(options, "password"):
            password = _password_retry_value(exc)
            options.password = password
            _run(operation_fn, *args, password_retry=False, **kwargs)
            return
        _fail_one_password(exc)
    except EngineUnavailableError as exc:
        typer.secho(f"Error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_NO_ENGINE) from exc
    except PageRangeError as exc:
        typer.secho(f"Error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_USAGE) from exc
    except PipelineError as exc:
        if exc.report is not None:
            _emit_report(exc.report, failed=True)
        typer.secho(f"Error: {exc}", fg=typer.colors.RED, err=True)
        cause = exc.__cause__
        if isinstance(cause, OutputCollisionError):
            raise typer.Exit(EXIT_COLLISION) from exc
        if isinstance(cause, FileNotFoundError):
            raise typer.Exit(EXIT_USAGE) from exc
        if isinstance(cause, ContentTypeError) and str(cause).startswith("Not a file:"):
            raise typer.Exit(EXIT_USAGE) from exc
        report = exc.report
        if report is not None and report.status == "cancelled":
            raise typer.Exit(EXIT_CANCELLED) from exc
        if report is not None and report.validation is not None and not report.validation.passed:
            raise typer.Exit(EXIT_VALIDATION) from exc
        raise typer.Exit(EXIT_FAILED) from exc
    else:
        _emit_report(report)
        for warning in report.security_warnings:
            typer.secho(f"⚠ {warning.message}", fg=typer.colors.YELLOW, err=True)


def _parse_range(value: str | None, *, what: str = "--pages") -> PageRange | None:
    if value is None:
        return None
    try:
        return PageRange(spec=value)
    except (PageRangeError, ValueError) as exc:
        typer.secho(f"Error in {what}: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_USAGE) from exc


def _expand_inputs(raw: list[Path]) -> list[Path]:
    """Expand glob patterns ourselves — PowerShell does not."""
    expanded: list[Path] = []
    for item in raw:
        if get_settings().strict_offline and is_remote_path(item):
            typer.secho(
                "Error: strict-offline mode forbids network filesystem inputs.",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(EXIT_USAGE)
        text = str(item)
        if any(char in text for char in "*?["):
            matches = sorted(_glob.glob(text))
            if not matches:
                typer.secho(f"Error: no files match {text!r}", fg=typer.colors.RED, err=True)
                raise typer.Exit(EXIT_USAGE)
            expanded.extend(Path(match) for match in matches)
        else:
            expanded.append(item)
    return expanded


Collision = Annotated[
    CollisionPolicy,
    typer.Option("--collision", help="What to do when the output already exists."),
]


# --------------------------------------------------------------------------- agent metadata


@app.command("agent-brief")
def agent_brief_cmd() -> None:
    """Print registry-derived Markdown or JSON guidance for document agents."""
    try:
        brief = build_agent_brief(strict_offline=get_settings().strict_offline)
    except AgentBriefError as exc:
        typer.secho(f"Error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_FAILED) from exc
    if _state["json"]:
        typer.echo(json.dumps(brief.to_dict(), indent=2, ensure_ascii=False))
    else:
        typer.echo(render_markdown(brief))


# --------------------------------------------------------------------------- doctor


@app.command()
def doctor() -> None:
    """Show engine availability and honest capability status."""
    registry = default_registry()
    infos = registry.all_infos()
    capabilities = registry.capabilities()
    settings = get_settings()
    if _state["json"]:
        payload = {
            "version": __version__,
            "strict_offline": settings.strict_offline,
            "outbound_network_client": False,
            "engines": [info.model_dump() for info in infos],
            "capabilities": [cap.model_dump() for cap in capabilities],
        }
        typer.echo(json.dumps(payload, indent=2))
        return
    typer.secho(f"LocalDocForge {__version__} — diagnostics", bold=True)
    typer.echo("\nEngines:")
    for info in infos:
        mark = "✓" if info.available else "✗"
        color = typer.colors.GREEN if info.available else typer.colors.RED
        line = f"  {mark} {info.name:<12} {info.version or '—'}"
        if info.notes:
            line += f"  ({info.notes})"
        typer.secho(line, fg=color)
        if not info.available and info.install_hint:
            typer.echo(f"      install: {info.install_hint}")
    typer.echo("\nCapabilities:")
    by_category: dict[str, list] = {}
    for capability in capabilities:
        by_category.setdefault(capability.category, []).append(capability)
    for category, caps in by_category.items():
        typer.echo(f"  {category}:")
        for capability in caps:
            mark = "✓" if capability.available else "·"
            color = typer.colors.GREEN if capability.available else typer.colors.BRIGHT_BLACK
            suffix = ""
            if not capability.available and capability.missing_requirements:
                suffix = f"  [{'; '.join(capability.missing_requirements)}]"
            typer.secho(f"    {mark} {capability.title}{suffix}", fg=color)
    strict_state = "enabled" if settings.strict_offline else "disabled"
    typer.echo(
        "\nPrivacy: shipped engines contain no outbound network client, telemetry, "
        f"update check, or remote resource loader. Strict-offline mode is {strict_state}."
    )


# --------------------------------------------------------------------------- web

_LOOPBACK_BIND_HOSTS = {"127.0.0.1", "::1", "localhost"}


def bind_allowed(host: str, allow_nonlocal: bool) -> bool:
    """Loopback is always fine; anything else needs the explicit opt-in flag."""
    return host in _LOOPBACK_BIND_HOSTS or allow_nonlocal


@app.command()
def web(
    host: Annotated[str, typer.Option("--host")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", min=1, max=65535)] = 8477,
    allow_nonlocal: Annotated[
        bool,
        typer.Option(
            "--allow-nonlocal",
            help="DANGEROUS: bind beyond loopback. Anyone who can reach the port "
            "can read and process files through this server.",
        ),
    ] = False,
) -> None:
    """Serve the local web API (and UI shell) on localhost."""
    import secrets
    import signal
    from importlib.util import find_spec

    required_modules = ("fastapi", "uvicorn", "python_multipart")
    if any(find_spec(module) is None for module in required_modules):
        typer.secho(
            "The web command requires the Standard profile. Install it with: "
            "pip install 'localdocforge[standard]'",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(EXIT_USAGE)

    import uvicorn

    from localdocforge.api.app import create_app
    from localdocforge.config.settings import get_settings

    settings = get_settings()

    if not bind_allowed(host, allow_nonlocal):
        typer.secho(
            f"Refusing to bind to {host!r}. LocalDocForge serves loopback only; "
            f"pass --allow-nonlocal if you truly accept the exposure.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(EXIT_USAGE)
    if settings.strict_offline and host not in _LOOPBACK_BIND_HOSTS:
        typer.secho(
            "Refusing non-loopback web access because strict-offline mode is enabled.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(EXIT_USAGE)
    if allow_nonlocal and host not in _LOOPBACK_BIND_HOSTS:
        typer.secho(
            "WARNING: binding beyond loopback. Every document on this machine that "
            "this server can read is now reachable by whoever can reach the port.",
            fg=typer.colors.YELLOW,
            err=True,
        )
    token = secrets.token_urlsafe(32)
    typer.echo(f"LocalDocForge web: http://{host}:{port}/")
    typer.echo(f"API token (send as X-LDF-Token header): {token}")
    if allow_nonlocal and host not in _LOOPBACK_BIND_HOSTS:
        typer.echo("Press Ctrl+C to stop. Non-loopback clients can reach this server.")
    else:
        typer.echo("Press Ctrl+C to stop. The server is restricted to loopback.")
    # Uvicorn handles SIGBREAK for graceful Windows shutdown, then re-raises it
    # through the handler that was installed before ``uvicorn.run``. Python's
    # default SIGBREAK action terminates with status 3, which collides with this
    # CLI's stable "engine unavailable" code. Translate that final re-raise to
    # KeyboardInterrupt so Uvicorn/this wrapper complete the graceful path with
    # a successful exit, matching ordinary Ctrl+C behavior.
    sigbreak = getattr(signal, "SIGBREAK", None)
    previous_sigbreak = signal.getsignal(sigbreak) if sigbreak is not None else None

    def graceful_sigbreak(_signum: int, _frame: object) -> None:
        raise KeyboardInterrupt

    if sigbreak is not None:
        signal.signal(sigbreak, graceful_sigbreak)
    try:
        uvicorn.run(
            create_app(
                settings,
                token=token,
                allow_nonlocal=allow_nonlocal and host not in _LOOPBACK_BIND_HOSTS,
            ),
            host=host,
            port=port,
            log_level="warning",
        )
    except KeyboardInterrupt:
        pass
    finally:
        if sigbreak is not None and previous_sigbreak is not None:
            signal.signal(sigbreak, previous_sigbreak)


# --------------------------------------------------------------------------- inspect


@app.command()
def inspect(
    input_file: Annotated[Path, typer.Argument(dir_okay=False)],
) -> None:
    """Read-only structural inventory of a PDF."""
    if get_settings().strict_offline and is_remote_path(input_file):
        typer.secho(
            "Error: strict-offline mode forbids network filesystem inputs.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(EXIT_USAGE)
    if not input_file.is_file():
        typer.secho(f"Error: file does not exist: {input_file}", fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_USAGE)
    password = _password_value()
    try:
        info = organize_ops.inspect_pdf(input_file, password=password)
    except organize_ops.EncryptedInputError as exc:
        password = _password_retry_value(exc)
        try:
            info = organize_ops.inspect_pdf(input_file, password=password)
        except Exception as retry_exc:  # wrong password or damaged file
            typer.secho(f"Error: {retry_exc}", fg=typer.colors.RED, err=True)
            raise typer.Exit(EXIT_FAILED) from retry_exc
    except Exception as exc:
        typer.secho(f"Error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_FAILED) from exc
    if _state["json"]:
        typer.echo(json.dumps(info, indent=2, ensure_ascii=False))
        return
    for key, value in info.items():
        typer.echo(f"{key:>14}: {value}")


# --------------------------------------------------------------------------- organize


@app.command()
def merge(
    inputs: Annotated[
        list[str],
        typer.Argument(
            help="Input PDFs, in order. Append ::RANGE to select pages, e.g. report.pdf::1-5"
        ),
    ],
    output: Annotated[Path, typer.Option("--output", "-o")],
    pages: Annotated[
        list[str] | None,
        typer.Option(
            "--pages", help="Page range for the input at the same position; repeat once per input."
        ),
    ] = None,
    collision: Collision = CollisionPolicy.FAIL,
) -> None:
    """Merge PDFs (whole files or selected page ranges) into one."""
    paths: list[Path] = []
    ranges: list[PageRange | None] = []
    for item in inputs:
        if "::" in item:
            path_text, _, range_text = item.rpartition("::")
            paths.append(Path(path_text))
            ranges.append(_parse_range(range_text, what=f"range for {path_text}"))
        else:
            paths.append(Path(item))
            ranges.append(None)
    if pages:
        if len(pages) != len(paths):
            typer.secho(
                f"Error: got {len(paths)} inputs but {len(pages)} --pages options; "
                f"repeat --pages once per input (or use file.pdf::RANGE).",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(EXIT_USAGE)
        ranges = [_parse_range(page_spec) for page_spec in pages]
    options = organize_ops.OrganizeOptions(
        collision=collision, password=_password_value()
    )
    _run(
        organize_ops.merge_pdfs,
        _expand_inputs(paths),
        output,
        page_ranges=ranges,
        options=options,
    )


@app.command()
def split(
    input_file: Annotated[Path, typer.Argument(dir_okay=False)],
    output_dir: Annotated[Path, typer.Option("--output-dir", "-d")],
    pages: Annotated[
        str | None,
        typer.Option("--pages", help='One output per comma token, e.g. "1-3,7,10-end".'),
    ] = None,
    every: Annotated[
        int | None,
        typer.Option("--every", min=1, help="Split into chunks of N pages."),
    ] = None,
    collision: Collision = CollisionPolicy.FAIL,
) -> None:
    """Split a PDF into ranges, every-N chunks, or single pages (default)."""
    options = organize_ops.OrganizeOptions(
        collision=collision, password=_password_value()
    )
    _run(
        organize_ops.split_pdf,
        input_file,
        output_dir,
        pages=_parse_range(pages),
        every=every,
        options=options,
    )


@app.command("remove-pages")
def remove_pages_cmd(
    input_file: Annotated[Path, typer.Argument(dir_okay=False)],
    output: Annotated[Path, typer.Option("--output", "-o")],
    pages: Annotated[str, typer.Option("--pages", help='Pages to remove, e.g. "2,5-7".')],
    collision: Collision = CollisionPolicy.FAIL,
) -> None:
    """Remove the selected pages."""
    options = organize_ops.OrganizeOptions(
        collision=collision, password=_password_value()
    )
    _run(
        organize_ops.remove_pages,
        input_file,
        output,
        _parse_range(pages),
        options=options,
    )


@app.command("extract-pages")
def extract_pages_cmd(
    input_file: Annotated[Path, typer.Argument(dir_okay=False)],
    output: Annotated[Path, typer.Option("--output", "-o")],
    pages: Annotated[str, typer.Option("--pages", help='Pages to extract, e.g. "1-3,10".')],
    collision: Collision = CollisionPolicy.FAIL,
) -> None:
    """Extract the selected pages into a new PDF."""
    options = organize_ops.OrganizeOptions(
        collision=collision, password=_password_value()
    )
    _run(
        organize_ops.extract_pages,
        input_file,
        output,
        _parse_range(pages),
        options=options,
    )


@app.command()
def organize(
    input_file: Annotated[Path, typer.Argument(dir_okay=False)],
    output: Annotated[Path, typer.Option("--output", "-o")],
    order: Annotated[str, typer.Option("--order", help='New page order, e.g. "3,1,2,4-end".')],
    collision: Collision = CollisionPolicy.FAIL,
) -> None:
    """Reorder, duplicate, or drop pages using an explicit order."""
    options = organize_ops.OrganizeOptions(
        collision=collision, password=_password_value()
    )
    _run(
        organize_ops.organize_pdf,
        input_file,
        output,
        _parse_range(order, what="--order"),
        options=options,
    )


@app.command()
def rotate(
    input_file: Annotated[Path, typer.Argument(dir_okay=False)],
    output: Annotated[Path, typer.Option("--output", "-o")],
    degrees: Annotated[int, typer.Option("--degrees", help="90, 180, 270 (or negative).")],
    pages: Annotated[str | None, typer.Option("--pages")] = None,
    collision: Collision = CollisionPolicy.FAIL,
) -> None:
    """Rotate the selected pages (default: all) by a multiple of 90 degrees."""
    options = organize_ops.OrganizeOptions(
        collision=collision, password=_password_value()
    )
    _run(
        organize_ops.rotate_pages,
        input_file,
        output,
        degrees=degrees,
        pages=_parse_range(pages),
        options=options,
    )


@app.command()
def crop(
    input_file: Annotated[Path, typer.Argument(dir_okay=False)],
    output: Annotated[Path, typer.Option("--output", "-o")],
    box: Annotated[
        str,
        typer.Option("--box", help='Visible area "x0,y0,x1,y1" in PDF points, origin bottom-left.'),
    ],
    pages: Annotated[str | None, typer.Option("--pages")] = None,
    collision: Collision = CollisionPolicy.FAIL,
) -> None:
    """Crop pages. NOT redaction: hidden content remains inside the file."""
    try:
        parts = [float(part) for part in box.split(",")]
        if len(parts) != 4:
            raise ValueError
    except ValueError:
        typer.secho(
            'Error: --box must be four numbers "x0,y0,x1,y1" in points.',
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(EXIT_USAGE) from None
    options = organize_ops.OrganizeOptions(
        collision=collision, password=_password_value()
    )
    _run(
        organize_ops.crop_pages,
        input_file,
        output,
        box=(parts[0], parts[1], parts[2], parts[3]),
        pages=_parse_range(pages),
        options=options,
    )


# --------------------------------------------------------------------------- optimize


@app.command()
def compress(
    input_file: Annotated[Path, typer.Argument(dir_okay=False)],
    output: Annotated[Path, typer.Option("--output", "-o")],
    preset: Annotated[
        str,
        typer.Option(
            "--preset",
            help="Only 'lossless' is implemented; image-downsampling presets are planned.",
        ),
    ] = "lossless",
    collision: Collision = CollisionPolicy.FAIL,
) -> None:
    """Losslessly optimize PDF structure. Image data is never re-encoded."""
    if preset not in optimize_ops.COMPRESS_PRESETS:
        available = ", ".join(optimize_ops.COMPRESS_PRESETS)
        planned = ", ".join(optimize_ops.PLANNED_COMPRESS_PRESETS)
        typer.secho(
            f"Error: --preset {preset!r} is not available in this build. "
            f"Available: {available}. Planned (not implemented): {planned}.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(EXIT_USAGE)
    options = organize_ops.OrganizeOptions(
        collision=collision, password=_password_value()
    )
    _run(optimize_ops.compress_pdf, input_file, output, preset=preset, options=options)


# --------------------------------------------------------------------------- images

_LLM_RENDER_PRESET = image_ops.CONVERT_PRESETS["llm"]
_LLM_RENDER_PRESET_HELP = (
    f"'llm' = {_LLM_RENDER_PRESET['image_format'].upper()} quality "
    f"{_LLM_RENDER_PRESET['quality']}, long edge <= "
    f"{_LLM_RENDER_PRESET['max_dimension']} px per page; never upscales from "
    "the default render. Explicit --format/--quality/--dpi override preset values."
)


@app.command("images-to-pdf")
def images_to_pdf_cmd(
    inputs: Annotated[list[Path], typer.Argument(help="Image files (globs allowed).")],
    output: Annotated[Path, typer.Option("--output", "-o")],
    page_size: Annotated[
        str, typer.Option("--page-size", help="A4, Letter, Legal, image, or WxH[mm|cm|in|pt].")
    ] = "A4",
    fit: Annotated[str, typer.Option("--fit", help="fit | stretch | center")] = "fit",
    margin: Annotated[float, typer.Option("--margin", help="Margin in points.")] = 24.0,
    background: Annotated[str, typer.Option("--background")] = "white",
    dpi: Annotated[int, typer.Option("--dpi", min=36, max=600)] = 200,
    quality: Annotated[int, typer.Option("--quality", min=1, max=100)] = 95,
    collision: Collision = CollisionPolicy.FAIL,
) -> None:
    """Combine images (JPG/PNG/TIFF/BMP/WebP) into a single PDF."""
    options = image_ops.ImagesToPdfOptions(
        page_size=page_size,
        fit=fit,
        margin_pt=margin,
        background=background,
        dpi=dpi,
        jpeg_quality=quality,
        collision=collision,
    )
    _run(image_ops.images_to_pdf, _expand_inputs(inputs), output, options=options)


@app.command("pdf-to-images")
def pdf_to_images_cmd(
    input_file: Annotated[Path, typer.Argument(dir_okay=False)],
    output_dir: Annotated[Path, typer.Option("--output-dir", "-d")],
    image_format: Annotated[
        str | None,
        typer.Option("--format", help="png | jpeg | webp | tiff (default png)."),
    ] = None,
    dpi: Annotated[
        int | None,
        typer.Option(
            "--dpi",
            min=18,
            max=1200,
            help="Fixed render DPI (default 150); explicitly setting it overrides "
            "the llm preset's pixel cap.",
        ),
    ] = None,
    pages: Annotated[str | None, typer.Option("--pages")] = None,
    quality: Annotated[
        int | None,
        typer.Option("--quality", min=1, max=100, help="JPEG/WebP quality (default 90)."),
    ] = None,
    preset: Annotated[
        str | None,
        typer.Option("--preset", help=_LLM_RENDER_PRESET_HELP),
    ] = None,
    collision: Collision = CollisionPolicy.FAIL,
) -> None:
    """Render PDF pages to PNG/JPEG/WebP/TIFF images."""
    if preset is not None and preset not in image_ops.CONVERT_PRESETS:
        available = ", ".join(sorted(image_ops.CONVERT_PRESETS))
        typer.secho(
            f"Error: --preset {preset!r} is not available. Available: {available}.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(EXIT_USAGE)
    options = image_ops.PdfToImagesOptions(
        pages=_parse_range(pages),
        preset=preset,
        collision=collision,
        password=_password_value(),
    )
    if image_format is not None:
        options.image_format = image_format
    if dpi is not None:
        options.dpi = dpi
    if quality is not None:
        options.jpeg_quality = quality
    _run(image_ops.pdf_to_images, input_file, output_dir, options=options)


@app.command("convert-images")
def convert_images_cmd(
    inputs: Annotated[
        list[Path],
        typer.Argument(help="Image files (globs allowed), including iPhone HEIC/HEIF."),
    ],
    output_dir: Annotated[Path, typer.Option("--output-dir", "-d")],
    image_format: Annotated[
        str | None,
        typer.Option("--format", help="png | jpeg | webp | tiff (default jpeg)."),
    ] = None,
    quality: Annotated[
        int | None,
        typer.Option(
            "--quality", min=1, max=100, help="JPEG/WebP quality (default 90)."
        ),
    ] = None,
    max_dimension: Annotated[
        int | None,
        typer.Option(
            "--max-dimension",
            min=16,
            max=30000,
            help="Downscale so the long edge is at most this many pixels; "
            "never upscales.",
        ),
    ] = None,
    preset: Annotated[
        str | None,
        typer.Option(
            "--preset",
            help="'llm' = JPEG quality 85, long edge ≤ 1568 px, metadata "
            "stripped — sized for compatible AI image inputs. "
            "Explicit flags override preset values.",
        ),
    ] = None,
    keep_metadata: Annotated[
        bool,
        typer.Option(
            "--keep-metadata",
            help="Retain EXIF metadata (including any GPS position) instead of "
            "stripping it.",
        ),
    ] = False,
    background: Annotated[
        str,
        typer.Option(
            "--background",
            help="Background color used when transparency is flattened for JPEG.",
        ),
    ] = "white",
    collision: Collision = CollisionPolicy.FAIL,
) -> None:
    """Convert images (incl. iPhone HEIC) to shareable PNG/JPEG/WebP/TIFF."""
    if preset is not None and preset not in image_ops.CONVERT_PRESETS:
        available = ", ".join(sorted(image_ops.CONVERT_PRESETS))
        typer.secho(
            f"Error: --preset {preset!r} is not available. Available: {available}.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(EXIT_USAGE)
    if image_format is not None and image_format.lower() not in image_ops.OUTPUT_IMAGE_FORMATS:
        typer.secho(
            f"Error: --format {image_format!r} is not supported; use png, jpeg, "
            "webp, or tiff.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(EXIT_USAGE)
    options = image_ops.ConvertImagesOptions(
        image_format=image_format,
        quality=quality,
        max_dimension=max_dimension,
        preset=preset,
        keep_metadata=keep_metadata,
        background=background,
        collision=collision,
    )
    _run(image_ops.convert_images, _expand_inputs(inputs), output_dir, options=options)


def app_entry() -> None:  # console_scripts entry point
    # Legacy Windows consoles default to a narrow codepage; our output contains
    # ✓/⚠ marks and Unicode filenames. Never crash over display encoding.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(errors="replace")
            except (OSError, ValueError):
                pass
    app()


if __name__ == "__main__":
    app_entry()
