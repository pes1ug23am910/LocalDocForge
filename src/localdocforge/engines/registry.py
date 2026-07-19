"""Engine registry and honest capability reporting.

A capability is advertised as available only when BOTH are true:
1. its pipeline is implemented and tested in this codebase, and
2. at least one engine that supports it passed a live probe this session.
"""

from __future__ import annotations

from dataclasses import dataclass

from localdocforge.domain.models import Capability, EngineInfo
from localdocforge.engines import adapters
from localdocforge.engines.adapters import (
    PdfiumEngine,
    PikepdfEngine,
    PillowEngine,
    PypdfEngine,
    build_external_engines,
)
from localdocforge.engines.base import EngineAdapter, EngineUnavailableError


@dataclass(frozen=True)
class CapabilitySpec:
    id: str
    title: str
    category: str
    operation: str | None  # operation id probed against engines; None = engine-independent
    implemented: bool
    extra_engines: tuple[str, ...] = ()  # engines required besides the operation's engine
    notes: str = ""
    install_hint: str = ""


# The single source of truth for what LocalDocForge claims it can do.
# ``implemented`` flips to True only in the same change that lands the
# pipeline plus its tests. Doctor output, the API, and the UI all read this.
CAPABILITY_SPECS: tuple[CapabilitySpec, ...] = (
    CapabilitySpec("merge", "Merge PDF", "Organize PDF", adapters.OP_MERGE, True),
    CapabilitySpec("split", "Split PDF", "Organize PDF", adapters.OP_SPLIT, True),
    CapabilitySpec("remove-pages", "Remove pages", "Organize PDF", adapters.OP_REMOVE_PAGES, True),
    CapabilitySpec(
        "extract-pages", "Extract pages", "Organize PDF", adapters.OP_EXTRACT_PAGES, True
    ),
    CapabilitySpec("organize", "Organize PDF", "Organize PDF", adapters.OP_ORGANIZE, True),
    CapabilitySpec("rotate", "Rotate PDF", "Edit PDF", adapters.OP_ROTATE, True),
    CapabilitySpec(
        "crop",
        "Crop PDF",
        "Edit PDF",
        adapters.OP_CROP,
        True,
        notes="Cropping hides content from view; it is NOT redaction.",
    ),
    CapabilitySpec("inspect", "Inspect PDF", "Secure and inspect", adapters.OP_INSPECT, True),
    CapabilitySpec(
        "images-to-pdf",
        "Images to PDF",
        "Convert to PDF",
        adapters.OP_IMAGES_TO_PDF,
        True,
    ),
    CapabilitySpec(
        "pdf-to-images",
        "PDF to images",
        "Convert from PDF",
        adapters.OP_PDF_TO_IMAGES,
        True,
    ),
    # ---- Not yet implemented; listed so doctor can say exactly what is missing ----
    CapabilitySpec(
        "compress",
        "Compress PDF",
        "Optimize PDF",
        None,
        False,
        notes="planned: Phase 2",
    ),
    CapabilitySpec("repair", "Repair PDF", "Optimize PDF", None, False, notes="planned: Phase 2"),
    CapabilitySpec(
        "ocr",
        "OCR PDF",
        "Optimize PDF",
        None,
        False,
        notes="planned: Phase 2",
        install_hint="Requires Tesseract and OCRmyPDF",
    ),
    CapabilitySpec(
        "office-to-pdf",
        "Office to PDF",
        "Convert to PDF",
        None,
        False,
        notes="planned: Phase 2",
        install_hint="Requires LibreOffice",
    ),
    CapabilitySpec(
        "pdf-to-markdown",
        "PDF to Markdown",
        "Convert from PDF",
        None,
        False,
        notes="planned: Phase 3",
    ),
    CapabilitySpec(
        "markdown-to-pdf",
        "Markdown to PDF",
        "Convert to PDF",
        None,
        False,
        notes="planned: Phase 3",
        install_hint="Typst is a candidate renderer, but no adapter pipeline is implemented",
    ),
    CapabilitySpec(
        "pdf-to-pdfa",
        "PDF to PDF/A",
        "Convert from PDF",
        None,
        False,
        notes="planned: Phase 2",
        install_hint="Requires Ghostscript and veraPDF for validation",
    ),
    CapabilitySpec("redact", "Redact PDF", "Secure and inspect", None, False,
                   notes="planned: Phase 4"),
    CapabilitySpec("protect", "Protect/unlock PDF", "Secure and inspect", None, False,
                   notes="planned: Phase 4"),
    CapabilitySpec("forms", "PDF forms", "Edit PDF", None, False, notes="planned: Phase 4"),
    CapabilitySpec("sign", "Digital signatures", "Secure and inspect", None, False,
                   notes="planned: Phase 5"),
    CapabilitySpec("compare", "Compare PDFs", "Secure and inspect", None, False,
                   notes="planned: Phase 5"),
    CapabilitySpec("scan", "Scan to PDF", "Organize PDF", None, False, notes="planned: Phase 5"),
)


class EngineRegistry:
    """Owns every adapter and answers 'which engine runs this operation?'."""

    def __init__(self, engines: list[EngineAdapter] | None = None) -> None:
        self._engines: list[EngineAdapter] = engines or [
            PikepdfEngine(),
            PypdfEngine(),
            PdfiumEngine(),
            PillowEngine(),
            *build_external_engines(),
        ]

    @property
    def engines(self) -> list[EngineAdapter]:
        return list(self._engines)

    def get(self, name: str) -> EngineAdapter | None:
        for engine in self._engines:
            if engine.name == name:
                return engine
        return None

    def all_infos(self) -> list[EngineInfo]:
        return [engine.probe() for engine in self._engines]

    def engine_for(self, operation: str, *, preferred: str | None = None) -> EngineAdapter:
        """First available engine supporting ``operation``; honors --engine override."""
        if preferred is not None:
            engine = self.get(preferred)
            if engine is None:
                raise EngineUnavailableError(operation, [f"unknown engine {preferred!r}"])
            info = engine.probe()
            if not engine.supports(operation):
                raise EngineUnavailableError(
                    operation, [f"{preferred} does not support this operation"]
                )
            if not info.available:
                raise EngineUnavailableError(operation, [info.install_hint or preferred])
            return engine
        hints: list[str] = []
        for engine in self._engines:
            if not engine.supports(operation):
                continue
            info = engine.probe()
            if info.available:
                return engine
            if info.install_hint:
                hints.append(info.install_hint)
        raise EngineUnavailableError(operation, hints)

    def fallback_engine_name(self, operation: str, primary: str) -> str | None:
        for engine in self._engines:
            if engine.name != primary and engine.supports(operation) and engine.probe().available:
                return engine.name
        return None

    def capabilities(self) -> list[Capability]:
        """Honest capability list for doctor, API, and UI feature gating."""
        results: list[Capability] = []
        for spec in CAPABILITY_SPECS:
            missing: list[str] = []
            engines_used: list[str] = []
            available = spec.implemented
            if spec.implemented and spec.operation is not None:
                try:
                    engine = self.engine_for(spec.operation)
                    engines_used.append(engine.name)
                except EngineUnavailableError as exc:
                    available = False
                    missing.extend(exc.hints or [f"engine for {spec.operation}"])
            elif not spec.implemented:
                available = False
                missing.append("not implemented in this build")
            for extra in spec.extra_engines:
                extra_engine = self.get(extra)
                if extra_engine is None or not extra_engine.probe().available:
                    available = False
                    missing.append(extra)
            results.append(
                Capability(
                    id=spec.id,
                    title=spec.title,
                    category=spec.category,
                    available=available,
                    engines=engines_used,
                    missing_requirements=missing,
                    install_hint=spec.install_hint,
                    notes=spec.notes,
                )
            )
        return results


_default_registry: EngineRegistry | None = None


def default_registry() -> EngineRegistry:
    global _default_registry
    if _default_registry is None:
        _default_registry = EngineRegistry()
    return _default_registry
