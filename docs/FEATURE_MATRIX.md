# Feature Matrix

Machine-checked source of truth: `ldf doctor --json` (capability gating is
tested in `tests/unit/test_registry.py`). This file adds the human detail the
JSON cannot carry. "Verified" means executed in this repository with output
inspected — see the test files listed.

Interfaces legend: **Lib** Python API · **CLI** · **API** local HTTP · **UI** browser.

| Capability | Status | Engine (fallback) | Interfaces | Verified by | Limitations |
|---|---|---|---|---|---|
| Merge PDF (whole + per-input ranges) | ✅ | pikepdf (pypdf) | Lib, CLI, API | `test_organize_ops.py::TestMerge`, `test_cli.py::TestMergeCommand`, `test_api.py` | outlines/forms/attachments not rebuilt across documents (warned); page labels not preserved |
| Split PDF (ranges / every-N / single pages) | ✅ | pikepdf | Lib, CLI, API | `TestSplit`, `TestSplitAndOrganizeCommands` | split-by-bookmarks pending (Phase 1 follow-up) |
| Remove pages | ✅ | pikepdf (pypdf) | Lib, CLI, API | `TestRemoveExtractOrganize` | — |
| Extract pages | ✅ | pikepdf (pypdf) | Lib, CLI, API | `TestRemoveExtractOrganize`, `test_api.py` | same cross-document caveats as merge |
| Organize (reorder/duplicate/reverse) | ✅ | pikepdf | Lib, CLI, API | `TestRemoveExtractOrganize` | UI drag-and-drop pending |
| Rotate pages | ✅ | pikepdf | Lib, CLI, API | `TestRotate`, `test_api.py` | — |
| Crop pages | ✅ | pikepdf | Lib, CLI, API | `TestCrop` | always warns crop ≠ redaction; YAML box spec pending |
| Inspect PDF | ✅ | pikepdf (pypdf) | Lib, CLI | `TestInspect` | inventory is summary-level (fonts/images per page pending) |
| Images → PDF (JPG/PNG/TIFF/BMP/WebP, multipage TIFF, EXIF) | ✅ | pillow | Lib, CLI, API | `test_image_ops.py::TestImagesToPdf` | re-encodes images; HEIC needs optional codec; auto-crop/deskew are Phase 5 scan features |
| PDF → images (PNG/JPEG/WebP/TIFF, DPI, ranges) | ✅ | pdfium | Lib, CLI, API | `TestPdfToImages`, `test_api.py` | — |
| Encrypted-input handling (open with password) | ✅ | pikepdf | Lib, CLI | `TestMerge::test_merge_encrypted_*` | protect/unlock as operations are Phase 4 |
| Compress PDF | ❌ planned P2 | — | — | — | |
| Repair PDF | ❌ planned P2 | — | — | — | garbage/bad-xref inputs already fail cleanly today |
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
| Validate (PDF/A, PDF/UA) | ❌ planned P5 | needs veraPDF | — | — | structural+render validation of outputs already runs on every job |
| Scan to PDF (hardware) | ❌ planned P5 | — | — | — | |
| Batch YAML/JSON jobs | ❌ planned P2 | — | — | — | |
| Local web API (`ldf web`) | ✅ | FastAPI/uvicorn | API | `test_api.py` (21 tests) + live curl verification | loopback-only; token auth; jobs run in-request (queue/cancel endpoints pending); browser UI not built yet |
| Browser UI (React) | ❌ next slice | — | — | — | current index page is an honest capability listing, no fake tools |

Rules: a ❌ capability is not visible as enabled anywhere (doctor lists it
under "not implemented in this build"); flipping to ✅ requires pipeline +
tests in the same change, enforced by `test_registry.py`.
