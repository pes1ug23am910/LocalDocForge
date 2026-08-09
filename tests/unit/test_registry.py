"""Engine registry: probes, selection, and honest capability gating."""

import pytest

import localdocforge.engines.adapters as adapters_module
from localdocforge.engines.adapters import (
    OP_CONVERT_IMAGES,
    OP_INSPECT,
    OP_MD_TO_PDF,
    OP_MERGE,
    OP_PDF_TO_IMAGES,
    OP_PDF_TO_MD,
    OP_RENDER,
    ExternalToolEngine,
)
from localdocforge.engines.base import EngineUnavailableError
from localdocforge.engines.registry import CAPABILITY_SPECS, EngineRegistry
from localdocforge.security.subproc import ToolResult

IMPLEMENTED_IDS = {
    "merge", "split", "remove-pages", "extract-pages", "organize",
    "rotate", "crop", "inspect", "compress", "images-to-pdf", "pdf-to-images",
    "pdf-to-markdown", "markdown-to-pdf", "convert-images",
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

    def test_implemented_capabilities_available_with_engines_present(self, registry):
        available = {c.id for c in registry.capabilities() if c.available}
        assert IMPLEMENTED_IDS <= available

    def test_unimplemented_capabilities_say_so(self, registry):
        capabilities = {c.id: c for c in registry.capabilities()}
        for spec_id in ("ocr", "repair", "redact", "sign", "office-to-pdf"):
            capability = capabilities[spec_id]
            assert not capability.available
            assert any("not implemented" in reason for reason in capability.missing_requirements)

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
