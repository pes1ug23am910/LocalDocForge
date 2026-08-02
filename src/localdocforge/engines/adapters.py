"""Concrete engine adapters: Python-library engines and external executables."""

from __future__ import annotations

from functools import cache

from localdocforge.domain.models import EngineInfo, EngineKind
from localdocforge.engines.base import EngineAdapter
from localdocforge.security.subproc import ToolError, ToolTimeout, find_executable, run_tool

# Operation ids (shared vocabulary between operations, registry, CLI, and API).
OP_MERGE = "merge"
OP_SPLIT = "split"
OP_REMOVE_PAGES = "remove-pages"
OP_EXTRACT_PAGES = "extract-pages"
OP_ORGANIZE = "organize"
OP_ROTATE = "rotate"
OP_CROP = "crop"
OP_INSPECT = "inspect"
OP_COMPRESS = "compress"
OP_RENDER = "render"
OP_PDF_TO_IMAGES = "pdf-to-images"
OP_IMAGES_TO_PDF = "images-to-pdf"

_STRUCTURAL_OPS = frozenset(
    {
        OP_MERGE,
        OP_SPLIT,
        OP_REMOVE_PAGES,
        OP_EXTRACT_PAGES,
        OP_ORGANIZE,
        OP_ROTATE,
        OP_CROP,
        OP_INSPECT,
        OP_COMPRESS,
    }
)


class PikepdfEngine(EngineAdapter):
    """Primary structural PDF engine (libqpdf via pikepdf, MPL-2.0)."""

    name = "pikepdf"

    @cache  # noqa: B019 - adapters are process-lifetime singletons
    def probe(self) -> EngineInfo:
        try:
            import pikepdf

            return EngineInfo(
                name=self.name,
                kind=EngineKind.PYTHON_LIBRARY,
                available=True,
                version=pikepdf.__version__,
                license="MPL-2.0",
                notes=f"qpdf library {pikepdf.__libqpdf_version__}",
            )
        except Exception as exc:  # pragma: no cover - present in this environment
            return EngineInfo(
                name=self.name,
                kind=EngineKind.PYTHON_LIBRARY,
                available=False,
                notes=f"import failed: {exc}",
                install_hint="pip install pikepdf",
            )

    def supported_operations(self) -> frozenset[str]:
        return _STRUCTURAL_OPS


class PypdfEngine(EngineAdapter):
    """Installed pure-Python PDF library, not yet wired as an operation engine."""

    name = "pypdf"

    @cache  # noqa: B019
    def probe(self) -> EngineInfo:
        try:
            import pypdf

            return EngineInfo(
                name=self.name,
                kind=EngineKind.PYTHON_LIBRARY,
                available=True,
                version=pypdf.__version__,
                license="BSD-3-Clause",
            )
        except Exception as exc:  # pragma: no cover
            return EngineInfo(
                name=self.name,
                kind=EngineKind.PYTHON_LIBRARY,
                available=False,
                notes=f"import failed: {exc}",
                install_hint="pip install pypdf",
            )

    def supported_operations(self) -> frozenset[str]:
        # Probing an installed library is not an implementation. Operations
        # currently call pikepdf-specific APIs, so advertising pypdf here as a
        # fallback would make capability selection lie when pikepdf is absent.
        return frozenset()


class PdfiumEngine(EngineAdapter):
    """PDF renderer (PDFium via pypdfium2; Apache-2.0/BSD-3-Clause)."""

    name = "pdfium"

    @cache  # noqa: B019
    def probe(self) -> EngineInfo:
        try:
            import pypdfium2

            version = getattr(pypdfium2, "PYPDFIUM_INFO", None) or pypdfium2.version.PYPDFIUM_INFO
            return EngineInfo(
                name=self.name,
                kind=EngineKind.PYTHON_LIBRARY,
                available=True,
                version=str(version),
                license="Apache-2.0 OR BSD-3-Clause",
                notes="used for render validation, previews, and PDF-to-image export",
            )
        except Exception as exc:  # pragma: no cover
            return EngineInfo(
                name=self.name,
                kind=EngineKind.PYTHON_LIBRARY,
                available=False,
                notes=f"import failed: {exc}",
                install_hint="pip install pypdfium2",
            )

    def supported_operations(self) -> frozenset[str]:
        return frozenset({OP_RENDER, OP_PDF_TO_IMAGES})


class PillowEngine(EngineAdapter):
    """Image codec engine (Pillow, MIT-CMU)."""

    name = "pillow"

    @cache  # noqa: B019
    def probe(self) -> EngineInfo:
        try:
            import PIL

            return EngineInfo(
                name=self.name,
                kind=EngineKind.PYTHON_LIBRARY,
                available=True,
                version=PIL.__version__,
                license="MIT-CMU",
            )
        except Exception as exc:  # pragma: no cover
            return EngineInfo(
                name=self.name,
                kind=EngineKind.PYTHON_LIBRARY,
                available=False,
                notes=f"import failed: {exc}",
                install_hint="pip install pillow",
            )

    def supported_operations(self) -> frozenset[str]:
        return frozenset({OP_IMAGES_TO_PDF})


class ExternalToolEngine(EngineAdapter):
    """Adapter for an optional external executable, probe-only until its
    operations are implemented. Probing runs ``<tool> --version`` under the
    hardened subprocess runner."""

    def __init__(
        self,
        name: str,
        *,
        version_args: list[str],
        license_name: str,
        install_hint_windows: str,
        operations: frozenset[str] = frozenset(),
        version_line: int = 0,
    ) -> None:
        self.name = name
        self._version_args = version_args
        self._license = license_name
        self._install_hint = install_hint_windows
        self._operations = operations
        self._version_line = version_line
        self._probe_cache: EngineInfo | None = None

    def probe(self) -> EngineInfo:
        if self._probe_cache is not None:
            return self._probe_cache
        path = None
        try:
            path = find_executable(self.name)
        except ToolError:
            path = None
        if path is None:
            info = EngineInfo(
                name=self.name,
                kind=EngineKind.EXECUTABLE,
                available=False,
                license=self._license,
                install_hint=self._install_hint,
            )
        else:
            try:
                result = run_tool(self.name, self._version_args, timeout=20.0)
                lines = [line.strip() for line in result.output.splitlines() if line.strip()]
                version = lines[self._version_line] if lines else None
                info = EngineInfo(
                    name=self.name,
                    kind=EngineKind.EXECUTABLE,
                    available=result.returncode == 0,
                    version=version,
                    path=path,
                    license=self._license,
                    notes="" if result.returncode == 0 else "version probe failed",
                    install_hint="" if result.returncode == 0 else self._install_hint,
                )
            except (ToolError, ToolTimeout) as exc:
                info = EngineInfo(
                    name=self.name,
                    kind=EngineKind.EXECUTABLE,
                    available=False,
                    path=path,
                    license=self._license,
                    notes=str(exc),
                    install_hint=self._install_hint,
                )
        self._probe_cache = info
        return info

    def supported_operations(self) -> frozenset[str]:
        return self._operations


def build_external_engines() -> list[ExternalToolEngine]:
    """External engines LocalDocForge knows how to use or plans to use.

    Operations stay empty until the corresponding pipeline is implemented and
    tested — an installed binary alone must not light up a feature.
    """
    return [
        ExternalToolEngine(
            "qpdf",
            version_args=["--version"],
            license_name="Apache-2.0",
            install_hint_windows="winget install qpdf.qpdf",
        ),
        ExternalToolEngine(
            "tesseract",
            version_args=["--version"],
            license_name="Apache-2.0",
            install_hint_windows="winget install UB-Mannheim.TesseractOCR",
        ),
        ExternalToolEngine(
            "ocrmypdf",
            version_args=["--version"],
            license_name="MPL-2.0",
            install_hint_windows="pip install ocrmypdf (requires Tesseract and Ghostscript)",
        ),
        ExternalToolEngine(
            "ghostscript",
            version_args=["--version"],
            license_name="AGPL-3.0 (external tool, invoked, never bundled)",
            install_hint_windows="winget install ArtifexSoftware.GhostScript",
        ),
        ExternalToolEngine(
            "libreoffice",
            version_args=["--version"],
            license_name="MPL-2.0",
            install_hint_windows="winget install TheDocumentFoundation.LibreOffice",
        ),
        ExternalToolEngine(
            "pandoc",
            version_args=["--version"],
            license_name="GPL-2.0-or-later (external tool, invoked, never bundled)",
            install_hint_windows="winget install JohnMacFarlane.Pandoc",
        ),
        ExternalToolEngine(
            "typst",
            version_args=["--version"],
            license_name="Apache-2.0",
            install_hint_windows="winget install Typst.Typst",
        ),
        ExternalToolEngine(
            "verapdf",
            version_args=["--version"],
            license_name="GPL-3.0-or-later OR MPL-2.0+ (external tool)",
            install_hint_windows="Download the installer from https://verapdf.org/software/",
        ),
    ]
