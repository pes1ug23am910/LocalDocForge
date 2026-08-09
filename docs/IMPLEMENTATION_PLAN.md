# LocalDocForge — Implementation Plan

This plan records the phased implementation requirements and release criteria.
Live progress: `docs/STATUS.md`. Honest capability state: `docs/FEATURE_MATRIX.md`.

## Delivery phases

### Phase 0 — Foundation ✅ (this checkpoint)
- `src/` package layout, `pyproject.toml`, editable install, lockfile.
- Typed domain models (Pydantic v2): specs, artifacts, reports, warnings, limits.
- Page-range grammar (`1-5,9,12-end`, `odd`, `even`, `reverse`, `last-5`, …).
- Security primitives: magic-byte sniffing, path containment, filename
  sanitization, hardened subprocess runner with executable allowlist.
- Job isolation: per-job workspaces, atomic publish, collision policies,
  startup sweep of stale workspaces.
- Engine adapter contract + registry with live probes; honest capability
  gating (implemented && probed, never either alone).
- CLI skeleton: `ldf doctor`, `ldf inspect`, global `--json/--quiet/
  --strict-offline/--report-dir`, stable exit codes.
- Pipeline runner: sniff → limits → workspace → execute → validate (reopen +
  render) → atomic publish → report → cleanup.

### Phase 1 — Dependable core PDF tools ✅ core + CLI + local API; React UI pending
- merge (whole/ranges), split (ranges/every-N/pages), remove-pages,
  extract-pages, organize (reorder/duplicate), rotate, crop (with
  crop-is-not-redaction warning), images-to-pdf, pdf-to-images.
- Unit + integration + security tests over generated synthetic fixtures.
- Remaining for Phase 1 completeness: the full React browser UI for these
  operations; n-up/booklet layouts; insert/interleave/blank-page operations;
  outline & form preservation during page moves; page labels.

### Phase 2 — Optimize and convert (started 2026-08-03)
- Compression: ✅ lossless structural preset (stream recompression, object
  streams, unused-resource pruning, sampled render-identity verification
  against the source). Lossy presets with a visual-quality floor, custom
  DPI/quality, and target-size search remain pending.
- Repair + inspect inventory; OCR via OCRmyPDF/Tesseract (optional engines);
  Office-to-PDF via isolated headless LibreOffice; HTML-to-PDF; PDF/A with
  veraPDF validation.

### Phase 3 — Markdown workflows
- ✅ PDF→Markdown/plain-text/JSONL core extraction via PDFium: explicit UTF-8,
  page provenance, per-page coverage, deterministic heading/reading-order
  heuristics, and stable uncertainty warnings. Structured table extraction,
  richer semantic/layout reconstruction, RAG/archival bundles, and a review UI
  remain pending.
- ✅ Markdown→PDF via Typst 0.15.1+: strict-UTF-8 CommonMark subset plus GFM
  tables, A4/Letter/Legal and margin/TOC controls, contained local raster
  images, context-safe text escaping, honest dropped-construct/source-line
  warnings, hard tool timeout, dependency/package audit, and standard full-PDF
  validation. Richer themes, math, footnotes, raw HTML, remote assets, and a
  WeasyPrint fallback remain future work.

### Phase 4 — Editing, forms, security features
- Visual editor (overlay vs existing-content honesty), AcroForm workflows,
  encryption/decryption, real redaction with post-verification, sanitize.

### Phase 5 — Advanced validation and signatures
- Compare, pyHanko signatures, PDF/A / PDF/UA validation surfacing, scanner
  adapters (WIA/TWAIN on Windows first).

### Phase 6 — Packaging and release hardening
- Implemented in this sprint: Lite/Standard/Full dependency profiles,
  universal hash locks, reproducible wheel/sdist gate, profile SBOM/notices,
  and a Windows/Linux/macOS CI matrix definition.
- Remaining: completed real-runner evidence on every claimed platform/Python,
  installers, benchmarks, and a container example with `--network none`.

## Standing rules (from the spec, enforced in code and tests)
- No placeholder features: `CAPABILITY_SPECS.implemented` flips only with the
  pipeline + tests in the same change; capability tests assert the honest set.
- Every generated PDF is reopened; risky outputs render all pages. Non-PDF
  candidates use operation-specific validators before publication; PDF text
  output requires strict UTF-8, provenance/record cardinality, exact JSONL
  schema, and report-coverage consistency.
- Sources immutable; outputs atomic; collisions explicit.
- No shipped outbound client; `--strict-offline` enforces the documented
  application boundary but is not represented as an OS network sandbox.
- Reports never contain document text or secrets (tested).
