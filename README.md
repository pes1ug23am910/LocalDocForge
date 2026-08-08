# LocalDocForge

[![Packaging and install matrix](https://github.com/pes1ug23am910/LocalDocForge/actions/workflows/packaging.yml/badge.svg)](https://github.com/pes1ug23am910/LocalDocForge/actions/workflows/packaging.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.12–3.14](https://img.shields.io/badge/python-3.12%20%7C%203.13%20%7C%203.14-blue.svg)](docs/PACKAGING.md)

**Merge, split, compress, convert, and inspect PDFs entirely on your own
machine.** No uploads, no account, no telemetry — the shipped package
contains no outbound network client at all.

LocalDocForge is a privacy-first document-processing workbench: a typed
Python core library, a scriptable `ldf` CLI, and a localhost API with a
status page. It exists for the documents you would never paste into a
cloud converter.

```text
$ ldf compress outline-6page.pdf -o smaller.pdf
Operation : compress
Status    : success
Engine    : pikepdf 10.10.0
Input     : outline-6page.pdf (5,777 B, 6 pages)
Output    : smaller.pdf (2,953 B, 6 pages)
Elapsed   : 0.36s
Validation: passed (7 checks)
```

*Real output from this repository's synthetic test fixture — 48.9 % smaller,
losslessly, with the result render-compared pixel-for-pixel against the
source before anything was published.*

## Why this instead of a cloud converter

- **Your files never leave the machine.** Every operation runs locally
  through pikepdf/libqpdf, PDFium, and Pillow. The web UI binds to
  `127.0.0.1` by default and authenticates every request.
- **It refuses to guess.** Damaged PDFs are rejected rather than silently
  "repaired", outputs are structurally reopened and render-checked before
  they are published, and originals are never modified in place.
- **It refuses to lie.** A feature is advertised only when its
  implementation *and* a live engine probe both pass (`ldf doctor` is the
  truth, not a brochure). Cropping is never called redaction. Known
  preservation losses are reported with stable warning codes instead of
  being dropped silently. `ldf agent-brief` turns the same registry and live
  probe state into compact Markdown or JSON for coding agents; planned
  capabilities cannot enter that output.

## What works today

| Area | Capabilities |
|---|---|
| Organize | merge (whole files or per-input page ranges) · split (ranges / every-N / single pages) · remove pages · extract pages · reorder/duplicate/reverse |
| Edit | rotate · crop (with an explicit **crop is not redaction** warning) |
| Optimize | compress — lossless structural preset (stream recompression, object streams, unused-resource pruning). Image data is never re-encoded; sampled pages must render pixel-identical to the source or nothing is published; "didn't shrink" is reported, never hidden |
| Convert | images → PDF (HEIC/JPG/PNG/TIFF/BMP/WebP, multipage TIFF, EXIF orientation, A4/Letter/Legal/image/custom page sizes) · PDF → images (PNG/JPEG/WebP/TIFF, 18–1200 DPI; `--preset llm` makes per-page JPEG q85 renders with long edge ≤ 1568 px) · convert images (iPhone HEIC and the other formats → PNG/JPEG/WebP/TIFF; `--preset llm` produces AI-assistant-ready JPEGs with GPS/EXIF stripped) |
| Inspect | page count, encryption, page sizes, annotations, outlines, forms, attachments, JavaScript presence |
| Agent integration | deterministic `ldf agent-brief` Markdown/JSON generated from implemented `CAPABILITY_SPECS` plus one live capability probe, including usage, exit codes, gotchas, workflow, and feedback rules |
| Local web API | loopback FastAPI service + status page; every conversion runs in a fresh OS-contained worker process |

Everything above is covered by the repository's test suite (504 tests) and a
full release gate. OCR, Office conversion, lossy compression presets,
redaction, signatures, and the rest of the roadmap are **not implemented
yet** and are honestly reported as unavailable by `ldf doctor` — see
[`docs/FEATURE_MATRIX.md`](docs/FEATURE_MATRIX.md).

## Install

Requires CPython 3.12–3.14. There is no PyPI package yet; install from the
repository:

```bash
# CLI + core PDF/image tools (the default "lite" set)
pip install "git+https://github.com/pes1ug23am910/LocalDocForge"

# with the localhost web API and status page
pip install "localdocforge[standard] @ git+https://github.com/pes1ug23am910/LocalDocForge"

ldf doctor
```

For a supply-chain-audited install — hash-locked dependencies first, then
the package with no re-resolution — use the lock profiles:

```powershell
# Windows PowerShell (the release-hardened platform)
git clone https://github.com/pes1ug23am910/LocalDocForge && cd LocalDocForge
py -3.14 -m venv .venv
.venv\Scripts\python.exe -m pip install --require-hashes `
  -r requirements\locks\lite.txt
.venv\Scripts\python.exe -m pip install --no-deps ".[lite]"
.venv\Scripts\ldf.exe --json doctor
```

```bash
# Linux/macOS (CI-tested; see "Status and maturity")
git clone https://github.com/pes1ug23am910/LocalDocForge && cd LocalDocForge
python3 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements/locks/lite.txt
.venv/bin/python -m pip install --no-deps ".[lite]"
.venv/bin/ldf --json doctor
```

Profiles: `lite` (CLI/core, the default), `standard` (adds the localhost
API), `full` (adds the pypdf diagnostic adapter), `dev` (adds test/lint/
build tooling). Exact recipes, locks, and uninstalls:
[`docs/PACKAGING.md`](docs/PACKAGING.md).

## Everyday commands

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
ldf pdf-to-images scanned.pdf -d vision/ --preset llm   # per-page vision-ready JPEGs
ldf convert-images photos/*.HEIC -d ready/ --preset llm  # iPhone photos → AI-ready JPEGs
ldf inspect input.pdf
ldf agent-brief                   # registry-derived Markdown for coding agents
ldf --json agent-brief            # the same ordered snapshot as structured JSON
ldf --json doctor
ldf --strict-offline web   # localhost API + status page; prints the session token
```

`agent-brief` must resolve the repository's writable
`docs/AGENT_FEEDBACK.md`. It works with a discoverable source checkout (including
the repository-local environment above); a detached wheel/direct VCS install
outside any checkout exits 1 rather than pointing agents at a packaged imitation.

Page ranges: `1-5,9,12-end`, `odd`, `even`, `reverse`, `last`, `last-5`
(the last five pages). For encrypted PDFs, non-interactive callers use the
global `--password-stdin` option (one UTF-8 line) or `LDF_PASSWORD`; precedence
is flag → environment → hidden TTY prompt. Password values are never taken as
command-line arguments or written to output/reports/logs. One password applies
to all encrypted inputs in an invocation. Existing outputs are never
overwritten unless you say
`--collision overwrite`. Full grammar, exit codes, and the HTTP API
contract: [`docs/CLI.md`](docs/CLI.md).

The `ldf web` server binds to `127.0.0.1`, authenticates every API call
with a per-session token header, sends CSP/hardening headers, and keeps job
history in memory only. Every conversion runs in a fresh spawned worker
under OS containment (a Windows Job Object with kill-on-close, memory, CPU,
and process limits; a POSIX process group with resource limits), and a
terminal state is published only after the process tree is verified gone.

## Status and maturity

**Early alpha, honestly scoped.** Phase 0 (foundation), the core of Phase 1
(structural PDF tools + image conversion), and the first Phase 2 slice
(lossless compression, 2026-08-03) are implemented, tested, and gated.

- **For everyday, non-sensitive documents:** working and validated — the
  full release gate (locks, lint, types, two full test-suite runs including
  a network-blocked one, reproducible builds, clean install matrix) passes,
  and every capability has been exercised end-to-end on real files.
- **For sensitive documents:** **not yet cleared, by the project's own
  release decision.** The open blockers (a bundled-dependency advisory,
  no OS-enforced outbound-network denial proof, in-process CLI parsing,
  platform scope) are recorded plainly in [`docs/STATUS.md`](docs/STATUS.md)
  — nothing is hidden, and nothing is promised early.
- **Platforms:** Windows 11 x64 is the release-hardened platform with
  retained local evidence. Linux and macOS pass the full test suite and
  clean-install matrix in CI (CPython 3.12–3.14, first executed
  2026-08-03); they are CI-tested, not yet release-hardened. Deleting temp
  files is best-effort, not forensic erasure, and `--strict-offline` is an
  application policy — not an OS firewall; see
  [`docs/THREAT_MODEL.md`](docs/THREAT_MODEL.md) for the boundaries stated
  without marketing.

## FAQ

**Is it really offline?** The shipped package contains no outbound network
client, telemetry, update check, or remote asset — verified by source
inspection and by running the complete test suite with Python DNS and
non-loopback sockets denied. `--strict-offline` additionally rejects
recognizable network filesystem paths and non-loopback serving. It is
application policy, not an OS firewall; a host firewall or offline VM
remains the stronger guarantee.

**Can it OCR / convert Office files / shrink scanned PDFs?** Not yet.
Those are roadmap phases, and `ldf doctor` will keep saying so until each
pipeline lands with tests. Lossless compression won't shrink scan-heavy
PDFs much (their bytes are already JPEG data) — and the report tells you
exactly that instead of pretending.

**Why does `remove-pages` sometimes refuse?** Your document has structures
(outlines, forms, internal links, tagged content) that this build cannot
yet rewrite safely. Refusing beats handing you a silently corrupted file.

**Can I use it as a Python library?** Yes —
[`docs/LIBRARY_API.md`](docs/LIBRARY_API.md), with executed examples.

## Documentation

The full index is at [`docs/README.md`](docs/README.md). Highlights:
[`docs/TECHNICAL_REFERENCE.md`](docs/TECHNICAL_REFERENCE.md) (every
subsystem in one document) · [`docs/CLI.md`](docs/CLI.md) (reference) ·
[`docs/GETTING_STARTED_WINDOWS.md`](docs/GETTING_STARTED_WINDOWS.md)
(task-oriented walkthrough) · [`docs/MACHINE_READINESS.md`](docs/MACHINE_READINESS.md)
(dated verification evidence) · [`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md)
(contributing and the capability golden path).

## Development

```powershell
pwsh -File scripts\bootstrap.ps1                # dev venv, locks, tests, lint, types
.venv\Scripts\python.exe -m pytest tests -q    # 504 tests
.venv\Scripts\python.exe -m ruff check src tests scripts
.venv\Scripts\python.exe -m mypy
```

Test fixtures are synthetic and generated by
`tests/fixtures/make_fixtures.py` — no third-party documents. The
capability rules are enforced by tests: a feature flips to "available" only
in the same change that lands its pipeline and tests, and the
documentation-consistency suite fails when these docs drift from shipped
reality. Start at [`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md).

## Roadmap

Lossy compression presets, repair, OCR, Office↔PDF, PDF/A, PDF↔Markdown,
editor, forms, encryption, redaction, signatures, compare, scanner
acquisition, full browser UI — phased plan in
[`docs/IMPLEMENTATION_PLAN.md`](docs/IMPLEMENTATION_PLAN.md), current truth
in [`docs/STATUS.md`](docs/STATUS.md).

## License

MIT. Dependency and external-engine licensing: [`docs/LICENSING.md`](docs/LICENSING.md).
