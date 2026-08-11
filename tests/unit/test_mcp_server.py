"""Serialization, cancellation, and error semantics at the MCP server boundary."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp import types
from mcp.shared.exceptions import McpError

from localdocforge.api import worker as worker_module
from localdocforge.api.worker import WorkerJobStatus, WorkerOutcome
from localdocforge.config.settings import Settings
from localdocforge.domain.models import (
    ConversionReport,
    FidelityWarning,
    OutputArtifact,
    ReportStatus,
    SecurityWarning,
    ValidationCheck,
    ValidationResult,
)
from localdocforge.mcp import runner as runner_module
from localdocforge.mcp import server as server_module
from localdocforge.mcp.runner import ToolRunResult
from localdocforge.mcp.tools import validate_tool_arguments


def _merge_request(tmp_path: Path, request_id: int) -> types.CallToolRequest:
    return types.CallToolRequest(
        params=types.CallToolRequestParams(
            name="merge",
            arguments={
                "inputs": [str(tmp_path / f"a-{request_id}.pdf"), str(tmp_path / "b.pdf")],
                "output": str(tmp_path / f"out-{request_id}.pdf"),
            },
        )
    )


def test_concurrent_calls_are_serialized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active = 0
    maximum = 0

    async def fake_run(_name: str, _arguments: Any, _settings: Settings) -> ToolRunResult:
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        await anyio.sleep(0.05)
        active -= 1
        return ToolRunResult(report={"operation": "merge"}, outputs=[])

    monkeypatch.setattr(server_module, "run_tool_in_worker", fake_run)

    async def scenario() -> None:
        server = server_module.build_server(Settings(jobs_root=tmp_path / "jobs"))
        handler = server.request_handlers[types.CallToolRequest]
        results: list[types.ServerResult] = []

        async def invoke(request: types.CallToolRequest) -> None:
            results.append(await handler(request))

        async with anyio.create_task_group() as group:
            group.start_soon(invoke, _merge_request(tmp_path, 1))
            group.start_soon(invoke, _merge_request(tmp_path, 2))
        assert len(results) == 2
        assert all(isinstance(result.root, types.CallToolResult) for result in results)

    anyio.run(scenario)
    assert maximum == 1


def test_unknown_tool_is_json_rpc_invalid_params(tmp_path: Path) -> None:
    async def scenario() -> None:
        server = server_module.build_server(Settings(jobs_root=tmp_path / "jobs"))
        handler = server.request_handlers[types.CallToolRequest]
        request = types.CallToolRequest(
            params=types.CallToolRequestParams(name="not-a-tool", arguments={})
        )
        with pytest.raises(McpError) as caught:
            await handler(request)
        assert caught.value.error.code == types.INVALID_PARAMS

    anyio.run(scenario)


def test_recognized_invalid_arguments_are_tool_errors(tmp_path: Path) -> None:
    async def scenario() -> None:
        server = server_module.build_server(Settings(jobs_root=tmp_path / "jobs"))
        handler = server.request_handlers[types.CallToolRequest]
        request = types.CallToolRequest(
            params=types.CallToolRequestParams(
                name="merge",
                arguments={"inputs": [], "output": str(tmp_path / "out.pdf")},
            )
        )
        result = await handler(request)
        assert isinstance(result.root, types.CallToolResult)
        assert result.root.isError is True

    anyio.run(scenario)


def test_handler_cancellation_terminates_worker_controller(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = threading.Event()
    terminated = threading.Event()
    stopped = threading.Event()

    class BlockingWorker:
        def __init__(self, _request: Any) -> None:
            pass

        def run(self, cancel_requested: threading.Event) -> WorkerOutcome:
            started.set()
            while not cancel_requested.is_set():
                time.sleep(0.005)
            stopped.set()
            return WorkerOutcome(
                status=WorkerJobStatus.CANCELLED,
                error="cancelled",
                http_status=409,
            )

        def terminate(self) -> bool:
            terminated.set()
            return True

    monkeypatch.setattr(runner_module, "WorkerProcess", BlockingWorker)
    arguments = validate_tool_arguments(
        "merge",
        {
            "inputs": [str(tmp_path / "a.pdf"), str(tmp_path / "b.pdf")],
            "output": str(tmp_path / "out.pdf"),
        },
    )

    async def scenario() -> None:
        async with anyio.create_task_group() as group:
            group.start_soon(
                runner_module.run_tool_in_worker,
                "merge",
                arguments,
                Settings(jobs_root=tmp_path / "jobs"),
            )
            while not started.is_set():
                await anyio.sleep(0.005)
            group.cancel_scope.cancel()

    anyio.run(scenario)
    assert terminated.is_set()
    assert stopped.is_set()
    assert not list((tmp_path / "jobs").glob("ldf-job-mcp-*"))


def test_large_success_reports_are_summarized_without_becoming_errors() -> None:
    report = {
        "operation": "split",
        "status": "success",
        "job_id": "mcp-large-report",
        "outputs": [
            {
                "path": f"C:/outputs/{index}-{'x' * 4096}.pdf",
                "media_type": "application/pdf",
                "size_bytes": 1,
            }
            for index in range(100)
        ],
        "details": {"per_output": "y" * (256 * 1024)},
    }
    compact, summary = worker_module._compact_mcp_report(report)
    assert summary["report_truncated"] is True
    assert summary["output_count"] == 100
    assert len(compact["outputs"]) == worker_module._MCP_REPORT_LIST_LIMIT
    assert len(
        json.dumps(compact, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ) <= worker_module._MAX_MCP_REPORT_IPC_BYTES
    ConversionReport.model_validate(compact)

    result = ToolRunResult(
        report={**report, "details": {"large": "z" * (800 * 1024)}},
        outputs=["C:/outputs/result.pdf"],
    )
    response = server_module._success_result("split", result)
    assert isinstance(response.root, types.CallToolResult)
    assert response.root.isError is False
    assert response.root.structuredContent is not None
    assert response.root.structuredContent["mcp_response"]["server_truncated"] is True


def test_mcp_report_preserves_typed_output_path_but_redacts_content_values(
    tmp_path: Path,
) -> None:
    password = "e"
    output = (tmp_path / "merged-here.pdf").resolve()
    report = ConversionReport(
        operation="merge",
        status=ReportStatus.SUCCESS,
        job_id="private-worker-job",
        outputs=[
            OutputArtifact(
                path=output,
                media_type="application/pdf",
                size_bytes=1,
            )
        ],
        security_warnings=[
            SecurityWarning(code="secret-warning", message=f"before-{password}-after")
        ],
        fidelity_warnings=[
            FidelityWarning(code="secret-fidelity", message=f"before-{password}-after")
        ],
        errors=[f"before-{password}-after"],
        validation=ValidationResult(
            passed=False,
            checks=[
                ValidationCheck(
                    name="secret-check",
                    passed=False,
                    detail=f"before-{password}-after",
                )
            ],
        ),
        details={"metadata": {"title": f"before-{password}-after"}},
    )

    payload = worker_module._sanitized_report(
        report,
        api_job_id="public-job",
        job_root=tmp_path / "private-workspace",
        secrets=(password,),
        preserve_paths=True,
        preserve_output_paths_exact=True,
    )

    redacted = f"before-{password}-after".replace(password, "<redacted>")
    assert payload["outputs"][0]["path"] == str(output)
    assert payload["security_warnings"][0]["message"] == redacted
    assert payload["fidelity_warnings"][0]["message"] == redacted
    assert payload["errors"] == [redacted]
    assert payload["validation"]["checks"][0]["detail"] == redacted
    assert payload["details"]["metadata"]["title"] == redacted
