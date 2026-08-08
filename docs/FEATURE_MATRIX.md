# Feature Matrix

Machine-checked source of truth: `ldf doctor --json` (capability gating is
tested in `tests/unit/test_registry.py`). This file adds the human detail the
JSON cannot carry. "Verified" means executed in this repository with output
inspected — see the test files listed.

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
| Inspect PDF | ✅ | pikepdf | Lib, CLI | `TestInspect` | inventory is summary-level (fonts/images per page pending) |
| Images → PDF (HEIC/JPG/PNG/TIFF/BMP/WebP, multipage TIFF, EXIF) | ✅ | pillow (+ pi-heif for HEIC) | Lib, CLI, API | `test_image_ops.py::TestImagesToPdf`, `test_convert_images_ops.py::TestHeicInput`, `test_pdf_integrity_regressions.py` | Re-encodes images; alpha is composited onto the chosen background; impossible margins are refused; auto-crop/deskew unavailable |
| PDF → images (PNG/JPEG/WebP/TIFF, DPI, ranges) | ✅ | pdfium | Lib, CLI, API | `TestPdfToImages`, `test_api.py`, `test_pdf_integrity_regressions.py` | Raster output is inherently lossy in editability; JPEG/WebP quality is applied; syntax-damaged PDFs are refused rather than silently repaired |
| Convert images (HEIC/JPG/PNG/TIFF/BMP/WebP → PNG/JPEG/WebP/TIFF) | ✅ | pillow + pi-heif | Lib, CLI, API | `test_convert_images_ops.py`, `test_cli.py::TestConvertImagesCommand`, `test_api.py` | Outputs are re-encoded; EXIF orientation is applied; EXIF/XMP metadata (including GPS positions) is stripped by default (`--keep-metadata` retains EXIF and warns when GPS is kept); non-sRGB color profiles are converted to sRGB or honestly retained; HEIF is decode-only (no HEIC output); the `llm` preset (JPEG q85, long edge ≤ 1568 px) targets AI-assistant ingestion |
| Encrypted-input handling (open with password) | ✅ | pikepdf/PDFium | Lib, CLI, API | `TestMerge::test_merge_encrypted_*`, `test_pdf_integrity_regressions.py` | Passwords only unlock inputs for reading; generated PDFs/images are unprotected and every such conversion emits a critical warning; protect/unlock operations are unavailable |
| Compress PDF | ✅ lossless preset | pikepdf | Lib, CLI, API | `test_optimize_ops.py`, `test_cli.py::TestCompressCommand`, `test_api.py` | Lossless structural optimization only: streams recompressed (generalized filters), object streams generated, unused page resources pruned; image data is never re-encoded or downsampled. Sampled pages must render pixel-identical to the source or nothing is published. Already-optimized files may not shrink (`compress-no-reduction` is reported, never hidden). `balanced`/`aggressive`/`archival` presets are planned and refused |
| Repair PDF | ❌ planned P2 | — | — | — | Syntax-damaged inputs are rejected; no automatic repair is performed |
| OCR PDF | ❌ planned P2 | needs Tesseract+OCRmyPDF (not installed) | — | — | |
| Office → PDF | ❌ planned P2 | needs LibreOffice (not installed) | — | — | |
| HTML → PDF | ❌ planned P2 | — | — | — | |
| PDF → PDF/A | ❌ planned P2 | needs Ghostscript + veraPDF (not installed) | — | — | |
| PDF → Markdown | ❌ planned P3 | — | — | — | |
| Markdown → PDF | ❌ planned P3 | Typst 0.15.1 installed, unwired | — | — | |
| PDF → DOCX/PPTX/XLSX | ❌ planned P2/P3 | — | — | — | |
| Edit PDF (editor) | ❌ planned P4 | — | — | — | |
| PDF forms | ❌ planned P4 | — | — | — | |
| Protect/unlock | ❌ planned P4 | — | — | — | |
| Redact + sanitize | ❌ planned P4 | — | — | — | |
| Watermark/page numbers | ❌ planned P4 | — | — | — | |
| Signatures | ❌ planned P5 | — | — | — | |
| Compare PDFs | ❌ planned P5 | — | — | — | |
| Validate (PDF/A, PDF/UA) | ❌ planned P5 | needs veraPDF | — | — | Output validation reopens with pikepdf/qpdf, rejects syntax warnings, checks expected page counts, and renders through PDFium; this is not PDF/A or PDF/UA conformance validation |
| Scan to PDF (hardware) | ❌ planned P5 | — | — | — | |
| Batch YAML/JSON jobs | ❌ planned P2 | — | — | — | |
| Local web API (`ldf web`) | ✅ | FastAPI/uvicorn + spawned workers | API | `test_api.py`, `test_privacy_boundary.py`, `test_worker_isolation.py` + live localhost verification | Loopback-only by default; strict-offline forbids non-loopback even with the exposure opt-in; token auth; bounded queue/rate/concurrency controls; synchronous compatibility or explicit async states/progress/cancel; workers retain user filesystem authority and are not an OS sandbox; browser UI not built |
| Browser UI (React) | ❌ next slice | — | — | — | current index page is an honest capability listing, no fake tools |

Rules: a ❌ capability is not visible as enabled anywhere (doctor lists it
under "not implemented in this build"); flipping to ✅ requires pipeline +
tests in the same change, enforced by `test_registry.py`.

Explicitly unavailable in this checkpoint include lossy compression presets,
repair, OCR, HTML/Markdown and Office conversion, PDF/A/PDF/UA conformance,
interactive editing/forms, cryptographic signatures, secure redaction, and
scanner/camera acquisition. Installed executables alone do not make an unwired capability
available.
