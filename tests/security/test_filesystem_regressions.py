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


@pytest.mark.parametrize(
    ("tool", "binary"),
    [
        ("ocrmypdf", "ocrmypdf.exe" if os.name == "nt" else "ocrmypdf"),
        ("ghostscript", "gswin64c.exe" if os.name == "nt" else "gs"),
    ],
)
def test_ocr_executables_in_document_cwd_are_not_discovered(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tool: str,
    binary: str,
) -> None:
    planted = tmp_path / binary
    planted.write_bytes(b"synthetic marker: never execute")
    if os.name != "nt":
        planted.chmod(0o700)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", "")
    monkeypatch.setattr(subproc_module, "_trusted_search_directories", lambda _tool: [])

    assert find_executable(tool) is None


def test_ocrmypdf_is_discovered_beside_active_venv_python_without_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scripts = tmp_path / ("Scripts" if os.name == "nt" else "bin")
    scripts.mkdir()
    python = scripts / ("python.exe" if os.name == "nt" else "python")
    python.write_bytes(b"synthetic python marker")
    executable = scripts / ("ocrmypdf.exe" if os.name == "nt" else "ocrmypdf")
    executable.write_bytes(b"synthetic OCRmyPDF marker")
    if os.name != "nt":
        python.chmod(0o700)
        executable.chmod(0o700)
    monkeypatch.setattr(subproc_module.sys, "executable", str(python))
    monkeypatch.setattr(subproc_module.sys, "prefix", str(tmp_path))
    monkeypatch.setenv("PATH", "")
    if os.name == "nt":
        monkeypatch.setenv("PATHEXT", ".EXE;.BAT;.CMD")

    assert find_executable("ocrmypdf") == str(executable.resolve())


def test_ghostscript_is_discoverable_but_cannot_be_launched_directly() -> None:
    with pytest.raises(ToolError, match="discovery-only"):
        run_tool("ghostscript", ["--version"])


def test_popen_oserror_becomes_safe_tool_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_error = "PRIVATE-PROCESS-START-PATH-C:/Users/name/document.pdf"
    monkeypatch.setattr(subproc_module, "find_executable", lambda _tool: sys.executable)

    def fail_start(*_args, **_kwargs):
        raise OSError(private_error)

    monkeypatch.setattr(subproc_module.subprocess, "Popen", fail_start)
    with pytest.raises(ToolError) as failure:
        run_tool("qpdf", ["--version"])

    assert "qpdf" in str(failure.value)
    assert private_error not in str(failure.value)
    assert isinstance(failure.value.__cause__, OSError)


def test_ocrmypdf_child_path_uses_only_resolved_tesseract_and_ghostscript_dirs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable_names = {
        "ocrmypdf": "ocrmypdf.exe" if os.name == "nt" else "ocrmypdf",
        "tesseract": "tesseract.exe" if os.name == "nt" else "tesseract",
        "ghostscript": "gswin64c.exe" if os.name == "nt" else "gs",
    }
    tool_paths: dict[str, Path] = {}
    for tool, name in executable_names.items():
        directory = tmp_path / tool
        directory.mkdir()
        executable = directory / name
        executable.write_bytes(b"synthetic executable marker")
        if os.name != "nt":
            executable.chmod(0o700)
        tool_paths[tool] = executable.resolve()
    ordinary_path = tmp_path / "ordinary-path"
    ordinary_path.mkdir()
    if os.name == "nt":
        (tool_paths["ghostscript"].parent / "gswin64c.com").write_bytes(
            b"same-directory wrapper must remain unreachable"
        )
    resolved_tools: list[str] = []

    def resolve(tool: str) -> str | None:
        resolved_tools.append(tool)
        path = tool_paths.get(tool)
        return str(path) if path is not None else None

    captured: dict[str, object] = {}

    class CompletedProcess:
        stdout = io.BytesIO(b"")
        pid = 424243
        returncode = 0

        @staticmethod
        def wait(timeout=None):
            return 0

        @staticmethod
        def poll():
            return 0

    def capture_popen(*args, **kwargs):
        captured.update(args=args, kwargs=kwargs)
        return CompletedProcess()

    monkeypatch.setattr(subproc_module, "find_executable", resolve)
    monkeypatch.setattr(subproc_module, "_safe_search_directories", lambda: [ordinary_path])
    monkeypatch.setattr(subproc_module.subprocess, "Popen", capture_popen)

    result = run_tool(
        "ocrmypdf",
        ["--version"],
        child_path_tools=("tesseract", "ghostscript"),
        expected_executable=str(tool_paths["ocrmypdf"]),
        expected_child_executables={
            "tesseract": str(tool_paths["tesseract"]),
            "ghostscript": str(tool_paths["ghostscript"]),
        },
    )

    assert result.returncode == 0
    assert resolved_tools == ["ocrmypdf", "tesseract", "ghostscript"]
    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    child_path = kwargs["env"]["PATH"].split(os.pathsep)
    assert child_path == [
        str(tool_paths["tesseract"].parent),
        str(tool_paths["ghostscript"].parent),
    ]
    if os.name == "nt":
        assert kwargs["env"]["NoDefaultCurrentDirectoryInExePath"] == "1"
        assert kwargs["env"]["PATHEXT"] == ".EXE"


@pytest.mark.skipif(os.name != "nt", reason="Windows executable-search semantics")
def test_ocrmypdf_child_env_disables_windows_cwd_executable_search(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    names = {
        "ocrmypdf": "ocrmypdf.exe",
        "tesseract": "tesseract.exe",
        "ghostscript": "gswin64c.exe",
    }
    paths: dict[str, Path] = {}
    for tool, name in names.items():
        directory = tmp_path / tool
        directory.mkdir()
        paths[tool] = directory / name
        paths[tool].write_bytes(b"trusted synthetic executable")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "tesseract.exe").write_bytes(b"cwd shadow")
    (workspace / "gswin64c.exe").write_bytes(b"cwd shadow")
    captured: dict[str, object] = {}

    class CompletedProcess:
        stdout = io.BytesIO(b"")
        pid = 424244
        returncode = 0

        @staticmethod
        def wait(timeout=None):
            return 0

        @staticmethod
        def poll():
            return 0

    def capture_popen(*args, **kwargs):
        captured.update(args=args, kwargs=kwargs)
        return CompletedProcess()

    monkeypatch.setattr(subproc_module, "find_executable", lambda tool: str(paths[tool]))
    monkeypatch.setattr(subproc_module.subprocess, "Popen", capture_popen)

    run_tool(
        "ocrmypdf",
        ["--version"],
        cwd=workspace,
        child_path_tools=("tesseract", "ghostscript"),
        expected_executable=str(paths["ocrmypdf"]),
        expected_child_executables={
            "tesseract": str(paths["tesseract"]),
            "ghostscript": str(paths["ghostscript"]),
        },
    )

    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["cwd"] == str(workspace.resolve())
    assert kwargs["env"]["NoDefaultCurrentDirectoryInExePath"] == "1"
    assert kwargs["env"]["PATHEXT"] == ".EXE"
    assert kwargs["env"]["PATH"].split(os.pathsep) == [
        str(paths["tesseract"].parent),
        str(paths["ghostscript"].parent),
    ]


@pytest.mark.skipif(os.name != "nt", reason="Windows Ghostscript executable spelling")
def test_windows_ghostscript_discovery_rejects_wrappers_and_unsupported_names(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "gswin64c.com",
        "gswin64c.bat",
        "gswin64c.cmd",
        "gswin32c.exe",
        "gs.exe",
    ):
        (tmp_path / name).write_bytes(b"unsupported Ghostscript launcher")
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("PATHEXT", ".COM;.EXE;.BAT;.CMD")
    monkeypatch.setattr(subproc_module, "_trusted_search_directories", lambda _tool: [])

    assert find_executable("ghostscript") is None


@pytest.mark.skipif(os.name != "nt", reason="Windows Tesseract executable spelling")
def test_windows_tesseract_discovery_rejects_wrappers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("tesseract.com", "tesseract.bat", "tesseract.cmd"):
        (tmp_path / name).write_bytes(b"unsupported Tesseract launcher")
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("PATHEXT", ".COM;.EXE;.BAT;.CMD")
    monkeypatch.setattr(subproc_module, "_trusted_search_directories", lambda _tool: [])

    assert find_executable("tesseract") is None


def test_ocr_child_path_rejects_cross_shadowing_between_engine_directories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    names = {
        "ocrmypdf": "ocrmypdf.exe" if os.name == "nt" else "ocrmypdf",
        "tesseract": "tesseract.exe" if os.name == "nt" else "tesseract",
        "ghostscript": "gswin64c.exe" if os.name == "nt" else "gs",
    }
    paths: dict[str, Path] = {}
    for tool, name in names.items():
        directory = tmp_path / tool
        directory.mkdir()
        paths[tool] = directory / name
        paths[tool].write_bytes(b"synthetic executable marker")
        if os.name != "nt":
            paths[tool].chmod(0o700)
    planted = paths["tesseract"].parent / names["ghostscript"]
    planted.write_bytes(b"cross-shadow marker")
    if os.name != "nt":
        planted.chmod(0o700)
    monkeypatch.setattr(
        subproc_module,
        "find_executable",
        lambda tool: str(paths[tool]),
    )
    monkeypatch.setattr(
        subproc_module.subprocess,
        "Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("ambiguous child PATH reached Popen")
        ),
    )

    with pytest.raises(ToolError, match="does not uniquely select"):
        run_tool(
            "ocrmypdf",
            ["--version"],
            child_path_tools=("tesseract", "ghostscript"),
            expected_executable=str(paths["ocrmypdf"]),
            expected_child_executables={
                "tesseract": str(paths["tesseract"]),
                "ghostscript": str(paths["ghostscript"]),
            },
        )


def test_ocr_child_path_requires_complete_expected_path_map(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(subproc_module, "find_executable", lambda _tool: sys.executable)

    with pytest.raises(ToolError, match="Expected child executable paths"):
        run_tool(
            "ocrmypdf",
            ["--version"],
            child_path_tools=("tesseract", "ghostscript"),
        )


def test_changed_primary_executable_path_is_rejected_before_popen(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = tmp_path / ("expected-qpdf.exe" if os.name == "nt" else "expected-qpdf")
    discovered = tmp_path / ("discovered-qpdf.exe" if os.name == "nt" else "discovered-qpdf")
    for executable in (expected, discovered):
        executable.write_bytes(b"synthetic executable marker")
        if os.name != "nt":
            executable.chmod(0o700)
    monkeypatch.setattr(subproc_module, "find_executable", lambda _tool: str(discovered))
    monkeypatch.setattr(
        subproc_module.subprocess,
        "Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("changed executable path reached Popen")
        ),
    )

    with pytest.raises(ToolError, match="path changed after its runtime probe"):
        run_tool("qpdf", ["--version"], expected_executable=str(expected))


def test_changed_ocr_child_path_is_rejected_before_popen(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    names = {
        "ocrmypdf": "ocrmypdf.exe" if os.name == "nt" else "ocrmypdf",
        "tesseract": "tesseract.exe" if os.name == "nt" else "tesseract",
        "ghostscript": "gswin64c.exe" if os.name == "nt" else "gs",
    }
    discovered: dict[str, Path] = {}
    expected: dict[str, Path] = {}
    for tool, name in names.items():
        tool_dir = tmp_path / tool
        expected_dir = tmp_path / f"expected-{tool}"
        tool_dir.mkdir()
        expected_dir.mkdir()
        discovered[tool] = tool_dir / name
        expected[tool] = expected_dir / name
        for executable in (discovered[tool], expected[tool]):
            executable.write_bytes(b"synthetic executable marker")
            if os.name != "nt":
                executable.chmod(0o700)
    monkeypatch.setattr(
        subproc_module,
        "find_executable",
        lambda tool: str(discovered[tool]),
    )
    monkeypatch.setattr(
        subproc_module.subprocess,
        "Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("changed child executable path reached Popen")
        ),
    )

    with pytest.raises(ToolError, match="path changed after its runtime probe"):
        run_tool(
            "ocrmypdf",
            ["--version"],
            child_path_tools=("tesseract", "ghostscript"),
            expected_executable=str(discovered["ocrmypdf"]),
            expected_child_executables={
                "tesseract": str(expected["tesseract"]),
                "ghostscript": str(discovered["ghostscript"]),
            },
        )


@pytest.mark.parametrize(
    ("tool", "child_tools"),
    [
        ("qpdf", ("tesseract",)),
        ("ocrmypdf", ("qpdf",)),
        ("ocrmypdf", ("curl",)),
    ],
)
def test_unauthorized_child_path_tools_are_rejected_before_popen(
    monkeypatch: pytest.MonkeyPatch,
    tool: str,
    child_tools: tuple[str, ...],
) -> None:
    monkeypatch.setattr(subproc_module, "find_executable", lambda _tool: sys.executable)
    monkeypatch.setattr(
        subproc_module.subprocess,
        "Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("unauthorized child PATH request reached Popen")
        ),
    )

    with pytest.raises(ToolError, match="child PATH|child-path|not allowed|forbidden"):
        run_tool(tool, ["--version"], child_path_tools=child_tools)


def test_child_path_override_and_relative_cwd_are_refused(monkeypatch) -> None:
    for extra in ({"PATH": "."}, {"Path": "."}):
        with pytest.raises(ToolError, match="PATH overrides"):
            minimal_env(extra)
    with pytest.raises(ToolError, match="not allowed"):
        minimal_env({"PYTHONPATH": "."})
    with pytest.raises(ToolError, match="non-local"):
        minimal_env({"TEMP": "relative"})
    with pytest.raises(ToolError, match="non-local"):
        minimal_env({"USERPROFILE": "relative"})

    monkeypatch.setattr(subproc_module, "find_executable", lambda _tool: sys.executable)
    with pytest.raises(ToolError, match="must be absolute"):
        run_tool("qpdf", ["-c", "pass"], cwd=Path("relative"))


def test_workspace_userprofile_is_explicit_but_host_profile_is_not_inherited(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host_profile = tmp_path / "host-profile"
    workspace_profile = tmp_path / "workspace-profile"
    host_profile.mkdir()
    workspace_profile.mkdir()
    monkeypatch.setenv("USERPROFILE", str(host_profile))

    assert "USERPROFILE" not in minimal_env()
    assert minimal_env({"USERPROFILE": str(workspace_profile)})["USERPROFILE"] == str(
        workspace_profile
    )


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
