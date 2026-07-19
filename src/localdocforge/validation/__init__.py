"""Output validation: every generated PDF is reopened; risky ops render all pages."""

from localdocforge.validation.pdf_checks import (
    count_pdf_pages,
    render_pdf_page,
    validate_pdf,
)

__all__ = ["count_pdf_pages", "render_pdf_page", "validate_pdf"]
