# LocalDocForge

A privacy-focused document-processing workbench: a typed Python core library,
a scriptable CLI, and a localhost API with a minimal status page. The current
package has no outbound network client, telemetry, update check, or remote
browser asset. A full browser workflow is still planned.

> **Project status: early alpha.** Phase 0 (foundation) and the core of
> Phase 1 (structural PDF tools + image conversion) are implemented and
> tested. Everything else in the roadmap is *not yet available* and is
> honestly reported as such by `ldf doctor`. See `docs/FEATURE_MATRIX.md`.

## Privacy guarantees (and their limits)

- The shipped operations run on the host machine and contain no cloud
  conversion or outbound network-resource loading. The web command listens on
  loopback by default; non-loopback serving requires an explicit dangerous
  opt-in.
- `--strict-offline` is recorded in reports and rejects recognizable network
  filesystem paths, network report/job/output locations, and non-loopback web
  serving. It is an application policy, not an operating-system firewall;
  container or host network isolation remains the stronger defense.
- Reports omit document text and passwords. Public API reports also replace
  server-side paths with artifact basenames; CLI reports intentionally name
  the user-selected input and output files.
- Limits, honestly stated: deleting temp files on SSDs is best-effort, not
  forensic erasure; a crash can leave a private session directory until a
  later startup sweep; and in-process PDF/image parsers execute with your user
  privileges (see `docs/THREAT_MODEL.md` for the full model).

## What works today (verified by the test suite)

| Area | Capabilities |
|---|---|
| Organize | merge (whole files or per-input page ranges) · split (ranges / every-N / single pages) · remove pages · extract pages · reorder/duplicate/reverse. Page-moving operations warn about known document-level losses; remove-pages refuses structures it cannot safely rewrite. |
| Edit | rotate · crop (with an explicit **crop is not redaction** warning) |
| Convert | images → PDF (JPG/PNG/TIFF/BMP/WebP, multipage TIFF, EXIF orientation, A4/Letter/Legal/image/custom page sizes, 36–600 DPI) · PDF → images (PNG/JPEG/WebP/TIFF, 18–1200 DPI) |
| Inspect | page count, encryption, page sizes, annotations, outlines, forms, attachments, JavaScript presence |
| Safety net | every candidate PDF is structurally reopened and render-checked (all pages for selected high-risk cases, otherwise up to 20 sampled pages); generated images must decode. All candidates validate before publication, each final file is published atomically, and input/output aliases are refused. Multi-output rollback after a handled publication failure is best effort, not a process-crash transaction. |

## Quick start

```powershell
# Windows (PowerShell) — from the repository root
py -3.12 -m venv .venv           # 3.12+ (this repo is developed on 3.14)
.venv\Scripts\python.exe -m pip install -r requirements-lock.txt
.venv\Scripts\python.exe -m pip install -e . --no-deps

.venv\Scripts\ldf.exe doctor     # what can this machine do right now?
```

```bash
# macOS/Linux
python3 -m venv .venv
.venv/bin/pip install -r requirements-lock.txt
.venv/bin/pip install -e . --no-deps
.venv/bin/ldf doctor
```

Or run `scripts/bootstrap.ps1` / `scripts/bootstrap.sh`.

## CLI examples (all verified in this repository)

```powershell
ldf merge a.pdf b.pdf -o merged.pdf
ldf merge a.pdf --pages 1-5 b.pdf --pages 2-end -o merged.pdf
ldf split input.pdf -d parts/ --every 10
ldf remove-pages input.pdf --pages "2,5-7" -o out.pdf
ldf extract-pages input.pdf --pages "1-3,10" -o out.pdf
ldf organize input.pdf --order "3,1,2,4-end" -o out.pdf
ldf rotate input.pdf --degrees 90 --pages odd -o out.pdf
ldf crop input.pdf --box "50,50,400,500" -o out.pdf    # warns: NOT redaction
ldf images-to-pdf scans/*.jpg -o scans.pdf --page-size A4
ldf pdf-to-images input.pdf -d pages/ --format png --dpi 300
ldf inspect input.pdf
ldf --json doctor
ldf --strict-offline web  # localhost API + status shell; prints the session token
```

The `ldf web` server binds to 127.0.0.1 by default, authenticates every API
call with a per-session token header, sends CSP/hardening headers, and keeps
job history in memory only. Outputs remain in a private session directory
until deletion, eviction, or graceful shutdown; crashed-session cleanup is
best effort on a later startup. `--allow-nonlocal` is an explicit dangerous
opt-in and is refused when strict-offline mode is active. Endpoint reference:
`docs/CLI.md`.

Page ranges: `1-5,9,12-end`, `odd`, `even`, `reverse`, `last`, `last-5`
(the last five pages). Full grammar and exit codes: `docs/CLI.md`.

Encrypted PDFs prompt for the password interactively (hidden input);
passwords are never taken as command-line arguments and never logged.

## Honesty rules baked into the product

- A capability shows as available only when its implementation **and** a live
  engine probe both pass — placeholder buttons do not exist.
- Cropping, whiteout, and covering content are never called redaction.
- Conversions report detected, known preservation losses through stable
  `fidelity_warnings` codes. The warning set is not a claim of exhaustive
  semantic equivalence; see `docs/CONVERSION_FIDELITY.md`.
- A file will only ever be labelled PDF/A-compliant after an authoritative
  local validator passes it (validator integration is a later phase — today
  nothing is labelled PDF/A).

## Development

```powershell
.venv\Scripts\python.exe -m pytest tests -q     # run the complete suite
.venv\Scripts\python.exe -m ruff check src tests
```

Test fixtures are synthetic and generated by
`tests/fixtures/make_fixtures.py` — no third-party documents.

Repository map: `src/localdocforge/` (domain, security, jobs, engines,
pipelines, operations, validation, reporting, config, cli) · `tests/`
(unit, integration, security, fixtures) · `docs/` (architecture, threat
model, engine decisions, feature matrix, status).

## Roadmap

Compression, repair, OCR, Office↔PDF, PDF/A, PDF↔Markdown, editor, forms,
encryption, redaction, signatures, compare, scanner acquisition, full browser
UI — phased plan in `docs/IMPLEMENTATION_PLAN.md`, current truth in
`docs/STATUS.md`.

## License

MIT. Dependency and external-engine licensing: `docs/LICENSING.md`.
