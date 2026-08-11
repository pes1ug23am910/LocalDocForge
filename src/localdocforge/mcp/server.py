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


def _tool_error(message: str) -> types.ServerResult:
    if len(message) > _MAX_TOOL_ERROR_CHARS:
        message = message[:_MAX_TOOL_ERROR_CHARS] + "…"
    return types.ServerResult(
        types.CallToolResult(
            content=[types.TextContent(type="text", text=message)],
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
    encoded = json.dumps(
        structured,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > _MAX_STRUCTURED_RESPONSE_BYTES:
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
        )
        compact_report = {
            key: result.report[key] for key in report_fields if key in result.report
        }
        for field in ("security_warnings", "fidelity_warnings", "errors"):
            value = result.report.get(field)
            compact_report[field] = value[:4] if isinstance(value, list) else []
        validation = result.report.get("validation")
        if isinstance(validation, dict):
            compact_report["validation"] = {"passed": validation.get("passed", False)}
        worker_summary = result.data.get("mcp_response")
        response_summary: dict[str, Any] = {
            "server_truncated": True,
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
        return types.ServerResult(
            types.CallToolResult(
                content=[
                    types.TextContent(
                        type="text",
                        text="LocalDocForge job completed; response summarized to size limit",
                    )
                ],
                structuredContent=structured,
                isError=False,
            )
        )
    return types.ServerResult(
        types.CallToolResult(
            content=[types.TextContent(type="text", text="LocalDocForge job completed")],
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
                return _tool_error(str(exc))
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
