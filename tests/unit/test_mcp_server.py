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
from localdocforge.mcp.runner import ToolRunError, ToolRunResult
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


def test_expected_tool_failure_preserves_structured_fidelity_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = ConversionReport(
        operation="merge",
        status=ReportStatus.FAILED,
        job_id="structured-fidelity-failure",
        fidelity_status="review-required",
        fidelity_coverage="complete",
        fidelity_warnings=[
            FidelityWarning(
                code="synthetic-review",
                message="Review required",
                basis="structural",
                impact="review",
            )
        ],
        validation=ValidationResult(
            passed=False,
            checks=[ValidationCheck(name="strict-fidelity", passed=False)],
        ),
    ).model_dump(mode="json")

    async def fail(_name: str, _arguments: Any, _settings: Settings) -> ToolRunResult:
        raise ToolRunError("strict fidelity refused output", report=report)

    monkeypatch.setattr(server_module, "run_tool_in_worker", fail)

    async def scenario() -> None:
        server = server_module.build_server(Settings(jobs_root=tmp_path / "jobs"))
        handler = server.request_handlers[types.CallToolRequest]
        result = await handler(_merge_request(tmp_path, 1))
        assert isinstance(result.root, types.CallToolResult)
        assert result.root.isError is True
        assert result.root.structuredContent is not None
        structured = result.root.structuredContent
        assert structured["status"] == "error"
        assert structured["report"]["fidelity_status"] == "review-required"
        assert structured["report"]["fidelity_coverage"] == "complete"
        assert structured["report"]["validation"]["checks"][0]["name"] == ("strict-fidelity")

    anyio.run(scenario)


def test_worker_failure_exception_retains_attached_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failed_report = ConversionReport(
        operation="merge",
        status=ReportStatus.FAILED,
        job_id="worker-fidelity-failure",
        fidelity_status="unassessed",
        fidelity_coverage="none",
    )

    class FailedWorker:
        def __init__(self, _request: Any) -> None:
            pass

        def run(self, _cancel_requested: threading.Event) -> WorkerOutcome:
            return WorkerOutcome(
                status=WorkerJobStatus.FAILED,
                report=failed_report,
                error="strict fidelity refused output",
                http_status=422,
            )

        def terminate(self) -> bool:
            return True

    monkeypatch.setattr(runner_module, "WorkerProcess", FailedWorker)
    arguments = validate_tool_arguments(
        "merge",
        {
            "inputs": [str(tmp_path / "a.pdf"), str(tmp_path / "b.pdf")],
            "output": str(tmp_path / "out.pdf"),
        },
    )

    async def scenario() -> None:
        with pytest.raises(ToolRunError) as caught:
            await runner_module.run_tool_in_worker(
                "merge",
                arguments,
                Settings(jobs_root=tmp_path / "jobs"),
            )
        assert caught.value.report is not None
        assert caught.value.report["fidelity_status"] == "unassessed"

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
        "fidelity_status": "known-loss",
        "fidelity_coverage": "partial",
        "job_id": "mcp-large-report",
        "fidelity_warnings": [
            {
                "code": f"advisory-{index}",
                "message": "Synthetic advisory",
                "basis": "structural",
                "impact": "advisory",
            }
            for index in range(12)
        ]
        + [
            {
                "code": "synthetic-known-loss",
                "message": "Synthetic loss",
                "basis": "structural",
                "impact": "known-loss",
            }
        ],
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
    assert compact["fidelity_status"] == "known-loss"
    assert compact["fidelity_coverage"] == "partial"
    assert any(warning["impact"] == "known-loss" for warning in compact["fidelity_warnings"])
    assert len(compact["outputs"]) == worker_module._MCP_REPORT_LIST_LIMIT
    assert compact["details"]["transport_compaction"]["output_count"] == 100
    assert "mcp_response" not in compact["details"]
    assert (
        len(json.dumps(compact, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        <= worker_module._MAX_MCP_REPORT_IPC_BYTES
    )
    ConversionReport.model_validate(compact)

    worker_message = worker_module._bounded_result_message(
        report,
        ["result.pdf"],
        {},
        is_mcp=True,
    )
    assert worker_message["kind"] == "result"
    assert worker_message["data"]["mcp_response"]["report_truncated"] is True
    assert worker_message["report"]["details"]["transport_compaction"] == (
        worker_message["data"]["mcp_response"]
    )

    result = ToolRunResult(
        report={**report, "details": {"large": "z" * (800 * 1024)}},
        outputs=["C:/outputs/result.pdf"],
    )
    response = server_module._success_result("split", result)
    assert isinstance(response.root, types.CallToolResult)
    assert response.root.isError is False
    assert response.root.structuredContent is not None
    assert response.root.structuredContent["mcp_response"]["server_truncated"] is True
    assert response.root.structuredContent["report"]["fidelity_status"] == "known-loss"
    assert response.root.structuredContent["report"]["fidelity_coverage"] == "partial"
    assert any(
        warning["impact"] == "known-loss"
        for warning in response.root.structuredContent["report"]["fidelity_warnings"]
    )


def test_extreme_success_and_error_reports_use_schema_valid_minimal_fallbacks() -> None:
    report = ConversionReport(
        operation="split",
        status=ReportStatus.SUCCESS,
        job_id="extreme-mcp-report",
        fidelity_coverage="partial",
        fidelity_warnings=[
            FidelityWarning(
                code="decisive-known-loss",
                message="m" * (2 * 1024 * 1024),
                basis="structural",
                impact="known-loss",
                remedy="r" * (2 * 1024 * 1024),
            )
        ],
        details={"oversized": "d" * (2 * 1024 * 1024)},
    ).model_dump(mode="json")
    result = ToolRunResult(
        report=report,
        outputs=[f"C:/outputs/{index}.pdf" for index in range(12)],
        data={"oversized": "x" * (2 * 1024 * 1024)},
        containment={"oversized": "c" * (2 * 1024 * 1024)},
    )

    success = server_module._success_result("split", result)
    assert isinstance(success.root, types.CallToolResult)
    assert success.root.structuredContent is not None
    success_structured = success.root.structuredContent
    assert (
        len(
            json.dumps(
                success_structured,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        <= server_module._MAX_STRUCTURED_RESPONSE_BYTES
    )
    assert success_structured["status"] == "success"
    assert success_structured["report"]["fidelity_status"] == "known-loss"
    assert success_structured["report"]["fidelity_coverage"] == "partial"
    assert success_structured["report"]["fidelity_warnings"][0]["impact"] == ("known-loss")
    assert success_structured["mcp_response"]["output_count"] == 12
    assert success_structured["mcp_response"]["listed_output_count"] == 0
    assert success_structured["mcp_response"]["fidelity_warning_count"] == 1
    ConversionReport.model_validate(success_structured["report"])

    error = server_module._tool_error(
        "strict fidelity refused output",
        tool_name="split",
        report={**report, "status": "failed"},
    )
    assert isinstance(error.root, types.CallToolResult)
    assert error.root.structuredContent is not None
    error_structured = error.root.structuredContent
    assert (
        len(
            json.dumps(
                error_structured,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        <= server_module._MAX_STRUCTURED_RESPONSE_BYTES
    )
    assert error_structured["status"] == "error"
    assert error_structured["report"]["fidelity_status"] == "known-loss"
    assert error_structured["report"]["fidelity_coverage"] == "partial"
    assert error_structured["report"]["fidelity_warnings"][0]["impact"] == "known-loss"
    assert error_structured["mcp_response"]["fidelity_warning_count"] == 1
    ConversionReport.model_validate(error_structured["report"])


def test_final_mcp_size_recheck_uses_a_fixed_shape_for_hostile_report_values() -> None:
    hostile_report = {
        "operation": "split",
        "status": "success",
        "job_id": "hostile-report-value",
        "fidelity_status": "review-required",
        "fidelity_coverage": "partial",
        "fidelity_warnings": [
            {
                "code": "synthetic-review",
                "message": "review",
                "severity": "x" * (2 * 1024 * 1024),
                "basis": "structural",
                "impact": "review",
            }
        ],
    }

    success = server_module._success_result(
        "split",
        ToolRunResult(report=hostile_report, outputs=[]),
    )
    assert success.root.structuredContent is not None
    success_structured = success.root.structuredContent
    assert server_module._structured_size(success_structured) <= (
        server_module._MAX_STRUCTURED_RESPONSE_BYTES
    )
    assert success_structured["report"]["fidelity_status"] == "unassessed"
    ConversionReport.model_validate(success_structured["report"])

    error = server_module._tool_error(
        "synthetic failure",
        tool_name="split",
        report={**hostile_report, "status": "failed"},
    )
    assert error.root.structuredContent is not None
    error_structured = error.root.structuredContent
    assert server_module._structured_size(error_structured) <= (
        server_module._MAX_STRUCTURED_RESPONSE_BYTES
    )
    assert error_structured["report"]["fidelity_status"] == "unassessed"
    ConversionReport.model_validate(error_structured["report"])


def test_oversized_failure_report_is_compacted_before_worker_ipc(tmp_path: Path) -> None:
    warnings = [
        FidelityWarning(
            code=f"advisory-{index}",
            message="m" * 8192,
            basis="structural",
            impact="advisory",
            remedy="r" * 8192,
        )
        for index in range(300)
    ]
    warnings.append(
        FidelityWarning(
            code="decisive-known-loss",
            message="loss" * 2048,
            basis="structural",
            impact="known-loss",
            remedy="review" * 2048,
        )
    )
    report = ConversionReport(
        operation="images-to-pdf",
        status=ReportStatus.FAILED,
        job_id="oversized-worker-failure",
        fidelity_coverage="complete",
        fidelity_warnings=warnings,
    )
    sanitized = worker_module._sanitized_report(
        report,
        api_job_id="oversized-worker-failure",
        job_root=tmp_path,
        secrets=(),
    )
    assert (
        len(json.dumps(sanitized, ensure_ascii=False, separators=(",", ":")).encode())
        > worker_module._MAX_IPC_BYTES
    )

    compact, summary = worker_module._compact_mcp_report(sanitized)

    assert summary["report_truncated"] is True
    assert summary["fidelity_warning_count"] == 301
    assert compact["fidelity_status"] == "known-loss"
    assert compact["fidelity_coverage"] == "complete"
    assert any(warning["impact"] == "known-loss" for warning in compact["fidelity_warnings"])
    assert compact["details"]["transport_compaction"]["fidelity_warning_count"] == 301
    assert (
        len(json.dumps(compact, ensure_ascii=False, separators=(",", ":")).encode())
        <= worker_module._MAX_MCP_REPORT_IPC_BYTES
    )
    ConversionReport.model_validate(compact)


def test_large_http_success_report_is_compacted_before_worker_ipc() -> None:
    output_names = [f"{index:04d}-{'x' * 80}.png" for index in range(2300)]
    report = ConversionReport(
        operation="pdf-to-images",
        status=ReportStatus.SUCCESS,
        job_id="large-http-success",
        fidelity_coverage="complete",
        outputs=[
            OutputArtifact(
                path=name,
                media_type="image/png",
                size_bytes=1,
            )
            for name in output_names
        ],
        validation=ValidationResult(
            passed=True,
            checks=[
                ValidationCheck(name=f"{name}:image-decodes", passed=True)
                for name in output_names
            ],
        ),
    )
    raw_report = report.model_dump(mode="json")
    raw_message = {
        "kind": "result",
        "report": raw_report,
        "outputs": output_names,
        "data": {},
    }
    raw_size = len(json.dumps(raw_message, ensure_ascii=False, separators=(",", ":")).encode())
    assert worker_module._MAX_IPC_BYTES < raw_size <= (
        worker_module._MAX_IPC_BYTES + 32 * 1024
    )

    bounded = worker_module._bounded_result_message(
        raw_report,
        output_names,
        {},
        is_mcp=False,
    )

    assert bounded["kind"] == "result"
    assert bounded["outputs"] == output_names
    assert bounded["report"]["details"]["transport_compaction"]["report_truncated"] is True
    assert "mcp_response" not in bounded["report"]["details"]
    assert (
        len(json.dumps(bounded, ensure_ascii=False, separators=(",", ":")).encode())
        <= worker_module._MAX_IPC_BYTES
    )
    ConversionReport.model_validate(bounded["report"])


def test_unrepresentable_http_success_metadata_becomes_bounded_failure() -> None:
    output_names = [f"{index:04d}-{'x' * 220}.png" for index in range(5000)]
    report = ConversionReport(
        operation="pdf-to-images",
        status=ReportStatus.SUCCESS,
        job_id="too-large-http-success",
        fidelity_coverage="complete",
    )

    bounded = worker_module._bounded_result_message(
        report.model_dump(mode="json"),
        output_names,
        {},
        is_mcp=False,
    )

    assert bounded["kind"] == "failure"
    assert bounded["http_status"] == 422
    assert "bounded IPC limit" in bounded["error"]
    assert bounded["report"]["status"] == "failed"
    assert (
        len(json.dumps(bounded, ensure_ascii=False, separators=(",", ":")).encode())
        <= worker_module._MAX_IPC_BYTES
    )
    ConversionReport.model_validate(bounded["report"])


def test_mcp_report_preserves_typed_output_path_but_redacts_content_values(
    tmp_path: Path,
) -> None:
    password = "TOKEN-123"
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
            FidelityWarning(
                code="secret-fidelity",
                message=f"before-{password}-after",
                remedy=f"replace-{password}-then-" + "x" * 5000,
            )
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
    remedy = payload["fidelity_warnings"][0]["remedy"]
    assert remedy.startswith("replace-<redacted>-then-")
    assert len(remedy) == 4096
    assert payload["errors"] == [redacted]
    assert payload["validation"]["checks"][0]["detail"] == redacted
    assert payload["details"]["metadata"]["title"] == redacted
