"""Async MCP supervision around one fresh isolated worker per tool call."""

from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import anyio

from localdocforge.api.operations import OperationParameters
from localdocforge.api.worker import (
    WorkerJobStatus,
    WorkerOutcome,
    WorkerProcess,
    WorkerRequest,
)
from localdocforge.config.settings import Settings
from localdocforge.jobs.workspace import JobWorkspace


class ToolRunError(RuntimeError):
    """Expected validation, policy, engine, timeout, or resource failure."""


class ToolInternalError(RuntimeError):
    """Unexpected worker/supervisor failure; never exposes its original text."""


@dataclass(frozen=True)
class ToolRunResult:
    report: dict[str, Any]
    outputs: list[str]
    data: dict[str, Any] = field(default_factory=dict)
    containment: dict[str, str | int | float | bool | None] = field(default_factory=dict)


def _worker_thread(
    controller: WorkerProcess,
    cancel_requested: threading.Event,
    outcomes: list[WorkerOutcome],
    failed: threading.Event,
    done: threading.Event,
) -> None:
    try:
        outcomes.append(controller.run(cancel_requested))
    except BaseException:
        # The request handler returns only a generic internal error. The child
        # and worker controller intentionally have no document-content logger.
        failed.set()
    finally:
        done.set()


async def _terminate_and_join_after_cancellation(
    controller: WorkerProcess,
    thread: threading.Thread,
) -> bool:
    """Retry tree termination while the shielded supervisor finalizes."""
    deadline = time.monotonic() + 20.0
    with anyio.CancelScope(shield=True):
        while thread.is_alive():
            controller.terminate()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            await anyio.to_thread.run_sync(thread.join, min(0.25, remaining))
    return not thread.is_alive()


async def run_tool_in_worker(
    tool_name: str,
    arguments: OperationParameters,
    settings: Settings,
) -> ToolRunResult:
    """Run one validated call and guarantee disconnect cancellation reaches its tree."""
    workspace = JobWorkspace(f"mcp-{uuid.uuid4().hex}", root=settings.jobs_root)
    workspace.subdir("in")
    workspace.subdir("out")
    workspace.subdir("work")

    arguments_payload = arguments.model_dump(mode="json")
    password = getattr(arguments, "password", None)
    request = WorkerRequest(
        job_id=workspace.job_id,
        operation=tool_name,
        job_root=str(workspace.path),
        input_names=(),
        params={"password": password} if isinstance(password, str) and password else {},
        settings_json=settings.model_dump_json(),
        mcp_arguments_json=json.dumps(
            arguments_payload,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
        ),
    )
    cancel_requested = threading.Event()
    outcomes: list[WorkerOutcome] = []
    failed = threading.Event()
    done = threading.Event()
    controller = WorkerProcess(request)
    thread = threading.Thread(
        target=_worker_thread,
        args=(controller, cancel_requested, outcomes, failed, done),
        name=f"ldf-mcp-supervisor-{workspace.job_id[-12:]}",
        daemon=False,
    )

    cancelled = False
    try:
        thread.start()
        try:
            while not done.is_set():
                await anyio.sleep(0.025)
        except anyio.get_cancelled_exc_class():
            cancelled = True
            cancel_requested.set()
            await _terminate_and_join_after_cancellation(controller, thread)
            raise
    finally:
        request.params.clear()
        request.mcp_arguments_json = None
        if not thread.is_alive():
            cleaned = workspace.cleanup()
            if not cleaned and not cancelled:
                raise ToolInternalError("Private MCP workspace cleanup failed") from None

    if thread.is_alive() or failed.is_set() or len(outcomes) != 1:
        raise ToolInternalError("MCP worker supervisor failed") from None
    outcome = outcomes[0]
    if outcome.status is WorkerJobStatus.CRASHED:
        raise ToolInternalError(outcome.error or "MCP worker failed internally") from None
    if outcome.status is not WorkerJobStatus.SUCCESS or outcome.report is None:
        message = outcome.error or "Document processing failed"
        raise ToolRunError(message) from None

    report = outcome.report.model_dump(mode="json")
    if tool_name == "inspect":
        # The pipeline's JSON artifact is private transport state consumed
        # before workspace cleanup, not a caller-visible output.
        report["outputs"] = []
        report["output_bytes"] = 0
        outputs: list[str] = []
    else:
        outputs = [str(artifact.path) for artifact in outcome.report.outputs]
    return ToolRunResult(
        report=report,
        outputs=outputs,
        data=outcome.result_data,
        containment=outcome.containment,
    )


__all__ = [
    "ToolInternalError",
    "ToolRunError",
    "ToolRunResult",
    "run_tool_in_worker",
]
