"""Structural and render validation of PDFs.

File existence is never enough: outputs are reopened with a real parser, and
high-risk outputs additionally have every page rendered through PDFium. Blank
pages are reported (they may be legitimate); zero pages, parse failures, and
render failures are hard failures.
"""

from __future__ import annotations

from pathlib import Path

from localdocforge.domain.models import ValidationCheck, ValidationResult
from localdocforge.security.sniff import detect_media_type

_BLANK_THRESHOLD = 250  # grayscale floor above which a rendered page counts as blank
_RENDER_SCALE = 0.5  # ~36 dpi — enough to detect blank/corrupt pages cheaply


def count_pdf_pages(path: Path, password: str | None = None) -> int:
    import pikepdf

    with pikepdf.open(path, password=password or "") as pdf:
        return len(pdf.pages)


def render_pdf_page(
    path: Path,
    page_index: int,
    *,
    scale: float = 1.0,
    password: str | None = None,
):
    """Render one page to a PIL image (caller owns interpretation)."""
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(str(path), password=password)
    try:
        page = pdf[page_index]
        try:
            bitmap = page.render(scale=scale)
            try:
                # PDFium-backed PIL buffers may share memory with the bitmap;
                # copy before closing native objects and returning to callers.
                return bitmap.to_pil().copy()
            finally:
                bitmap.close()
        finally:
            page.close()
    finally:
        pdf.close()


def _page_is_blank(image) -> bool:
    grayscale = image.convert("L")
    extrema = grayscale.getextrema()
    return extrema[0] >= _BLANK_THRESHOLD


def validate_pdf(
    path: Path,
    *,
    expected_pages: int | None = None,
    render_pages: bool = True,
    render_sample_limit: int | None = None,
    forbid_all_blank: bool = False,
    password: str | None = None,
) -> ValidationResult:
    """Validate a generated PDF. Never raises; failures land in the result.

    ``render_sample_limit`` caps how many pages are rendered (evenly sampled);
    high-risk operations pass ``None`` to render every page.
    """
    checks: list[ValidationCheck] = []

    if not path.is_file() or path.stat().st_size == 0:
        checks.append(ValidationCheck(name="file-exists", passed=False, detail=str(path)))
        return ValidationResult.combine(checks)
    checks.append(
        ValidationCheck(name="file-exists", passed=True, detail=f"{path.stat().st_size} bytes")
    )

    media = detect_media_type(path)
    checks.append(
        ValidationCheck(
            name="pdf-signature",
            passed=media == "application/pdf",
            detail=media or "unrecognized content",
        )
    )
    if media != "application/pdf":
        return ValidationResult.combine(checks)

    try:
        import pikepdf

        with pikepdf.open(path, password=password or "") as pdf:
            page_count = len(pdf.pages)
            syntax_issues = list(pdf.check_pdf_syntax())
        structural_ok = page_count >= 1
        checks.append(
            ValidationCheck(
                name="structure-reopen",
                passed=structural_ok,
                detail=f"{page_count} pages" if structural_ok else "zero pages",
            )
        )
    except Exception as exc:
        checks.append(
            ValidationCheck(name="structure-reopen", passed=False, detail=type(exc).__name__)
        )
        return ValidationResult.combine(checks)
    if not structural_ok:
        return ValidationResult.combine(checks)

    checks.append(
        ValidationCheck(
            name="pdf-syntax",
            passed=not syntax_issues,
            detail=(
                "no syntax warnings"
                if not syntax_issues
                else f"parser reported {len(syntax_issues)} syntax warning(s)"
            ),
        )
    )
    if syntax_issues:
        return ValidationResult.combine(checks)

    if expected_pages is not None:
        checks.append(
            ValidationCheck(
                name="page-count",
                passed=page_count == expected_pages,
                detail=f"expected {expected_pages}, found {page_count}",
            )
        )

    if render_pages:
        if (
            render_sample_limit is None
            or render_sample_limit < 1
            or page_count <= render_sample_limit
        ):
            indices = list(range(page_count))
        else:
            if render_sample_limit == 1:
                indices = [0]
            else:
                # Include both endpoints and distribute the remaining samples
                # across the document. The previous floor-based calculation
                # never rendered the final page of a long routine output.
                indices = sorted(
                    {
                        round(i * (page_count - 1) / (render_sample_limit - 1))
                        for i in range(render_sample_limit)
                    }
                )
        blank_pages: list[int] = []
        render_failures: list[int] = []
        for index in indices:
            try:
                image = render_pdf_page(path, index, scale=_RENDER_SCALE, password=password)
                try:
                    if _page_is_blank(image):
                        blank_pages.append(index + 1)
                finally:
                    image.close()
            except Exception:
                render_failures.append(index + 1)
        checks.append(
            ValidationCheck(
                name="render",
                passed=not render_failures,
                detail=(
                    f"rendered {len(indices)} of {page_count} pages"
                    + (f"; failed: {render_failures}" if render_failures else "")
                ),
            )
        )
        all_rendered_blank = len(blank_pages) == len(indices) and bool(indices)
        checks.append(
            ValidationCheck(
                name="blank-pages",
                passed=not (forbid_all_blank and all_rendered_blank),
                detail=f"blank pages: {blank_pages}" if blank_pages else "none blank",
            )
        )

    return ValidationResult.combine(checks)
