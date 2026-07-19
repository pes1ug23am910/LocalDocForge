# STATUS — LocalDocForge

Last updated: 2026-07-19 (independent-audit checkpoint). Keep this file aligned
with runtime probes and tests; do not treat roadmap text as implementation
evidence.

## How to resume safely

```powershell
.venv\Scripts\python.exe -m pytest tests -q
.venv\Scripts\python.exe -m ruff check src tests
.venv\Scripts\ldf.exe doctor
.venv\Scripts\python.exe scripts\generate_release_artifacts.py --check
```

Environment used for this checkpoint: Windows 11, PowerShell 7.6.3, Python
3.14.4 in `.venv`, Node 24, npm 11, Git 2.55, and the .NET 8.0.21 runtime (no
.NET SDK). The environment was
created from `requirements-lock.txt` and the package is installed editable.
The lock was captured on Windows/Python 3.14; it is not a tested Lite,
Standard, or Full profile and does not prove cross-platform wheel availability.

At the audit start, commit `0966b924da41d10ec36798af77cb97e8c2b2db54`
had a clean working tree. Its existing suite produced 207 passed and 1 skipped
(the POSIX-permissions check skipped on Windows), with Ruff clean. The audit
adds security and PDF-fidelity regressions; the final post-fix full-suite result
and release decision belong in `docs/INDEPENDENT_AUDIT.md`, not in a hard-coded
README test count.

## Verified implementation state

- `ldf doctor` probes the live environment. At this checkpoint the Python
  pikepdf, pypdf, PDFium, and Pillow adapters are available; Typst 0.15.1 is on
  `PATH` but its Markdown pipeline is not implemented. qpdf, Tesseract,
  OCRmyPDF, Ghostscript, LibreOffice, Pandoc, and veraPDF command-line engines
  are absent. Presence of an executable does not make an unimplemented
  capability available.
- The CLI and a synchronous FastAPI service are implemented. `ldf web` was
  live-tested over real HTTP on an ephemeral 127.0.0.1 port. The browser page
  is an honest status shell, not the planned React job UI.
- The API uses a random per-session `X-LDF-Token`, rejects cookie-only API
  requests, grants no CORS access, validates the Host header, sends CSP and
  hardening headers, caps multipart bodies/uploads, sanitizes names, uses
  unguessable job ids, and serves only files contained in the owning job.
  API reports expose basenames rather than server filesystem paths.
- Strict-offline state survives `LDF_STRICT_OFFLINE=true`, is recorded in
  conversion reports and `/api/health`, and is the only mode in which the UI
  says “processed locally; nothing leaves this machine.” It rejects UNC and
  Windows mapped-drive job/input/output/report paths and overrides
  `--allow-nonlocal` web serving. It is application enforcement, not an OS
  firewall; POSIX mounts that look like ordinary local paths are not
  distinguishable by this path check.
- The shipped package has no outbound network client, telemetry, update check,
  CDN, remote font, or remote browser asset. A synthetic PDF with an external
  URI was processed under a socket/DNS-denial test without a network primitive
  being called. Non-loopback inbound API serving remains possible only through
  the explicit dangerous opt-in outside strict mode.
- Implemented operations are merge, split, remove-pages, extract-pages,
  organize, rotate, crop, images-to-pdf, pdf-to-images, and inspect. The
  capability registry requires both an implementation bit and a successful
  engine probe.
- PDF candidates are reopened, checked for parser syntax warnings, page-count
  checked when expected, and rendered with PDFium before publication. Selected
  high-risk outputs render every page; routine outputs render at most 20
  evenly sampled pages. Generated image candidates must decode. Blank-page
  metrics are reported but a legitimately blank document is not rejected by
  default.
- All candidates validate before the first destination write. Each artifact is
  published through a same-directory, fsynced staging file; overwrite uses
  atomic replace and fail/rename use atomic no-clobber publication. Input/output
  aliases (including existing hard links) are refused. A handled multi-output
  publication failure attempts rollback, but publication is not an all-files
  transaction across process or machine crashes.
- Aggregate input and output byte limits, page limits (including encrypted PDFs
  after opening), image pixel/decompressed-byte limits, and cooperative job
  timeouts are wired for the implemented operations. The general runner checks
  aggregate output size after candidates have been generated, so the cap does
  not itself prevent temporary workspace growth. Archive-entry/expansion and
  subprocess-count fields are reserved for unimplemented pipelines and must not
  be advertised as active defenses.
- CLI workspaces are removed with retries on success, failure, and cancellation;
  an incomplete cleanup is a critical report warning and old workspaces are
  swept after 24 hours on startup. API uploads are removed after successful
  processing; outputs remain until DELETE, 50-job eviction, or graceful server
  shutdown. A crash may leave a private API session until the 24-hour startup
  sweep. Deletion is best effort, never secure erasure.

## Known limitations inside implemented features

- Page-moving operations do not rebuild outlines, AcroForm trees,
  attachments, XMP, page labels, document actions/JavaScript, or signature
  semantics. Detected losses receive fidelity warnings; signature loss is
  critical and duplicate form names are reported. `remove-pages` refuses
  page-referencing structures it cannot safely rewrite rather than emitting a
  broken PDF.
- Crop changes the visible CropBox only. Hidden page content remains and the
  operation always warns that it is not redaction.
- Opening an encrypted input does not preserve encryption in the output; this
  receives a critical security warning.
- In-process pikepdf/libqpdf, PDFium, and Pillow parsing runs with the user's
  privileges. Timeouts are cooperative checkpoints and cannot forcibly stop a
  parser stuck inside native code. Worker-process isolation is not implemented.
- API jobs execute synchronously in the request process. There is no background
  queue, progress/cancellation endpoint, concurrency quota, or rate limiter.
- Split-by-bookmarks, n-up, booklet, insert/interleave/blank-page, and page-label
  preservation are not implemented. CLI progress bars and `--dry-run` are not
  wired.
- Compression, repair, OCR, Office/HTML/Markdown conversion, PDF/A or PDF/UA
  validation/conversion, forms editing, encryption tools, redaction,
  signatures, compare, scanner/camera acquisition, and the full browser UI are
  unavailable. Their engines or data-model fields do not imply otherwise.
- Online dependency-license and security-advisory verification was not
  performed without user-approved network access. Generated notices and the
  SBOM are environment-specific offline evidence only.

## In progress

No edit is mid-operation. The independent audit/remediation pass is complete;
`docs/INDEPENDENT_AUDIT.md` records the final 265-passed/one-skipped gate,
remaining findings, and sensitive-document release decision.

## Next work, highest priority first

1. Obtain explicit network authorization and complete the authoritative
   advisory/upstream-license review, then perform clean cross-platform builds
   and installation-profile tests described by the independent audit.
2. Add worker-process isolation, hard cancellation, API concurrency/rate caps,
   and a background queue before treating hostile-document web processing as a
   hardened release mode.
3. Build the real browser UI against the existing API without adding remote
   assets or advertising unavailable capabilities.
4. Preserve or safely rewrite document-level structures for page moves; remove
   warnings only when semantic and render regressions pass.
5. Define and clean-install distinct dependency profiles with per-platform
   locks/hashes and profile-specific SBOMs before claiming Lite/Standard/Full.

## Stable interface decisions

- Engines: pikepdf for structural operations, PDFium for rendering, Pillow for
  imaging, and optional external tools only through the allowlisted subprocess
  runner.
- `last-5` means “the last five pages.”
- Split naming: `<stem>-page-NNN.pdf`, `<stem>-part-NNN.pdf`, or
  `<stem>-pages-<token>.pdf`.
- Reports: human summary by default, `--json` for machines, and `--report-dir`
  for files; document text and passwords are excluded.
- Exit codes: 0/1/2/3/4/5/130 as documented in `docs/CLI.md`.
