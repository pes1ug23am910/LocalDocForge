# Getting Started on Windows

A practical, task-oriented guide to using LocalDocForge on Windows 11. Every
original command set in this guide was executed and verified on the primary
workstation on 2026-08-03 (see `docs/MACHINE_READINESS.md` for that evidence
run); the S4 text-extraction and S6 Markdown-rendering additions are recorded
in `docs/STATUS.md`. The authoritative reference for flags, grammar, exit
codes, and the API contract remains `docs/CLI.md`; this guide does not replace
it.

> **Scope honesty:** LocalDocForge is early alpha. What this guide shows —
> PDF organization, page editing, lossless compression, PDF text extraction,
> Markdown→PDF rendering, image↔PDF conversion, inspection, and the localhost
> API — is everything that exists. Lossy
> compression presets, OCR, Office conversion, redaction, signatures, and the
> rest of the roadmap are **not available** and no command in this build
> pretends otherwise. The project's
> own release gate currently marks the tool **not cleared for sensitive
> documents** (`docs/STATUS.md`).

## 1. The environment on this workstation

| Item | Value |
|---|---|
| Repository | `E:\Sem-VI-Break\Pdf-Conversion-Tool` |
| Virtual environment | `.venv` — CPython 3.14.4, dev profile (all extras + test/lint/build tools) |
| Entry points | `.venv\Scripts\ldf.exe` and `.venv\Scripts\localdocforge.exe` (identical) |
| Other interpreters via `py` | 3.14.4 (default), 3.13.5 (Astral/uv-managed), 3.10.11 (unsupported by this package) |
| Shell assumed here | PowerShell 7 |

Three equivalent ways to run the CLI:

```powershell
# 1. Full path — works from any directory, nothing to set up
E:\Sem-VI-Break\Pdf-Conversion-Tool\.venv\Scripts\ldf.exe doctor

# 2. Activate the venv for the session, then use the short name
cd E:\Sem-VI-Break\Pdf-Conversion-Tool
.venv\Scripts\Activate.ps1
ldf doctor

# 3. Put it on PATH permanently (current user; new shells only)
[Environment]::SetEnvironmentVariable('Path',
  $env:Path + ';E:\Sem-VI-Break\Pdf-Conversion-Tool\.venv\Scripts', 'User')
```

The examples below assume `ldf` resolves (option 2 or 3).

## 2. First command: ask the tool what it can do

```powershell
ldf doctor          # human-readable engine + capability list
ldf --json doctor   # machine-readable; same data the status page uses
```

`doctor` is the live truth. A capability is listed as available only when its
implementation and an engine probe both pass; nothing is a placeholder. The
current build has fourteen implemented capability entries: merge, split,
remove-pages, extract-pages, organize, rotate, crop, inspect, compress,
images-to-pdf, pdf-to-images, convert-images, pdf-to-markdown, and
markdown-to-pdf. Runtime availability still depends on the listed engine
probes.

## 3. Everyday recipes

Global flags go **before** the command: `ldf [--json] [--quiet]
[--password-stdin] [--strict-offline] [--report-dir DIR] <command> …`.

### Combine and reorganize PDFs

```powershell
# Merge whole files, in order
ldf merge a.pdf b.pdf c.pdf -o merged.pdf

# Merge selected pages from each input (two spellings, same result)
ldf merge a.pdf --pages 1-5 b.pdf --pages 2-end -o merged.pdf
ldf merge "a.pdf::1,3" "b.pdf::2" -o merged.pdf

# Split: one file per page / by groups of N / by explicit range tokens
ldf split input.pdf -d parts\
ldf split input.pdf -d parts\ --every 10
ldf split input.pdf -d parts\ --pages "1-3,7"

# Delete or keep pages
ldf remove-pages input.pdf --pages "2,5-7" -o out.pdf
ldf extract-pages input.pdf --pages "1-3,last" -o out.pdf

# Reorder / duplicate / reverse
ldf organize input.pdf --order "3,1,2,4-end" -o out.pdf
ldf organize input.pdf --order reverse -o out.pdf
```

Split output names follow `stem-page-NNN.pdf` (per page), `stem-part-NNN.pdf`
(`--every`), or `stem-pages-token.pdf` (`--pages`).

Page-moving operations print `fidelity_warnings` when they detect
document-level structures they cannot preserve (outlines, forms, attachments,
XMP metadata, signatures — signature loss is critical). `remove-pages`
deliberately **refuses** documents whose outlines, forms, named destinations,
internal links, or tagged structure it cannot safely rewrite, instead of
corrupting them.

### Rotate and crop

```powershell
ldf rotate input.pdf --degrees 90 --pages odd -o out.pdf
ldf crop input.pdf --box "50,50,400,500" -o out.pdf
```

Crop sets the PDF CropBox and always warns: **cropping hides content; it is
not redaction.** The bytes remain in the file.

### Compress (lossless)

```powershell
ldf compress input.pdf -o smaller.pdf
```

The only implemented preset is `lossless`: streams are recompressed, object
streams generated, and unused page resources pruned. Image data is never
re-encoded or downsampled, outlines/forms/attachments are preserved, and
sampled pages are rendered and compared pixel-for-pixel against the source —
any difference blocks publication. Text-heavy or loosely-built PDFs shrink
substantially; scan/photo PDFs often barely shrink because their bytes are
already JPEG data, and the report says so (`compress-no-reduction`) instead of
pretending. `--preset balanced/aggressive/archival` are planned lossy presets
and are refused with exit 2 until they actually exist.

### Images ↔ PDF

```powershell
# JPG/PNG/TIFF/BMP/WebP → PDF; multipage TIFF becomes multiple pages;
# EXIF orientation is honored. Globs are expanded by ldf itself.
ldf images-to-pdf scans\*.jpg -o scans.pdf --page-size A4 --fit fit
ldf images-to-pdf photo.png -o photo.pdf --page-size image

# PDF → PNG/JPEG/WebP/TIFF at 18–1200 DPI, optionally a page subset
ldf pdf-to-images input.pdf -d pages\ --format png --dpi 300
ldf pdf-to-images input.pdf -d pages\ --format jpeg --dpi 150 --pages odd
ldf pdf-to-images scanned.pdf -d vision\ --preset llm
```

`--page-size` accepts `A4`, `Letter`, `Legal`, `image`, or `WxH` with a
`pt|mm|cm|in` suffix (default mm), e.g. `210x297mm`. Images-to-PDF accepts
36–600 DPI. Values that would blow past the configured pixel/byte/page bounds
are rejected up front (§6).

### Extract PDF text for agents

```powershell
# Markdown is the default; each occurrence starts with <!-- ldf:page N -->
ldf pdf-to-md input.pdf -o content.md

# Plain text subset; use --no-page-anchors for form-feed page separators
ldf pdf-to-md input.pdf -o content.txt --format txt --pages "1-5,9"

# One strict-schema JSON object per selected page occurrence
ldf pdf-to-md input.pdf -o content.jsonl --format jsonl
```

The output file is always written explicitly as UTF-8 with LF line endings;
stdout carries only the report. Markdown headings and top-to-bottom/left-to-
right reading order are heuristics. The extractor normalizes Unicode to NFC but
does not silently dehyphenate or repair bidi order. Columns, rotated/angled
text, RTL scripts, and flattened tables can require review. Stable report codes
are `no-text-layer`, `headings-inferred`, `reading-order-uncertain`, and
`tables-flattened`; exact affected pages appear in
`details.coverage.per_page[].warning_codes`. If a page has no text layer, use
`ldf pdf-to-images input.pdf -d vision\ --preset llm` for vision input; OCR is
not implemented.

The full format/separator contract, exact JSONL keys, coverage schema, and
limitations are in `docs/CLI.md` and `docs/CONVERSION_FIDELITY.md`.

### Render Markdown to a validated PDF

```powershell
ldf md-to-pdf notes.md -o notes.pdf --paper A4 --margin 20 --toc
```

This machine's Typst 0.15.1 probe enables the command. Inputs must be strict
UTF-8 `.md`/`.markdown` files. The supported subset covers ordinary CommonMark,
code, safe web/mail/telephone links, and GFM tables; local raster images may be
referenced relative to the Markdown file but cannot escape its directory.
Remote assets, imports/packages, raw HTML, footnotes, and math are not executed;
unsupported constructs are dropped with source-line warnings. All generated
PDF pages are reopened, syntax-checked, and rendered before publication. See
`docs/CLI.md` for the precise subset, warning codes, and sandbox limitations.

### Inspect without modifying

```powershell
ldf inspect input.pdf          # pages, sizes, encryption, annotations,
ldf --json inspect input.pdf   # + page_text_stats and text_coverage
```

`page_text_stats` contains one `{page, char_count, has_text_layer}` record per
source page; `text_coverage` summarizes pages and character counts. These are
extraction-vs-render decision signals, not a cheap probe or a reading-order
guarantee: inspect still walks PDFium text rectangles, so dense or adversarial
pages can take substantial time.

### Encrypted inputs

Password-protected PDFs use this precedence: global `--password-stdin` (one
UTF-8 line from stdin) → `LDF_PASSWORD` → hidden interactive prompt
(TTY only). The password value is never accepted as a command-line argument or
written to stdout, stderr, logs, or reports. A non-interactive call with no
source exits 2 and names both mechanisms. One value applies to every encrypted
input in that invocation; files needing different passwords fail clearly.
Outputs are **not** re-encrypted; every such conversion emits a critical
`input-encryption-removed` warning. (There is no protect/unlock operation yet.)
For interactive terminal entry, omit `--password-stdin` so the fallback prompt
hides input; the explicit flag always performs its documented raw line read.
Windows NUL, redirected files, and pipes are non-interactive even though some
runtimes label NUL a character device. An exported empty `LDF_PASSWORD` is an
explicit empty credential, not an absent source; remove the variable when you
want missing-password guidance or the hidden console prompt.

## 4. Behaviors worth knowing before you rely on them

- **Collisions fail by default.** If the output path exists, the command exits
  with code 5 and touches nothing. Use `--collision rename` (produces
  `name (1).pdf`) or `--collision overwrite`. Writing an output onto an input
  is refused even with `overwrite`.
- **Every candidate is validated before publication.** Generated PDFs are
  structurally reopened (pikepdf) and render-checked (PDFium; all pages for
  high-risk cases, otherwise up to 20 sampled pages); generated images must
  decode; generated Markdown/TXT/JSONL must pass strict UTF-8, provenance, and
  exact-schema/count checks. Files are published atomically — you never get a half-written output —
  but multi-output rollback after a handled failure is best-effort, not a
  crash-proof transaction.
- **Reports.** Human summary by default, `--quiet` suppresses it, `--json`
  emits machine-readable output, `--report-dir DIR` additionally writes
  `<operation>-<job-id>.report.json` and `.txt`. Reports omit document text and
  passwords but intentionally name your input/output files.
- **Page-range grammar** (full spec in `docs/CLI.md`): `7` · `2-9` · `9-2`
  (descending) · `12-end` · `odd` · `even` · `all` · `reverse` · `last` ·
  `last-5` (final five pages) — comma-combinable, duplicates preserved.
- **Exit codes:** 0 success · 1 operation failed · 2 usage/range error ·
  3 no engine · 4 output failed validation · 5 collision refused ·
  130 cancelled/timeout.

## 5. The localhost web API (`ldf web`)

```powershell
ldf web                    # http://127.0.0.1:8477/, loopback only
ldf --strict-offline web   # additionally refuse any non-local configuration
ldf web --port 9000        # different loopback port
```

Startup prints a random session token. Every `/api` request must carry it in
the `X-LDF-Token` header; requests without it get 401 (verified live on this
machine). The `/` status page is an honest capability listing — no fake
buttons. Working PowerShell session, exactly as verified:

```powershell
$tok = @{ 'X-LDF-Token' = '<token printed at startup>' }

Invoke-RestMethod http://127.0.0.1:8477/api/health -Headers $tok
Invoke-RestMethod http://127.0.0.1:8477/api/capabilities -Headers $tok

# Submit a merge job (multipart field name is 'files'; server names all paths)
$form = @{ files = @(Get-Item a.pdf; Get-Item b.pdf) }
$job = Invoke-RestMethod -Method Post http://127.0.0.1:8477/api/jobs/merge `
  -Headers $tok -Form $form

# Download the result, then delete the private server copy
Invoke-WebRequest "http://127.0.0.1:8477/api/jobs/$($job.job_id)/outputs/0" `
  -Headers $tok -OutFile merged.pdf
Invoke-RestMethod -Method Delete `
  "http://127.0.0.1:8477/api/jobs/$($job.job_id)" -Headers $tok
```

Operations: `merge`, `split`, `remove-pages`, `extract-pages`, `organize`,
`rotate`, `crop`, `compress`, `images-to-pdf`, `pdf-to-images`,
`convert-images`, `pdf-to-md` — form fields
per operation are tabulated in `docs/CLI.md`. Add `Prefer: respond-async` (or `?async=true`)
for a 202 + status/events/cancel URLs instead of the default synchronous 201.

Every accepted job runs in a **fresh spawned worker process**; on Windows the
worker is assigned to a Job Object (kill-on-close, memory, CPU-time, and
active-process limits) before it is allowed to touch document bytes, and a job
only reports success after Job accounting proves the process tree exited empty.
This was observed live on this machine (`docs/MACHINE_READINESS.md`, §4). The
worker is a failure boundary with the user's filesystem authority — not an OS
sandbox; see `docs/THREAT_MODEL.md`.

Defaults: 2 concurrent workers, 16 queued jobs, 4 active jobs per client,
30 submissions/60 s per client, 2 GiB upload cap. Over-limit requests get
429/503 with `Retry-After`.

`--allow-nonlocal` exists, is deliberately labelled dangerous, provides no TLS,
and is refused outright in strict-offline mode. Don't use it for anything you
care about.

Stop the server with Ctrl+C in its console; that path is regression-tested to
exit 0 and remove the leased session directory. If the process is killed hard
instead, the next startup removes the residue only after acquiring the
session's OS-held lease (fail-closed; see `README.md` privacy limits).

## 6. Configuration

Sources in precedence order: CLI flags → `LDF_`-prefixed environment variables
(`__` for nesting) → private-by-default built-ins (`src/localdocforge/config/settings.py`).

```powershell
$env:LDF_STRICT_OFFLINE = 'true'          # same as passing --strict-offline
$env:LDF_COLLISION = 'rename'             # default collision policy
$env:LDF_JOBS_ROOT = 'D:\ldf-scratch'     # per-job scratch location (default: system temp)
$env:LDF_BIND_PORT = '9000'               # ldf web default port
$env:LDF_LIMITS__MAX_INPUT_BYTES = '1073741824'   # nested limit override (1 GiB)
$env:LDF_API_MAX_CONCURRENT_JOBS = '4'
```

Key per-job bounds (defaults chosen to be private, local, bounded; a bound set
to `None` is disabled — see `ResourceLimits` in
`src/localdocforge/domain/models.py`): input 2 GiB · output
4 GiB · temporary 8 GiB · worker memory 2 GiB · CPU 600 s · wall clock 600 s ·
5000 pages · 200 MP per image · 4 GiB decompressed · 8 subprocesses.

## 7. What is *not* here yet, and the external engines

`ldf doctor` on this machine shows the truth; summarized:

| External engine | Installed here? | Needed by (future phase) |
|---|---|---|
| Typst 0.15.1 | ✅ (winget) | Markdown→PDF — **wired and available when the ≥0.15.1 probe passes** |
| qpdf CLI | ❌ `winget install qpdf.qpdf` | repair/compression diagnostics (P2) |
| Tesseract + OCRmyPDF | ❌ `winget install UB-Mannheim.TesseractOCR` | OCR (P2) |
| Ghostscript | ❌ `winget install ArtifexSoftware.GhostScript` | PDF/A (P2) |
| LibreOffice | ❌ `winget install TheDocumentFoundation.LibreOffice` | Office→PDF (P2) |
| Pandoc | ❌ `winget install JohnMacFarlane.Pandoc` | possible future document paths; not used by pdf-to-md |
| veraPDF | ❌ verapdf.org installer | PDF/A validation (P2/P5) |

Installing an executable alone does not unlock a capability: its pipeline,
registry bit, tests, and compatible live probe must all agree
(`docs/FEATURE_MATRIX.md` rules). Typst is the one currently wired optional
executable; the other listed tools remain future inputs.

## 8. Sensitive documents — read before trusting it with them

The project's own release gate currently says **FAIL / not cleared for
sensitive documents** (`docs/STATUS.md`), even though everything above works.
The honest reasons, condensed:

- Bundled OpenJPEG 2.5.4 and libheif 1.23.0 have recorded advisories; PDFium and
  the other version-unknown native inventory total 19 advisory-unknown children.
- `--strict-offline` is application policy plus Python-level socket guards —
  **not** an OS firewall. No OS-enforced outbound+DNS denial has been proven on
  this machine.
- CLI parsing runs in-process (cooperative timeouts only); API workers retain
  your filesystem authority — containment is a failure boundary, not a sandbox.
- Deleting temp files on an SSD is best-effort, not forensic erasure; crop is
  never redaction; there is no redaction.

For ordinary local documents this is a working, validated toolset. For
documents whose exposure would hurt you, wait for the blockers in
`docs/STATUS.md` to close, or add OS-level isolation yourself.

## 9. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| exit 5, "output exists" | Default collision policy. Add `--collision rename` or `overwrite`. |
| exit 2 on a page range | Range grammar or out-of-bounds page (pages are 1-based; `0` is invalid). |
| `remove-pages` refuses the file | Deliberate: the document has structures this build can't safely rewrite (outlines/forms/links/tags). Use `extract-pages` of the complement only if you accept the documented losses. |
| DPI/size value rejected | Outside 36–600 (images-to-pdf) / 18–1200 (pdf-to-images), or would exceed §6 limits. |
| File rejected before conversion | Content sniffing found an extension/content mismatch, or the PDF is syntax-damaged (repair is not implemented — the tool won't silently "fix" your file). |
| `compress` barely shrinks a file | The input is image-heavy or already optimized; lossless mode never re-encodes images. The report's `compression` details show exact before/after bytes. |
| `pdf-to-md` reports `no-text-layer` | The page is scanned/image-only. Use `pdf-to-images --preset llm`; OCR is not shipped yet. |
| 401 from every API call | Missing/wrong `X-LDF-Token` header — a browser cookie alone never authorizes API calls. |
| 429/503 from the API | Queue/rate/per-client caps (§5). Honor `Retry-After`. |
| Password prompt appears | Input is encrypted and stdin is interactive. Type it (hidden), or for a non-interactive invocation use global `--password-stdin` or `LDF_PASSWORD`; it is used to unlock only and never written to output/reports/logs. |

## 10. Keeping the installation healthy

```powershell
# Full local release gate (~5 min): locks, lint, types, both full test runs,
# reproducible builds, artifact drift, clean profile matrix
.venv\Scripts\python.exe scripts\release_gate.py `
  --profile-evidence packaging-evidence\windows-3.14.4.json

# Faster individual checks
.venv\Scripts\python.exe -m pytest tests -q
.venv\Scripts\python.exe -m ruff check src tests scripts
.venv\Scripts\python.exe -m mypy
.venv\Scripts\python.exe scripts\lock_profiles.py --check
.venv\Scripts\python.exe scripts\generate_release_artifacts.py --check
```

Dependency updates go through `scripts\lock_profiles.py --write` (see
`docs/PACKAGING.md`) — never edit lock files by hand. Built artifacts live in
`dist\windows-11-x64\`; executed evidence in `packaging-evidence\`.

## Related documents

- `docs/MACHINE_READINESS.md` — the dated readiness verdict and full evidence
  run for this workstation
- `docs/CLI.md` — complete CLI/API reference
- `docs/FEATURE_MATRIX.md` — per-capability status, tests, limitations
- `docs/PACKAGING.md` — install profiles, locks, reproducible builds
- `docs/THREAT_MODEL.md` — what is and is not defended against
- `docs/STATUS.md` — release posture and remaining blockers
