# LocalDocForge

A privacy-focused document-processing workbench: a typed Python core library,
a scriptable CLI, and a localhost API with a minimal status page. The current
package has no outbound network client, telemetry, update check, or remote
browser asset. A full browser workflow is still planned.

> **Project status: early alpha.** Phase 0 (foundation), the core of
> Phase 1 (structural PDF tools + image conversion), and the first Phase 2
> slice (lossless PDF compression, 2026-08-03) are implemented and tested.
> Everything else in the roadmap is *not yet available* and is honestly
> reported as such by `ldf doctor`. See `docs/FEATURE_MATRIX.md`.

> **Release decision: FAIL / not cleared for sensitive documents.** Windows 11
> x64 is the primary and only locally executed release-hardening platform for
> this checkpoint. Linux and macOS remain unverified; declared package markers
> and configured CI jobs are not pass evidence. Exact results and blockers are
> recorded in `docs/STATUS.md` and `docs/INDEPENDENT_AUDIT.md`.

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
  forensic erasure; a crash can leave a private session directory, and startup
  removes it only after acquiring its external OS-held session lease (missing,
  unreadable, or still-held leases are preserved fail-closed); CLI parsers run
  in-process, while API parsers run in bounded one-job workers that still retain
  your filesystem authority and are not an OS filesystem/network sandbox (see
  `docs/THREAT_MODEL.md`).

## What works today

The capability rows below are implemented and covered by the repository test
suite. Current release-platform execution evidence is Windows 11 x64 only; see
`docs/PACKAGING.md` for the exact interpreter matrix.

| Area | Capabilities |
|---|---|
| Organize | merge (whole files or per-input page ranges) · split (ranges / every-N / single pages) · remove pages · extract pages · reorder/duplicate/reverse. Page-moving operations warn about known document-level losses; remove-pages refuses structures it cannot safely rewrite. |
| Edit | rotate · crop (with an explicit **crop is not redaction** warning) |
| Optimize | compress — lossless structural preset (stream recompression, object streams, unused-resource pruning). Image data is never re-encoded; sampled pages must render pixel-identical to the source or nothing is published; "didn't shrink" is reported, never hidden. Lossy presets are planned and refused until they exist. |
| Convert | images → PDF (JPG/PNG/TIFF/BMP/WebP, multipage TIFF, EXIF orientation, A4/Letter/Legal/image/custom page sizes, 36–600 DPI) · PDF → images (PNG/JPEG/WebP/TIFF, 18–1200 DPI) |
| Inspect | page count, encryption, page sizes, annotations, outlines, forms, attachments, JavaScript presence |
| Safety net | every candidate PDF is structurally reopened and render-checked (all pages for selected high-risk cases, otherwise up to 20 sampled pages); generated images must decode. All candidates validate before publication, each final file is published atomically, and input/output aliases are refused. Multi-output rollback after a handled publication failure is best effort, not a process-crash transaction. |

## Quick start

The default and explicit Lite installs contain the local CLI/core PDF and image
tools. This repository's reproducible path installs a hash-locked dependency
set first, then installs LocalDocForge without re-resolving it:

```powershell
# Windows PowerShell — declared CPython 3.12, 3.13, or 3.14 recipe
py -3.14 -m venv .venv
.venv\Scripts\python.exe -m pip install --require-hashes `
  -r requirements\locks\lite.txt
.venv\Scripts\python.exe -m pip install --no-deps ".[lite]"
.venv\Scripts\ldf.exe --json doctor
```

```bash
# Declared portable recipe; Linux/macOS are not verified at this checkpoint
python3 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements/locks/lite.txt
.venv/bin/python -m pip install --no-deps ".[lite]"
.venv/bin/ldf --json doctor
```

Use `requirements/locks/standard.txt` plus `".[standard]"` to add the
localhost API/status page. Use `full.txt` plus `".[full]"` to additionally
install the optional pypdf diagnostic adapter. “Full” means all currently
shipped Python adapters, not the unimplemented roadmap. Exact profile,
regeneration, build, CI, and uninstall commands are in
[`docs/PACKAGING.md`](docs/PACKAGING.md).

The bootstrap scripts install the development set by default and accept an
explicit `lite`, `standard`, `full`, or `dev` profile.

For the primary Windows workstation there is a task-oriented walkthrough,
[`docs/GETTING_STARTED_WINDOWS.md`](docs/GETTING_STARTED_WINDOWS.md), and a
dated machine-readiness/evidence report,
[`docs/MACHINE_READINESS.md`](docs/MACHINE_READINESS.md).

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
ldf compress input.pdf -o smaller.pdf                  # lossless; images untouched
ldf images-to-pdf scans/*.jpg -o scans.pdf --page-size A4
ldf pdf-to-images input.pdf -d pages/ --format png --dpi 300
ldf inspect input.pdf
ldf --json doctor
ldf --strict-offline web  # localhost API + status shell; prints the session token
```

The `ldf web` server binds to 127.0.0.1 by default, authenticates every API
call with a per-session token header, sends CSP/hardening headers, and keeps
job history in memory only. Admission happens before upload bytes enter a
request-scoped, aggregate-bounded transport spool inside the private API session.
Each conversion then runs in a fresh spawned worker; the API has a bounded queue,
global and per-client caps, rate control, progress, hard cancellation, and an
explicit asynchronous mode while preserving the default synchronous response.
After a Windows Job Object boundary is established, the parent requires Job
accounting to prove zero active processes before publishing a terminal state. If
the leader exits before assignment, the document gate remains unopened and only
leader-exit proof is reported—never a fictitious empty-Job result. Successful
downloads hold a lease that blocks deletion/eviction until streaming closes, and
session residue cleanup requires the external session lease. `--allow-nonlocal`
is an explicit dangerous opt-in and is refused in strict-offline mode. Endpoint
reference: `docs/CLI.md`.

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
.venv\Scripts\python.exe -m ruff check src tests scripts
.venv\Scripts\python.exe -m mypy
```

Test fixtures are synthetic and generated by
`tests/fixtures/make_fixtures.py` — no third-party documents.

Repository map: `src/localdocforge/` (domain, security, jobs, engines,
pipelines, operations, validation, reporting, config, cli) · `tests/`
(unit, integration, security, fixtures) · `docs/` (architecture, threat
model, engine decisions, feature matrix, status).

Documentation index: [`docs/README.md`](docs/README.md). Developer
onboarding and the capability golden path:
[`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md). Python library usage:
[`docs/LIBRARY_API.md`](docs/LIBRARY_API.md).

## Roadmap

Lossy compression presets, repair, OCR, Office↔PDF, PDF/A, PDF↔Markdown,
editor, forms, encryption, redaction, signatures, compare, scanner
acquisition, full browser UI — phased plan in `docs/IMPLEMENTATION_PLAN.md`,
current truth in `docs/STATUS.md`.

## License

MIT. Dependency and external-engine licensing: `docs/LICENSING.md`.
