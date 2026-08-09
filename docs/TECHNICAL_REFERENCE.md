# Technical Reference

The one-document consolidation of how LocalDocForge works, subsystem by
subsystem, with the concrete constants, contracts, and invariants that the
per-topic documents explain in prose. Written 2026-08-03 against the code as
shipped (407-test suite and full release gate passing that day), with the S4
PDF text-extraction and S6 Markdown-rendering surfaces updated on 2026-08-09. Where a
per-topic document is the authority, it is linked; when this file and the code
disagree, the code and its tests win.

Contents: [1 Stack](#1-system-identity-and-stack) · [2 Layout](#2-package-layout) ·
[3 Domain](#3-domain-layer) · [4 Security primitives](#4-security-primitives) ·
[5 Workspaces & publication](#5-job-workspaces-and-atomic-publication) ·
[6 Engines & capabilities](#6-engines-and-the-capability-registry) ·
[7 Pipeline](#7-the-pipeline-lifecycle) · [8 Operations](#8-operation-catalog) ·
[9 Limits](#9-resource-limits) · [10 Configuration](#10-configuration) ·
[11 CLI](#11-cli) · [12 Local API](#12-local-api-and-worker-containment) ·
[13 Reporting](#13-reporting-and-scrubbing) · [14 Packaging](#14-packaging-and-reproducibility) ·
[15 Testing](#15-testing-and-quality-gates) · [16 Security posture](#16-security-posture-in-one-view)

## 1. System identity and stack

| Component | Choice | Notes |
|---|---|---|
| Language / runtime | CPython 3.12–3.14 (`>=3.12,<3.15`) | 3.14.4 is the primary executed platform; 3.13.5 side evidence; 3.12 declared only |
| Models/validation | Pydantic v2 (+ pydantic-settings) | every report/spec/settings object is typed |
| CLI | Typer | stable exit codes, shell completion built in |
| Structural PDF engine | pikepdf 10.x (libqpdf 12.x) | merge/split/remove/extract/organize/rotate/crop/inspect/compress |
| Renderer/text engine | pypdfium2 5.12.1 (PDFium 152.0.7947.0) | validation renders, PDF→images, compress pixel-compare, PDF→Markdown/text/JSONL, inspect text coverage |
| Imaging | Pillow 12.x | image decode/encode, images→PDF, decompression-bomb guard |
| Diagnostic PDF lib | pypdf (Full profile) | probed, used in tests; deliberately **not** an operation engine |
| API service | FastAPI + Uvicorn + python-multipart (Standard profile) | loopback by default |
| Build backend | setuptools==83.0.0, hash-pinned, isolated | see §14 |
| Resolver/locks | uv 0.11.26 (pinned), SHA-256 marker-aware exports | see §14 |

The shipped package contains **no outbound network client, telemetry, update
check, or remote browser asset** — verified by source inspection and
socket-denial test runs (`docs/THREAT_MODEL.md` §T6).

## 2. Package layout

```text
src/localdocforge/
  domain/      typed models, warnings, limits, page-range grammar  (no I/O)
  security/    sniffing, path containment, filenames, subprocess runner
  jobs/        per-job workspaces, atomic publication, stale sweep
  engines/     adapter contract, live probes, capability registry
  pipelines/   runner.py — the one job lifecycle every operation uses
  operations/  organize.py (structural) · optimize.py (compress) · images.py · text.py
  validation/  pikepdf reopen + syntax + PDFium render; image/text checks
  reporting/   JSON + human report writers
  config/      LDF_* settings, precedence, strict-offline validation
  cli/         Typer app (`ldf` / `localdocforge`)
  api/         app.py (admission/transport/routes) · worker.py (containment)
```

Interfaces never call document engines directly; operations own engine calls
and hand an `execute()` closure to the pipeline runner.

## 3. Domain layer

### 3.1 Core models (`domain/models.py`)

- `ConversionReport` — the result object of every operation: `status`
  (`success`/`failed`/`cancelled`), `job_id`, `engine`+`engine_version`,
  `inputs`/`outputs` (path, media type, bytes, pages, optional sha256),
  byte/page totals, timing, `security_warnings`, `fidelity_warnings`,
  `errors`, `validation` (per-check results), and operation-specific
  `details`. Never contains document text or passwords; `pdf-to-md` puts only
  coverage counts and warning codes in `details`. `to_human()` renders
  the CLI summary; it is a Pydantic model, so `--json` is just
  `model_dump_json()`.
- `SecurityWarning` / `FidelityWarning` — stable `code` + message + severity
  (`info`/`warning`/`critical`); fidelity warnings may carry a page number.
  The code vocabulary is documented in `docs/CONVERSION_FIDELITY.md`.
- `JobContext` — runtime handle given to `execute()`: workspace path, limits,
  `check_cancelled()` (raises `JobCancelled` on cancel **or** cooperative
  timeout), and `emit()` for progress events.
- `OperationSpec` — base for typed parameter objects, `extra="forbid"`.

### 3.2 Page-range grammar (`domain/pages.py`)

`PageRange(spec=...)` validates syntax eagerly and resolves lazily against the
real page count. Grammar per comma-separated token: `7` · `2-9` · `9-2`
(descending) · `12-end` · `odd` · `even` · `all` · `reverse` · `last` /
`end` · `last-N` (final N pages). Duplicates are preserved (`organize` uses
this to duplicate pages). Out-of-range pages raise `PageRangeError` at
resolve time — page numbers are 1-based; `0` is invalid.

## 4. Security primitives

### 4.1 Content sniffing (`security/sniff.py`)

`detect_media_type` reads the leading 4,096 bytes and matches real signatures
(PDF, JPEG, PNG, TIFF, BMP, WebP…); extensions are never trusted.
`require_media_type` raises `ContentTypeError` on mismatch — a PNG named
`.pdf` is refused before any engine opens it.

### 4.2 Path containment (`security/paths.py`)

`validate_path_before_access` is applied wherever a path crosses a trust
boundary (config, inputs, outputs, report dirs, downloads):

- Windows form validation *before* filesystem access: distinguishes local
  extended paths (`\\?\C:\...`) from extended UNC; rejects `\\.\` device
  namespaces, NTFS alternate data streams, reserved device names (`CON`,
  `NUL`, `COM1`, … including with extensions), and trailing-dot/space
  aliases.
- Existing reparse points (junctions/symlinks) are rejected root-to-leaf
  without following them.
- Strict-offline additionally requires `GetDriveTypeW` to confirm a local
  drive and rejects UNC/mapped roots before any metadata query.
- `is_remote_path` is the lexical+drive-type network detector (POSIX network
  mounts at ordinary paths are honestly documented as undetectable).
- `ensure_contained`/`is_within` resolve fully (following links) and require
  containment under an allowed root.

### 4.3 Filename sanitization (`security/filenames.py`)

`sanitize_filename` strips control/format characters and `<>:"/\|?*`,
neutralizes Windows reserved names, and truncates to 150 UTF-8 bytes with a
fallback name — applied to upload names and generated output stems.

### 4.4 Hardened subprocess runner (`security/subproc.py`)

For optional external engines only (no shipped conversion needs one):
allowlisted logical tool names; argument arrays with `shell=False`; executable
resolution from absolute, non-network PATH entries only (never the CWD);
minimal environment (safe key allowlist; path-like values must be local);
working directory defaults to the executable's directory; stdout/stderr
drained with a hard retention cap; timeout and cancellation terminate the
tool's process group (standalone calls get their own group; calls inside an
API worker deliberately stay in the worker's group so the supervisor owns
them). `probe()` uses this runner for `<tool> --version`.

## 5. Job workspaces and atomic publication (`jobs/workspace.py`)

- Every job gets a private `ldf-job-<uuid>` directory (0700-style creation)
  under `LDF_JOBS_ROOT` or the system temp; candidates and temp paths are
  re-contained beneath it (`workspace.contain`).
- `atomic_publish`: copy candidate to a hidden same-directory staging file,
  fsync, then — `overwrite` → `os.replace`; `fail`/`rename` → `os.link` as an
  atomic no-clobber; `rename` probes `name (1).pdf`, `name (2).pdf`, … Each
  final file is individually atomic; a concurrent create cannot silently
  defeat `fail`.
- Input/output aliasing (including via hard links) is refused even under
  `overwrite`; duplicate destinations across candidates are refused.
- `cleanup_stale_workspaces` sweeps CLI leftovers older than 24 h at startup;
  `remove_tree_with_retries` handles Windows file-lock retries, and an
  incomplete removal becomes a **critical** report warning, never silence.

## 6. Engines and the capability registry (`engines/`)

- `EngineAdapter.probe() -> EngineInfo` must return unavailability instead of
  raising; probes are cached per process. `supported_operations()` declares
  operation ids (`OP_MERGE` … `OP_COMPRESS`, `OP_RENDER`,
  `OP_PDF_TO_IMAGES`, `OP_PDF_TO_MD`, `OP_MD_TO_PDF`, `OP_IMAGES_TO_PDF`).
- `EngineRegistry.engine_for(operation, preferred=None)` returns the first
  available supporting engine or raises `EngineUnavailableError` with install
  hints; a `preferred` engine must both support the operation and probe
  available.
- `CAPABILITY_SPECS` is the single source of product claims:
  `available = implemented && live probe passed`. Flipping `implemented`
  requires the pipeline + tests in the same change
  (`tests/unit/test_registry.py` enforces the honest set). Doctor, the API
  `/api/capabilities`, and the status page all read this one registry.
- An installed executable never lights a feature by itself. Typst now declares
  only `OP_MD_TO_PDF`, requires a parseable version ≥0.15.1, and still needs the
  matching implemented registry spec before the capability can be available.

## 7. The pipeline lifecycle (`pipelines/runner.py`)

Every operation runs the same eight steps (authoritative prose:
`docs/ARCHITECTURE.md`):

1. **Inputs**: strict-offline network-path refusal → signature sniff (or the
   explicit `.md`/`.markdown` + full strict-UTF-8/NUL-free boundary) → cumulative
   `max_input_bytes` → best-effort page counts (re-counted after password
   opening so encryption cannot bypass the page cap).
2. **Workspace**: private `ldf-job-<uuid>`; `JobContext` carries limits +
   cancellation.
3. **Execute**: the operation writes candidates only inside the workspace and
   calls `check_cancelled()` between units of work (CLI timeouts are
   cooperative; API preemption is the worker's job, §12).
4. **Pre-publication boundaries**: canonicalize once; strict output policy,
   `allowed_output_roots` containment, duplicate-destination and
   input-alias refusal; aggregate candidate bytes vs `max_output_bytes`
   (a publication limit, not a workspace quota).
5. **Collision check** (`fail` short-circuits before any validation cost).
6. **Validate every candidate** before anything is published:
   PDFs — nonempty, signature check, pikepdf reopen, ≥1 page, **zero parser
   syntax warnings** (repair is not silently performed), expected page count,
   PDFium render at scale 0.5 (~36 dpi): **all pages** for high-risk
   candidates (`render_all=True`: crop, compress), otherwise ≤20 evenly
   sampled pages including both endpoints; blank pages recorded (grayscale
   floor 250), only rejected when a caller forbids all-blank. Images must
   fully decode. A deterministic per-candidate validator hook covers other
   media types: `pdf-to-md` requires strict UTF-8, coverage schema,
   anchor cardinality, and exact JSONL record/schema/count agreement.
7. **Publish** all candidates atomically (§5); handled multi-output failure
   rolls back new files and restores overwrite backups (best-effort, not
   crash-transactional).
8. **Report + cleanup**: `ConversionReport` finalized; workspace removed on
   every path; incomplete cleanup → critical warning.

Failures raise `PipelineError` with the failed report attached
(`error.report`); `EncryptedInputError` signals a password retry.

## 8. Operation catalog

| Operation | Engine | Mechanism and specifics |
|---|---|---|
| merge | pikepdf | whole files or per-input `PageRange`s appended into `pikepdf.new()`; docinfo copied from first source; form-field name-conflict detection; page-moving fidelity warnings (§8.1) |
| split | pikepdf | per-token (`stem-pages-<token>.pdf`), every-N (`stem-part-NNN.pdf`), or per-page (`stem-page-NNN.pdf`); repeated selections get `-repeat-NNN` |
| remove-pages | pikepdf | deletes in reverse order; **refuses** documents with outlines, forms/signatures, page labels, open actions, tagged structure, named destinations, or internal links it cannot safely rewrite; refuses emptying the document |
| extract-pages / organize | pikepdf | copy selected/ordered pages to a new document (duplicates allowed in organize) |
| rotate | pikepdf | relative `/Rotate` on selected pages; multiples of 90 only |
| crop | pikepdf | sets `/CropBox`, clamped to the media box (`crop-clamped`); non-intersecting boxes refused; always emits `crop-is-not-redaction`; renders **all** pages at validation |
| inspect | pikepdf + PDFium text API | read-only inventory: version, encryption, page count/sizes, annotations, outlines/AcroForm/attachments/JavaScript/open-action presence, docinfo, plus ordered per-page character/text-layer records and aggregate text coverage (no page text); configured page/decompressed-text/memory bounds apply |
| compress | pikepdf (+ PDFium compare) | lossless only: `remove_unreferenced_resources()` (failure → `resource-cleanup-skipped`, info), then save with `compress_streams=True`, `stream_decode_level=generalized` (never decodes DCT/JPX image data), `object_stream_mode=generate`, `recompress_flate=True`, `deterministic_id=True`. Then ≤5 sampled pages of source and candidate are rendered identically (scale 1.0) and compared per-channel via `ImageChops.difference`; **any nonzero delta or size mismatch blocks publication**. `details.compression` reports exact bytes/reduction; `compress-no-reduction` (info) when output ≥ input. Presets `balanced`/`aggressive`/`archival` are refused |
| images-to-pdf | Pillow | multipage-TIFF aware, EXIF orientation honored; fixed page sizes composed on a raster canvas at `dpi` (36–600), `fit`/`stretch`/`center`, alpha flattened onto `background`; `image` page size keeps the pixel grid; photographs pass one JPEG generation (`images-reencoded` info) |
| pdf-to-images | PDFium | 18–1200 dpi, PNG/JPEG/WebP/TIFF, page ranges; incremental pixel/byte limit checks; syntax-damaged inputs refused |
| pdf-to-md | PDFium text API | Streams selected occurrences one page at a time; UTF-8/LF + NFC; Markdown `<!-- ldf:page N -->`, TXT `--- ldf:page N ---` (or form-feed without anchors), or exact-schema JSONL. Top-to-bottom/left-to-right ordering and font-size headings are heuristics; no bidi repair or silent dehyphenation. Per-page raw-character/memory preflight, 50,000-rectangle fallback, and bounded 4,096-object/15-level/512-ruling scans prevent unbounded layout work. Coverage/warning metadata only in reports |
| md-to-pdf | markdown-it-py + Typst ≥0.15.1 | Strict-UTF-8 CommonMark subset with GFM tables; known tokens become application-controlled Typst function calls and all untrusted values use a punctuation/control-escaping string serializer. A4/Letter/Legal, finite margin, optional outline/TOC. Preprocessing is capped at min(16 MiB, input bytes, memory/512, temporary/64), 100k lines, 250k tokens, and 256 image occurrences; detailed drops cap at 256 plus a summary. Relative contained raster images are signature-checked additional inputs, single-frame decoded, metadata-stripped, and normalized to neutral PNGs. Typst runs through the hardened subprocess runner with private root/home/temp/package paths, hard remaining-job timeout, bounded hidden diagnostics, static generated-source audit, empty-package and exact dependency-manifest checks. Post-compile `max_pages` plus full standard PDF validation apply. Unsupported constructs emit `markdown-construct-dropped`; font fallback emits `system-font-dependent` |

### 8.1 Fidelity and security warning model

Page-moving operations emit per-input codes for what this build does not yet
rebuild (`outlines-dropped`, `form-fields-detached`, `attachments-dropped`,
`xmp-metadata-dropped`, `page-labels-dropped`, `document-actions-dropped`,
critical `signature-semantics-dropped`, `form-field-name-conflict`).
Single-document rewrites (rotate/crop/compress) preserve those structures but
emit critical `signature-invalidated` when signature fields exist and critical
`input-encryption-removed` when an encrypted input yields an unprotected
output. Full vocabulary: `docs/CONVERSION_FIDELITY.md`.

Text extraction emits at most one aggregate entry for each of
`no-text-layer`, `headings-inferred`, `reading-order-uncertain`, and
`tables-flattened`. Exact page attribution is the ordered
`details.coverage.per_page[].warning_codes` array. Coverage keys are
`pages_total`, `pages_with_text`, `pages_with_text_layer`,
`char_count_min`, `char_count_median`, `char_count_max`, and `per_page`; the
report never embeds extracted text.

## 9. Resource limits

`ResourceLimits` defaults (each `None`-disableable; env: `LDF_LIMITS__<NAME>`):

| Limit | Default | Enforced at |
|---|---|---:|
| `max_input_bytes` | 2 GiB | pipeline input gathering (cumulative) |
| `max_output_bytes` | 4 GiB | pre-publication aggregate + API parent monitor + pdf-to-images incremental |
| `max_temporary_bytes` | 8 GiB | API parent directory monitor (sampled) + transport cap component |
| `max_memory_bytes` | 2 GiB | Windows Job Object job memory / POSIX `RLIMIT_AS` |
| `max_cpu_seconds` | 600 | Job Object job time + parent accounting watchdog / `RLIMIT_CPU` |
| `timeout_seconds` | 600 | CLI cooperative checkpoints; API parent wall-clock watchdog |
| `max_pages` | 5000 | pre-open counts + post-open re-check |
| `max_image_pixels` | 200 M | Pillow decompression-bomb guard + canvas pre-check |
| `max_decompressed_bytes` | 4 GiB | image pipelines |
| `max_subprocesses` | 8 | Job Object active-process limit (+1 leader) / Linux `/proc` monitor |
| `max_archive_entries` / `max_archive_expansion_ratio` | 10,000 / 200× | reserved for future archive/Office pipelines |

Directory monitors are sampled (50 ms cadence) and can overshoot between
samples; none of this is a filesystem quota or sandbox.

`md-to-pdf` adds operation-specific preprocessing ceilings described in §8.
If `timeout_seconds=None`, the general job clock is disabled but the external
Typst child still receives a non-disableable 600-second safety ceiling.

PDF text extraction checks selection length against `max_pages`, writes and
accounts output incrementally, and releases each PDFium page before advancing;
its report grows only with compact per-page counts/codes, not text bodies.

## 10. Configuration (`config/settings.py`)

Precedence: constructor/CLI flags → `LDF_`-prefixed environment (`__` nesting)
→ private-by-default built-ins. Key fields: `strict_offline` (also honored
from `LDF_STRICT_OFFLINE` when the flag is absent), `jobs_root`, `collision`
(`fail`/`rename`/`overwrite`), `allowed_output_roots` (optional output jail;
empty list denies everything), `limits`, `bind_host`=127.0.0.1,
`bind_port`=8477, and the API admission caps (§12.3). A validator rejects
non-positive admission values and applies strict path policy to configured
roots. Library callers pass explicit `Settings` via the options objects
instead of mutating the environment.

## 11. CLI (`cli/main.py`)

- Entry points `ldf` and `localdocforge`; global flags before the command:
  `--json`, `--quiet`, `--password-stdin`, `--strict-offline`, `--report-dir`,
  `--version`.
- Exit codes: 0 success · 1 operation failed · 2 usage · 3 no engine ·
  4 output validation failed · 5 collision · 130 cancelled/timeout.
- Encrypted-input precedence is UTF-8 `--password-stdin` →
  `LDF_PASSWORD` → one hidden TTY prompt/retry. Password values are never CLI
  arguments or report/log output; a non-interactive invocation without either
  source exits 2. Windows interactivity requires `GetConsoleMode` success for
  stdin rather than `isatty()` alone, excluding NUL. An empty-but-present
  `LDF_PASSWORD` is an explicit empty credential. One value applies to every
  encrypted input. Globs are expanded by the CLI itself (PowerShell does not).
  Startup sweeps stale workspaces.
  Security warnings echo to stderr; `--report-dir` writes
  `<op>-<job-id>.report.{json,txt}`.
- `pdf-to-md` accepts `--pages`, `--format md|txt|jsonl`, and
  `--no-page-anchors`. Extracted text is always written to the explicit output
  path as UTF-8; stdout remains report/diagnostic output.
- `ldf web` refuses non-loopback binds without `--allow-nonlocal`, refuses
  them entirely under strict-offline, and translates Windows Ctrl+Break into
  the graceful shutdown path (exit 0, session lease released).

## 12. Local API and worker containment (`api/`)

### 12.1 Session and authentication

Startup creates a private `api-data/ldf-api-<uuid>` session root and holds an
external OS-released exclusive lease for its lifetime; crash residue from a
previous session is removed only after acquiring that lease (held/missing/
unreadable ⇒ preserved fail-closed). A random token is printed at startup;
every `/api` request must send `X-LDF-Token` (the status-page cookie alone
never authorizes). Host-header validation accepts loopback names only (unless
nonlocal mode was explicitly chosen). No CORS grants; CSP, no-store,
frame-deny, nosniff, referrer, and camera/mic/geolocation restrictions on
responses.

### 12.2 Endpoints

```text
GET  /                       status shell (sets cookie)
GET  /api/health             status, version, strict_offline, loopback_only
GET  /api/capabilities       registry-backed capability gating
POST /api/jobs/{operation}   multipart 'files' + per-op string fields → 201
                             (Prefer: respond-async / ?async=true → 202)
GET  /api/jobs               recent jobs (memory-only, cap 50)
GET  /api/jobs/{id}          state, sanitized report, containment record
GET  /api/jobs/{id}/events   bounded progress events (?after=<event-id>)
GET  /api/jobs/{id}/outputs/{index}   download (success-state only)
POST /api/jobs/{id}/cancel   terminate the contained worker tree
DELETE /api/jobs/{id}        delete private files, forget the job
```

Operations = the twelve worker-backed conversion operations; allowed form fields per operation are
allowlisted (`_OPERATION_PARAMS`) — unknown/duplicate/out-of-range fields are
422. `pdf-to-md` honors `pages`, `format`, `page_anchors`, and `password`
(`md`/true defaults). Job states: `queued, running, success, failed, cancelled, timed_out,
crashed, limit_exceeded`.

### 12.3 Admission and transport

Admission is reserved **before** multipart parsing. Defaults: 2 concurrent
workers, 16 queued, 4 active per client, 30 submissions/60 s per client,
2 GiB upload ceiling (active even if the job input limit is disabled);
rejections are 429/503 with `Retry-After`. Uploads spool into a random
`.transport-*` directory inside the private session, bounded by the lower of
the upload/input/temporary ceilings plus file/field-count and field-size
caps; handles and the spool are removed before enqueue and on malformed
bodies or disconnects.

### 12.4 The spawned worker protocol (`api/worker.py`)

Every accepted job runs in a fresh `multiprocessing.spawn` child — the API
process never parses document bytes. Sequence:

1. Child starts, applies POSIX pre-containment where applicable, sends a
   `ready` IPC message with its containment self-description, and **waits on a
   start gate**.
2. Parent establishes the boundary: on Windows a Job Object with
   kill-on-close, job-memory, job-CPU-time, and active-process limits,
   assigned **before** the gate opens (assignment failure ⇒ terminate, fail
   closed). On POSIX: new session/process group + rlimits.
3. Gate opens (`document_gate=opened_after_containment`); the child scrubs
   its environment (temp/home redirected into the job tree, secrets dropped),
   silences stdout/stderr, optionally installs the Python socket guard
   (strict mode), then runs the normal pipeline with outputs jailed to the
   job's `out/` directory.
4. IPC is JSON-framed, 1 MiB-capped, kinds `ready | progress | result |
   failure | fatal | probe_result`; every message is validated, and progress/
   reports/errors are scrubbed of paths and secrets before crossing.
   Passwords travel only in the private spawn channel and are cleared from
   parent state at completion.
5. The parent watchdog (50 ms cadence) enforces wall clock, Job-accounting
   CPU, sampled temporary and output directory sizes, and (Linux) descendant
   count; violations terminate the tree with a specific terminal state. A
   POSIX `RLIMIT_FSIZE` boundary that fires first is still classified as
   `limit_exceeded`: via the `SIGXFSZ` exit signal, or — on hosts where that
   signal is inherited-ignored — via the child reporting the `EFBIG` write
   failure as a typed limit message.
6. Finalization proves process-tree exit before any terminal event releases
   admission: Windows requires Job accounting to report zero active
   processes (`process_tree_exit=verified_empty`); a leader that exited
   before assignment yields only `pre_gate_leader_verified` with the gate
   `never_opened`; anything unverifiable **fails closed to `crashed`**.
7. Success publishes outputs and state together under the job lock after
   uploads/scratch/worker-temp are removed. Downloads take an
   active-download lease for the whole stream (DELETE/eviction cannot race
   it); non-success states never expose bytes. Failed/cancelled jobs remove
   their entire job root.

The worker is a **failure and resource boundary with the user's filesystem
authority — not an OS sandbox** (`docs/THREAT_MODEL.md`).

## 13. Reporting and scrubbing

`reporting/writers.py::write_report_files` emits `<basename>.report.json` +
`.txt` (same content as `--json`/human output). CLI reports intentionally name
user-selected files; API serialization replaces every path with its basename
and recursively scrubs the private session root and secrets. Public API
pipeline errors pass an allowlist of safe policy prefixes (limits, collisions,
strict-offline, aliasing); anything parser-derived is genericized to
"Document processing failed". Unexpected worker errors cross IPC only as
generic `fatal` messages.

For text extraction, the artifact — not stdout, report files, progress, or IPC
— is the fidelity channel. Reports expose the bounded coverage schema and
stable warning codes above. JSONL artifact records contain document text by
design; conversion-report JSON never does.

## 14. Packaging and reproducibility

Authoritative: `docs/PACKAGING.md`. In brief:

- **Profiles**: default/`lite` (core+CLI), `standard` (+FastAPI service),
  `full` (+pypdf), `dev` (+test/lint/type/build tools) — real dependency
  sets with marker-aware SHA-256 locks under `requirements/locks/`, exported
  from the pinned-uv (`0.11.26`) universal `uv.lock` with a dated cutoff.
  Audited installs: hash-locked deps first, then the package with
  `--no-deps`.
- **Build**: PEP 517 with `setuptools==83.0.0` only; the exact wheel is
  hash-verified from PyPI into a one-use offline wheelhouse; build isolation
  is kept, ambient `PIP_*` stripped.
- **Reproducibility gate**: two independent staged source trees built twice
  with `SOURCE_DATE_EPOCH=1704067200` must be byte-identical; the wheel
  rebuilt from the sdist must equal the direct wheel; Twine/metadata checks;
  then comparison against `packaging/release-artifact-manifest.json`
  (`--update-artifact-manifest` is the only legitimate refresh, for inspected
  intentional changes — drift otherwise fails the gate). The manifest is
  platform-scoped (schema 3): METADATA newlines, zip attributes, and deflate
  output differ across build hosts, so identities are recorded per
  `System-Machine` key and `--allow-unrecorded-platform` lets CI build
  platforms without a recorded identity skip only the comparison.
- **Canonical identity (2026-08-03)**: source tree `150b4aeb…`; wheel
  `049345cb…` (98,083 B); sdist `531c4084…` (84,593 B); authenticated by
  `packaging-evidence/windows-11-x64-SHA256SUMS.txt`; superseded sets are
  archived under `dist/windows-11-x64-2026-07-20/` and the two
  `dist/windows-11-x64-2026-08-03-superseded*/` directories.
- **SBOMs/notices** per profile (CycloneDX 1.6) are generated and
  drift-checked; advisory review is dated in `docs/ADVISORY_REPORT.json`.

## 15. Testing and quality gates

- `tests/unit` (grammar, models, sniffing, registry, workspace, doc
  consistency), `tests/integration` (operations, CLI contract, API flows),
  `tests/security` (filesystem/Windows-path regressions, privacy boundary,
  worker isolation with real spawned processes and a live-uvicorn
  disconnect case, release-artifact checks), `tests/packaging`. 407 tests as
  of 2026-08-03; fixtures are synthetic and generated
  (`tests/fixtures/make_fixtures.py`) — no third-party documents.
- The blocked-network harness (`scripts/run_blocked_network.py`) re-runs the
  complete suite with Python DNS and non-loopback sockets denied, including
  inside spawned workers via a verified `sitecustomize` guard path.
- `scripts/release_gate.py` chains: lock drift → Ruff → mypy (native plus
  `--platform linux` and `--platform darwin` for static portability) →
  `git diff --check` → `pip check` → SBOM/notices drift → full suite →
  blocked-network suite → reproducible builds + Twine + sdist→wheel
  equivalence → artifact manifest → clean Base/Lite/Standard/Full
  install/smoke/uninstall matrix (+ dev full-test venv), writing refreshed
  evidence to `packaging-evidence/`. A pass applies only to the executed
  OS/architecture/interpreter.
- `tests/unit/test_documentation_consistency.py` pins README/STATUS/
  ARCHITECTURE/THREAT_MODEL/CLI/FEATURE_MATRIX claims to shipped reality —
  documentation is part of the change, enforced.

## 16. Security posture in one view

| Boundary | Status |
|---|---|
| Untrusted input handling | signature sniffing; syntax-damaged PDFs refused (no silent repair); limits everywhere; PDFs reopen/render, images decode, and text artifacts pass strict UTF-8/provenance/schema validation before publication |
| CLI execution | in-process engines, cooperative timeouts only |
| API execution | one fresh spawned worker per job; Windows Job Object (kill-on-close, memory, CPU, process count) established before document bytes; verified-empty tree exit or fail-closed |
| Filesystem | per-job private workspaces; atomic no-clobber publication; alias refusal; Windows path-form/reparse/device/ADS rejection; optional output jail |
| Secrets | CLI password values come from stdin, an explicit environment variable, or a hidden prompt; API values are form-only; never in argv/reports/logs/IPC returns; reports carry no document text |
| Network | no outbound client in the package; loopback-only API by default; token auth; strict-offline = app policy + Python socket guards — **not** an OS firewall |
| Not provided | OS sandboxing of parsers, forensic erasure, crash-transactional multi-output publish, cross-platform execution evidence beyond Windows 11 x64 |

Open release blockers and the sensitive-document FAIL decision:
`docs/STATUS.md`. Full adversarial analysis: `docs/THREAT_MODEL.md`.

---

Related: `docs/README.md` (index) · `docs/ARCHITECTURE.md` (contract prose) ·
`docs/DEVELOPMENT.md` (extending the system) · `docs/LIBRARY_API.md` (Python
surface) · `docs/CLI.md` (user-facing reference)
