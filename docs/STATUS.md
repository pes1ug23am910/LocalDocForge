# STATUS — LocalDocForge

Last updated: 2026-07-19 (session 1, end). Update this file after every meaningful slice.

## How to resume safely
```powershell
.venv\Scripts\python.exe -m pytest tests -q      # must pass before continuing
.venv\Scripts\python.exe -m ruff check src tests # must be clean
.venv\Scripts\ldf.exe doctor                      # see live engine/capability state
```
Environment: Windows 11, Python 3.14.4 venv at `.venv` (created with
`py -3.14 -m venv .venv`; deps per `requirements-lock.txt`; package installed
editable with `pip install -e . --no-deps`). Node 24 available for the future
frontend. Typst 0.15.1 installed on PATH; qpdf/Tesseract/Ghostscript/
LibreOffice/Pandoc/veraPDF are NOT installed on this machine.

## Verified state (executed here — do not re-verify unless tests fail)
- **Test suite: 207 passed, 1 skipped (POSIX-perms check on Windows), ruff clean.**
- **Local HTTP API implemented and live-verified**: `ldf web` was launched on
  127.0.0.1:8491 and exercised with real curl requests — 401 without token,
  health OK with token, multipart merge upload → 201 with report → output
  downloaded and reopened as a valid 5-page PDF, index page served, server
  stopped cleanly. Security baseline per THREAT_MODEL §T7: loopback-only bind
  guard (`--allow-nonlocal` opt-in with warning), Host-header check, random
  per-session token via `X-LDF-Token` header (cookie alone rejected — CSRF),
  no CORS grants, CSP/hardening headers on every response, streaming upload
  size limits (413), sanitized upload names, unguessable job ids, job-scoped
  output serving with containment, in-memory-only job history (50 cap),
  honest index page listing unavailable features as unavailable.
  Tests: `tests/integration/test_api.py` (21 tests).
- Phase 0 foundation complete: domain models, page-range grammar, sniffing,
  filename/path hardening, subprocess allowlist runner, job workspaces with
  atomic publish + collision policies + stale sweep, engine registry with
  live probes, honest capability gating, config via `LDF_*` env, CLI base
  (`doctor`, `inspect`, global flags, stable exit codes).
- Phase 1 core+CLI complete and integration-tested on generated fixtures:
  merge (whole/ranges/inline `::` ranges), split (ranges/every-N/pages),
  remove-pages, extract-pages, organize, rotate, crop (with not-redaction
  warning), images-to-pdf (A4/Letter/Legal/image/custom sizes, fit modes,
  EXIF, multipage TIFF), pdf-to-images (png/jpeg/webp/tiff, DPI, ranges).
- Pipeline guarantees tested: magic-byte rejection of polyglots, input
  size/page limits, decompression-bomb guard, workspace cleanup on success
  and failure, no staging litter, atomic publish, collision fail/rename/
  overwrite, allowed-output-roots jail, reports free of document text and
  passwords, encrypted-input password flow.
- Docs written: IMPLEMENTATION_PLAN, ARCHITECTURE, ENGINE_DECISIONS,
  THREAT_MODEL, CONVERSION_FIDELITY, LICENSING, FEATURE_MATRIX, CLI, STATUS.
  Product README at repo root (launch-kit README preserved as
  `LAUNCH_KIT_README.md`).

## In progress
- Nothing mid-edit. Working tree is a coherent checkpoint.

## Known gaps inside implemented features (tracked, warned in reports)
- Cross-document page moves do not rebuild outlines/AcroForm/attachments
  (fidelity warnings emitted; see CONVERSION_FIDELITY.md).
- Split-by-bookmarks, n-up, booklet, insert/interleave/blank-page, page
  labels: not implemented (Phase 1 follow-ups).
- Progress bars and `--dry-run` not wired into the CLI yet.
- `ldf doctor` Unicode marks render as `?` under legacy cp1252 consoles
  (output is correct under Windows Terminal/UTF-8; errors="replace" prevents
  crashes).

## Next work, highest priority first
1. **React/TypeScript browser UI (Phase 1 completeness):** real frontend for
   the nine job endpoints + doctor page, bundled into the Python dist
   (no Node at runtime), local PDF.js assets for previews, drag-drop
   organizer with thumbnails (`/api` contract is ready; keep the honest
   feature gating — the current index page shows the pattern).
2. API hardening follow-ups: per-session rate/JOB caps, worker-subprocess
   execution for jobs (currently in-request; fine for Phase 1 sizes),
   background job queue with progress + cancellation endpoints.
3. Phase 1 follow-ups: split-by-bookmarks (pikepdf outline walk),
   insert/interleave/blank pages, n-up/booklet, page-label preservation.
4. Outline/AcroForm preservation for cross-document merges (walk and rebuild
   trees; remove the corresponding fidelity warnings when tested).
5. Phase 2 start: `repair` (pikepdf rewrite + qpdf second opinion when
   installed) and `compress` (image downsampling with quality floor +
   before/after render comparison).

## Decisions already made (do not relitigate without cause)
- Engines: pikepdf primary structural, pypdf fallback, PDFium rendering,
  Pillow imaging. External tools only via allowlisted subprocess runner.
- `last-5` in the range grammar means "the last 5 pages" (documented).
- Split naming: `<stem>-page-NNN.pdf` / `<stem>-part-NNN.pdf` /
  `<stem>-pages-<token>.pdf`.
- Reports: human summary on stdout, `--json` for machines, `--report-dir`
  for files; no document text in reports ever.
- Exit codes: 0/1/2/3/4/5/130 as in docs/CLI.md.
