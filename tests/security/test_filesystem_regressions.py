"""Regression tests for independently audited filesystem and process defects.

Every path and process is synthetic and local to pytest's temporary directory.
"""

from __future__ import annotations

import hashlib
import io
import os
import shutil
import sys
from pathlib import Path

import pytest

import localdocforge.config.settings as settings_module
import localdocforge.jobs.workspace as workspace_module
import localdocforge.security.subproc as subproc_module
from localdocforge.config.settings import Settings
from localdocforge.domain.pages import PageRange
from localdocforge.jobs.workspace import (
    CollisionPolicy,
    JobWorkspace,
    OutputCollisionError,
    atomic_publish,
    contained_output_path,
)
from localdocforge.operations.organize import OrganizeOptions, extract_pages, merge_pdfs, split_pdf
from localdocforge.pipelines.runner import PipelineError
from localdocforge.security.filenames import sanitize_filename
from localdocforge.security.paths import PathSecurityError
from localdocforge.security.subproc import ToolError, find_executable, minimal_env, run_tool


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _settings(root: Path, **overrides) -> Settings:
    values = {
        "strict_offline": True,
        "jobs_root": root / "jobs",
        "allowed_output_roots": [root],
    }
    values.update(overrides)
    return Settings(**values)


def test_current_directory_executable_is_not_discovered(tmp_path: Path, monkeypatch) -> None:
    planted = tmp_path / ("qpdf.exe" if os.name == "nt" else "qpdf")
    planted.write_bytes(b"synthetic marker: never execute")
    if os.name != "nt":
        planted.chmod(0o700)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", "")

    assert find_executable("qpdf") is None


def test_child_path_override_and_relative_cwd_are_refused(monkeypatch) -> None:
    for extra in ({"PATH": "."}, {"Path": "."}):
        with pytest.raises(ToolError, match="PATH overrides"):
            minimal_env(extra)
    with pytest.raises(ToolError, match="not allowed"):
        minimal_env({"PYTHONPATH": "."})
    with pytest.raises(ToolError, match="non-local"):
        minimal_env({"TEMP": "relative"})

    monkeypatch.setattr(subproc_module, "find_executable", lambda _tool: sys.executable)
    with pytest.raises(ToolError, match="must be absolute"):
        run_tool("qpdf", ["-c", "pass"], cwd=Path("relative"))


def test_subprocess_output_is_bounded_while_pipe_is_drained(monkeypatch) -> None:
    monkeypatch.setattr(subproc_module, "find_executable", lambda _tool: sys.executable)
    result = run_tool(
        "qpdf",
        ["-c", "import sys; sys.stdout.buffer.write(b'x' * 200000)"],
        max_output_bytes=257,
    )

    assert result.returncode == 0
    assert result.output == "x" * 257


def test_base_exception_triggers_process_tree_cleanup(monkeypatch) -> None:
    class InterruptingProcess:
        stdout = io.BytesIO(b"")
        pid = 424242
        returncode = None

        def wait(self, timeout=None):
            raise KeyboardInterrupt

    process = InterruptingProcess()
    killed: list[object] = []
    monkeypatch.setattr(subproc_module, "find_executable", lambda _tool: sys.executable)
    monkeypatch.setattr(subproc_module.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(subproc_module, "kill_process_tree", killed.append)

    with pytest.raises(KeyboardInterrupt):
        run_tool("qpdf", ["--version"])
    assert killed == [process]


def test_atomic_fail_policy_does_not_clobber_racing_creator(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "source.pdf"
    destination = tmp_path / "result.pdf"
    source.write_bytes(b"validated output")
    real_link = workspace_module.os.link

    def create_competitor_then_link(staging, final):
        Path(final).write_bytes(b"racing creator")
        return real_link(staging, final)

    monkeypatch.setattr(workspace_module.os, "link", create_competitor_then_link)
    with pytest.raises(OutputCollisionError):
        atomic_publish(source, destination, collision=CollisionPolicy.FAIL)

    assert destination.read_bytes() == b"racing creator"
    assert source.read_bytes() == b"validated output"


def test_multi_output_fail_preflight_leaves_no_partial_publication(
    fixtures_dir: Path, tmp_path: Path
) -> None:
    blocker = tmp_path / "simple-3page-page-002.pdf"
    blocker.write_bytes(b"pre-existing")

    with pytest.raises(PipelineError) as excinfo:
        split_pdf(
            fixtures_dir / "simple-3page.pdf",
            tmp_path,
            options=OrganizeOptions(settings=_settings(tmp_path)),
        )

    assert isinstance(excinfo.value.__cause__, OutputCollisionError)
    assert not (tmp_path / "simple-3page-page-001.pdf").exists()
    assert blocker.read_bytes() == b"pre-existing"
    assert not (tmp_path / "simple-3page-page-003.pdf").exists()


@pytest.mark.parametrize("use_hardlink", [False, True])
def test_output_alias_never_modifies_source(
    fixtures_dir: Path, tmp_path: Path, use_hardlink: bool
) -> None:
    source = tmp_path / "source.pdf"
    shutil.copy2(fixtures_dir / "simple-3page.pdf", source)
    destination = source
    if use_hardlink:
        destination = tmp_path / "alias.pdf"
        try:
            os.link(source, destination)
        except OSError as exc:
            pytest.skip(f"hard links unavailable: {exc}")
    before = _sha256(source)

    with pytest.raises(PipelineError, match="aliases an input"):
        extract_pages(
            source,
            destination,
            PageRange(spec="1"),
            options=OrganizeOptions(
                collision=CollisionPolicy.OVERWRITE,
                settings=_settings(tmp_path),
            ),
        )

    assert _sha256(source) == before


@pytest.mark.parametrize("job_id", ["../escape", "..\\escape", "é", "", "a/b"])
def test_workspace_rejects_untrusted_job_ids(tmp_path: Path, job_id: str) -> None:
    with pytest.raises(ValueError, match="job_id"):
        JobWorkspace(job_id, root=tmp_path)


def test_workspace_id_is_exclusive_and_temp_suffix_is_constrained(tmp_path: Path) -> None:
    workspace = JobWorkspace("fixed-id", root=tmp_path)
    try:
        with pytest.raises(FileExistsError):
            JobWorkspace("fixed-id", root=tmp_path)
        with pytest.raises(ValueError, match="suffix"):
            workspace.temp_file("../escape")
    finally:
        workspace.cleanup()


def test_empty_allowed_output_roots_means_deny_all(tmp_path: Path) -> None:
    with pytest.raises(PathSecurityError, match="outside every"):
        contained_output_path(tmp_path / "result.pdf", [])


def test_strict_offline_rejects_unc_configuration_and_inputs_before_io(
    fixtures_dir: Path, tmp_path: Path
) -> None:
    unc_root = Path(r"\\synthetic.invalid\share\jobs")
    with pytest.raises(ValueError, match="UNC|network-drive"):
        Settings(strict_offline=True, jobs_root=unc_root)

    settings = _settings(tmp_path)
    with pytest.raises(PipelineError, match="network filesystem inputs"):
        merge_pdfs(
            [Path(r"\\synthetic.invalid\share\input.pdf"), fixtures_dir / "simple-3page.pdf"],
            tmp_path / "never.pdf",
            options=OrganizeOptions(settings=settings),
        )
    assert not (tmp_path / "never.pdf").exists()


def test_strict_offline_rejects_remote_effective_default_temp(monkeypatch) -> None:
    monkeypatch.setattr(
        settings_module,
        "default_jobs_root",
        lambda: Path(r"\\synthetic.invalid\share\default-jobs"),
    )
    with pytest.raises(ValueError, match="UNC|network-drive"):
        Settings(strict_offline=True)


def test_strict_offline_rejects_unc_output_without_touching_it(
    fixtures_dir: Path, tmp_path: Path
) -> None:
    with pytest.raises(PipelineError, match="network filesystem outputs"):
        merge_pdfs(
            [fixtures_dir / "simple-3page.pdf", fixtures_dir / "second-2page.pdf"],
            Path(r"\\synthetic.invalid\share\output.pdf"),
            options=OrganizeOptions(settings=_settings(tmp_path)),
        )


def test_long_astral_and_bidi_filename_is_bounded_and_keeps_extension() -> None:
    result = sanitize_filename("\u202e" + "😀" * 200 + ".pdf")

    assert result.endswith(".pdf")
    assert len(result) <= 150
    assert len(result.encode("utf-8")) <= 180
    assert "\u202e" not in result


def test_incomplete_workspace_cleanup_is_reported(
    fixtures_dir: Path, tmp_path: Path, monkeypatch
) -> None:
    real_cleanup = JobWorkspace.cleanup

    def remove_but_report_failure(workspace: JobWorkspace) -> bool:
        real_cleanup(workspace)
        return False

    monkeypatch.setattr(JobWorkspace, "cleanup", remove_but_report_failure)
    report = extract_pages(
        fixtures_dir / "simple-3page.pdf",
        tmp_path / "output.pdf",
        PageRange(spec="1"),
        options=OrganizeOptions(settings=_settings(tmp_path)),
    )

    warnings = {warning.code: warning for warning in report.security_warnings}
    assert warnings["workspace-cleanup-incomplete"].severity.value == "critical"
