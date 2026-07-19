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

### Phase 1 — Dependable core PDF tools ✅ core+CLI (this checkpoint), API/UI pending
- merge (whole/ranges), split (ranges/every-N/pages), remove-pages,
  extract-pages, organize (reorder/duplicate), rotate, crop (with
  crop-is-not-redaction warning), images-to-pdf, pdf-to-images.
- Unit + integration + security tests over generated synthetic fixtures.
- Remaining for Phase 1 completeness: local HTTP API and browser UI for these
  operations; n-up/booklet layouts; insert/interleave/blank-page operations;
  outline & form preservation during page moves; page labels.

### Phase 2 — Optimize and convert
- Compression presets with visual-quality floor and honest reporting.
- Repair + inspect inventory; OCR via OCRmyPDF/Tesseract (optional engines);
  Office-to-PDF via isolated headless LibreOffice; HTML-to-PDF; PDF/A with
  veraPDF validation.

### Phase 3 — Markdown workflows
- PDF→Markdown (semantic / layout-aware / RAG / archival bundle) with
  provenance JSON and review UI; Markdown→PDF (Typst present on this machine
  is the candidate renderer; WeasyPrint fallback to evaluate).

### Phase 4 — Editing, forms, security features
- Visual editor (overlay vs existing-content honesty), AcroForm workflows,
  encryption/decryption, real redaction with post-verification, sanitize.

### Phase 5 — Advanced validation and signatures
- Compare, pyHanko signatures, PDF/A / PDF/UA validation surfacing, scanner
  adapters (WIA/TWAIN on Windows first).

### Phase 6 — Packaging and release hardening
- Installers, SBOM, THIRD_PARTY_NOTICES, benchmarks, cross-platform CI,
  container example with `--network none`.

## Standing rules (from the spec, enforced in code and tests)
- No placeholder features: `CAPABILITY_SPECS.implemented` flips only with the
  pipeline + tests in the same change; capability tests assert the honest set.
- Every generated PDF is reopened; risky outputs render all pages.
- Sources immutable; outputs atomic; collisions explicit.
- No network code paths; `--strict-offline` pins the guarantee.
- Reports never contain document text or secrets (tested).
