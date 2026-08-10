"""Concrete engine adapters: Python-library engines and external executables."""

from __future__ import annotations

import os
import re
import stat
from functools import cache
from pathlib import Path
from tempfile import TemporaryDirectory

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
OP_PDF_TO_MD = "pdf-to-md"
OP_MD_TO_PDF = "md-to-pdf"
OP_OCR = "ocr"
OP_IMAGES_TO_PDF = "images-to-pdf"
OP_CONVERT_IMAGES = "convert-images"

_GHOSTSCRIPT_PROBE_MAX_PDF_BYTES = 4 * 1024 * 1024
_GHOSTSCRIPT_PROBE_MAX_WORKSPACE_BYTES = 16 * 1024 * 1024
_GHOSTSCRIPT_PROBE_MAX_WORKSPACE_ENTRIES = 256


def _bounded_local_tree_size(root: Path, *, max_bytes: int, max_entries: int) -> int | None:
    """Measure a small private tree without following links or reparse points."""
    from localdocforge.security.paths import PathSecurityError, validate_path_before_access

    total = 0
    seen = 0
    pending = [root]
    try:
        while pending:
            directory = pending.pop()
            with os.scandir(directory) as entries:
                for entry in entries:
                    seen += 1
                    if seen > max_entries:
                        return None
                    path = Path(entry.path)
                    validate_path_before_access(
                        path,
                        what="Ghostscript probe workspace entry",
                        require_local=True,
                        reject_reparse=True,
                    )
                    if entry.is_symlink():
                        return None
                    metadata = entry.stat(follow_symlinks=False)
                    if stat.S_ISDIR(metadata.st_mode):
                        pending.append(path)
                    elif stat.S_ISREG(metadata.st_mode):
                        total += metadata.st_size
                        if total > max_bytes:
                            return None
                    else:
                        return None
    except (OSError, PathSecurityError):
        return None
    return total


def _validated_local_probe_temp_root() -> Path | None:
    """Return an existing local scratch root without touching a remote path."""
    from localdocforge.security.paths import PathSecurityError, validate_path_before_access

    raw_candidate = next(
        (
            value
            for key in ("TMPDIR", "TEMP", "TMP")
            if (value := os.environ.get(key))
        ),
        None,
    )
    if raw_candidate is None:
        if os.name == "nt":
            local_app_data = os.environ.get("LOCALAPPDATA")
            system_root = os.environ.get("SYSTEMROOT") or os.environ.get("WINDIR")
            raw_candidate = (
                str(Path(local_app_data) / "Temp")
                if local_app_data
                else (str(Path(system_root) / "Temp") if system_root else None)
            )
        else:
            raw_candidate = str(Path(os.sep) / "tmp")
    if raw_candidate is None:
        return None
    candidate = Path(os.path.expandvars(raw_candidate))
    if not candidate.is_absolute():
        return None
    try:
        validate_path_before_access(
            candidate,
            what="Ghostscript probe temporary root",
            require_local=True,
            reject_reparse=True,
        )
        resolved = candidate.resolve(strict=True)
        validate_path_before_access(
            resolved,
            what="Ghostscript probe temporary root",
            require_local=True,
            reject_reparse=True,
        )
    except (OSError, RuntimeError, PathSecurityError):
        return None
    return (
        resolved
        if resolved.is_dir() and os.access(resolved, os.W_OK | os.X_OK)
        else None
    )


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
                notes=(
                    "used for render validation, previews, PDF-to-image export, "
                    "and text extraction"
                ),
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
        return frozenset({OP_RENDER, OP_PDF_TO_IMAGES, OP_PDF_TO_MD})


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
        return frozenset({OP_IMAGES_TO_PDF, OP_CONVERT_IMAGES})


class PiHeifEngine(EngineAdapter):
    """HEIF/HEIC decode plugin for Pillow (pi-heif, BSD-3-Clause wrapper).

    A codec plugin, not an operation engine: Pillow runs the image
    operations and calls into this plugin for HEIF input, so
    ``supported_operations`` stays empty and capabilities that need HEIF
    decoding list this engine in ``extra_engines`` instead.
    """

    name = "pi-heif"

    @cache  # noqa: B019
    def probe(self) -> EngineInfo:
        try:
            import pi_heif

            info = pi_heif.libheif_info()
            return EngineInfo(
                name=self.name,
                kind=EngineKind.PYTHON_LIBRARY,
                available=True,
                version=pi_heif.__version__,
                license="BSD-3-Clause (bundles LGPL-3.0-or-later libheif/libde265)",
                notes=f"libheif {info.get('libheif', 'unknown')}, decode-only",
            )
        except Exception as exc:
            return EngineInfo(
                name=self.name,
                kind=EngineKind.PYTHON_LIBRARY,
                available=False,
                notes=f"import failed: {exc}",
                install_hint="pip install pi-heif",
            )

    def supported_operations(self) -> frozenset[str]:
        return frozenset()


class ExternalToolEngine(EngineAdapter):
    """Adapter for an optional external executable.

    Operations remain empty until a tested pipeline is wired. Probing runs
    ``<tool> --version`` through the hardened runner and can enforce a minimum
    semantic version without adding a packaging dependency.
    """

    def __init__(
        self,
        name: str,
        *,
        version_args: list[str],
        license_name: str,
        install_hint_windows: str,
        operations: frozenset[str] = frozenset(),
        version_line: int = 0,
        minimum_version: tuple[int, int, int] | None = None,
        rejected_versions: frozenset[tuple[int, ...]] = frozenset(),
    ) -> None:
        self.name = name
        self._version_args = version_args
        self._license = license_name
        self._install_hint = install_hint_windows
        self._operations = operations
        self._version_line = version_line
        self._minimum_version = minimum_version
        self._rejected_versions = rejected_versions
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
                result = run_tool(
                    self.name,
                    self._version_args,
                    timeout=20.0,
                    expected_executable=path,
                )
                lines = [line.strip() for line in result.output.splitlines() if line.strip()]
                version = lines[self._version_line] if lines else None
                version_match = re.search(
                    r"(?<!\d)(\d+(?:\.\d+){1,3})(?!\d)",
                    version or "",
                )
                parsed_version = (
                    tuple(int(part) for part in version_match.group(1).split("."))
                    if version_match is not None
                    else None
                )
                minimum_ok = self._minimum_version is None or (
                    parsed_version is not None and parsed_version >= self._minimum_version
                )
                version_allowed = parsed_version not in self._rejected_versions
                if result.returncode != 0:
                    notes = "version probe failed"
                elif not minimum_ok:
                    required = ".".join(str(part) for part in self._minimum_version or ())
                    notes = f"requires version >= {required}; detected {version or 'unknown'}"
                elif not version_allowed:
                    notes = f"version {version or 'unknown'} is rejected for known regressions"
                else:
                    notes = ""
                info = EngineInfo(
                    name=self.name,
                    kind=EngineKind.EXECUTABLE,
                    available=result.returncode == 0 and minimum_ok and version_allowed,
                    version=version,
                    path=path,
                    license=self._license,
                    notes=notes,
                    install_hint=(
                        ""
                        if result.returncode == 0 and minimum_ok and version_allowed
                        else self._install_hint
                    ),
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


class GhostscriptPresenceEngine(EngineAdapter):
    """Probe Ghostscript through OCRmyPDF without ever launching it directly."""

    name = "ghostscript"

    def __init__(self) -> None:
        self._probe_cache: EngineInfo | None = None

    def probe(self) -> EngineInfo:
        if self._probe_cache is not None:
            return self._probe_cache
        paths: dict[str, str] = {}
        try:
            for name in (self.name, "ocrmypdf", "tesseract"):
                resolved = find_executable(name)
                if resolved is not None:
                    paths[name] = resolved
        except ToolError:
            paths = {}
        path = paths.get(self.name)
        if path is None:
            info = EngineInfo(
                name=self.name,
                kind=EngineKind.EXECUTABLE,
                available=False,
                license="AGPL-3.0 (external OCRmyPDF child; never bundled)",
                notes=("Ghostscript was not found; LocalDocForge never launches it directly"),
                install_hint=(
                    "Install 64-bit Ghostscript from the official Artifex release page; "
                    "OCRmyPDF invokes it as a separate child process"
                ),
            )
        else:
            version = None
            for part in reversed(Path(path).parts):
                match = re.fullmatch(
                    r"(?:gs)?(\d+\.\d+(?:\.\d+)?)",
                    part,
                    flags=re.IGNORECASE,
                )
                if match is not None:
                    version = match.group(1)
                    break
            ocrmypdf_path = paths.get("ocrmypdf")
            tesseract_path = paths.get("tesseract")
            supported = False
            probe_error = None
            if ocrmypdf_path is None or tesseract_path is None:
                probe_error = "OCRmyPDF and Tesseract are required for the delegated live probe"
            else:
                temp_root = _validated_local_probe_temp_root()
                if temp_root is None:
                    probe_error = (
                        "A confirmed local temporary directory is required for the delegated "
                        "Ghostscript probe"
                    )
                else:
                    try:
                        probe_valid = False
                        with TemporaryDirectory(
                            prefix="ldf-gs-probe-",
                            dir=temp_root,
                        ) as temporary:
                            workspace = Path(temporary).resolve()
                            probe_input = workspace / "probe-input.pdf"
                            output = workspace / "probe-output.pdf"
                            from PIL import Image

                            image = Image.new("L", (64, 64), color=255)
                            try:
                                image.paste(0, (16, 16, 48, 48))
                                image.save(probe_input, format="PDF", resolution=72.0)
                            finally:
                                image.close()
                            result = run_tool(
                                "ocrmypdf",
                                [
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
                                    str(probe_input),
                                    str(output),
                                ],
                                timeout=20.0,
                                cwd=workspace,
                                max_output_bytes=64 * 1024,
                                env_extra={
                                    "HOME": str(workspace),
                                    "USERPROFILE": str(workspace),
                                    "TEMP": str(workspace),
                                    "TMP": str(workspace),
                                    "TMPDIR": str(workspace),
                                },
                                child_path_tools=("tesseract", "ghostscript"),
                                expected_executable=ocrmypdf_path,
                                expected_child_executables={
                                    "tesseract": tesseract_path,
                                    "ghostscript": path,
                                },
                            )
                            workspace_size = _bounded_local_tree_size(
                                workspace,
                                max_bytes=_GHOSTSCRIPT_PROBE_MAX_WORKSPACE_BYTES,
                                max_entries=_GHOSTSCRIPT_PROBE_MAX_WORKSPACE_ENTRIES,
                            )
                            if (
                                result.returncode == 0
                                and workspace_size is not None
                                and output.is_file()
                                and not output.is_symlink()
                            ):
                                output_size = output.stat().st_size
                                if 0 < output_size <= _GHOSTSCRIPT_PROBE_MAX_PDF_BYTES:
                                    from localdocforge.validation.pdf_checks import validate_pdf

                                    validation = validate_pdf(
                                        output,
                                        expected_pages=1,
                                        render_pages=False,
                                    )
                                    probe_valid = validation.passed
                        supported = probe_valid
                    except Exception:  # noqa: BLE001 - probe contract must never raise
                        supported = False
                        probe_error = "OCRmyPDF-mediated Ghostscript probe could not run safely"
            info = EngineInfo(
                name=self.name,
                kind=EngineKind.EXECUTABLE,
                available=supported,
                version=version,
                path=path,
                license="AGPL-3.0 (external OCRmyPDF child; never bundled)",
                notes=(
                    "live one-page render/PDF-A probe delegated to OCRmyPDF; LocalDocForge "
                    "never launches Ghostscript directly"
                    if supported
                    else (
                        probe_error
                        or "OCRmyPDF rejected Ghostscript or another required OCR dependency"
                    )
                ),
                install_hint=(
                    "Install compatible Tesseract and 64-bit Ghostscript >=9.54, then rerun "
                    "ldf doctor; Ghostscript is invoked only by OCRmyPDF"
                    if not supported
                    else ""
                ),
            )
        self._probe_cache = info
        return info

    def supported_operations(self) -> frozenset[str]:
        return frozenset()


def build_external_engines() -> list[EngineAdapter]:
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
            minimum_version=(4, 1, 1),
            rejected_versions=frozenset({(5, 4, 0)}),
        ),
        ExternalToolEngine(
            "ocrmypdf",
            version_args=["--version"],
            license_name="MPL-2.0 AND Apache-2.0 AND OFL-1.1 AND Zlib",
            install_hint_windows=(
                "Install LocalDocForge's locked runtime dependencies, Tesseract, and Ghostscript"
            ),
            operations=frozenset({OP_OCR}),
            minimum_version=(17, 8, 1),
        ),
        GhostscriptPresenceEngine(),
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
            operations=frozenset({OP_MD_TO_PDF}),
            minimum_version=(0, 15, 1),
        ),
        ExternalToolEngine(
            "verapdf",
            version_args=["--version"],
            license_name="GPL-3.0-or-later OR MPL-2.0+ (external tool)",
            install_hint_windows="Download the installer from https://verapdf.org/software/",
        ),
    ]
