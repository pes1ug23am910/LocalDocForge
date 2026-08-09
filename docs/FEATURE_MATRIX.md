# Feature Matrix

Machine-checked source of truth: `ldf --json doctor` (capability gating is
tested in `tests/unit/test_registry.py`). `ldf agent-brief` is the compact
agent-facing view: implemented entries follow registry order and retain their
live availability state, while unimplemented entries cannot render. This file
adds the human detail the JSON cannot carry. "Verified" means executed in this
repository with output inspected — see the test files listed.

Interfaces legend: **Lib** Python API · **CLI** · **API** local HTTP · **UI** browser.

| Capability | Status | Engine | Interfaces | Verified by | Limitations |
|---|---|---|---|---|---|
| Merge PDF (whole + per-input ranges) | ✅ | pikepdf | Lib, CLI, API | `test_organize_ops.py::TestMerge`, `test_pdf_integrity_regressions.py`, `test_cli.py::TestMergeCommand`, `test_api.py` | Page-moving operations warn when outlines, forms, attachments, XMP metadata, page labels, document actions/JavaScript, or signature semantics are dropped; signature loss is critical |
| Split PDF (ranges / every-N / single pages) | ✅ | pikepdf | Lib, CLI, API | `TestSplit`, `TestSplitAndOrganizeCommands`, `test_pdf_integrity_regressions.py` | Same page-moving caveats as merge; split-by-bookmarks is unavailable |
| Remove pages | ✅ with refusal policy | pikepdf | Lib, CLI, API | `TestRemoveExtractOrganize`, `test_pdf_integrity_regressions.py` | Refuses inputs whose outlines, forms/signatures, page labels, open action, tagged structure, named destinations, or internal links cannot yet be safely rewritten |
| Extract pages | ✅ | pikepdf | Lib, CLI, API | `TestRemoveExtractOrganize`, `test_api.py`, `test_pdf_integrity_regressions.py` | Same page-moving caveats as merge |
| Organize (reorder/duplicate/reverse) | ✅ | pikepdf | Lib, CLI, API | `TestRemoveExtractOrganize`, `test_pdf_integrity_regressions.py` | Same page-moving caveats as merge; UI drag-and-drop unavailable |
| Rotate pages | ✅ | pikepdf | Lib, CLI, API | `TestRotate`, `test_api.py`, `test_pdf_integrity_regressions.py` | Rewrites invalidate cryptographic signatures; password protection is not retained and triggers a critical warning |
| Crop pages | ✅ | pikepdf | Lib, CLI, API | `TestCrop` | Always warns crop ≠ redaction; active content remains; rewriting invalidates signatures; password protection is not retained |
| Inspect PDF | ✅ | pikepdf + pdfium text API | Lib, CLI | `TestInspect`, `test_text_ops.py` | Includes ordered per-page extracted character/text-layer records and a text-coverage summary; fonts/images per page remain pending; counts do not prove correct reading order |
| Images → PDF (HEIC/JPG/PNG/TIFF/BMP/WebP, multipage TIFF, EXIF) | ✅ | pillow (+ pi-heif for HEIC) | Lib, CLI, API | `test_image_ops.py::TestImagesToPdf`, `test_convert_images_ops.py::TestHeicInput`, `test_pdf_integrity_regressions.py` | Re-encodes images; alpha is composited onto the chosen background; impossible margins are refused; auto-crop/deskew unavailable |
| PDF → images (PNG/JPEG/WebP/TIFF, DPI, ranges, `llm` preset) | ✅ | pdfium | Lib, CLI, API | `TestPdfToImages`, `TestImageCommands::test_pdf_to_images_llm_preset_json_report`, `test_api.py`, `test_pdf_integrity_regressions.py` | Raster output is inherently lossy in editability; JPEG/WebP quality is applied; `llm` produces per-page JPEG q85 renders with long edge ≤ 1568 px without enlarging pages already below the cap at the 150-DPI ceiling; explicit format/quality/DPI values win, and explicit DPI disables the cap; syntax-damaged PDFs are refused rather than silently repaired |
| PDF → Markdown/text/JSONL | ✅ | pdfium text API + pdfplumber (opt-in tables) | Lib, CLI, API | `test_text_ops.py`, `test_cli.py`, `test_api.py`, `test_documentation_consistency.py` | Explicit UTF-8/LF output; Markdown page anchors and heuristic headings; JSONL is one exact-schema record per selected occurrence; reports contain bounded coverage/table counters/per-page warning codes but no extracted text. Markdown-only table recovery is off by default and emits GFM only for confident explicit line grids; the first physical row is an inferred header. Borderless, merged-cell, rotated, overly dense, and low-confidence candidates remain flowed text. No OCR, bidi repair, or silent dehyphenation; RTL, rotation, columns, and tables are best effort and use stable warnings. No warning proves no table exists; `no-text-layer` points to `pdf-to-images --preset llm` |
| Convert images (HEIC/JPG/PNG/TIFF/BMP/WebP → PNG/JPEG/WebP/TIFF) | ✅ | pillow + pi-heif | Lib, CLI, API | `test_convert_images_ops.py`, `test_cli.py::TestConvertImagesCommand`, `test_api.py` | Outputs are re-encoded; EXIF orientation is applied; EXIF/XMP metadata (including GPS positions) is stripped by default (`--keep-metadata` retains EXIF and warns when GPS is kept); non-sRGB color profiles are converted to sRGB or honestly retained; HEIF is decode-only (no HEIC output); the `llm` preset (JPEG q85, long edge ≤ 1568 px) targets AI-assistant ingestion |
| Encrypted-input handling (open with password) | ✅ | pikepdf/PDFium | Lib, CLI, API | `TestMerge::test_merge_encrypted_*`, `test_cli.py::TestPasswordSources`, `test_pdf_integrity_regressions.py` | CLI precedence is global `--password-stdin` → `LDF_PASSWORD` → hidden TTY prompt; an empty-but-present environment value is an explicit empty credential; one password applies to all encrypted inputs per invocation, so differing passwords fail clearly. Passwords only unlock inputs for reading; generated PDFs/images/extracted-text files are unprotected and every such conversion emits a critical warning; protect/unlock operations are unavailable |
| Compress PDF | ✅ lossless preset | pikepdf | Lib, CLI, API | `test_optimize_ops.py`, `test_cli.py::TestCompressCommand`, `test_api.py` | Lossless structural optimization only: streams recompressed (generalized filters), object streams generated, unused page resources pruned; image data is never re-encoded or downsampled. Sampled pages must render pixel-identical to the source or nothing is published. Already-optimized files may not shrink (`compress-no-reduction` is reported, never hidden). `balanced`/`aggressive`/`archival` presets are planned and refused |
| Repair PDF | ❌ planned P2 | — | — | — | Syntax-damaged inputs are rejected; no automatic repair is performed |
| OCR PDF | ❌ planned P2 | needs Tesseract+OCRmyPDF (not installed) | — | — | |
| Office → PDF | ❌ planned P2 | needs LibreOffice (not installed) | — | — | |
| HTML → PDF | ❌ planned P2 | — | — | — | |
| PDF → PDF/A | ❌ planned P2 | needs Ghostscript + veraPDF (not installed) | — | — | |
| Markdown → PDF | ✅ when Typst ≥0.15.1 is available | markdown-it-py + separately installed Typst | Lib, CLI, API | `test_markdown_ops.py`, `test_cli.py`, `test_api.py`, `test_documentation_consistency.py` | Strict-UTF-8 CommonMark subset plus GFM tables; A4/Letter/Legal, margin, optional TOC, and contained local raster images. User values become escaped Typst strings; packages/imports and remote/escaping images are refused. Raw HTML, math, footnotes, and unknown constructs are dropped with source-line warnings; system-font fallback can change wrapping. Every PDF is fully rendered before publication |
| PDF → DOCX/PPTX/XLSX | ❌ planned P2/P3 | — | — | — | |
| Edit PDF (editor) | ❌ planned P4 | — | — | — | |
| PDF forms | ❌ planned P4 | — | — | — | |
| Protect/unlock | ❌ planned P4 | — | — | — | |
| Redact + sanitize | ❌ planned P4 | — | — | — | |
| Watermark/page numbers | ❌ planned P4 | — | — | — | |
| Signatures | ❌ planned P5 | — | — | — | |
| Compare PDFs | ❌ planned P5 | — | — | — | |
| Validate (PDF/A, PDF/UA) | ❌ planned P5 | needs veraPDF | — | — | Generated-PDF validation reopens with pikepdf/qpdf, rejects syntax warnings, checks expected page counts, and renders through PDFium; text/image outputs use their own validators. None of these establishes PDF/A or PDF/UA conformance |
| Scan to PDF (hardware) | ❌ planned P5 | — | — | — | |
| Batch YAML/JSON jobs | ❌ planned P2 | — | — | — | |
| Registry-derived agent brief | ✅ | `CAPABILITY_SPECS` + live capability probes | CLI | `test_agent_brief.py`, `test_cli.py::TestAgentBrief`, `test_documentation_consistency.py` | Read-only Markdown/JSON on stdout; every implemented command appears with live availability and one-line usage, while unimplemented capabilities are structurally excluded; requires a discoverable source checkout because the writable feedback log is intentionally not packaged; no API/UI endpoint |
| Local web API (`ldf web`) | ✅ | FastAPI/uvicorn + spawned workers | API | `test_api.py`, `test_privacy_boundary.py`, `test_worker_isolation.py` + live localhost verification | Loopback-only by default; strict-offline forbids non-loopback even with the exposure opt-in; token auth; bounded queue/rate/concurrency controls; synchronous compatibility or explicit async states/progress/cancel; workers retain user filesystem authority and are not an OS sandbox; browser UI not built |
| Browser UI (React) | ❌ next slice | — | — | — | current index page is an honest capability listing, no fake tools |

Rules: a ❌ capability is not visible as enabled anywhere (doctor lists it
under "not implemented in this build"); flipping to ✅ requires pipeline +
tests in the same change, enforced by `test_registry.py`.

Explicitly unavailable in this checkpoint include lossy compression presets,
repair, OCR, Office-to-PDF, HTML-to-PDF, advanced PDF text
reconstruction (including table extraction), PDF/A/PDF/UA conformance,
interactive editing/forms, cryptographic signatures, secure redaction, and
scanner/camera acquisition. Installed executables alone do not make an unwired capability
available.
