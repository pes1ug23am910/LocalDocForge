# CLI and Local API Reference (`ldf` / `localdocforge`)

Implemented conversion engines execute on the host machine. The shipped
package contains no outbound network client or remote browser asset, but
non-strict CLI paths may refer to filesystems the operating system exposes over
a network. Use strict-offline mode for the application's strongest local-only
policy.

Global options come **before** the command:

```text
ldf [--json] [--quiet] [--password-stdin] [--strict-offline] [--report-dir DIR] <command> …
```

- `--json` — machine-readable report or diagnostics on stdout.
- `--quiet` — suppress the human conversion-report summary.
- `--password-stdin` — read exactly one UTF-8 password line from
  stdin. One trailing CRLF, LF, or CR terminator is removed; every other
  character, including spaces, is preserved. This source outranks
  `LDF_PASSWORD`. The flag performs this raw line read even when stdin is a
  TTY; terminal users should omit it when they want the separate hidden
  interactive prompt instead. Help and subcommand parse failures do not drain
  stdin; a password-capable command resolves the selected source immediately
  before operation setup.
- `--strict-offline` — record strict state in reports and reject recognizable
  network filesystem inputs, outputs, jobs/report locations, external-tool
  paths, and non-loopback web serving. `LDF_STRICT_OFFLINE=true` is preserved
  when the flag is omitted. This is application enforcement, not an OS
  firewall; ordinary-looking POSIX network mounts cannot be distinguished.
- `--report-dir DIR` — additionally write
  `<operation>-<job-id>.report.json` and `.txt`. In strict mode this must be a
  recognized local path.

## Exit codes

| Code | Meaning |
|---:|---|
| 0 | success |
| 1 | operation failed |
| 2 | usage error (bad arguments, bad page range, missing file) |
| 3 | no engine available for the operation |
| 4 | generated-output validation failed before publication |
| 5 | output exists and collision policy is `fail` |
| 130 | cancelled or cooperative job timeout |

Every writing command accepts `--collision fail|rename|overwrite` (default
`fail`; `rename` chooses `name (1).pdf`, then the next available suffix).
Input/output aliases are refused even with `overwrite`.

All candidates are validated before publication starts. Each final file is
published atomically, but a multi-output job is not an all-files transaction
across a process or machine crash. A handled publication failure performs
best-effort rollback.

## Page-range grammar

`7` · `2-9` · `9-2` (descending) · `12-end` · `odd` · `even` · `all` ·
`reverse` · `last`/`end` · `last-5` (the last five pages), comma-combinable as
`1-5,9,12-end`. Repeated pages remain repeated. Ranges are resolved against the
real page count before page processing.

## Commands available in this build

```powershell
ldf doctor                      # engines + capability list
ldf --json doctor               # machine-readable diagnostics
ldf inspect input.pdf           # read-only structural inventory
ldf --strict-offline web        # API + status shell on http://127.0.0.1:8477
ldf web --port 9000             # loopback on another port
ldf web --host 0.0.0.0 --allow-nonlocal  # dangerous; refused in strict mode

ldf merge a.pdf b.pdf -o merged.pdf
ldf merge a.pdf --pages 1-5 b.pdf --pages 2-end -o merged.pdf
ldf merge "a.pdf::1,3" "b.pdf::2" -o merged.pdf

ldf split input.pdf -d parts/                  # one file per page
ldf split input.pdf -d parts/ --pages "1-3,7"  # one file per token
ldf split input.pdf -d parts/ --every 10

ldf remove-pages input.pdf --pages "2,5-7" -o out.pdf
ldf extract-pages input.pdf --pages "1-3,10" -o out.pdf
ldf organize input.pdf --order "3,1,2,4-end" -o out.pdf
ldf rotate input.pdf --degrees 90 --pages "1,3-5" -o out.pdf
ldf crop input.pdf --box "50,50,400,500" -o out.pdf

ldf compress input.pdf -o smaller.pdf              # lossless structural preset

ldf images-to-pdf scans/*.jpg -o scans.pdf --page-size A4 --fit fit
ldf images-to-pdf photo.png -o photo.pdf --page-size image
ldf images-to-pdf photos/*.HEIC -o photos.pdf            # iPhone HEIC input
ldf pdf-to-images input.pdf -d pages/ --format png --dpi 300 --pages odd

ldf convert-images photos/*.HEIC -d converted/ --preset llm   # AI-assistant-ready JPEGs
ldf convert-images scan.heic -d out/ --format png --keep-metadata
ldf convert-images big.png -d out/ --max-dimension 1024
```

Notes:

- PowerShell does not expand `*`; LocalDocForge expands globs itself.
- Encrypted CLI-input precedence is global `--password-stdin` (before the
  command) → `LDF_PASSWORD` → hidden interactive prompt (TTY only). A non-TTY
  invocation with no supplied password exits 2 and names both non-interactive
  mechanisms. The password value is never accepted in argv or written to
  stdout, stderr, reports, or logs. One password is tried for every encrypted
  input in an invocation; inputs requiring different passwords fail at the
  first mismatch. Successful output is not re-encrypted and carries a critical
  `input-encryption-removed` warning.
- `--page-size` accepts `A4`, `Letter`, `Legal`, `image`, or `WxH` with optional
  `pt|mm|cm|in` (default `mm`), such as `210x297mm`.
- Images-to-PDF accepts 36–600 DPI. PDF-to-images accepts 18–1200 DPI and
  PNG/JPEG/WebP/TIFF output. Resource limits may reject a value that would
  exceed configured pixel, decompressed-byte, page, or output bounds.
- Image inputs (images-to-pdf and convert-images) may be HEIC/HEIF, JPG, PNG,
  TIFF, BMP, or WebP; HEIC decoding runs through the decode-only pi-heif
  engine, so HEIF *output* is never offered.
- `convert-images` applies EXIF orientation, converts pixels tagged with a
  non-sRGB color profile (iPhone photos are typically Display P3) to sRGB, and
  strips EXIF/XMP metadata — including GPS positions — by default;
  `--keep-metadata` retains EXIF and emits a `location-metadata-retained`
  warning when GPS data is kept. `--preset llm` is shorthand for JPEG at
  quality 85 with the long edge bounded to 1568 px — the largest size current
  Compatible AI systems ingest without
  server-side downscaling. Explicit flags override preset values; images are
  never upscaled.
- Crop sets the PDF CropBox. Hidden content remains; it is not redaction.
- `compress` implements only the `lossless` preset: streams are recompressed,
  object streams generated, and unused page resources pruned — image data is
  never re-encoded. Sampled pages are rendered and compared pixel-for-pixel
  against the source; any difference blocks publication. Already-optimized
  inputs may not shrink, which the report states via `compress-no-reduction`.
  The planned `balanced`/`aggressive`/`archival` presets are refused with a
  usage error (exit 2).
- Page-moving operations emit warnings for detected document-level losses.
  `remove-pages` refuses outlines/forms/signatures/page labels/open actions,
  named destinations, internal links, and tagged structures that this build
  cannot safely rewrite.
- CLI reports omit document text and passwords but intentionally include
  artifact filenames and user-selected output paths. Treat saved reports as
  local metadata.
- The local API is unchanged: its existing optional multipart `password` form
  field provides interface parity without using the CLI's stdin/environment
  mechanisms.

## The local API (`ldf web`)

The API design is portable, but this checkpoint's executed release evidence is
Windows 11 x64 only. Linux and macOS remain unverified until their own retained
runner gates pass.

The default service binds to `127.0.0.1`. A random token is printed at startup;
every `/api` request must send it in `X-LDF-Token`. The status page sets a
same-site, HTTP-only cookie, but a cookie alone cannot authorize an API request.
No CORS grant is sent. CSP, no-store, frame, MIME-sniffing, referrer, and
camera/microphone/geolocation restrictions are applied to responses.

`--allow-nonlocal` is a deliberate dangerous opt-in. It disables the loopback
Host restriction but not token authentication. Strict-offline mode refuses a
non-loopback host even if that option is supplied. Nonlocal mode provides no
TLS; it is not recommended for sensitive documents.

```text
GET  /                      capability/status page (sets token cookie)
GET  /api/health            status, version, strict_offline, loopback_only
GET  /api/capabilities      API-safe engine probes + capability gating
POST /api/jobs/{operation}  multipart files/params; worker-backed 201 by default
GET  /api/jobs              recent in-memory jobs
GET  /api/jobs/{id}         state/report/latest progress (paths are basenames)
GET  /api/jobs/{id}/events  bounded progress events; optional ?after=<event-id>
GET  /api/jobs/{id}/outputs/{index}   contained artifact download
POST /api/jobs/{id}/cancel  terminate the contained worker tree
DELETE /api/jobs/{id}       delete private job files, then forget the job
```

The default POST waits for its isolated worker and preserves the existing
`201` response contract. Send `Prefer: respond-async` or `?async=true` to
receive `202`, `Preference-Applied: respond-async`, and status/events/cancel
URLs. States are `queued`, `running`, `success`, `failed`, `cancelled`,
`timed_out`, `crashed`, and `limit_exceeded`. Progress history is bounded and
poll-based; this release does not claim a streaming/SSE channel.

`operation` is one of `merge`, `split`, `remove-pages`, `extract-pages`,
`organize`, `rotate`, `crop`, `compress`, `images-to-pdf`, `pdf-to-images`,
or `convert-images`.
Upload each
source under multipart field `files`. The server, not the request, chooses all
output paths. Supported string form fields are:

| Operation | Honored form fields |
|---|---|
| merge | `pages` as a JSON list of string/null entries, optional `password` |
| split | `pages` or integer `every`, optional `password` |
| remove-pages / extract-pages | required `pages`, optional `password` |
| organize | required `order`, optional `password` |
| rotate | required integer `degrees`, optional `pages` and `password` |
| crop | required finite `box=x0,y0,x1,y1`, optional `pages` and `password` |
| compress | optional `preset` (only `lossless` exists), optional `password` |
| images-to-pdf | optional `page_size`, `fit`, non-negative finite `margin`, `background`, `dpi` (36–600), and `quality` (1–100) |
| pdf-to-images | optional `format`, `dpi` (18–1200), `pages`, `quality` (1–100), and `password` |
| convert-images | optional `format` (png/jpeg/webp/tiff), `quality` (1–100), `max_dimension` (16–30000), `preset` (`llm`), boolean `keep_metadata`, and `background` |

Unknown, duplicated, invalid, or out-of-range parameters return 422. Upload
bytes are counted cumulatively against the lower of
`LDF_LIMITS__MAX_INPUT_BYTES` and `LDF_API_MAX_UPLOAD_BYTES`. The API upload
ceiling remains active if the general job limit is disabled; an enabled
`LDF_LIMITS__MAX_TEMPORARY_BYTES` can lower the aggregate transport cap further.
After admission, multipart files spool only beneath a random `.transport-*`
directory inside the private API session. Middleware also caps the total request
with bounded overhead and limits file/field counts and non-file field size.
Handles and the transport root are closed/removed before enqueue, including for
malformed input and browser disconnects.

Admission is reserved before multipart spooling. Defaults are two concurrent
workers, sixteen queued jobs, four queued/running jobs per client, and thirty
submissions per client per sixty seconds. They are configurable through
`LDF_API_MAX_CONCURRENT_JOBS`, `LDF_API_MAX_QUEUED_JOBS`,
`LDF_API_MAX_ACTIVE_JOBS_PER_CLIENT`, `LDF_API_RATE_LIMIT_JOBS`, and
`LDF_API_RATE_LIMIT_WINDOW_SECONDS`. Rejections return 429 or 503 with
`Retry-After` where applicable.

Every accepted conversion executes in a fresh multiprocessing `spawn` child;
the Uvicorn process does not parse uploaded document bytes. The parent enforces
wall time and sampled aggregate output/temp usage, kills the complete process
tree on cancellation/failure/shutdown, verifies the configured containment
boundary is empty before publishing a terminal state, and reports the active
Windows/POSIX mechanism in job state. On Windows, `verified_empty` is used only
after an established Job Object reports zero active processes. A bootstrap
leader that exits before assignment leaves the document gate `never_opened` and
reports only `pre_gate_leader_verified`; any other unverifiable exit fails
closed. POSIX containment was not executed in this Windows-only checkpoint.
Output publication and `success` are atomic under the job lock; a download holds
an active lease through the entire file stream, so DELETE/eviction cannot race
it. Download rejects queued, running, failed, cancelled, timed-out, crashed, and
limit-exceeded jobs. Generated bytes also remain subject
to the pipeline's publication limit. See `docs/ARCHITECTURE.md` for
platform-specific memory/CPU/process/file-size controls and their stated
limitations, including the POSIX `setsid()` escape residual.

History is memory-only and capped at 50. Successful jobs remove uploads,
scratch, and worker-temp data while outputs remain in a private session
directory until DELETE, eviction, or graceful shutdown. Failed/cancelled jobs
remove the whole job root. The API holds an external OS-released lease for the
session lifetime. Startup removes crash residue only when that lease can be
acquired; held, missing, or unreadable leases are preserved fail-closed. Cleanup
failure is surfaced as an error (and as a critical report warning when a report
exists); none of these paths is described as secure erasure.

## Planned (commands and job endpoints do not exist)

`repair`, `ocr`, `office-to-pdf`, `html-to-pdf`, `pdf-to-md`,
`md-to-pdf`, `pdf-to-pdfa`, `pdf-to-docx/pptx/xlsx`, `watermark`,
`page-numbers`, `forms`, `protect`, `unlock`, `redact`, `sanitize`, `metadata`,
`attachments`, `sign`, `verify-signatures`, `compare`, `validate`, `batch`,
`scan`, and `edit` are unavailable. The full browser job UI is also planned;
the shipped page is a status shell only.
