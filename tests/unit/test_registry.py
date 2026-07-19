"""Engine registry: probes, selection, and honest capability gating."""

import pytest

from localdocforge.engines.adapters import OP_MERGE, OP_PDF_TO_IMAGES, OP_RENDER
from localdocforge.engines.base import EngineUnavailableError
from localdocforge.engines.registry import CAPABILITY_SPECS, EngineRegistry

IMPLEMENTED_IDS = {
    "merge", "split", "remove-pages", "extract-pages", "organize",
    "rotate", "crop", "inspect", "images-to-pdf", "pdf-to-images",
}


@pytest.fixture(scope="module")
def registry():
    return EngineRegistry()


class TestProbes:
    def test_python_engines_available_here(self, registry):
        infos = {info.name: info for info in registry.all_infos()}
        for name in ("pikepdf", "pypdf", "pdfium", "pillow"):
            assert infos[name].available, f"{name} must be importable in the test environment"
            assert infos[name].version

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
        for spec_id in ("ocr", "compress", "redact", "sign", "office-to-pdf"):
            capability = capabilities[spec_id]
            assert not capability.available
            assert any("not implemented" in reason for reason in capability.missing_requirements)

    def test_every_spec_has_category_and_title(self):
        for spec in CAPABILITY_SPECS:
            assert spec.title and spec.category
