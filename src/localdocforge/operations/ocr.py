"""Bounded OCR pipeline through the separately executed OCRmyPDF engine."""

from __future__ import annotations

import math
import os
import re
import shutil
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from localdocforge.config.settings import Settings, get_settings
from localdocforge.domain.models import (
    ArtifactKind,
    ConversionReport,
    FidelityBasis,
    FidelityImpact,
    FidelityWarning,
    InputArtifact,
    JobCancelled,
    JobContext,
    ProgressCallback,
    ResourceLimits,
    SecurityWarning,
    ValidationCheck,
    ValidationResult,
    WarningSeverity,
)
from localdocforge.engines.adapters import OP_OCR
from localdocforge.engines.base import EngineUnavailableError
from localdocforge.engines.registry import default_registry
from localdocforge.jobs.workspace import CollisionPolicy
from localdocforge.operations import organize as organize_ops
from localdocforge.operations.text import inspect_page_text_stats
from localdocforge.pipelines.runner import (
    CandidateOutput,
    ExecuteResult,
    PipelineError,
    run_pipeline,
)
from localdocforge.security.subproc import ToolError, ToolResult, ToolTimeout, run_tool
from localdocforge.validation.pdf_checks import PDF_VALIDATION_RENDER_SCALE, validate_pdf

OCR_TEXT_APPROXIMATE = "ocr-text-approximate"
OCR_FORCE_RASTERIZED = "ocr-force-rasterized"
OCR_SIDECAR_OMITS_EXISTING_TEXT = "ocr-sidecar-omits-existing-text"
OCR_ENGINE_PAGE_SKIPPED = "ocr-engine-page-skipped"

_LANGUAGE_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*(?:/[A-Za-z0-9][A-Za-z0-9_.-]*)?$")
_WORD_TOKEN = re.compile(r"\w+", flags=re.UNICODE)
_MAX_LANGUAGES = 16
_MAX_LANGUAGE_ARGUMENT_CHARS = 256
_MAX_TOOL_OUTPUT_BYTES = 256 * 1024
_MAX_OCR_TIMEOUT_SECONDS = 900.0
_MAX_TESSERACT_PAGE_SECONDS = 300.0
_EXPECTED_TOKEN_LIMIT = 16
_EXPECTED_TOKEN_READ_CHARS = 1_000_000
_MAX_EXPECTED_TOKEN_CHARS = 128
_OCR_RENDER_MEMORY_BYTES_PER_PIXEL = 16
_SIDECAR_READ_CHARS = 64 * 1024
_SIDECAR_PAGE_SEPARATOR = "\f"
_FAILED_PAGE_MARKER = "[skipped page]"
_OCR_SKIPPED_PAGE_MARKER = re.compile(
    r"\[OCR skipped on page\(s\) ([1-9]\d{0,9})(?:-([1-9]\d{0,9}))?\]"
)
_MAX_INTERNAL_MARKER_CHARS = 64
_MAX_INTERNAL_MARKER_PAGE_SPAN = 10_000
_TRAILING_WORD_TOKEN = re.compile(r"\w+$", flags=re.UNICODE)


class OcrToolFailure(RuntimeError):
    """Safe, typed OCRmyPDF failure used by CLI/API exit mapping."""

    def __init__(self, returncode: int | None, ldf_exit_code: int, message: str) -> None:
        self.returncode = returncode
        self.ldf_exit_code = ldf_exit_code
        super().__init__(message)


class OcrToolTimeout(JobCancelled):
    """Typed engine timeout retained through the pipeline/worker boundary."""


@dataclass(frozen=True)
class _OcrSidecarSample:
    tokens: tuple[str, ...]
    failed_page_markers: int
    has_non_marker_content: bool


def _ocr_skipped_page_range(record: str) -> tuple[int, int] | None:
    match = _OCR_SKIPPED_PAGE_MARKER.fullmatch(record)
    if match is None:
        return None
    first = int(match.group(1))
    last = int(match.group(2)) if match.group(2) is not None else first
    if last < first:
        return None
    return first, last


@dataclass
class OcrOptions:
    language: str = "eng"
    sidecar: Path | None = None
    skip_text: bool = False
    force_ocr: bool = False
    collision: CollisionPolicy | None = None
    settings: Settings | None = None
    progress: ProgressCallback | None = None
    password: str | None = None


def _language_codes(value: str) -> tuple[str, ...]:
    normalized = value.strip()
    codes = tuple(normalized.split("+"))
    if (
        not normalized
        or len(normalized) > _MAX_LANGUAGE_ARGUMENT_CHARS
        or len(codes) > _MAX_LANGUAGES
        or any(not _LANGUAGE_TOKEN.fullmatch(code) for code in codes)
    ):
        raise PipelineError(
            "OCR language must be one to sixteen Tesseract codes joined by '+', "
            "for example eng or eng+deu"
        )
    return codes


def _require_ocr_engines():
    registry = default_registry()
    missing: list[str] = []
    infos = {}
    for name in ("ocrmypdf", "tesseract", "ghostscript"):
        engine = registry.get(name)
        if engine is None:
            missing.append(name)
            continue
        info = engine.probe()
        infos[name] = info
        if not info.available:
            missing.append(info.install_hint or name)
        elif not info.path:
            missing.append(f"{name} probe did not report a trusted executable path")
    primary = registry.get("ocrmypdf")
    if primary is None or not primary.supports(OP_OCR):
        missing.append("an OCRmyPDF adapter that supports OCR")
    if missing:
        requirement = "; ".join(dict.fromkeys(missing))
        raise EngineUnavailableError(OP_OCR, [f"all OCR requirements: {requirement}"])
    assert primary is not None
    return primary, infos


def _installed_tesseract_languages(expected_executable: str) -> frozenset[str]:
    try:
        result = run_tool(
            "tesseract",
            ["--list-langs"],
            timeout=20.0,
            max_output_bytes=64 * 1024,
            expected_executable=expected_executable,
        )
    except (ToolError, ToolTimeout) as exc:
        raise EngineUnavailableError(
            OP_OCR,
            ["Tesseract could not list its language packs; reinstall it and rerun ldf doctor"],
        ) from exc
    if result.returncode != 0:
        raise EngineUnavailableError(
            OP_OCR,
            ["Tesseract language-pack discovery failed; reinstall it and rerun ldf doctor"],
        )
    languages = {
        line.strip()
        for line in result.output.splitlines()
        if _LANGUAGE_TOKEN.fullmatch(line.strip())
    }
    return frozenset(languages)


def _require_languages(codes: tuple[str, ...], expected_executable: str) -> None:
    installed = _installed_tesseract_languages(expected_executable)
    missing = [code for code in codes if code not in installed]
    if not missing:
        return
    raise EngineUnavailableError(
        OP_OCR,
        [
            "Tesseract language pack(s) missing: "
            + ", ".join(missing)
            + "; install the matching traineddata files in Tesseract's tessdata directory"
        ],
    )


def _remaining_timeout(context: JobContext) -> float:
    configured = context.limits.timeout_seconds
    elapsed = (datetime.now(UTC) - context.started_at).total_seconds()
    safety_remaining = _MAX_OCR_TIMEOUT_SECONDS - elapsed
    if safety_remaining <= 0:
        raise JobCancelled(
            f"Job {context.job_id} exceeded the {_MAX_OCR_TIMEOUT_SECONDS:g}s OCR safety limit"
        )
    if configured is None:
        return safety_remaining
    remaining = configured - elapsed
    if remaining <= 0:
        raise JobCancelled(f"Job {context.job_id} exceeded its {configured:g}s time limit")
    return min(remaining, safety_remaining)


def _workspace_size(root: Path) -> int:
    total = 0
    for path in root.rglob("*"):
        if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
            raise PipelineError("OCRmyPDF created an unexpected linked workspace entry")
        if path.is_file():
            total += path.stat().st_size
    return total


def _check_temporary_limit(root: Path, limit: int | None) -> None:
    try:
        used = _workspace_size(root)
    except OSError as exc:
        raise PipelineError("OCR workspace contents could not be inspected safely") from exc
    if limit is None:
        return
    if used > limit:
        raise PipelineError(
            f"OCR temporary files total {used:,} bytes, over the configured limit of {limit:,}"
        )


def _snapshot_pdf(
    source: Path,
    destination: Path,
    *,
    password: str | None,
) -> tuple[int, bool, organize_ops._SignatureAssessment]:
    import pikepdf

    try:
        with source.open("rb") as source_stream:
            opened = os.fstat(source_stream.fileno())
            try:
                pdf = pikepdf.open(source_stream, password=password or "")
            except pikepdf.PasswordError as exc:
                if password:
                    raise organize_ops.EncryptedInputError("Wrong password for OCR input.") from exc
                raise organize_ops.EncryptedInputError(
                    "OCR input is encrypted. Provide the password to process it."
                ) from None
            with pdf:
                syntax_issues = list(pdf.check_pdf_syntax())
                if syntax_issues:
                    raise PipelineError(
                        f"OCR input has {len(syntax_issues)} structural syntax warning(s); "
                        "automatic repair is not implemented"
                    )
                page_count = len(pdf.pages)
                encrypted = bool(pdf.is_encrypted)
                signature_assessment = organize_ops._assess_signature_fields(pdf)
                if encrypted:
                    pdf.save(destination)
                else:
                    source_stream.seek(0)
                    with destination.open("xb") as destination_stream:
                        shutil.copyfileobj(source_stream, destination_stream, length=1024 * 1024)
            closed = os.fstat(source_stream.fileno())
            if opened.st_size != closed.st_size or opened.st_mtime_ns != closed.st_mtime_ns:
                raise PipelineError("OCR input changed while its private snapshot was created")
    except organize_ops.EncryptedInputError:
        raise
    except PipelineError:
        raise
    except (OSError, pikepdf.PdfError) as exc:
        raise PipelineError("OCR input could not be copied into the private job workspace") from exc
    return page_count, encrypted, signature_assessment


def _normalize_sidecar(source: Path, destination: Path) -> None:
    if not source.is_file():
        raise PipelineError("OCRmyPDF reported success without producing its required sidecar")
    try:
        with source.open("r", encoding="utf-8", errors="strict", newline=None) as input_stream:
            with destination.open("x", encoding="utf-8", errors="strict", newline="\n") as output:
                while chunk := input_stream.read(64 * 1024):
                    output.write(chunk)
    except (OSError, UnicodeError) as exc:
        raise PipelineError("OCRmyPDF sidecar could not be normalized as strict UTF-8") from exc


def _validate_sidecar(path: Path) -> ValidationResult:
    checks: list[ValidationCheck] = []
    exists = path.is_file()
    checks.append(
        ValidationCheck(
            name="file-exists",
            passed=exists,
            detail=path.name if exists else "missing",
        )
    )
    if not exists:
        return ValidationResult.combine(checks)
    lf_normalized = True
    try:
        with path.open("r", encoding="utf-8", errors="strict", newline="") as stream:
            while chunk := stream.read(64 * 1024):
                if "\r" in chunk:
                    lf_normalized = False
        valid_utf8 = True
    except (OSError, UnicodeError):
        valid_utf8 = False
    checks.append(
        ValidationCheck(
            name="utf8",
            passed=valid_utf8,
            detail="strict UTF-8" if valid_utf8 else "failed",
        )
    )
    checks.append(
        ValidationCheck(
            name="lf-newlines",
            passed=valid_utf8 and lf_normalized,
            detail="LF normalized" if valid_utf8 and lf_normalized else "not normalized",
        )
    )
    return ValidationResult.combine(checks)


def _sample_ocr_sidecar(
    sidecar: Path,
    *,
    check_cancelled: Callable[[], None] | None = None,
    expected_pages: int | None = None,
    ineligible_pages: frozenset[int] = frozenset(),
) -> _OcrSidecarSample:
    sample_parts: list[str] = []
    sample_chars = 0
    sample_truncated = False
    sample_separator_count = 0
    sample_ends_with_separator = False
    sample_partial_record: int | None = None
    sample_partial_is_marker: bool | None = None
    failed_pages: set[int] = set()
    has_non_marker_content = False
    record_index = 0
    page_cursor = 1
    record_has_non_whitespace = False
    record_marker_text = ""
    record_marker_overflow = False

    def finish_page_record() -> None:
        nonlocal record_index, page_cursor
        nonlocal sample_partial_is_marker, has_non_marker_content
        nonlocal record_has_non_whitespace, record_marker_text, record_marker_overflow
        failed_marker = not record_marker_overflow and record_marker_text == _FAILED_PAGE_MARKER
        marker_range = (
            _ocr_skipped_page_range(record_marker_text) if not record_marker_overflow else None
        )
        if marker_range is not None:
            first, last = marker_range
            valid_span = last - first + 1 <= _MAX_INTERNAL_MARKER_PAGE_SPAN
            valid_bounds = expected_pages is None or last <= expected_pages
            valid_position = first == page_cursor
            if not (valid_span and valid_bounds and valid_position):
                raise PipelineError(
                    "OCRmyPDF sidecar contained an invalid or discontinuous page range"
                )
            ocr_marker = True
        else:
            ocr_marker = False
        if failed_marker:
            if page_cursor not in ineligible_pages:
                failed_pages.add(page_cursor)
            page_cursor += 1
        elif ocr_marker and marker_range is not None:
            first, last = marker_range
            failed_pages.update(
                page for page in range(first, last + 1) if page not in ineligible_pages
            )
            page_cursor = max(page_cursor, last + 1)
        else:
            page_cursor += 1
        if record_has_non_whitespace and not (failed_marker or ocr_marker):
            has_non_marker_content = True
        if sample_partial_record == record_index:
            sample_partial_is_marker = failed_marker or ocr_marker
        record_index += 1
        record_has_non_whitespace = False
        record_marker_text = ""
        record_marker_overflow = False

    def scan_page_fragment(fragment: str) -> None:
        nonlocal record_has_non_whitespace
        nonlocal record_marker_text, record_marker_overflow
        if not record_has_non_whitespace and any(not character.isspace() for character in fragment):
            record_has_non_whitespace = True
        marker_space = _MAX_INTERNAL_MARKER_CHARS - len(record_marker_text)
        if marker_space > 0:
            record_marker_text += fragment[:marker_space]
        if len(fragment) > marker_space:
            record_marker_overflow = True

    try:
        with sidecar.open("r", encoding="utf-8", errors="strict", newline="") as stream:
            while chunk := stream.read(_SIDECAR_READ_CHARS):
                if check_cancelled is not None:
                    check_cancelled()
                if sample_chars < _EXPECTED_TOKEN_READ_CHARS:
                    retained = chunk[: _EXPECTED_TOKEN_READ_CHARS - sample_chars]
                    sample_parts.append(retained)
                    sample_chars += len(retained)
                    sample_separator_count += retained.count(_SIDECAR_PAGE_SEPARATOR)
                    if retained:
                        sample_ends_with_separator = retained.endswith(_SIDECAR_PAGE_SEPARATOR)
                    if len(retained) < len(chunk):
                        sample_truncated = True
                        if not sample_ends_with_separator:
                            sample_partial_record = sample_separator_count
                else:
                    if not sample_truncated:
                        sample_truncated = True
                        if not sample_ends_with_separator:
                            sample_partial_record = sample_separator_count
                fragments = chunk.split(_SIDECAR_PAGE_SEPARATOR)
                for index, fragment in enumerate(fragments):
                    scan_page_fragment(fragment)
                    if index < len(fragments) - 1:
                        finish_page_record()
            finish_page_record()
        if expected_pages is not None and page_cursor != expected_pages + 1:
            raise PipelineError("OCRmyPDF sidecar page topology did not match the input PDF")
    except (OSError, UnicodeError) as exc:
        raise PipelineError("OCRmyPDF sidecar could not be sampled safely") from exc
    text = "".join(sample_parts)
    if sample_truncated and sample_partial_record is not None:
        last_separator = text.rfind(_SIDECAR_PAGE_SEPARATOR)
        last_complete_record = last_separator + 1 if last_separator >= 0 else 0
        if sample_partial_is_marker is None:
            raise PipelineError("OCRmyPDF sidecar sample boundary could not be classified safely")
        if sample_partial_is_marker:
            text = text[:last_complete_record]
        else:
            trailing_word = _TRAILING_WORD_TOKEN.search(text)
            if trailing_word is not None:
                text = text[: trailing_word.start()]
    filtered_records: list[str] = []
    for record in text.split(_SIDECAR_PAGE_SEPARATOR):
        if record == _FAILED_PAGE_MARKER:
            continue
        marker_range = _ocr_skipped_page_range(record)
        if marker_range is not None:
            first, last = marker_range
            valid_span = last - first + 1 <= _MAX_INTERNAL_MARKER_PAGE_SPAN
            valid_bounds = expected_pages is None or last <= expected_pages
            if valid_span and valid_bounds:
                continue
        filtered_records.append(record)
    filtered = "\n".join(filtered_records)
    normalized = unicodedata.normalize("NFKC", filtered).casefold()
    tokens: list[str] = []
    for match in _WORD_TOKEN.finditer(normalized):
        token = match.group(0)
        if len(token) > _MAX_EXPECTED_TOKEN_CHARS:
            continue
        if token not in tokens:
            tokens.append(token)
        if len(tokens) == _EXPECTED_TOKEN_LIMIT:
            break
    return _OcrSidecarSample(
        tokens=tuple(tokens),
        failed_page_markers=len(failed_pages),
        has_non_marker_content=has_non_marker_content,
    )


def _expected_ocr_tokens(sidecar: Path) -> tuple[str, ...]:
    return _sample_ocr_sidecar(sidecar).tokens


def _ocr_render_resource_check(
    path: Path,
    *,
    limits: ResourceLimits,
    check_cancelled: Callable[[], None] | None,
) -> ValidationCheck:
    import pypdfium2 as pdfium

    pixel_limits = [
        value
        for value in (
            limits.max_image_pixels,
            (
                limits.max_memory_bytes // _OCR_RENDER_MEMORY_BYTES_PER_PIXEL
                if limits.max_memory_bytes is not None
                else None
            ),
        )
        if value is not None
    ]
    pixel_limit = min(pixel_limits) if pixel_limits else None
    unsafe_pages: list[int] = []
    try:
        document = pdfium.PdfDocument(str(path))
        try:
            for index in range(len(document)):
                if check_cancelled is not None:
                    check_cancelled()
                page = document[index]
                try:
                    width, height = (float(value) for value in page.get_size())
                finally:
                    page.close()
                if (
                    not math.isfinite(width)
                    or not math.isfinite(height)
                    or width <= 0
                    or height <= 0
                ):
                    unsafe_pages.append(index + 1)
                    continue
                render_width = max(1, math.ceil(width * PDF_VALIDATION_RENDER_SCALE))
                render_height = max(1, math.ceil(height * PDF_VALIDATION_RENDER_SCALE))
                if pixel_limit is not None and render_width * render_height > pixel_limit:
                    unsafe_pages.append(index + 1)
            if check_cancelled is not None:
                check_cancelled()
        finally:
            document.close()
    except JobCancelled:
        raise
    except Exception as exc:
        return ValidationCheck(
            name="ocr-render-resource-limits",
            passed=False,
            detail=f"page geometry inspection failed ({type(exc).__name__})",
        )
    if unsafe_pages:
        shown = unsafe_pages[:16]
        omitted = len(unsafe_pages) - len(shown)
        suffix = f" (+{omitted} more)" if omitted else ""
        limit_detail = (
            f"effective limit {pixel_limit:,} pixels"
            if pixel_limit is not None
            else "invalid page geometry"
        )
        return ValidationCheck(
            name="ocr-render-resource-limits",
            passed=False,
            detail=f"{limit_detail}; unsafe pages {shown}{suffix}",
        )
    return ValidationCheck(
        name="ocr-render-resource-limits",
        passed=True,
        detail=(
            f"all pages within effective {pixel_limit:,}-pixel render limit"
            if pixel_limit is not None
            else "page geometry finite; configured pixel/memory limits disabled"
        ),
    )


def _pdf_contains_tokens(
    path: Path,
    expected: tuple[str, ...],
    *,
    limits: ResourceLimits,
    check_cancelled: Callable[[], None] | None = None,
) -> tuple[int, int]:
    import pypdfium2 as pdfium

    remaining = set(expected)
    decoded_bytes = 0
    document = pdfium.PdfDocument(str(path))
    try:
        for index in range(len(document)):
            if check_cancelled is not None:
                check_cancelled()
            page = document[index]
            try:
                textpage = page.get_textpage()
                try:
                    raw_char_count = textpage.count_chars()
                    decompressed_limit = limits.max_decompressed_bytes
                    if decompressed_limit is not None and raw_char_count > max(
                        0, decompressed_limit - decoded_bytes
                    ):
                        raise PipelineError(
                            "OCR output text exceeds the configured decompressed-text limit"
                        )
                    memory_limit = limits.max_memory_bytes
                    if memory_limit is not None and raw_char_count > memory_limit // 64:
                        raise PipelineError(
                            "OCR output text exceeds the configured extraction-memory preflight"
                        )
                    text = textpage.get_text_bounded(errors="strict")
                finally:
                    textpage.close()
            finally:
                page.close()
            decoded_bytes += len(text.encode("utf-8", errors="strict"))
            if (
                limits.max_decompressed_bytes is not None
                and decoded_bytes > limits.max_decompressed_bytes
            ):
                raise PipelineError(
                    "OCR output text exceeds the configured decompressed-text limit"
                )
            if remaining:
                normalized = unicodedata.normalize("NFKC", text).casefold()
                remaining.difference_update(
                    match.group(0) for match in _WORD_TOKEN.finditer(normalized)
                )
            if check_cancelled is not None:
                check_cancelled()
    finally:
        document.close()
    return len(expected) - len(remaining), len(expected)


def _ocr_pdf_validator(
    path: Path,
    *,
    expected_pages: int,
    expected_tokens: tuple[str, ...],
    sidecar_has_non_marker_content: bool = False,
    limits: ResourceLimits,
    check_cancelled: Callable[[], None] | None = None,
) -> ValidationResult:
    if check_cancelled is not None:
        check_cancelled()
    structural = validate_pdf(
        path,
        expected_pages=expected_pages,
        render_pages=False,
    )
    if not structural.passed:
        return structural
    render_resource = _ocr_render_resource_check(
        path,
        limits=limits,
        check_cancelled=check_cancelled,
    )
    if not render_resource.passed:
        return ValidationResult.combine([*structural.checks, render_resource])
    standard = validate_pdf(
        path,
        expected_pages=expected_pages,
        render_pages=True,
        render_sample_limit=None,
        check_cancelled=check_cancelled,
    )
    if not standard.passed:
        return ValidationResult.combine([*standard.checks, render_resource])
    try:
        matched, total = _pdf_contains_tokens(
            path,
            expected_tokens,
            limits=limits,
            check_cancelled=check_cancelled,
        )
        embedded = (
            not sidecar_has_non_marker_content if total == 0 else matched == total
        )
    except JobCancelled:
        raise
    except PipelineError as exc:
        resource_check = ValidationCheck(
            name="ocr-text-resource-limits",
            passed=False,
            detail=str(exc),
        )
        return ValidationResult.combine([*standard.checks, render_resource, resource_check])
    except Exception:
        matched, total, embedded = 0, len(expected_tokens), False
    semantic = ValidationCheck(
        name="ocr-text-embedded",
        passed=embedded,
        detail=(
            "nonblank sidecar contained no bounded OCR token; semantic agreement unavailable"
            if total == 0 and sidecar_has_non_marker_content
            else "sidecar contained no OCR tokens; blank scan accepted"
            if total == 0
            else f"matched {matched} of {total} sampled OCR tokens"
        ),
    )
    return ValidationResult.combine([*standard.checks, render_resource, semantic])


def _tool_failure(
    result: ToolResult,
    *,
    max_image_pixels: int | None = None,
) -> OcrToolFailure:
    code = result.returncode
    compact_diagnostic = re.sub(r"\s+", "", result.output.casefold())
    if (
        code == 15
        and max_image_pixels is not None
        and "decompressionbomberror" in compact_diagnostic
        and ("pixel" in compact_diagnostic or "imagesize" in compact_diagnostic)
    ):
        pixel_unit = "pixel" if max_image_pixels == 1 else "pixels"
        return OcrToolFailure(
            code,
            1,
            "OCR input exceeds the configured max_image_pixels safety limit "
            f"({max_image_pixels:,} {pixel_unit}); reduce scan resolution or raise the limit "
            "only after reviewing memory risk",
        )
    if code == 3:
        return OcrToolFailure(
            code,
            3,
            "OCRmyPDF reported that a required engine became unavailable; run ldf doctor",
        )
    if code in {4, 10}:
        return OcrToolFailure(
            code,
            4,
            f"OCRmyPDF could not produce a valid PDF (engine exit {code})",
        )
    if code == 6:
        return OcrToolFailure(
            code,
            1,
            "OCRmyPDF detected existing text; retry with --skip-text or --force-ocr",
        )
    return OcrToolFailure(code, 1, f"OCRmyPDF failed safely (engine exit {code})")


def _engine_diagnostic_warnings(
    output: str,
    *,
    failed_page_markers: int = 0,
) -> list[FidelityWarning]:
    lowered = output.casefold()
    skipped = failed_page_markers > 0 or any(
        marker in lowered
        for marker in (
            "took too long to ocr - skipping",
            "timed out",
            "page too big, skipping ocr",
            "image too large",
        )
    )
    if not skipped:
        return []
    if failed_page_markers:
        message = (
            f"OCRmyPDF reported {failed_page_markers} OCR-eligible page(s) without a new text "
            "layer; inspect the corresponding pages in the output PDF."
        )
    else:
        message = (
            "OCRmyPDF reported at least one page skipped because of an engine timeout or "
            "size limit; inspect the corresponding pages in the output PDF."
        )
    return [
        FidelityWarning(
            code=OCR_ENGINE_PAGE_SKIPPED,
            message=message,
            severity=WarningSeverity.CRITICAL,
            basis=FidelityBasis.STRUCTURAL,
            impact=FidelityImpact.KNOWN_LOSS,
            remedy="Inspect the corresponding pages in the output PDF.",
        )
    ]


def ocr_pdf(
    input_file: Path,
    output: Path,
    *,
    options: OcrOptions | None = None,
) -> ConversionReport:
    """Add a best-effort text layer without ever modifying the source PDF."""
    options = options or OcrOptions()
    if options.skip_text and options.force_ocr:
        raise PipelineError("OCR modes --skip-text and --force-ocr are mutually exclusive")
    language_codes = _language_codes(options.language)
    language = "+".join(language_codes)
    settings = options.settings or get_settings()
    if settings.limits.max_image_pixels == 0:
        raise PipelineError(
            "OCR requires max_image_pixels to be greater than zero; OCRmyPDF treats zero "
            "as disabling its image safety limit"
        )
    engine, engine_infos = _require_ocr_engines()
    engine_info = engine_infos["ocrmypdf"]
    executable_paths = {
        name: info.path for name, info in engine_infos.items() if info.path is not None
    }
    _require_languages(language_codes, executable_paths["tesseract"])
    mode = "force" if options.force_ocr else "skip" if options.skip_text else "default"

    def execute(context: JobContext, artifacts: list[InputArtifact]) -> ExecuteResult:
        if len(artifacts) != 1:
            raise PipelineError("OCR requires exactly one PDF input")
        subprocess_limit = context.limits.max_subprocesses
        if subprocess_limit is not None and subprocess_limit < 2:
            raise PipelineError(
                "OCR requires a max_subprocesses limit of at least 2 for OCRmyPDF and Tesseract"
            )

        source_snapshot = context.workspace / "input.pdf"
        candidate = context.workspace / "candidate.pdf"
        raw_sidecar = context.workspace / "ocr-sidecar.raw.txt"
        normalized_sidecar = context.workspace / "ocr-sidecar.txt"
        page_count, was_encrypted, signature_assessment = _snapshot_pdf(
            artifacts[0].path,
            source_snapshot,
            password=options.password,
        )
        page_limit = context.limits.max_pages
        if page_limit is not None and page_count > page_limit:
            raise PipelineError(
                f"OCR input has {page_count} pages, over the configured limit of {page_limit}"
            )
        _check_temporary_limit(context.workspace, context.limits.max_temporary_bytes)

        per_page, coverage = inspect_page_text_stats(
            source_snapshot,
            limits=context.limits,
        )
        text_layer_pages = frozenset(
            index + 1 for index, item in enumerate(per_page) if bool(item["has_text_layer"])
        )
        text_layer_page_count = len(text_layer_pages)
        if text_layer_page_count and mode == "default":
            raise PipelineError(
                "OCR policy refused the input because it already contains a text layer on "
                f"{text_layer_page_count} page(s); use --skip-text to OCR only image pages "
                "or --force-ocr to rasterize and re-OCR every page"
            )

        engine_home = context.workspace / "ocr-home"
        engine_temp = context.workspace / "ocr-temp"
        engine_home.mkdir(mode=0o700)
        engine_temp.mkdir(mode=0o700)
        remaining = _remaining_timeout(context)
        args = [
            "--language",
            language,
            "--jobs",
            "1",
            "--output-type",
            "pdf",
            "--optimize",
            "0",
            "--no-overwrite",
            "--tesseract-timeout",
            f"{min(remaining, _MAX_TESSERACT_PAGE_SECONDS):g}",
        ]
        if context.limits.max_image_pixels is not None:
            args.extend(
                [
                    "--max-image-mpixels",
                    f"{context.limits.max_image_pixels / 1_000_000:g}",
                ]
            )
        args.extend(["--sidecar", str(raw_sidecar)])
        if options.skip_text:
            args.append("--skip-text")
        elif options.force_ocr:
            args.append("--force-ocr")
        if signature_assessment.present:
            args.append("--invalidate-digital-signatures")
        args.extend([str(source_snapshot), str(candidate)])

        context.emit("ocr", message="running OCRmyPDF with one bounded worker")
        try:
            result = run_tool(
                "ocrmypdf",
                args,
                timeout=_remaining_timeout(context),
                cwd=context.workspace,
                max_output_bytes=_MAX_TOOL_OUTPUT_BYTES,
                env_extra={
                    "HOME": str(engine_home),
                    "USERPROFILE": str(engine_home),
                    "TEMP": str(engine_temp),
                    "TMP": str(engine_temp),
                    "TMPDIR": str(engine_temp),
                },
                child_path_tools=("tesseract", "ghostscript"),
                expected_executable=executable_paths["ocrmypdf"],
                expected_child_executables={
                    "tesseract": executable_paths["tesseract"],
                    "ghostscript": executable_paths["ghostscript"],
                },
            )
        except ToolTimeout as exc:
            raise OcrToolTimeout(str(exc)) from exc
        except ToolError:
            failure = OcrToolFailure(
                None,
                3,
                "OCRmyPDF could not be launched safely; run ldf doctor",
            )
            raise PipelineError(str(failure)) from failure
        if result.returncode == 130:
            raise JobCancelled("OCRmyPDF was cancelled")
        if result.returncode != 0:
            failure = _tool_failure(
                result,
                max_image_pixels=context.limits.max_image_pixels,
            )
            raise PipelineError(str(failure)) from failure
        context.check_cancelled()
        # A compromised external engine must not make us follow links out of
        # the private workspace while normalizing or validating its outputs.
        _check_temporary_limit(context.workspace, context.limits.max_temporary_bytes)
        _normalize_sidecar(raw_sidecar, normalized_sidecar)
        # Measure the duplication peak before removing OCRmyPDF's raw sidecar.
        _check_temporary_limit(context.workspace, context.limits.max_temporary_bytes)
        raw_sidecar.unlink(missing_ok=True)
        _check_temporary_limit(context.workspace, context.limits.max_temporary_bytes)

        def validation_checkpoint() -> None:
            context.check_cancelled()
            _remaining_timeout(context)

        sidecar_sample = _sample_ocr_sidecar(
            normalized_sidecar,
            check_cancelled=validation_checkpoint,
            expected_pages=page_count,
            ineligible_pages=text_layer_pages if options.skip_text else frozenset(),
        )
        expected_tokens = sidecar_sample.tokens
        ocr_eligible_pages = page_count - text_layer_page_count if options.skip_text else page_count
        diagnostic_warnings = _engine_diagnostic_warnings(
            result.output,
            failed_page_markers=sidecar_sample.failed_page_markers,
        )
        if ocr_eligible_pages > 0 and (
            sidecar_sample.failed_page_markers >= ocr_eligible_pages
            or (ocr_eligible_pages == 1 and not expected_tokens and diagnostic_warnings)
        ):
            raise PipelineError(
                "OCRmyPDF skipped every OCR-eligible page; no searchable text was produced"
            )
        warnings = [
            FidelityWarning(
                code=OCR_TEXT_APPROXIMATE,
                message=(
                    "The OCR text layer and any requested sidecar are best-effort machine "
                    "recognition; verify important names, numbers, and formatting against the "
                    "page image."
                ),
                severity=WarningSeverity.WARNING,
                basis=FidelityBasis.DECLARED,
                impact=FidelityImpact.REVIEW,
                remedy=(
                    "Verify important names, numbers, and formatting against the page image."
                ),
            )
        ]
        if options.force_ocr:
            warnings.append(
                FidelityWarning(
                    code=OCR_FORCE_RASTERIZED,
                    message=(
                        "Force OCR rasterized and re-encoded every page, including vector text "
                        "and graphics; content fidelity may be materially reduced."
                    ),
                    severity=WarningSeverity.CRITICAL,
                    basis=FidelityBasis.STRUCTURAL,
                    impact=FidelityImpact.KNOWN_LOSS,
                )
            )
        if options.sidecar is not None and options.skip_text and text_layer_page_count:
            warnings.append(
                FidelityWarning(
                    code=OCR_SIDECAR_OMITS_EXISTING_TEXT,
                    message=(
                        "The sidecar contains newly recognized OCR text only; pre-existing text "
                        "on pages skipped by --skip-text is not copied into it."
                    ),
                    severity=WarningSeverity.WARNING,
                    basis=FidelityBasis.STRUCTURAL,
                    impact=FidelityImpact.KNOWN_LOSS,
                )
            )
        warnings.extend(diagnostic_warnings)

        candidates = [
            CandidateOutput(
                workspace_path=candidate,
                destination=output,
                expected_pages=page_count,
                render_all=True,
                validator=lambda path: _ocr_pdf_validator(
                    path,
                    expected_pages=page_count,
                    expected_tokens=expected_tokens,
                    sidecar_has_non_marker_content=sidecar_sample.has_non_marker_content,
                    limits=context.limits,
                    check_cancelled=validation_checkpoint,
                ),
            )
        ]
        if options.sidecar is not None:
            candidates.append(
                CandidateOutput(
                    workspace_path=normalized_sidecar,
                    destination=options.sidecar,
                    media_type="text/plain; charset=utf-8",
                    kind=ArtifactKind.SIDECAR,
                    validator=_validate_sidecar,
                )
            )
        security_warnings = []
        if was_encrypted:
            security_warnings.append(
                SecurityWarning(
                    code="input-encryption-removed",
                    message=(
                        "The OCR input was password protected. The generated PDF is not password "
                        "protected; secure it separately before sharing."
                    ),
                    severity=WarningSeverity.CRITICAL,
                )
            )
        if signature_assessment.present:
            security_warnings.append(
                SecurityWarning(
                    code="signature-invalidated",
                    message=(
                        "OCR rewrites the PDF and invalidates existing cryptographic "
                        "signatures. Signature appearance objects may remain."
                    ),
                    severity=WarningSeverity.CRITICAL,
                )
            )
            warnings.append(
                organize_ops._signature_invalidated_fidelity_warning("OCR")
            )
        elif signature_assessment.uncertain:
            warnings.append(
                organize_ops._signature_presence_uncertain_warning("OCR")
            )
        return ExecuteResult(
            candidates=candidates,
            details={
                "language": language,
                "mode": mode,
                "sidecar": options.sidecar is not None,
                "input_pages_with_text_layer": text_layer_page_count,
                "input_pages_without_text_layer": page_count - text_layer_page_count,
                "input_text_coverage": {
                    "pages_total": coverage["pages_total"],
                    "pages_with_text_layer": coverage["pages_with_text_layer"],
                },
                "ocr_jobs": 1,
                "ocr_output_type": "pdf",
                "ocr_optimization": 0,
                "engine_diagnostics_withheld": bool(result.output.strip()),
                "semantic_ocr_tokens_sampled": len(expected_tokens),
                "ocr_failed_page_markers": sidecar_sample.failed_page_markers,
            },
            security_warnings=security_warnings,
            fidelity_warnings=warnings,
            output_page_count=page_count,
        )

    return run_pipeline(
        operation=OP_OCR,
        input_paths=[input_file],
        execute=execute,
        engine_name=engine.name,
        engine_version=engine_info.version,
        collision=options.collision,
        settings=settings,
        progress=options.progress,
        details={
            "language": language,
            "mode": mode,
            "sidecar": options.sidecar is not None,
        },
    )
