"""LocalDocForge MCP 2025-11-25 stdio server."""

from __future__ import annotations

import contextlib
import json
import logging
import sys
import warnings
from collections.abc import Iterator
from typing import Any, cast

import anyio
from mcp import types
from mcp.server.lowlevel import Server
from mcp.shared.exceptions import McpError

from localdocforge import __version__
from localdocforge.config.settings import Settings

from .runner import ToolInternalError, ToolRunError, run_tool_in_worker
from .tools import (
    ToolArgumentsError,
    ToolLookupError,
    tool_definitions,
    validate_tool_arguments,
)
from .transport import bounded_stdio_server

PROTOCOL_REVISION = "2025-11-25"
_MAX_STRUCTURED_RESPONSE_BYTES = 768 * 1024
_MAX_TOOL_ERROR_CHARS = 4096


def _compact_fidelity_warnings(
    report: dict[str, Any],
    *,
    limit: int,
) -> list[Any]:
    value = report.get("fidelity_warnings")
    if not isinstance(value, list):
        return []
    selected = value[:limit]
    status = report.get("fidelity_status")
    required_impact = (
        "known-loss"
        if status == "known-loss"
        else "review"
        if status == "review-required"
        else None
    )
    if required_impact and not any(
        isinstance(item, dict) and item.get("impact") == required_impact for item in selected
    ):
        required = next(
            (
                item
                for item in value
                if isinstance(item, dict) and item.get("impact") == required_impact
            ),
            None,
        )
        if required is not None:
            selected = [*selected[:-1], required] if selected else [required]
    return selected


def _structured_size(payload: dict[str, Any]) -> int:
    return len(
        json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def _report_counts(report: dict[str, Any]) -> dict[str, int]:
    fields = {
        "inputs": "input_count",
        "outputs": "output_count",
        "security_warnings": "security_warning_count",
        "fidelity_warnings": "fidelity_warning_count",
        "errors": "error_count",
    }
    counts = {
        count_name: len(value)
        for field, count_name in fields.items()
        if isinstance((value := report.get(field)), list)
    }
    validation = report.get("validation")
    checks = validation.get("checks") if isinstance(validation, dict) else None
    counts["validation_checks_count"] = len(checks) if isinstance(checks, list) else 0
    return counts


def _bounded_report_text(value: object, limit: int) -> str:
    return str(value).replace("\x00", "")[:limit]


def _minimal_report(
    report: dict[str, Any],
    *,
    default_status: str,
) -> dict[str, Any]:
    """Return a small, schema-valid report retaining the fidelity verdict."""
    compact: dict[str, Any] = {
        "operation": _bounded_report_text(report.get("operation", "unknown"), 256),
        "status": report.get("status", default_status),
        "job_id": _bounded_report_text(report.get("job_id", "unknown"), 256),
        "fidelity_status": report.get("fidelity_status", "unassessed"),
        "fidelity_coverage": report.get("fidelity_coverage", "none"),
        "inputs": [],
        "outputs": [],
        "security_warnings": [],
        "fidelity_warnings": [],
        "errors": [],
    }
    selected = _compact_fidelity_warnings(report, limit=1)
    if selected and isinstance(selected[0], dict):
        warning = selected[0]
        minimal_warning: dict[str, Any] = {
            "code": _bounded_report_text(warning.get("code", "fidelity-warning"), 256),
            "message": _bounded_report_text(warning.get("message", ""), 512),
            "severity": warning.get("severity", "warning"),
            "basis": warning.get("basis", "declared"),
            "impact": warning.get("impact", "review"),
        }
        if isinstance(warning.get("page"), int):
            minimal_warning["page"] = warning["page"]
        remedy = warning.get("remedy")
        if isinstance(remedy, str):
            minimal_warning["remedy"] = _bounded_report_text(remedy, 512)
        compact["fidelity_warnings"] = [minimal_warning]
    validation = report.get("validation")
    if isinstance(validation, dict):
        compact["validation"] = {
            "passed": bool(validation.get("passed", False)),
            "checks": [],
        }
    return compact


def _last_resort_report(
    report: dict[str, Any],
    *,
    default_status: str,
) -> dict[str, Any]:
    """Return a fixed-shape report when even the bounded fidelity form is oversized.

    This unreachable-in-normal-bounds fallback deliberately downgrades fidelity
    to unassessed instead of emitting a status without its supporting warning.
    It therefore stays schema-valid and can never manufacture a clean verdict.
    """
    return {
        "operation": _bounded_report_text(report.get("operation", "unknown"), 64),
        "status": default_status,
        "job_id": _bounded_report_text(report.get("job_id", "unknown"), 64),
        "fidelity_status": "unassessed",
        "fidelity_coverage": "none",
        "inputs": [],
        "outputs": [],
        "security_warnings": [],
        "fidelity_warnings": [],
        "errors": [],
    }


def _tool_error(
    message: str,
    *,
    tool_name: str | None = None,
    report: dict[str, Any] | None = None,
) -> types.ServerResult:
    if len(message) > _MAX_TOOL_ERROR_CHARS:
        message = message[:_MAX_TOOL_ERROR_CHARS] + "…"
    structured: dict[str, Any] | None = None
    if tool_name is not None and report is not None:
        counts = _report_counts(report)
        structured = {
            "status": "error",
            "tool": tool_name,
            "report": report,
        }
        if _structured_size(structured) > _MAX_STRUCTURED_RESPONSE_BYTES:
            report_fields = (
                "operation",
                "status",
                "job_id",
                "engine",
                "engine_version",
                "fidelity_status",
                "fidelity_coverage",
            )
            compact_report = {key: report[key] for key in report_fields if key in report}
            for field in ("security_warnings", "fidelity_warnings", "errors"):
                if field == "fidelity_warnings":
                    compact_report[field] = _compact_fidelity_warnings(report, limit=4)
                else:
                    value = report.get(field)
                    compact_report[field] = value[:4] if isinstance(value, list) else []
            validation = report.get("validation")
            if isinstance(validation, dict):
                compact_report["validation"] = {
                    "passed": validation.get("passed", False),
                    "checks": validation.get("checks", [])[:4]
                    if isinstance(validation.get("checks"), list)
                    else [],
                }
            structured = {
                "status": "error",
                "tool": tool_name,
                "report": compact_report,
                "mcp_response": {"server_truncated": True, **counts},
            }
        if _structured_size(structured) > _MAX_STRUCTURED_RESPONSE_BYTES:
            structured = {
                "status": "error",
                "tool": tool_name,
                "report": _minimal_report(report, default_status="failed"),
                "mcp_response": {"server_truncated": True, **counts},
            }
        # A final measurement guards future report fields from silently
        # invalidating the transport bound. Tool names come from the registry,
        # but bound the value here as well so this path always stays a tool
        # error instead of degrading to a generic JSON-RPC internal error.
        if _structured_size(structured) > _MAX_STRUCTURED_RESPONSE_BYTES:
            structured["tool"] = _bounded_report_text(tool_name, 256)
        if _structured_size(structured) > _MAX_STRUCTURED_RESPONSE_BYTES:
            structured = {
                "status": "error",
                "tool": _bounded_report_text(tool_name, 64),
                "report": _last_resort_report(report, default_status="failed"),
                "mcp_response": {"server_truncated": True},
            }
        if _structured_size(structured) > _MAX_STRUCTURED_RESPONSE_BYTES:
            raise RuntimeError("MCP error response could not be bounded")
    return types.ServerResult(
        types.CallToolResult(
            content=[types.TextContent(type="text", text=message)],
            structuredContent=structured,
            isError=True,
        )
    )


def _success_result(tool_name: str, result) -> types.ServerResult:
    structured: dict[str, Any] = {
        "status": "success",
        "tool": tool_name,
        "outputs": result.outputs,
        "report": result.report,
        "containment": result.containment,
        **result.data,
    }
    summarized = False
    if _structured_size(structured) > _MAX_STRUCTURED_RESPONSE_BYTES:
        summarized = True
        report_fields = (
            "operation",
            "status",
            "job_id",
            "engine",
            "engine_version",
            "fallback_engine",
            "input_page_count",
            "output_page_count",
            "input_bytes",
            "output_bytes",
            "started_at",
            "finished_at",
            "elapsed_seconds",
            "fidelity_status",
            "fidelity_coverage",
        )
        compact_report = {key: result.report[key] for key in report_fields if key in result.report}
        for field in ("security_warnings", "fidelity_warnings", "errors"):
            if field == "fidelity_warnings":
                compact_report[field] = _compact_fidelity_warnings(
                    result.report,
                    limit=4,
                )
            else:
                value = result.report.get(field)
                compact_report[field] = value[:4] if isinstance(value, list) else []
        validation = result.report.get("validation")
        if isinstance(validation, dict):
            compact_report["validation"] = {"passed": validation.get("passed", False)}
        worker_summary = result.data.get("mcp_response")
        response_summary: dict[str, Any] = {
            "server_truncated": True,
            **_report_counts(result.report),
            "output_count": len(result.outputs),
            "listed_output_count": min(4, len(result.outputs)),
        }
        if isinstance(worker_summary, dict):
            response_summary.update(
                {
                    str(key): value
                    for key, value in worker_summary.items()
                    if isinstance(value, (str, int, float, bool)) or value is None
                }
            )
        structured = {
            "status": "success",
            "tool": tool_name,
            "outputs": result.outputs[:4],
            "report": compact_report,
            "containment": result.containment,
            "mcp_response": response_summary,
        }
    if _structured_size(structured) > _MAX_STRUCTURED_RESPONSE_BYTES:
        summarized = True
        response_summary = {
            "server_truncated": True,
            **_report_counts(result.report),
            "output_count": len(result.outputs),
            "listed_output_count": 0,
        }
        structured = {
            "status": "success",
            "tool": tool_name,
            "outputs": [],
            "report": _minimal_report(result.report, default_status="success"),
            "containment": {},
            "mcp_response": response_summary,
        }
    if _structured_size(structured) > _MAX_STRUCTURED_RESPONSE_BYTES:
        structured["tool"] = _bounded_report_text(tool_name, 256)
    if _structured_size(structured) > _MAX_STRUCTURED_RESPONSE_BYTES:
        structured = {
            "status": "success",
            "tool": _bounded_report_text(tool_name, 64),
            "outputs": [],
            "report": _last_resort_report(result.report, default_status="success"),
            "containment": {},
            "mcp_response": {"server_truncated": True},
        }
    if _structured_size(structured) > _MAX_STRUCTURED_RESPONSE_BYTES:
        raise RuntimeError("MCP success response could not be bounded")
    message = (
        "LocalDocForge job completed; response summarized to size limit"
        if summarized
        else "LocalDocForge job completed"
    )
    return types.ServerResult(
        types.CallToolResult(
            content=[types.TextContent(type="text", text=message)],
            structuredContent=structured,
            isError=False,
        )
    )


def build_server(settings: Settings) -> Server[Any, Any]:
    """Create a low-level SDK server whose public tools come only from the registry."""
    server: Server[Any, Any] = Server(
        "localdocforge",
        version=__version__,
        instructions=(
            "Local, synchronous document tools. Calls are serialized; v1 does not "
            "stream progress. All file paths must be absolute."
        ),
    )
    call_lock = anyio.Lock()

    @server.list_tools()
    async def list_tools(_request: types.ListToolsRequest) -> types.ListToolsResult:
        return types.ListToolsResult(tools=tool_definitions())

    async def call_tool(request: types.CallToolRequest) -> types.ServerResult:
        async with call_lock:
            arguments = request.params.arguments or {}
            try:
                parsed = validate_tool_arguments(request.params.name, arguments)
            except ToolLookupError as exc:
                raise McpError(
                    types.ErrorData(code=types.INVALID_PARAMS, message=str(exc))
                ) from None
            except ToolArgumentsError as exc:
                return _tool_error(f"Invalid tool arguments: {exc}")

            try:
                result = await run_tool_in_worker(
                    request.params.name,
                    parsed,
                    settings,
                )
                return _success_result(request.params.name, result)
            except ToolRunError as exc:
                return _tool_error(
                    str(exc),
                    tool_name=request.params.name,
                    report=exc.report,
                )
            except anyio.get_cancelled_exc_class():
                raise
            except ToolInternalError as exc:
                print(f"MCP diagnostic: {exc}", file=sys.stderr, flush=True)
                raise McpError(
                    types.ErrorData(
                        code=types.INTERNAL_ERROR,
                        message="Internal server error",
                    )
                ) from None
            except Exception as exc:
                print(
                    f"MCP diagnostic: unexpected {type(exc).__name__}",
                    file=sys.stderr,
                    flush=True,
                )
                raise McpError(
                    types.ErrorData(
                        code=types.INTERNAL_ERROR,
                        message="Internal server error",
                    )
                ) from None

    server.request_handlers[types.CallToolRequest] = cast(Any, call_tool)
    return server


@contextlib.contextmanager
def _quiet_protocol_process() -> Iterator[None]:
    """Prevent SDK request reprs, warnings, or credentials reaching diagnostics."""
    previous_disable = logging.root.manager.disable
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        logging.disable(100)
        try:
            yield
        finally:
            logging.disable(previous_disable)


async def _serve(settings: Settings) -> None:
    server = build_server(settings)
    initialization = server.create_initialization_options()
    async with bounded_stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            initialization,
            raise_exceptions=False,
        )


def run_mcp_server(settings: Settings) -> None:
    """Run the synchronous-v1 stdio server until its client disconnects."""
    with _quiet_protocol_process():
        anyio.run(_serve, settings)


__all__ = ["PROTOCOL_REVISION", "build_server", "run_mcp_server"]
