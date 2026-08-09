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
- `--report-dir DIR` — for conversion commands, additionally write
  `<operation>-<job-id>.report.json` and `.txt`. In strict mode this must be a
  recognized local path. Read-only metadata commands such as `agent-brief` do
  not create report files.

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
ldf agent-brief                 # registry-derived Markdown for coding agents
ldf --json agent-brief          # the same ordered snapshot as structured JSON
ldf inspect input.pdf           # read-only structure + per-page text counts
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
ldf pdf-to-images scan.pdf -d vision/ --preset llm       # vision-ready JPEGs
ldf pdf-to-md input.pdf -o content.md                     # Markdown (default)
ldf pdf-to-md report.pdf -o content.md --tables           # confident ruled grids → GFM
ldf pdf-to-md input.pdf -o content.txt --format txt --pages 1-5,9
ldf pdf-to-md input.pdf -o content.jsonl --format jsonl
ldf md-to-pdf notes.md -o notes.pdf --paper A4 --margin 20 --toc

ldf convert-images photos/*.HEIC -d converted/ --preset llm   # AI-assistant-ready JPEGs
ldf convert-images scan.heic -d out/ --format png --keep-metadata
ldf convert-images big.png -d out/ --max-dimension 1024
```

### PDF text extraction

```text
ldf pdf-to-md INPUT.pdf -o OUTPUT [--pages RANGE]
    [--format md|txt|jsonl] [--no-page-anchors] [--tables]
```

`pdf-to-md` extracts one selected page at a time through pypdfium2/PDFium and
atomically publishes one explicitly UTF-8 file. It never sends extracted text
through stdout: ordinary stdout is the human report, and global `--json` emits
the conversion report. This is intentional because an external Windows console
or pipe may not preserve non-ANSI stdout characters; the output artifact is the
text-fidelity channel.

The CLI and API operation id is `pdf-to-md`; the pre-existing stable registry/
doctor capability id remains `pdf-to-markdown`.

Formats and separators are deterministic:

- `md` (default) begins each selected occurrence with the exact source anchor
  `<!-- ldf:page N -->`. Extracted line/blank-line boundaries form paragraph
  blocks, and larger-font lines may become Markdown headings. With
  `--no-page-anchors`, those comments are
  omitted; pages remain separated by blank lines.
- `txt` begins each selected occurrence with `--- ldf:page N ---`. With
  `--no-page-anchors`, markers are omitted and a single form-feed character
  (`U+000C`, `\f`) separates page occurrences.
- `jsonl` writes exactly one JSON object followed by LF per selected occurrence:
  `{"page": N, "text": "…", "char_count": N,
  "has_text_layer": true|false}`. The keys and key order are stable, and
  `--no-page-anchors` has no semantic effect because the `page` field is the
  provenance marker. `page` is a positive 1-based integer, `text` is a string,
  `char_count` is a non-negative integer, and `has_text_layer` is boolean.

`--tables` is opt-in, defaults off, and is valid only with `--format md`.
Combining it with TXT or JSONL is a usage error (exit 2); those formats retain
their exact S4 schemas and flowed-text behavior.

Output order follows the shared page-range grammar, including descending,
repeated, and reverse selections. Page numbers remain the 1-based source page
numbers; repeated pages therefore repeat their number. Text line endings are
normalized to LF and Unicode is normalized to NFC. Non-newline whitespace runs
(including tabs, form-feed, and NBSP) collapse to one ASCII space, trailing
space and outer blank lines are trimmed, and geometry supplies paragraph
breaks. Inter-fragment spacing is heuristic: a gap of at most 1 pt or 10% of
the smaller line height concatenates styled runs; a larger gap inserts one
ASCII space. LocalDocForge does
not silently dehyphenate, join words across lines, apply bidi repair, or rewrite
ligatures beyond the Unicode mapping returned by PDFium. In Markdown and TXT,
with or without structural page anchors, source lines matching either reserved syntax
(`<!-- ldf:page N -->` or `--- ldf:page N ---`) are escaped so document text
cannot forge provenance. JSONL preserves both strings unchanged as ordinary
`text` data because framing comes from the JSON record itself.

Markdown structure and reading order are **heuristics**, not semantic recovery.
The baseline is top-to-bottom then left-to-right over PDFium text rectangles;
font-size clustering provides heading inference. Columns, rotated/angled text,
and RTL scripts can be ordered incorrectly.

With `--tables`, pdfplumber's explicit horizontal/vertical-line strategy may
replace a confidently bounded rectangular region with a GFM pipe table. The
first physical row becomes an **inferred** header; LocalDocForge does not claim
that the PDF marked it semantically. Backslashes are doubled, pipe characters
are escaped as `\|`, and cell newlines become `<br>` so extracted content stays
inside its cell. PDFium remains the text source outside each accepted region;
pdfplumber alone supplies the accepted region's cell text, so competing text
from the two extractors is never interleaved for the same region.

Borderless tables, merged/spanning cells, rotated pages, overlapping or
coordinate-mismatched regions, dense-vector pages, parser failures, and any
other low-confidence or over-limit candidate remain deterministic flowed text.
When the bounded heuristics recognize such a candidate, the report warns.
Absence of a table warning is not proof that no table exists; it means only that
those heuristics found no evidence. The five stable fidelity codes are:

- `no-text-layer` — a page has no PDF text objects; use
  `pdf-to-images --preset llm` for vision input (OCR remains unavailable).
- `headings-inferred` — Markdown headings were inferred from font-size
  clustering.
- `reading-order-uncertain` — geometry suggests columns, rotated/angled text,
  or RTL content whose logical order may differ from the baseline.
- `table-fidelity-best-effort` — one or more confident line-grid regions were
  emitted as GFM; verify the inferred header, cell order, and spanning-cell
  fidelity.
- `tables-flattened` — table output was disabled, or a candidate was kept as
  flowed text because confidence, geometry, parser, or resource checks refused
  a rectangular GFM table.

To keep reports bounded, `fidelity_warnings[]` contains at most one aggregate
entry for each code. Exact attribution lives in
`details.coverage.per_page[]`: each ordered occurrence record has the exact
keys `page`, `char_count`, `has_text_layer`, and stable `warning_codes[]`.
`details.coverage` has the exact keys `pages_total`, `pages_with_text`,
`pages_with_text_layer`, `char_count_min`, `char_count_median`,
`char_count_max`, and `per_page`. Counts apply to selected
occurrences, so a repeated page is counted repeatedly. A text layer containing
only whitespace can have `has_text_layer=true` but `char_count=0`.
`details.tables` has the exact safe metadata keys `requested`, `engine_status`,
`emitted`, and `flattened_candidates`; the status is `not-requested`,
`available`, or `fallback`, and the counters contain no cell or document text.
`char_count` is Python's length of the normalized combined plain page text
before anchors/Markdown markup (Unicode code points). An accepted table region
uses pdfplumber's cell text while ordinary regions use PDFium; in JSONL it
therefore still equals `len(record["text"])`. It is not an encoded byte length
or grapheme count.
Consequently, selecting one empty page with anchors disabled can legitimately
publish a zero-byte MD/TXT artifact; the coverage record, not file non-emptiness,
distinguishes that success from a failed extraction.

Before publication the text-specific validator requires strict UTF-8 and the
coverage fields above. It also requires exactly one valid anchor per selected
occurrence when anchors are enabled, or validates every JSONL record against
the exact schema and reported counts. A validation mismatch blocks publication.
The configured input/output/page limits and worker isolation apply as they do
to other operations; extraction retains only one page's text/layout data at a
time rather than accumulating the document body in memory. Per page, raw
PDFium character count is conservatively preflighted against the remaining
decompressed budget and `max_memory_bytes // 64`; more than 50,000 rectangles
uses bounded full-page fallback plus `reading-order-uncertain`. Object
inventory stops at 4,096 objects, descends through at most 15 nested Form
levels, and retains at most 512 horizontal and 512 vertical ruling candidates.
If either bounded traversal cannot establish whether a zero-character page has
a text object, the operation refuses with
`[reading-order-uncertain]` rather than emitting a false `no-text-layer` claim.
Pages above one million raw characters are also preflighted against the
remaining output budget; the streaming writer performs the exact final check.
When `--tables` is enabled, structured table finding is skipped once the PDFium
scan exceeds 8,192 PDF path segments. It refuses structured output above 1,024
pdfplumber edges, above 4,096 vertical×horizontal edge pairs, above 32
detected tables per page, or
above 4,096 cumulative cells per page. Normalized table-cell UTF-8 is capped at
4 MiB per page and further reduced by the remaining extraction-byte budget and
`max_memory_bytes // 64`. Crossing any table-specific limit falls back to
flowed text rather than running unbounded analysis or publishing a partial
table.

### Markdown to PDF

```text
ldf md-to-pdf INPUT.md -o OUTPUT.pdf
    [--paper A4|Letter|Legal] [--margin MM] [--toc]
    [--collision fail|rename|overwrite]
```

`md-to-pdf` requires Typst 0.15.1 or newer. It accepts only a strict-UTF-8
`.md` or `.markdown` primary input; text has no reliable magic bytes, so this is
an intentionally limited extension-plus-full-decode boundary. The supported
document subset is CommonMark headings, paragraphs, emphasis/strong text,
ordered and unordered lists, block quotes, horizontal rules, code spans and
fences, links with `http`/`https`/`mailto`/`tel` destinations, plus the built-in
GFM table rule. The default is A4 with a 20 mm margin and no table of contents.
`--toc` inserts a Typst outline followed by a page break. Margins must be finite,
non-negative, and leave a drawable page area.

Before parsing, the Markdown snapshot is capped at 16 MiB and further reduced
to the lowest enabled `max_input_bytes`, `max_memory_bytes / 512`, and
`max_temporary_bytes / 64` allowance; the default effective ceiling is 4 MiB.
It is then limited to 100,000 source lines and 250,000 parser tokens. At most
256 image occurrences are accepted, including repeated references. These
preprocessing bounds run before the ordinary job clock; the API's outer worker
watchdog covers the whole call, while CLI/library preprocessing is bounded by
cardinality rather than preemptively timed.

Raw HTML markup, footnotes, math, strikethrough, and unknown parser tokens are
not interpreted. They are dropped and reported with stable
`markdown-construct-dropped` warnings and 1-based source lines. Text enclosed by
inline HTML tags remains ordinary text when the parser exposes it separately;
the tags themselves are dropped. Strikethrough formatting and unsafe/local link
destinations are dropped while their enclosed label text remains.
`system-font-dependent` records that Typst's
embedded fonts may fall back to installed fonts, so line wrapping/page counts
can vary between machines. Reports include only bounded counts, settings, and
construct names/lines—never source text, URL values, or raw Typst diagnostics.
At most 256 distinct construct/line entries are reported; one synthetic summary
then records the omitted count. Report details expose
`dropped_constructs_truncated`, `dropped_constructs_omitted`, and
`dropped_construct_report_limit` so callers can detect that aggregation.

CLI image references must be relative to the Markdown file and remain within
its containing directory. Remote/data/file/UNC/absolute, query-bearing,
traversing, escaping-symlink, Windows reparse/junction, missing, binary, or
multi-frame assets are refused. A POSIX symlink whose resolved target remains
inside the Markdown directory is accepted. Accepted HEIC/HEIF, JPEG, PNG, TIFF,
BMP, and WebP inputs are copied from identity-checked pinned handles to private
neutral snapshots, signature-checked again there, and normalized as single-frame
PNGs under neutral names in the private job workspace; source metadata, EXIF,
text chunks, and ICC-profile
bytes are discarded, while EXIF orientation is applied to pixels first.
Referenced image bytes count toward job limits and
an output may not alias any source or image even with overwrite enabled.

Only generated source and normalized assets are placed beneath Typst's
`--root`; imports/plugins/packages are unsupported, package directories and the
dependency manifest are audited, diagnostics are bounded and withheld on
failure, and the compiler runs with a hard remaining-job timeout. Even when the
general timeout is disabled, Typst retains a 600-second safety ceiling. This narrows
the external executable boundary but is not an OS filesystem or network
sandbox. The generated PDF must also stay within the configured page, temporary,
and output limits and pass the standard full-PDF reopen/syntax/page/render
validation before atomic publication.

### Registry-derived agent brief

`ldf agent-brief` prints compact Markdown on stdout. Put the global option
before the command (`ldf --json agent-brief`) to receive one structured JSON
document containing the same snapshot. `--quiet` does not suppress the brief,
and `--report-dir` does not create files for it.

Command selection and ordering come from `CAPABILITY_SPECS`, not from a static
command list: every `implemented=True` capability receives a one-line usage
template and appears in registry order. One live
`EngineRegistry.capabilities()` probe supplies its current `available`, engine,
and missing-requirement state. Implemented commands remain visible but are
clearly marked unavailable when their live engine probe fails; a capability
with `implemented=False` cannot render at all. Template coverage is checked
before stdout is emitted, so a future capability flip without a usage entry
fails loudly instead of producing partial guidance.

The brief also carries the stable exit-code table; five agent gotchas covering
encrypted inputs, collision policy, glob expansion, warning arrays, and output
fitness; and the structured `verify` -> `fallback` -> `review` workflow.
`warnings[]` is agent shorthand: current conversion reports expose the exact
`security_warnings[]` and `fidelity_warnings[]` arrays, whose entries contain
stable `code` values.

The feedback section resolves and prints the existing absolute path to
`docs/AGENT_FEEDBACK.md` plus its rules: append only; an entry is required for
failed or unsatisfactory output and whenever the agent falls back; a one-line
smooth-success entry is optional; no other repository file may be changed
unless the user explicitly commissioned development work;
and entries must describe documents generically without sensitive paths or
document text.

The writable feedback log is intentionally not copied into wheels. Therefore a
standalone wheel or direct VCS install with no discoverable source checkout
exits 1 before writing stdout instead of inventing a feedback path. Run
`agent-brief` from a LocalDocForge source checkout (the repository-local
environment is supported); the checkout may still be discovered after changing
to another working directory.

`agent-brief` is read-only and stdout-only on success. It opens no document,
creates no job/report/output directory, does not consume `--password-stdin`,
and bypasses stale-workspace cleanup. Its only engine interaction is the same
normal live capability-probe path used by `doctor`; it performs no conversion
and has no local API job endpoint.

Notes:

- PowerShell does not expand `*`; LocalDocForge expands globs itself.
- Encrypted CLI-input precedence is global `--password-stdin` (before the
  command) → `LDF_PASSWORD` → hidden interactive prompt (TTY only). A non-TTY
  invocation with no supplied password exits 2 and names both non-interactive
  mechanisms. On Windows, only a stdin handle accepted by `GetConsoleMode` is
  interactive; character devices such as NUL are non-interactive. Environment
  presence is significant: `LDF_PASSWORD=` supplies the empty password (which
  can unlock a PDF whose user password is empty) and a mismatch exits 1 rather
  than falling through to the prompt. Unset the variable to select the
  missing-credential or hidden-prompt path. The password value is never
  accepted in argv or written to stdout, stderr, reports, or logs. One password
  is tried for every encrypted input in an invocation; inputs requiring
  different passwords fail at the first mismatch. Successful output is not
  re-encrypted and carries a critical `input-encryption-removed` warning.
- `--page-size` accepts `A4`, `Letter`, `Legal`, `image`, or `WxH` with optional
  `pt|mm|cm|in` (default `mm`), such as `210x297mm`.
- Images-to-PDF accepts 36–600 DPI. PDF-to-images accepts 18–1200 DPI and
  PNG/JPEG/WebP/TIFF output. Resource limits may reject a value that would
  exceed configured pixel, decompressed-byte, page, or output bounds.
- For `pdf-to-images`, `--preset llm` selects JPEG quality 85 and computes a
  separate render scale for every page so its long edge is at most 1568 px.
  The ordinary 150-DPI render is the ceiling: a page already below the pixel
  bound stays at that size and is never enlarged merely to reach 1568 px.
  Explicit `--format` and `--quality` independently replace those preset
  values while retaining the per-page cap. Explicit `--dpi` selects fixed-DPI
  rendering and disables the preset cap, so it can intentionally produce a
  larger image. Reports record the resolved preset/format, configured and
  applied quality (`null` for lossless PNG/TIFF), cap mode, and every output's
  actual width, height, and effective DPI.
- `inspect` reports `page_text_stats`, one ordered
  `{page, char_count, has_text_layer}` record per source page, plus the
  `text_coverage` summary. These counts use the same PDFium text policy as
  `pdf-to-md` and help agents choose extraction versus page rendering; no
  extracted page text enters the inventory. For a valid zero-page source,
  `page_text_stats` is empty and the summary's min/median/max fields are JSON
  `null` (all three page counters are zero). The inventory refuses inputs over
  the configured `max_pages` limit and applies the configured cumulative
  `max_decompressed_bytes` and per-page `max_memory_bytes // 64` text
  preflights; it never uses unbounded full-page text extraction. This is a
  decision aid, not a cheap probe: rectangle-dense pages can still be slow.
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
`pdf-to-md`, `md-to-pdf`, or `convert-images`.
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
| pdf-to-images | optional `format`, `dpi` (18–1200), `pages`, `quality` (1–100), `preset` (`llm`), and `password` |
| pdf-to-md | optional `pages`, `format` (`md`, `txt`, or `jsonl`; default `md`), strict boolean `page_anchors` (`true`/`false`; default `true`), strict boolean `tables` (`true`/`false`; default `false`, valid only with `md`), and `password` |
| md-to-pdf | exactly one `.md`/`.markdown` upload plus only referenced sibling raster-image uploads; optional `paper` (`A4`, `Letter`, or `Legal`), finite non-negative `margin`, and strict boolean `toc` (`true`/`false`) |
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

For a `pdf-to-md` API job, the server-selected output is `document.md`,
`document.txt`, or `document.jsonl` according to the resolved format. As with
the CLI, the download artifact carries text while the job report carries only
coverage, safe table counters/status, and warnings. `tables=true` is accepted
only with `format=md`; other combinations return 422.

For `md-to-pdf`, the API selects `document.pdf`. Transport-renamed uploads are
mapped back to distinct sanitized basenames so a Markdown image such as
`![diagram](diagram.png)` can match a sibling upload. Directory-qualified image
references and unreferenced extra uploads are refused; the API never interprets
a client path as a server-side filesystem path.

## Planned (commands and job endpoints do not exist)

`repair`, `ocr`, `office-to-pdf`, `html-to-pdf`, `pdf-to-pdfa`,
`pdf-to-docx/pptx/xlsx`, `watermark`,
`page-numbers`, `forms`, `protect`, `unlock`, `redact`, `sanitize`, `metadata`,
`attachments`, `sign`, `verify-signatures`, `compare`, `validate`, `batch`,
`scan`, and `edit` are unavailable. The full browser job UI is also planned;
the shipped page is a status shell only.
