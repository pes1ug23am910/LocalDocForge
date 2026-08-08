"""Packaging profile, lock, bootstrap, and release-gate contracts."""

from __future__ import annotations

import hashlib
import io
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tomllib
from pathlib import Path

import pytest
from scripts import profile_matrix, profile_smoke, release_gate
from typer.testing import CliRunner

from localdocforge.cli.main import EXIT_USAGE, app

ROOT = Path(__file__).resolve().parents[2]
NAME_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==")


def _tree_fingerprint(root: Path):
    if not root.exists():
        return None
    return [
        (
            path.relative_to(root).as_posix(),
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        for path in sorted(candidate for candidate in root.rglob("*") if candidate.is_file())
    ]


def _profile_names(profile: str) -> set[str]:
    names: set[str] = set()
    text = (ROOT / "requirements" / "locks" / f"{profile}.txt").read_text(encoding="utf-8")
    for line in text.splitlines():
        match = NAME_RE.match(line)
        if match:
            names.add(re.sub(r"[-_.]+", "-", match.group(1)).lower())
    return names


def test_published_profiles_match_shipped_capabilities():
    with (ROOT / "pyproject.toml").open("rb") as stream:
        project = tomllib.load(stream)
    metadata = project["project"]
    assert metadata["requires-python"] == ">=3.12,<3.15"
    assert project["build-system"]["requires"] == ["setuptools==83.0.0"]
    declared_base = frozenset(
        release_gate.Requirement(requirement).name.lower()
        for requirement in metadata["dependencies"]
    )
    assert release_gate.EXPECTED_BASE_DEPENDENCIES == declared_base
    assert "pi-heif" in release_gate.EXPECTED_BASE_DEPENDENCIES
    assert "Operating System :: Microsoft :: Windows :: Windows 11" in metadata[
        "classifiers"
    ]
    assert "Operating System :: OS Independent" not in metadata["classifiers"]

    extras = metadata["optional-dependencies"]
    assert extras["lite"] == []
    assert extras["standard"] == [
        "fastapi>=0.115",
        "uvicorn>=0.30",
        "python-multipart>=0.0.9",
    ]
    assert extras["full"] == [*extras["standard"], "pypdf>=5.0"]
    for requirement in (*extras["full"], "pytest>=8.3", "mypy>=2.3", "build>=1.5"):
        assert requirement in extras["dev"]


def test_hash_locks_have_expected_strict_profile_deltas():
    profiles = {name: _profile_names(name) for name in ("lite", "standard", "full", "dev")}
    assert profiles["lite"] < profiles["standard"] < profiles["full"] < profiles["dev"]
    assert profiles["standard"] - profiles["lite"] == {
        "anyio",
        "click",
        "fastapi",
        "h11",
        "idna",
        "python-multipart",
        "starlette",
        "uvicorn",
    }
    assert profiles["full"] - profiles["standard"] == {"pypdf"}
    for profile in profiles:
        text = (ROOT / "requirements" / "locks" / f"{profile}.txt").read_text(encoding="utf-8")
        assert "--hash=sha256:" in text
        assert "implementation_name == 'cpython'" in text
        assert "--trusted-host" not in text
        assert "--allow-insecure-host" not in text


def test_uv_bootstrap_is_exact_and_every_release_artifact_is_hashed():
    text = (ROOT / "requirements" / "uv-bootstrap.txt").read_text(encoding="utf-8")
    assert "uv==0.11.26" in text
    assert len(re.findall(r"--hash=sha256:[0-9a-f]{64}", text)) == 19
    last = text.rstrip().splitlines()[-1]
    assert not last.endswith("\\")


def test_build_backend_lock_is_exact_hashed_and_official_pypi_only():
    text = (ROOT / "requirements" / "build-backend.txt").read_text(encoding="utf-8")
    assert "--index-url https://pypi.org/simple" in text
    assert re.findall(r"(?m)^([A-Za-z0-9._-]+==[^\s\\]+)", text) == [
        "setuptools==83.0.0"
    ]
    assert set(re.findall(r"--hash=sha256:([0-9a-f]{64})", text)) == {
        "29b23c360f22f414dc7336bb39178cc7bcbf6021ed2733cde173f09dba19abb3",
        "025bccbbf0fa05b6192bc64ae1e7b16e001fd6d6d4d5de03c97b1c1ade523bef",
    }
    assert not any(
        token in text
        for token in ("--extra-index-url", "--trusted-host", "--find-links", "http://")
    )


def test_build_backend_wheelhouse_download_is_bounded_and_hash_verified(
    monkeypatch, tmp_path
):
    payload = b"synthetic audited backend wheel"
    payload_hash = hashlib.sha256(payload).hexdigest()
    other_hash = "0" * 64
    backend_lock = tmp_path / "build-backend.txt"
    backend_lock.write_text(
        "--index-url https://pypi.org/simple\n"
        "setuptools==83.0.0 \\\n"
        f"    --hash=sha256:{payload_hash} \\\n"
        f"    --hash=sha256:{other_hash}\n",
        encoding="utf-8",
    )

    class Response(io.BytesIO):
        headers = {"Content-Length": str(len(payload))}

        def geturl(self):
            return release_gate.BUILD_BACKEND_WHEEL_URL

    requests = []

    def urlopen(request, timeout):
        requests.append((request, timeout))
        return Response(payload)

    monkeypatch.setattr(release_gate, "BUILD_BACKEND_REQUIREMENTS", backend_lock)
    monkeypatch.setattr(
        release_gate,
        "BUILD_BACKEND_RELEASE_HASHES",
        frozenset({payload_hash, other_hash}),
    )
    monkeypatch.setattr(release_gate, "BUILD_BACKEND_WHEEL_SHA256", payload_hash)
    monkeypatch.setattr(release_gate.urllib.request, "urlopen", urlopen)

    wheel = release_gate._prepare_build_backend_wheelhouse(tmp_path / "wheelhouse")
    assert wheel.read_bytes() == payload
    assert requests[0][0].full_url.startswith("https://files.pythonhosted.org/")
    assert requests[0][1] == 60


def test_legacy_lock_entrypoint_enforces_hashes():
    assert (ROOT / "requirements-lock.txt").read_text(encoding="utf-8").splitlines()[-2:] == [
        "--require-hashes",
        "-r requirements/locks/dev.txt",
    ]


def test_lite_web_command_has_actionable_profile_hint(monkeypatch):
    import importlib.util

    original = importlib.util.find_spec

    def without_standard(name: str, *args, **kwargs):
        if name in {"fastapi", "uvicorn", "python_multipart"}:
            return None
        return original(name, *args, **kwargs)

    monkeypatch.setattr(importlib.util, "find_spec", without_standard)
    result = CliRunner().invoke(app, ["web"])
    assert result.exit_code == EXIT_USAGE
    assert "pip install 'localdocforge[standard]'" in result.output
    assert "Traceback" not in result.output


def test_blocked_network_wrapper_propagates_to_child_python():
    child = (
        "import socket; "
        "\ntry: socket.getaddrinfo('example.invalid', 443)"
        "\nexcept OSError as e: "
        "\n raise SystemExit(0 if 'blocked-network gate denied' in str(e) else 2)"
        "\nraise SystemExit(3)"
    )
    parent = (
        "import subprocess,sys; "
        f"raise SystemExit(subprocess.run([sys.executable, '-c', {child!r}]).returncode)"
    )
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "run_blocked_network.py"),
            "--",
            sys.executable,
            "-c",
            parent,
        ],
        cwd=ROOT,
        check=True,
    )


def test_sdist_canonicalization_removes_archive_metadata_drift(tmp_path):
    archives = [tmp_path / "first.tar.gz", tmp_path / "second.tar.gz"]
    for index, archive_path in enumerate(archives):
        entries = [
            ("localdocforge-0.1.0", b""),
            ("localdocforge-0.1.0/PKG-INFO", b"Metadata-Version: 2.4\n"),
        ]
        if index:
            entries.reverse()
        with tarfile.open(archive_path, "w:gz") as archive:
            for name, payload in entries:
                info = tarfile.TarInfo(name)
                info.mtime = 100 + index
                info.uid = 500 + index
                info.gid = 600 + index
                info.uname = f"builder-{index}"
                info.gname = f"group-{index}"
                if payload:
                    info.size = len(payload)
                    archive.addfile(info, io.BytesIO(payload))
                else:
                    info.type = tarfile.DIRTYPE
                    archive.addfile(info)

    for archive_path in archives:
        release_gate._canonicalize_sdist(archive_path)
    assert archives[0].read_bytes() == archives[1].read_bytes()


def test_build_uses_separate_isolated_sdist_and_direct_wheel_invocations(
    monkeypatch, tmp_path
):
    commands: list[tuple[list[str], dict[str, str] | None]] = []
    output = tmp_path / "dist"
    wheelhouse = tmp_path / "backend-wheelhouse"
    wheelhouse.mkdir()
    wheel = output / "localdocforge-0.1.0-py3-none-any.whl"
    sdist = output / "localdocforge-0.1.0.tar.gz"

    def record(command: list[str], *, environment=None):
        commands.append((command, environment))

    monkeypatch.setattr(release_gate, "_run", record)
    monkeypatch.setattr(release_gate, "_find_artifacts", lambda directory: (wheel, sdist))
    monkeypatch.setattr(release_gate, "_canonicalize_sdist", lambda path: None)

    assert release_gate._build(
        output,
        build_backend_wheelhouse=wheelhouse,
    ) == (wheel, sdist)
    build_records = [
        record for record in commands if record[0][1:3] == ["-m", "build"]
    ]
    build_commands = [record[0] for record in build_records]
    assert [
        next(mode for mode in ("--sdist", "--wheel") if mode in command)
        for command in build_commands
    ] == ["--sdist", "--wheel"]
    assert all("--outdir" in command for command in build_commands)
    assert all("--installer" in command and "pip" in command for command in build_commands)
    assert all("--no-isolation" not in command for command in build_commands)
    for _, environment in build_records:
        assert environment is not None
        assert environment["PIP_NO_INDEX"] == "1"
        assert environment["PIP_FIND_LINKS"] == str(wheelhouse.resolve())
        assert environment["PIP_NO_CACHE_DIR"] == "1"
        assert environment["PIP_ONLY_BINARY"] == ":all:"
        assert "PIP_INDEX_URL" not in environment
        assert "PIP_EXTRA_INDEX_URL" not in environment


def test_clean_source_stage_excludes_checkout_build_state(tmp_path):
    staged = release_gate._stage_source(tmp_path / "source")

    assert staged != ROOT
    assert (staged / "pyproject.toml").is_file()
    assert (staged / "src" / "localdocforge" / "api" / "worker.py").is_file()
    assert not (staged / "build").exists()
    assert not (staged / "src" / "localdocforge.egg-info").exists()
    assert not any(path.name == "__pycache__" for path in staged.rglob("__pycache__"))


def test_build_gate_uses_two_distinct_staged_sources(monkeypatch, tmp_path):
    sources: list[Path] = []
    backend_wheelhouses: list[Path] = []
    staged_present: list[bool] = []
    residue_paths = (ROOT / "build", ROOT / "src" / "localdocforge.egg-info")
    before = [_tree_fingerprint(path) for path in residue_paths]

    def fake_prepare(wheelhouse: Path):
        wheelhouse.mkdir()
        wheel = wheelhouse / release_gate.BUILD_BACKEND_WHEEL_NAME
        wheel.write_bytes(b"backend")
        return wheel

    def fake_build(output: Path, source: Path, *, build_backend_wheelhouse: Path):
        sources.append(source)
        backend_wheelhouses.append(build_backend_wheelhouse)
        staged_present.append(source != ROOT and (source / "pyproject.toml").is_file())
        output.mkdir()
        wheel = output / "localdocforge-0.1.0-py3-none-any.whl"
        sdist = output / "localdocforge-0.1.0.tar.gz"
        wheel.write_bytes(b"wheel")
        sdist.write_bytes(b"sdist")
        return wheel, sdist

    def fake_rebuild(_sdist: Path, output: Path, *, build_backend_wheelhouse: Path):
        backend_wheelhouses.append(build_backend_wheelhouse)
        output.mkdir()
        wheel = output / "localdocforge-0.1.0-py3-none-any.whl"
        wheel.write_bytes(b"wheel")
        return wheel

    monkeypatch.setattr(release_gate, "_prepare_build_backend_wheelhouse", fake_prepare)
    monkeypatch.setattr(release_gate, "_build", fake_build)
    monkeypatch.setattr(release_gate, "_rebuild_sdist", fake_rebuild)
    monkeypatch.setattr(release_gate, "_validate_metadata", lambda wheel: None)
    monkeypatch.setattr(release_gate, "_artifact_record", lambda wheel, sdist, source: {})
    monkeypatch.setattr(
        release_gate,
        "_check_or_update_manifest",
        lambda record, update, **_: None,
    )

    retained = tmp_path / "dist"
    release_gate._build_gate(False, retained)

    assert len(sources) == 2
    assert sources[0] != sources[1]
    assert staged_present == [True, True]
    assert len(set(backend_wheelhouses)) == 1
    assert backend_wheelhouses[0].name == "build-backend-wheelhouse"
    assert [_tree_fingerprint(path) for path in residue_paths] == before


def test_build_only_gate_removes_retained_wheel_directory(monkeypatch, tmp_path):
    retained = tmp_path / "ldf-release-wheel-test"
    wheel = retained / "localdocforge-0.1.0-py3-none-any.whl"

    def fake_build_gate(
        update_manifest: bool,
        dist_directory: Path | None,
        allow_unrecorded: bool = False,
    ) -> Path:
        assert update_manifest is False
        assert dist_directory is None
        assert allow_unrecorded is False
        retained.mkdir()
        wheel.write_bytes(b"wheel")
        return wheel

    monkeypatch.setattr(release_gate, "_build_gate", fake_build_gate)
    assert release_gate.main(["--steps", "build"]) == 0
    assert not retained.exists()


def test_explicit_release_dist_directory_is_retained(monkeypatch, tmp_path):
    retained = tmp_path / "dist"
    wheel = retained / "localdocforge-0.1.0-py3-none-any.whl"

    def fake_build_gate(
        update_manifest: bool,
        dist_directory: Path | None,
        allow_unrecorded: bool = False,
    ) -> Path:
        assert update_manifest is False
        assert dist_directory == retained
        assert allow_unrecorded is False
        retained.mkdir()
        wheel.write_bytes(b"wheel")
        return wheel

    monkeypatch.setattr(release_gate, "_build_gate", fake_build_gate)
    assert release_gate.main(["--steps", "build", "--dist-dir", str(retained)]) == 0
    assert wheel.is_file()


def test_default_gate_runs_clean_full_tests_while_profile_only_stays_focused(
    monkeypatch, tmp_path
):
    commands: list[list[str]] = []
    build_count = 0

    def fake_build_gate(
        update_manifest: bool,
        dist_directory: Path | None,
        allow_unrecorded: bool = False,
    ) -> Path:
        nonlocal build_count
        assert update_manifest is False
        assert dist_directory is None
        assert allow_unrecorded is False
        build_count += 1
        retained = tmp_path / f"retained-{build_count}"
        retained.mkdir()
        wheel = retained / "localdocforge-0.1.0-py3-none-any.whl"
        wheel.write_bytes(b"wheel")
        return wheel

    def record(command: list[str], **kwargs):
        commands.append(command)

    monkeypatch.setattr(release_gate, "_build_gate", fake_build_gate)
    monkeypatch.setattr(release_gate, "_run", record)

    assert release_gate.main([]) == 0
    profile_command = next(
        command for command in commands if command[1].endswith("profile_matrix.py")
    )
    assert "--install-source" in profile_command
    assert "--full-tests" in profile_command

    commands.clear()
    assert release_gate.main(["--steps", "profiles"]) == 0
    profile_command = next(
        command for command in commands if command[1].endswith("profile_matrix.py")
    )
    assert "--install-source" in profile_command
    assert "--full-tests" not in profile_command


def test_distribution_text_files_are_normalized_to_lf():
    attributes = (ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert "*.py text eol=lf" in attributes
    assert "*.toml text eol=lf" in attributes
    for path in [ROOT / "README.md", ROOT / "pyproject.toml"]:
        assert b"\r\n" not in path.read_bytes()


def test_profile_cli_subprocess_is_isolated_from_host_pythonpath(monkeypatch):
    captured: dict[str, object] = {}
    monkeypatch.setenv("PYTHONHOME", "host-home")
    monkeypatch.setenv("PYTHONPATH", "host-path")

    def fake_run(command: list[str], **kwargs):
        captured["command"] = command
        captured["environment"] = kwargs["env"]
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(profile_smoke.subprocess, "run", fake_run)
    profile_smoke._run_cli("--help", check=False)

    command = captured["command"]
    assert isinstance(command, list)
    assert command[:4] == [sys.executable, "-I", "-m", "localdocforge.cli.main"]
    environment = captured["environment"]
    assert isinstance(environment, dict)
    assert "PYTHONHOME" not in environment
    assert "PYTHONPATH" not in environment
    assert environment["LDF_STRICT_OFFLINE"] == "true"


def test_profile_matrix_outer_smoke_is_isolated(monkeypatch, tmp_path):
    commands: list[list[str]] = []

    def record(command: list[str], **kwargs):
        commands.append(command)
        if kwargs.get("capture"):
            return subprocess.CompletedProcess(command, 0, "[]", "")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(profile_matrix, "_run", record)
    monkeypatch.setattr(
        profile_matrix,
        "_interpreter_identity",
        lambda python: {"version": "3.13.5", "implementation": "CPython"},
    )
    monkeypatch.setattr(profile_matrix, "_assert_uninstalled", lambda python: None)
    lock = tmp_path / "lite.txt"
    lock.write_text("synthetic", encoding="utf-8")
    monkeypatch.setattr(profile_matrix, "LOCK_DIR", tmp_path)
    wheel = tmp_path / "localdocforge-0.1.0-py3-none-any.whl"
    wheel.write_bytes(b"wheel")

    profile_matrix._profile_run(
        "uv", "python3.13", wheel, "lite", tmp_path / "matrix", install_source=False
    )

    smoke = next(command for command in commands if "profile_smoke.py" in " ".join(command))
    assert smoke[1] == "-I"


def test_profile_evidence_identity_records_os_release_and_build():
    identity = profile_matrix._interpreter_identity(Path(sys.executable))

    assert identity["implementation"] == platform.python_implementation()
    assert identity["version"] == platform.python_version()
    assert identity["system"] == platform.system()
    assert identity["platform_release"] == platform.release()
    assert identity["platform_version"] == platform.version()
    assert identity["machine"] == platform.machine()


def test_checksum_file_binds_selected_wheel(tmp_path):
    wheel = tmp_path / "localdocforge-0.1.0-py3-none-any.whl"
    wheel.write_bytes(b"audited-wheel")
    checksum = tmp_path / "SHA256SUMS"
    checksum.write_text(f"{profile_matrix._sha256(wheel)}  {wheel.name}\n", encoding="utf-8")

    profile_matrix._verify_checksum_file(wheel, checksum)
    checksum.write_text(f"{'0' * 64}  {wheel.name}\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="checksum mismatch"):
        profile_matrix._verify_checksum_file(wheel, checksum)


def test_release_manifest_matches_any_recorded_platform_identity(monkeypatch, tmp_path):
    wheel = tmp_path / "localdocforge-0.1.0-py3-none-any.whl"
    wheel.write_bytes(b"audited-wheel")
    manifest = tmp_path / "release-artifact-manifest.json"
    source_digest = "1" * 64
    manifest.write_text(
        json.dumps(
            {
                "schema": 3,
                "platforms": {
                    "Windows-AMD64": {
                        "source_tree_sha256": source_digest,
                        "artifacts": {wheel.name: {"sha256": profile_matrix._sha256(wheel)}},
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(profile_matrix, "MANIFEST_PATH", manifest)

    matched = profile_matrix._verify_release_manifest(wheel, source_digest)
    assert matched == "Windows-AMD64"
    with pytest.raises(RuntimeError, match="do not match any recorded"):
        profile_matrix._verify_release_manifest(wheel, "2" * 64)
    assert (
        profile_matrix._verify_release_manifest(wheel, "2" * 64, allow_unrecorded=True)
        is None
    )


def test_gate_manifest_is_platform_scoped(monkeypatch, tmp_path):
    manifest = tmp_path / "release-artifact-manifest.json"
    monkeypatch.setattr(release_gate, "MANIFEST_PATH", manifest)
    monkeypatch.setattr(release_gate, "_platform_key", lambda: "Testing-x86")
    record = {"source_date_epoch": 1, "source_tree_sha256": "a" * 64, "artifacts": {}}

    release_gate._check_or_update_manifest(record, True)
    stored = json.loads(manifest.read_text(encoding="utf-8"))
    assert stored["schema"] == 3
    assert stored["platforms"]["Testing-x86"] == record

    release_gate._check_or_update_manifest(record, False)
    with pytest.raises(RuntimeError, match="drift detected"):
        release_gate._check_or_update_manifest(
            {**record, "source_tree_sha256": "b" * 64}, False
        )

    monkeypatch.setattr(release_gate, "_platform_key", lambda: "Other-arm64")
    with pytest.raises(RuntimeError, match="no recorded release identity"):
        release_gate._check_or_update_manifest(record, False)
    release_gate._check_or_update_manifest(record, False, allow_unrecorded=True)

    release_gate._check_or_update_manifest(record, True)
    stored = json.loads(manifest.read_text(encoding="utf-8"))
    assert set(stored["platforms"]) == {"Testing-x86", "Other-arm64"}


def test_gate_manifest_migrates_legacy_flat_schema_on_update(monkeypatch, tmp_path):
    manifest = tmp_path / "release-artifact-manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema": 2,
                "source_date_epoch": 1,
                "source_tree_sha256": "c" * 64,
                "artifacts": {"w.whl": {"sha256": "d" * 64}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(release_gate, "MANIFEST_PATH", manifest)
    monkeypatch.setattr(release_gate, "_platform_key", lambda: "Linux-x86_64")
    record = {"source_date_epoch": 1, "source_tree_sha256": "e" * 64, "artifacts": {}}

    release_gate._check_or_update_manifest(record, True)
    stored = json.loads(manifest.read_text(encoding="utf-8"))
    assert stored["platforms"]["Windows-AMD64"]["source_tree_sha256"] == "c" * 64
    assert stored["platforms"]["Linux-x86_64"] == record


def test_source_digest_matches_profile_and_ignores_build_residue(monkeypatch, tmp_path):
    root = tmp_path / "source"
    package = root / "src" / "localdocforge"
    package.mkdir(parents=True)
    for name, content in (
        ("LICENSE", "license\n"),
        ("README.md", "readme\n"),
        ("pyproject.toml", "[build-system]\n"),
    ):
        (root / name).write_text(content, encoding="utf-8")
    (package / "__init__.py").write_text('__version__ = "0.1.0"\n', encoding="utf-8")

    monkeypatch.setattr(profile_matrix, "ROOT", root)
    expected = profile_matrix._package_source_sha256()
    assert release_gate._source_tree_sha256(root) == expected

    (root / "build").mkdir()
    (root / "build" / "derived.py").write_text("derived\n", encoding="utf-8")
    egg_info = root / "src" / "localdocforge.egg-info"
    egg_info.mkdir()
    (egg_info / "PKG-INFO").write_text("derived\n", encoding="utf-8")
    cache = package / "__pycache__"
    cache.mkdir()
    (cache / "cached.pyc").write_bytes(b"derived")

    assert release_gate._source_tree_sha256(root) == expected
    assert profile_matrix._package_source_sha256() == expected


def test_bootstrap_scripts_have_expected_headers_lf_and_no_patch_debris():
    shell = (ROOT / "scripts" / "bootstrap.sh").read_bytes()
    powershell = (ROOT / "scripts" / "bootstrap.ps1").read_bytes()
    assert shell.startswith(b"#!/usr/bin/env bash\n")
    assert powershell.startswith(b"# LocalDocForge development/profile bootstrap")
    for payload in (shell, powershell):
        assert b"\r\n" not in payload
        assert b"diff --git" not in payload
        assert b"*** Begin Patch" not in payload
        assert b"*** End Patch" not in payload
    attributes = (ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert "*.sh text eol=lf" in attributes
    assert "*.ps1 text eol=lf" in attributes


def test_powershell_bootstrap_makes_native_failures_terminating():
    bootstrap = (ROOT / "scripts" / "bootstrap.ps1").read_text(encoding="utf-8")
    preference = "$PSNativeCommandUseErrorActionPreference = $true"
    assert preference in bootstrap
    assert bootstrap.index("if (-not $python)") < bootstrap.index(preference)
    assert bootstrap.index(preference) < bootstrap.index("& $venvPython -m pip install")

    pwsh = shutil.which("pwsh")
    if pwsh is None:
        pytest.skip("PowerShell 7 is unavailable on this runner")

    environment = os.environ.copy()
    environment["LDF_TEST_NATIVE"] = sys.executable
    probe = subprocess.run(
        [
            pwsh,
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            (
                "$ErrorActionPreference = 'Stop'; "
                "$PSNativeCommandUseErrorActionPreference = $true; "
                "& $env:LDF_TEST_NATIVE -c 'import sys; sys.exit(23)'; "
                "Write-Output 'continued-after-failure'"
            ),
        ],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert probe.returncode != 0
    assert "continued-after-failure" not in probe.stdout
