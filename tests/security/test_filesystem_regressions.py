"""Regression tests for independently audited filesystem and process defects.

Every path and process is synthetic and local to pytest's temporary directory.
"""

from __future__ import annotations

import ctypes
import hashlib
import io
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import localdocforge.config.settings as settings_module
import localdocforge.jobs.workspace as workspace_module
import localdocforge.security.paths as paths_module
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
from localdocforge.security.paths import PathSecurityError, is_remote_path
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


def _extended_windows_path(path: Path) -> Path:
    return Path(f"\\\\?\\{path.resolve()}")


def _short_windows_path(path: Path) -> Path | None:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetShortPathNameW.argtypes = (
        ctypes.c_wchar_p,
        ctypes.c_wchar_p,
        ctypes.c_uint,
    )
    kernel32.GetShortPathNameW.restype = ctypes.c_uint
    buffer = ctypes.create_unicode_buffer(32768)
    length = kernel32.GetShortPathNameW(str(path), buffer, len(buffer))
    if not length or length >= len(buffer):
        return None
    return Path(buffer.value)


def _make_windows_junction(link: Path, target: Path) -> None:
    command = os.environ.get("COMSPEC", r"C:\Windows\System32\cmd.exe")
    result = subprocess.run(  # noqa: S603 - fixed Windows shell and mklink builtin
        [command, "/d", "/c", "mklink", "/J", str(link), str(target)],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.skip(f"directory junction creation unavailable: {result.stderr.strip()}")


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


@pytest.mark.skipif(os.name != "nt", reason="Windows path-form regression")
def test_windows_extended_drive_is_local_and_extended_unc_or_device_is_rejected(
    tmp_path: Path,
) -> None:
    extended_jobs = _extended_windows_path(tmp_path / "extended-jobs")
    assert not is_remote_path(extended_jobs)
    Settings(strict_offline=True, jobs_root=extended_jobs)

    with pytest.raises(ValueError, match="UNC|network-drive"):
        Settings(
            strict_offline=True,
            jobs_root=Path(r"\\?\UNC\synthetic.invalid\share\jobs"),
        )
    with pytest.raises(ValueError, match="device path"):
        Settings(strict_offline=False, jobs_root=Path(r"\\.\NUL"))


@pytest.mark.skipif(os.name != "nt", reason="Windows mapped-drive API regression")
def test_windows_mapped_drive_detection_uses_mocked_drive_type(monkeypatch) -> None:
    queried: list[str] = []

    def mapped(root: str) -> int:
        queried.append(root)
        return 4  # DRIVE_REMOTE; mocked because this host has no mapped drive.

    monkeypatch.setattr(paths_module, "_windows_drive_type", mapped)

    assert is_remote_path(Path(r"Z:\synthetic\input.pdf"))
    with pytest.raises(ValueError, match="UNC|network-drive"):
        Settings(strict_offline=True, jobs_root=Path(r"Z:\synthetic\jobs"))
    assert queried and set(queried) == {"Z:\\"}


@pytest.mark.skipif(os.name != "nt", reason="Windows filename-alias regression")
@pytest.mark.parametrize(
    ("name", "message"),
    [
        ("file.pdf:stream", "alternate-data-stream"),
        ("CON.pdf", "reserved Windows device"),
        ("NUL .txt", "reserved Windows device"),
        ("normal.pdf.", "trailing-dot"),
        ("normal.pdf ", "trailing-dot|trailing-space"),
    ],
)
def test_windows_unsafe_output_forms_are_rejected_before_publication(
    tmp_path: Path,
    name: str,
    message: str,
) -> None:
    source = tmp_path / "validated-output.bin"
    source.write_bytes(b"validated output")
    destination = tmp_path / name

    with pytest.raises(PathSecurityError, match=message):
        contained_output_path(destination, [tmp_path])
    with pytest.raises(PathSecurityError, match=message):
        atomic_publish(source, destination, collision=CollisionPolicy.OVERWRITE)

    assert source.read_bytes() == b"validated output"


@pytest.mark.skipif(os.name != "nt", reason="Windows input path-form regression")
@pytest.mark.parametrize(
    "hostile",
    [
        lambda root: root / "source.pdf:stream",
        lambda _root: Path(r"\\.\NUL"),
    ],
)
def test_windows_unsafe_input_form_is_rejected_before_parser(
    fixtures_dir: Path,
    tmp_path: Path,
    hostile,
) -> None:
    with pytest.raises(PipelineError, match="alternate-data-stream|device path"):
        merge_pdfs(
            [hostile(tmp_path), fixtures_dir / "simple-3page.pdf"],
            tmp_path / "never.pdf",
            options=OrganizeOptions(settings=_settings(tmp_path)),
        )
    assert not (tmp_path / "never.pdf").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows junction/reparse regression")
def test_windows_junction_is_rejected_at_containment_and_publication(
    tmp_path: Path,
) -> None:
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    junction = allowed / "redirect"
    _make_windows_junction(junction, outside)
    source = tmp_path / "validated-output.bin"
    source.write_bytes(b"validated output")
    try:
        with pytest.raises(ValueError, match="reparse point"):
            Settings(strict_offline=True, jobs_root=junction)
        with pytest.raises(PathSecurityError, match="reparse point"):
            JobWorkspace("junction-root", root=junction)
        with pytest.raises(PathSecurityError, match="reparse point"):
            contained_output_path(junction / "escaped.bin", [allowed])
        with pytest.raises(PathSecurityError, match="reparse point"):
            atomic_publish(
                source,
                junction / "escaped.bin",
                collision=CollisionPolicy.OVERWRITE,
            )
        assert not (outside / "escaped.bin").exists()
    finally:
        if junction.exists():
            junction.rmdir()


@pytest.mark.skipif(os.name != "nt", reason="Windows case-insensitive alias regression")
def test_windows_case_alias_never_modifies_source(fixtures_dir: Path, tmp_path: Path) -> None:
    source = tmp_path / "Case-Alias.pdf"
    shutil.copy2(fixtures_dir / "simple-3page.pdf", source)
    destination = tmp_path / "case-alias.PDF"
    if not destination.exists():
        pytest.skip("temporary filesystem is case-sensitive")
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


@pytest.mark.skipif(os.name != "nt", reason="Windows 8.3 alias regression")
def test_windows_short_path_alias_never_modifies_source(
    fixtures_dir: Path,
    tmp_path: Path,
) -> None:
    source = fixtures_dir / "simple-3page.pdf"
    short_source = _short_windows_path(source)
    if short_source is None or os.path.normcase(str(short_source)) == os.path.normcase(str(source)):
        pytest.skip("8.3 alias unavailable for the synthetic fixture path")
    before = _sha256(source)
    settings = Settings(
        strict_offline=True,
        jobs_root=tmp_path / "jobs",
        allowed_output_roots=[fixtures_dir],
    )

    with pytest.raises(PipelineError, match="aliases an input"):
        extract_pages(
            source,
            short_source,
            PageRange(spec="1"),
            options=OrganizeOptions(
                collision=CollisionPolicy.OVERWRITE,
                settings=settings,
            ),
        )

    assert _sha256(source) == before


@pytest.mark.skipif(os.name != "nt", reason="Windows long-path regression")
def test_windows_extended_and_long_publication_paths(tmp_path: Path) -> None:
    source = tmp_path / "validated-output.bin"
    source.write_bytes(b"validated output")
    extended_normal = tmp_path / "extended-result.bin"
    extended_result = atomic_publish(
        source,
        _extended_windows_path(extended_normal),
        collision=CollisionPolicy.FAIL,
    )
    assert extended_result.read_bytes() == b"validated output"
    assert extended_normal.read_bytes() == b"validated output"

    second_source = tmp_path / "second-validated-output.bin"
    second_source.write_bytes(b"second validated output")
    deep = tmp_path
    while len(str(deep)) <= 280:
        deep /= "long-path-segment-0123456789abcdef"
    try:
        deep.mkdir(parents=True)
    except OSError as exc:
        pytest.skip(f"Windows long paths unavailable: {exc}")
    long_result = atomic_publish(second_source, deep / "result.bin")

    assert len(str(long_result)) > 260
    assert long_result.read_bytes() == b"second validated output"


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
