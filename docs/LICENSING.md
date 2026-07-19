# Licensing

LocalDocForge itself: MIT (see `pyproject.toml`).

## Runtime dependency licenses (core, as pinned in requirements-lock.txt)

| Package | License | Notes |
|---|---|---|
| pikepdf | MPL-2.0 | File-level copyleft; used as an unmodified library dependency — no obligations propagate to LocalDocForge code |
| pypdf | BSD-3-Clause | |
| pypdfium2 / PDFium | Apache-2.0 OR BSD-3-Clause | PDFium binary bundled by the wheel |
| Pillow | MIT-CMU | |
| pydantic, pydantic-settings | MIT | |
| typer, click | MIT / BSD-3-Clause | |
| FastAPI, Starlette, uvicorn | MIT / BSD-3-Clause | API layer (planned use) |

Dev-only: pytest (MIT), reportlab (BSD-3-Clause; fixture generation only),
ruff (MIT), httpx (BSD-3-Clause).

## External engines (invoked as subprocesses, never bundled, never linked)

Optional tools the user installs themselves: qpdf (Apache-2.0), Tesseract
(Apache-2.0), OCRmyPDF (MPL-2.0), Ghostscript (AGPL-3.0), LibreOffice
(MPL-2.0), Pandoc (GPL-2.0+), Typst (Apache-2.0), veraPDF (GPL/MPL dual).

Policy:
- Copyleft engines (Ghostscript, Pandoc, veraPDF) stay behind optional
  adapters; LocalDocForge invokes installed binaries via subprocess and
  redistributes nothing, so their licenses do not attach to this codebase.
- No silent bundling of external binaries — installation is explicit and
  documented per profile (Lite/Standard/Full).

## Obligations tracked for release (Phase 6)
- `THIRD_PARTY_NOTICES` generation.
- SBOM (CycloneDX) for the Python distribution and frontend bundle.
- License texts for MPL components (pikepdf) alongside binary distributions.
