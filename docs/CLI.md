# CLI Reference (`ldf` / `localdocforge`)

All commands run fully locally. Global options come **before** the command:

```
ldf [--json] [--quiet] [--strict-offline] [--report-dir DIR] <command> …
```

- `--json` — machine-readable report/diagnostics on stdout.
- `--quiet` — suppress the human report summary.
- `--strict-offline` — pin the no-network guarantee (no network code paths
  exist in this build; the flag records the pledge and gates future engines).
- `--report-dir DIR` — additionally write `<op>-<job>.report.json` and `.txt`.

## Exit codes
| Code | Meaning |
|---|---|
| 0 | success |
| 1 | operation failed |
| 2 | usage error (bad arguments, bad page range, missing file) |
| 3 | no engine available for the operation |
| 4 | output validation failed — nothing was written |
| 5 | output exists and collision policy is `fail` |
| 130 | cancelled |

Every writing command accepts `--collision fail|rename|overwrite`
(default `fail`; `rename` writes `name (1).pdf`).

## Page-range grammar
`7` · `2-9` · `9-2` (descending) · `12-end` · `odd` · `even` · `all` ·
`reverse` · `last` · `last-5` (the last 5 pages) — comma-combinable:
`1-5,9,12-end`. Ranges are validated against the real page count before any
work happens.

## Commands verified in this build

```powershell
ldf doctor                      # engines + honest capability list
ldf --json doctor               # machine-readable diagnostics
ldf inspect input.pdf           # read-only structural inventory
ldf web                         # local API + UI shell on http://127.0.0.1:8477
ldf web --port 9000             # loopback only; --allow-nonlocal is a
                                # deliberate, warned, dangerous opt-in

ldf merge a.pdf b.pdf -o merged.pdf
ldf merge a.pdf --pages 1-5 b.pdf --pages 2-end -o merged.pdf
ldf merge "a.pdf::1,3" "b.pdf::2" -o merged.pdf   # inline-range form

ldf split input.pdf -d parts/                  # one file per page
ldf split input.pdf -d parts/ --pages "1-3,7"  # one file per token
ldf split input.pdf -d parts/ --every 10

ldf remove-pages input.pdf --pages "2,5-7" -o out.pdf
ldf extract-pages input.pdf --pages "1-3,10" -o out.pdf
ldf organize input.pdf --order "3,1,2,4-end" -o out.pdf
ldf rotate input.pdf --degrees 90 --pages "1,3-5" -o out.pdf
ldf crop input.pdf --box "50,50,400,500" -o out.pdf   # warns: NOT redaction

ldf images-to-pdf scans/*.jpg -o scans.pdf --page-size A4 --fit fit
ldf images-to-pdf photo.png -o photo.pdf --page-size image
ldf pdf-to-images input.pdf -d pages/ --format png --dpi 300 --pages odd
```

Notes:
- Globs are expanded by `ldf` itself (PowerShell does not expand `*`).
- Encrypted inputs: run interactively and you get a hidden password prompt;
  passwords are never accepted as command-line arguments.
- `--page-size` accepts `A4`, `Letter`, `Legal`, `image`, or `WxH` with an
  optional `pt|mm|cm|in` suffix (default mm), e.g. `210x297mm`.
- Reports never contain document text or passwords.

## The local API (`ldf web`)

Serves loopback only. Authentication: a random per-session token printed at
startup; send it as the `X-LDF-Token` header. State-changing requests
require the header (a cookie alone is refused — CSRF protection). Browser
payloads never contain filesystem paths: files are uploaded, results are
downloaded by job id. Job history is in memory and vanishes with the process.

```text
GET  /                      honest capability page (sets session cookie)
GET  /api/health            {status, version}
GET  /api/capabilities      engines + capability gating (doctor as JSON)
POST /api/jobs/{op}         multipart 'files' + form params; 201 + report
                            op ∈ merge, split, remove-pages, extract-pages,
                                 organize, rotate, crop, images-to-pdf,
                                 pdf-to-images
GET  /api/jobs              recent jobs (in-memory)
GET  /api/jobs/{id}         full ConversionReport
GET  /api/jobs/{id}/outputs/{index}   download one artifact
DELETE /api/jobs/{id}       forget the job and delete its outputs
```

## Planned (not in this build — commands do not exist yet)
`compress`, `repair`, `ocr`, `office-to-pdf`, `html-to-pdf`, `pdf-to-md`,
`md-to-pdf`, `pdf-to-pdfa`, `pdf-to-docx/pptx/xlsx`, `watermark`,
`page-numbers`, `forms`, `protect`, `unlock`, `redact`, `sanitize`,
`metadata`, `attachments`, `sign`, `verify-signatures`, `compare`,
`validate`, `batch`, `scan`, `edit`.
