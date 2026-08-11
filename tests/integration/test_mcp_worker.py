"""Direct MCP-mode worker integration coverage."""

from __future__ import annotations

import json
import threading
from pathlib import Path

from localdocforge.api.worker import WorkerJobStatus, WorkerProcess, WorkerRequest
from localdocforge.config.settings import Settings
from localdocforge.jobs.workspace import JobWorkspace
from localdocforge.mcp.tools import validate_tool_arguments
from localdocforge.mcp.transport import _private_protocol_stdout


def test_mcp_worker_runs_pipeline_to_explicit_destination(
    fixtures_dir: Path,
    tmp_path: Path,
) -> None:
    settings = Settings(jobs_root=tmp_path / "jobs")
    output = tmp_path / "merged.pdf"
    arguments = validate_tool_arguments(
        "merge",
        {
            "inputs": [
                str((fixtures_dir / "simple-3page.pdf").resolve()),
                str((fixtures_dir / "second-2page.pdf").resolve()),
            ],
            "output": str(output.resolve()),
        },
    )
    workspace = JobWorkspace("mcp-worker-test", root=settings.jobs_root)
    for name in ("in", "out", "work"):
        workspace.subdir(name)
    request = WorkerRequest(
        job_id=workspace.job_id,
        operation="merge",
        job_root=str(workspace.path),
        input_names=(),
        params={},
        settings_json=settings.model_dump_json(),
        mcp_arguments_json=json.dumps(arguments.model_dump(mode="json")),
    )

    outcomes = []

    def run_worker() -> None:
        outcomes.append(WorkerProcess(request).run(threading.Event()))

    with _private_protocol_stdout():
        thread = threading.Thread(target=run_worker, daemon=False)
        thread.start()
        thread.join(timeout=30)
    assert not thread.is_alive()
    outcome = outcomes[0]
    try:
        assert outcome.status is WorkerJobStatus.SUCCESS, outcome
        assert output.is_file()
        assert outcome.report is not None
        assert [artifact.path for artifact in outcome.report.outputs] == [output.resolve()]
    finally:
        workspace.cleanup()


def test_mcp_worker_default_collision_is_actionable_and_redacted(
    fixtures_dir: Path,
    tmp_path: Path,
) -> None:
    settings = Settings(jobs_root=tmp_path / "jobs")
    output = tmp_path / "already-exists.pdf"
    original = b"existing destination must not be replaced"
    output.write_bytes(original)
    password = "mcp-collision-password-DO-NOT-LEAK"
    arguments = validate_tool_arguments(
        "merge",
        {
            "inputs": [
                str((fixtures_dir / "simple-3page.pdf").resolve()),
                str((fixtures_dir / "second-2page.pdf").resolve()),
            ],
            "output": str(output.resolve()),
            "password": password,
        },
    )
    workspace = JobWorkspace("mcp-worker-collision-test", root=settings.jobs_root)
    for name in ("in", "out", "work"):
        workspace.subdir(name)
    request = WorkerRequest(
        job_id=workspace.job_id,
        operation="merge",
        job_root=str(workspace.path),
        input_names=(),
        params={"password": password},
        settings_json=settings.model_dump_json(),
        mcp_arguments_json=json.dumps(arguments.model_dump(mode="json")),
    )

    outcomes = []

    def run_worker() -> None:
        outcomes.append(WorkerProcess(request).run(threading.Event()))

    with _private_protocol_stdout():
        thread = threading.Thread(target=run_worker, daemon=False)
        thread.start()
        thread.join(timeout=30)
    assert not thread.is_alive()
    outcome = outcomes[0]
    try:
        assert outcome.status is WorkerJobStatus.FAILED
        assert outcome.error is not None
        assert outcome.error.startswith("Output already exists:")
        assert str(output.resolve()) in outcome.error
        public_payload = json.dumps(
            {
                "error": outcome.error,
                "report": (
                    outcome.report.model_dump(mode="json")
                    if outcome.report is not None
                    else None
                ),
            },
            ensure_ascii=False,
        )
        assert password not in public_payload
        assert str(workspace.path.resolve()) not in public_payload
        assert output.read_bytes() == original
    finally:
        workspace.cleanup()
