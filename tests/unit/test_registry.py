"""Engine registry: probes, selection, and honest capability gating."""

from pathlib import Path

import pytest

import localdocforge.engines.adapters as adapters_module
from localdocforge.domain.models import EngineInfo, EngineKind
from localdocforge.engines.adapters import (
    OP_CONVERT_IMAGES,
    OP_INSPECT,
    OP_MD_TO_PDF,
    OP_MERGE,
    OP_OCR,
    OP_PDF_TO_IMAGES,
    OP_PDF_TO_MD,
    OP_RENDER,
    ExternalToolEngine,
    GhostscriptPresenceEngine,
    build_external_engines,
)
from localdocforge.engines.base import EngineUnavailableError
from localdocforge.engines.registry import CAPABILITY_SPECS, EngineRegistry
from localdocforge.security.subproc import ToolResult, ToolTimeout

IMPLEMENTED_IDS = {
    "merge", "split", "remove-pages", "extract-pages", "organize",
    "rotate", "crop", "inspect", "compress", "images-to-pdf", "pdf-to-images",
    "pdf-to-markdown", "markdown-to-pdf", "convert-images", "ocr",
}


@pytest.fixture(scope="module")
def registry():
    return EngineRegistry()


class TestProbes:
    def test_python_engines_available_here(self, registry):
        infos = {info.name: info for info in registry.all_infos()}
        for name in ("pikepdf", "pypdf", "pdfium", "pillow", "pi-heif"):
            assert infos[name].available, f"{name} must be importable in the test environment"
            assert infos[name].version

    def test_pi_heif_probe_is_honest_about_decode_only(self, registry):
        info = registry.get("pi-heif").probe()
        assert "decode-only" in info.notes
        assert "libheif" in info.notes

    def test_probe_never_raises_for_externals(self, registry):
        # Whatever is or is not installed, probing must return info, not throw.
        for info in registry.all_infos():
            assert info.name
            assert isinstance(info.available, bool)

    def test_missing_externals_carry_install_hints(self, registry):
        for info in registry.all_infos():
            if not info.available:
                assert info.install_hint, f"{info.name} lacks an install hint"


class TestSelection:
    def test_engine_for_merge_prefers_pikepdf(self, registry):
        assert registry.engine_for(OP_MERGE).name == "pikepdf"

    def test_engine_for_render_is_pdfium(self, registry):
        assert registry.engine_for(OP_RENDER).name == "pdfium"

    def test_engine_for_pdf_to_md_is_pdfium(self, registry):
        assert registry.engine_for(OP_PDF_TO_MD).name == "pdfium"

    def test_engine_for_md_to_pdf_is_typst_when_minimum_version_is_present(self, registry):
        typst = registry.get("typst")
        assert typst is not None
        if not typst.probe().available:
            pytest.skip("Typst >=0.15.1 is unavailable on this host")
        assert registry.engine_for(OP_MD_TO_PDF).name == "typst"

    def test_engine_for_convert_images_is_pillow(self, registry):
        assert registry.engine_for(OP_CONVERT_IMAGES).name == "pillow"

    def test_heif_plugin_is_not_an_operation_engine(self, registry):
        # The decode plugin backs capabilities via extra_engines, never by
        # claiming operations of its own.
        assert registry.get("pi-heif").supported_operations() == frozenset()
        with pytest.raises(EngineUnavailableError):
            registry.engine_for(OP_CONVERT_IMAGES, preferred="pi-heif")

    def test_convert_images_capability_requires_the_heif_plugin(self, registry):
        capability = {c.id: c for c in registry.capabilities()}["convert-images"]
        assert capability.available
        spec = next(item for item in CAPABILITY_SPECS if item.id == "convert-images")
        assert spec.extra_engines == ("pi-heif",)

    def test_inspect_capability_requires_pdfium_for_text_inventory(self, registry):
        capability = {c.id: c for c in registry.capabilities()}["inspect"]
        assert capability.available
        spec = next(item for item in CAPABILITY_SPECS if item.id == "inspect")
        assert spec.operation == OP_INSPECT
        assert spec.extra_engines == ("pdfium",)

    def test_inspect_capability_is_gated_off_when_pdfium_probe_fails(self, monkeypatch):
        registry = EngineRegistry()
        pdfium = registry.get("pdfium")
        assert pdfium is not None
        unavailable = pdfium.probe().model_copy(
            update={"available": False, "install_hint": "install synthetic pdfium"}
        )
        monkeypatch.setattr(pdfium, "probe", lambda: unavailable)

        capability = {c.id: c for c in registry.capabilities()}["inspect"]
        assert not capability.available
        assert "pdfium" in capability.missing_requirements

    def test_unwired_library_cannot_be_selected_as_an_engine(self, registry):
        with pytest.raises(EngineUnavailableError):
            registry.engine_for(OP_MERGE, preferred="pypdf")

    def test_preferred_engine_must_support_operation(self, registry):
        with pytest.raises(EngineUnavailableError):
            registry.engine_for(OP_PDF_TO_IMAGES, preferred="pikepdf")

    def test_unknown_operation_raises(self, registry):
        with pytest.raises(EngineUnavailableError):
            registry.engine_for("teleport")

    def test_no_unimplemented_fallback_is_reported(self, registry):
        assert registry.fallback_engine_name(OP_MERGE, "pikepdf") is None


class TestCapabilityHonesty:
    def test_pdf_to_markdown_keeps_stable_capability_id(self):
        matching = [spec for spec in CAPABILITY_SPECS if spec.id == "pdf-to-markdown"]
        assert len(matching) == 1
        assert matching[0].implemented
        assert matching[0].operation == OP_PDF_TO_MD
        assert all(spec.id != "pdf-to-md" for spec in CAPABILITY_SPECS)

    def test_markdown_to_pdf_keeps_stable_capability_id(self):
        matching = [spec for spec in CAPABILITY_SPECS if spec.id == "markdown-to-pdf"]
        assert len(matching) == 1
        assert matching[0].implemented
        assert matching[0].operation == OP_MD_TO_PDF
        assert all(spec.id != "md-to-pdf" for spec in CAPABILITY_SPECS)

    def test_only_implemented_capabilities_can_be_available(self, registry):
        for capability in registry.capabilities():
            if capability.available:
                assert capability.id in IMPLEMENTED_IDS, (
                    f"{capability.id} is advertised but not in the implemented set"
                )

    def test_implemented_capabilities_available_with_required_engines_present(self, registry):
        available = {c.id for c in registry.capabilities() if c.available}
        assert IMPLEMENTED_IDS - {"ocr"} <= available
        ocr_requirements_present = all(
            registry.get(name) is not None and registry.get(name).probe().available
            for name in ("ocrmypdf", "tesseract", "ghostscript")
        )
        assert ("ocr" in available) is ocr_requirements_present

    def test_unimplemented_capabilities_say_so(self, registry):
        capabilities = {c.id: c for c in registry.capabilities()}
        for spec_id in ("repair", "redact", "sign", "office-to-pdf"):
            capability = capabilities[spec_id]
            assert not capability.available
            assert any("not implemented" in reason for reason in capability.missing_requirements)

    def test_ocr_spec_is_implemented_and_gated_on_all_live_requirements(self, monkeypatch):
        registry = EngineRegistry()
        for name in ("ocrmypdf", "tesseract", "ghostscript"):
            engine = registry.get(name)
            assert engine is not None
            monkeypatch.setattr(
                engine,
                "probe",
                lambda name=name: EngineInfo(
                    name=name,
                    kind=EngineKind.EXECUTABLE,
                    available=True,
                    version="synthetic",
                ),
            )

        spec = next(item for item in CAPABILITY_SPECS if item.id == "ocr")
        assert spec.implemented
        assert spec.operation == OP_OCR
        assert spec.extra_engines == ("tesseract", "ghostscript")
        assert {item.id: item for item in registry.capabilities()}["ocr"].available

        ghostscript = registry.get("ghostscript")
        assert ghostscript is not None
        monkeypatch.setattr(
            ghostscript,
            "probe",
            lambda: EngineInfo(
                name="ghostscript",
                kind=EngineKind.EXECUTABLE,
                available=False,
                install_hint="install Ghostscript",
            ),
        )
        unavailable = {item.id: item for item in registry.capabilities()}["ocr"]
        assert not unavailable.available
        assert "ghostscript" in unavailable.missing_requirements

    def test_every_spec_has_category_and_title(self):
        for spec in CAPABILITY_SPECS:
            assert spec.title and spec.category


@pytest.mark.parametrize(
    ("version_output", "available"),
    [
        ("typst 0.15.0 (old)", False),
        ("typst 0.15.1 (minimum)", True),
        ("typst 0.16.0", True),
        ("typst development-build", False),
    ],
)
def test_external_engine_minimum_version_is_enforced_fail_closed(
    monkeypatch,
    version_output,
    available,
):
    monkeypatch.setattr(adapters_module, "find_executable", lambda _name: "C:/tools/typst.exe")
    monkeypatch.setattr(
        adapters_module,
        "run_tool",
        lambda *_args, **_kwargs: ToolResult(returncode=0, output=version_output),
    )
    engine = ExternalToolEngine(
        "typst",
        version_args=["--version"],
        license_name="Apache-2.0",
        install_hint_windows="install Typst",
        operations=frozenset({OP_MD_TO_PDF}),
        minimum_version=(0, 15, 1),
    )

    info = engine.probe()

    assert info.available is available
    assert info.version == version_output
    assert engine.supported_operations() == frozenset({OP_MD_TO_PDF})
    registry = EngineRegistry(engines=[engine])
    if available:
        assert registry.engine_for(OP_MD_TO_PDF) is engine
        assert info.install_hint == ""
    else:
        with pytest.raises(EngineUnavailableError):
            registry.engine_for(OP_MD_TO_PDF)
        assert info.install_hint == "install Typst"
        assert "requires version >= 0.15.1" in info.notes


def _delegate_ghostscript_probe(
    monkeypatch: pytest.MonkeyPatch,
    *,
    ghostscript_path: str,
    returncode: int,
    output_mode: str = "valid",
) -> list[tuple[str, list[str], dict[str, object]]]:
    paths = {
        "ghostscript": ghostscript_path,
        "ocrmypdf": "/opt/venv/bin/ocrmypdf",
        "tesseract": "/opt/tesseract/bin/tesseract",
    }
    monkeypatch.setattr(adapters_module, "find_executable", paths.get)
    calls: list[tuple[str, list[str], dict[str, object]]] = []

    def delegated(tool: str, args: list[str], **kwargs) -> ToolResult:
        calls.append((tool, args, kwargs))
        if returncode == 0 and output_mode == "valid":
            Path(args[-1]).write_bytes(Path(args[-2]).read_bytes())
        elif returncode == 0 and output_mode == "oversize":
            Path(args[-1]).write_bytes(
                b"x" * (adapters_module._GHOSTSCRIPT_PROBE_MAX_PDF_BYTES + 1)
            )
        elif returncode == 0 and output_mode == "invalid":
            Path(args[-1]).write_bytes(b"not a PDF")
        elif returncode == 0 and output_mode == "two-page":
            import pikepdf

            with pikepdf.new() as output:
                output.add_blank_page(page_size=(64, 64))
                output.add_blank_page(page_size=(64, 64))
                output.save(args[-1])
        return ToolResult(returncode=returncode, output="withheld probe diagnostics")

    monkeypatch.setattr(adapters_module, "run_tool", delegated)
    return calls


def test_ghostscript_live_probe_is_delegated_to_ocrmypdf(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key in ("TMPDIR", "TEMP", "TMP"):
        monkeypatch.setenv(key, str(tmp_path))
    calls = _delegate_ghostscript_probe(
        monkeypatch,
        ghostscript_path="/opt/gs/gs10.05.1/bin/gs",
        returncode=0,
    )
    engine = GhostscriptPresenceEngine()
    info = engine.probe()

    assert info.available
    assert info.version == "10.05.1"
    assert len(calls) == 1
    tool, args, kwargs = calls[0]
    assert tool == "ocrmypdf"
    assert args[:-2] == [
        "--quiet",
        "--ocr-engine",
        "none",
        "--rasterizer",
        "ghostscript",
        "--output-type",
        "pdfa-2",
        "--pdfa-image-compression",
        "lossless",
        "--jobs",
        "1",
        "--optimize",
        "0",
        "--max-image-mpixels",
        "1",
        "--no-overwrite",
    ]
    probe_input = Path(args[-2])
    probe_output = Path(args[-1])
    assert probe_input.parent == probe_output.parent
    assert probe_input.parent.parent == tmp_path.resolve()
    assert not probe_input.parent.exists()
    assert kwargs["timeout"] == 20.0
    assert kwargs["cwd"] == probe_input.parent
    assert kwargs["max_output_bytes"] == 64 * 1024
    assert kwargs["env_extra"] == {
        "HOME": str(probe_input.parent),
        "USERPROFILE": str(probe_input.parent),
        "TEMP": str(probe_input.parent),
        "TMP": str(probe_input.parent),
        "TMPDIR": str(probe_input.parent),
    }
    assert kwargs["child_path_tools"] == ("tesseract", "ghostscript")
    assert kwargs["expected_executable"] == "/opt/venv/bin/ocrmypdf"
    assert kwargs["expected_child_executables"] == {
        "tesseract": "/opt/tesseract/bin/tesseract",
        "ghostscript": "/opt/gs/gs10.05.1/bin/gs",
    }
    assert "never launches Ghostscript directly" in info.notes
    assert engine.supported_operations() == frozenset()


def test_ghostscript_probe_rejects_nonlocal_temp_root_before_scratch_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _delegate_ghostscript_probe(
        monkeypatch,
        ghostscript_path="/opt/gs/gs10.05.1/bin/gs",
        returncode=0,
    )
    for key in ("TMPDIR", "TEMP", "TMP"):
        monkeypatch.setenv(key, r"\\server\share")

    class UnexpectedTemporaryDirectory:
        def __init__(self, **_kwargs) -> None:
            raise AssertionError("unsafe temp root reached scratch creation")

    monkeypatch.setattr(
        adapters_module,
        "TemporaryDirectory",
        UnexpectedTemporaryDirectory,
    )

    info = GhostscriptPresenceEngine().probe()

    assert not info.available
    assert calls == []
    assert "confirmed local temporary directory" in info.notes


def test_ghostscript_presence_probe_rejects_known_unsupported_version(monkeypatch) -> None:
    _delegate_ghostscript_probe(
        monkeypatch,
        ghostscript_path="/opt/gs/gs9.53.3/bin/gs",
        returncode=3,
    )
    engine = GhostscriptPresenceEngine()
    info = engine.probe()

    assert not info.available
    assert info.version == "9.53.3"
    assert ">=9.54" in info.install_hint


def test_ghostscript_two_part_minimum_version_is_accepted(monkeypatch) -> None:
    _delegate_ghostscript_probe(
        monkeypatch,
        ghostscript_path="/opt/gs/gs9.54/bin/gs",
        returncode=0,
    )
    info = GhostscriptPresenceEngine().probe()

    assert info.available
    assert info.version == "9.54"
    assert info.install_hint == ""


def test_ghostscript_unknown_version_fails_closed_without_false_compatibility_claim(
    monkeypatch,
) -> None:
    _delegate_ghostscript_probe(
        monkeypatch,
        ghostscript_path="/opt/ghostscript/bin/gs",
        returncode=0,
    )
    info = GhostscriptPresenceEngine().probe()

    assert info.version is None
    assert "compatibility is checked by OCRmyPDF at job time" not in info.notes
    assert info.available
    assert info.install_hint == ""
    assert "live one-page render/PDF-A probe delegated to OCRmyPDF" in info.notes


def test_ghostscript_delegated_probe_timeout_returns_unavailable(monkeypatch) -> None:
    _delegate_ghostscript_probe(
        monkeypatch,
        ghostscript_path="/opt/ghostscript/bin/gs",
        returncode=0,
    )

    def stalled(*_args, **_kwargs):
        raise ToolTimeout("private stalled-probe diagnostic")

    monkeypatch.setattr(adapters_module, "run_tool", stalled)
    info = GhostscriptPresenceEngine().probe()

    assert not info.available
    assert info.install_hint
    assert info.notes == "OCRmyPDF-mediated Ghostscript probe could not run safely"
    assert "private stalled-probe diagnostic" not in info.notes


@pytest.mark.parametrize("output_mode", ["missing", "oversize", "invalid", "two-page"])
def test_ghostscript_exit_zero_requires_bounded_valid_one_page_output(
    monkeypatch: pytest.MonkeyPatch,
    output_mode: str,
) -> None:
    _delegate_ghostscript_probe(
        monkeypatch,
        ghostscript_path="/opt/ghostscript/bin/gs",
        returncode=0,
        output_mode=output_mode,
    )

    info = GhostscriptPresenceEngine().probe()

    assert not info.available
    assert info.install_hint


def test_ghostscript_probe_cleanup_failure_cannot_retain_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _delegate_ghostscript_probe(
        monkeypatch,
        ghostscript_path="/opt/ghostscript/bin/gs",
        returncode=0,
    )

    class CleanupFailure:
        def __init__(self, **_kwargs) -> None:
            pass

        def __enter__(self) -> str:
            return str(tmp_path)

        def __exit__(self, *_args) -> None:
            raise OSError("private cleanup failure")

    monkeypatch.setattr(adapters_module, "TemporaryDirectory", CleanupFailure)
    info = GhostscriptPresenceEngine().probe()

    assert not info.available
    assert info.notes == "OCRmyPDF-mediated Ghostscript probe could not run safely"
    assert "private cleanup failure" not in info.notes


def test_ghostscript_probe_workspace_walk_enforces_byte_and_entry_limits(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first.bin"
    second = tmp_path / "second.bin"
    first.write_bytes(b"123")
    second.write_bytes(b"4567")

    assert (
        adapters_module._bounded_local_tree_size(
            tmp_path,
            max_bytes=7,
            max_entries=2,
        )
        == 7
    )
    assert (
        adapters_module._bounded_local_tree_size(
            tmp_path,
            max_bytes=6,
            max_entries=2,
        )
        is None
    )
    assert (
        adapters_module._bounded_local_tree_size(
            tmp_path,
            max_bytes=7,
            max_entries=1,
        )
        is None
    )


def test_ghostscript_probe_workspace_walk_rejects_links(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("local symlink creation is unavailable")

    assert (
        adapters_module._bounded_local_tree_size(
            tmp_path,
            max_bytes=1024,
            max_entries=4,
        )
        is None
    )


@pytest.mark.parametrize(
    ("version_output", "available"),
    [
        ("tesseract v5.4.0", False),
        ("tesseract v5.4.0.20240606", True),
    ],
)
def test_tesseract_rejects_exact_upstream_540_but_accepts_vendor_build(
    monkeypatch,
    version_output: str,
    available: bool,
) -> None:
    monkeypatch.setattr(
        adapters_module,
        "find_executable",
        lambda _name: "C:/Program Files/Tesseract-OCR/tesseract.exe",
    )
    monkeypatch.setattr(
        adapters_module,
        "run_tool",
        lambda *_args, **_kwargs: ToolResult(returncode=0, output=version_output),
    )
    engine = next(item for item in build_external_engines() if item.name == "tesseract")

    info = engine.probe()

    assert info.available is available
    assert info.version == version_output
    if available:
        assert info.install_hint == ""
        assert "rejected" not in info.notes
    else:
        assert info.install_hint
        assert "known regressions" in info.notes
