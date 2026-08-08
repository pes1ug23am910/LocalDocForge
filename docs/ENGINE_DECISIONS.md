# Engine Decisions

Decisions recorded when made; revisit points listed at the bottom.
Probed state on this machine is always visible via `ldf doctor`.

## Selected for Phase 0/1 (installed, probed, in use)

| Engine | Version (this env) | License | Role | Why |
|---|---|---|---|---|
| pikepdf (libqpdf) | 10.10.0 / qpdf 12.3.2 | MPL-2.0 (pikepdf) / Apache-2.0 (qpdf) | Primary structural engine: merge/split/remove/extract/organize/rotate/crop/inspect | Mature, actively maintained, binds qpdf (the reference structural tool), preserves objects faithfully, handles encryption, robust against malformed files |
| pypdf | 6.14.2 | BSD-3-Clause | Installed library used for independent text assertions in tests; no production operation is wired to it | It remains probed for diagnostics but is not advertised or selected as a fallback |
| pypdfium2 (PDFium) | 5.12.1 / PDFium 152.0.7947.0 | Apache-2.0 OR BSD-3-Clause (wrapper); bundled PDFium uses BSD-style and `BUILD_LICENSES` notices | Rendering: validation renders and pdf-to-images; text/geometry extraction for pdf-to-md and inspect coverage | Chrome's widely deployed PDF engine: robust on hostile files, exposes page-scoped text rectangles/font geometry without another runtime dependency, permissive license, abi3 wheels |
| Pillow | 12.3.0 | MIT-CMU; bundled codecs have per-component terms | Image decode/encode, images-to-pdf composition, convert-images transcoding | The standard Python imaging library; built-in decompression-bomb guard which we wire to `ResourceLimits.max_image_pixels` |
| pi-heif (libheif) | 1.4.0 / libheif 1.23.0, libde265 1.1.1 | BSD-3-Clause wrapper; LGPL-3.0-or-later libheif + libde265 | HEIF/HEIC **decode-only** Pillow plugin: iPhone photo input for convert-images and images-to-pdf | The decode-only distribution of pillow-heif — its wheels bundle no GPLv2 x265 encoder, keeping the runtime license ceiling at LGPLv3; the full pillow-heif package is a dev-profile fixture-encoding tool only |

Rationale for the split: structural edits (pikepdf) and rendering/text
extraction (PDFium) are different failure domains; no single library is trusted for
both. A pypdf production fallback remains a future implementation task; an
installed library alone is not reported as an executable operation engine.

PDF→Markdown/text uses PDFium's page-scoped text and geometry APIs. It was
chosen because PDFium is already shipped, probed, worker-contained for API
jobs, and licensed Apache-2.0/BSD-3-Clause; it supports deterministic
one-page-at-a-time extraction without expanding the dependency closure.
Layout and heading reconstruction remain documented heuristics. **PyMuPDF and
pymupdf4llm are banned from core**: linking/importing their AGPL runtime would
exceed this project's weak-copyleft-or-lighter license ceiling. They are not a
fallback and are not optional adapters. Structured table extraction is a
separate future slice rather than a reason to mislabel flattened PDFium text.

## Optional executable probes (features gated off; availability varies)

| Engine | License | Planned role | Install hint (Windows) |
|---|---|---|---|
| qpdf CLI | Apache-2.0 | Repair second-opinion, JSON introspection | `winget install qpdf.qpdf` |
| Tesseract | Apache-2.0 | OCR | `winget install UB-Mannheim.TesseractOCR` |
| OCRmyPDF | MPL-2.0 | OCR orchestration, PDF/A-ish output | `pip install ocrmypdf` (+ Tesseract, Ghostscript) |
| Ghostscript | AGPL-3.0 / commercial | PDF/A conversion, compression fallback | `winget install ArtifexSoftware.GhostScript` — exact upstream terms and the intended distribution/use model require review before enablement or redistribution |
| LibreOffice | MPL-2.0 | Office↔PDF in isolated headless mode | `winget install TheDocumentFoundation.LibreOffice` |
| Pandoc | GPL-2.0+ | Markdown/Office conversions (invoked, not linked) | `winget install JohnMacFarlane.Pandoc` |
| Typst | Apache-2.0 | **Present on this machine** (0.15.1) — candidate Markdown→PDF renderer for Phase 3 | `winget install Typst.Typst` |
| veraPDF | GPL-3.0+ / MPL | Authoritative PDF/A validation | installer from verapdf.org |

## Rules
- An installed binary alone never lights a feature: `supported_operations()`
  stays empty until the pipeline using it lands with tests.
- External tools run only through the hardened subprocess runner
  (allowlist in `security/subproc.py`).
- Engine names + versions are recorded in every `ConversionReport`.
- `--engine` lets users override selection where multiple engines support an
  operation (registry enforces support + availability).

## Revisit points
- Compression (Phase 2): pikepdf image recompression vs Ghostscript
  pipelines — decide with benchmarks and quality-floor checks.
- Markdown→PDF (Phase 3): Typst (installed, fast, hermetic) vs WeasyPrint
  (HTML/CSS themes, pure-Python install) — likely Typst primary.
- PDF→Markdown follow-up: evaluate S5's separately licensed table extractor and
  confidence thresholds. Keep PDFium text as the source for ordinary regions;
  do not interleave competing text engines or weaken the PyMuPDF ban.
- Semantic PDF→DOCX (Phase 2/3): candidate pdf2docx; verify license and
  output honesty before adoption.
