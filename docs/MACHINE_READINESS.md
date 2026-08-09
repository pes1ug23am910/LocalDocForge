# Machine Readiness Report — Primary Windows 11 Workstation

Assessment run: **2026-08-03**. Everything below marked "executed" was run on
this machine on that date; nothing is inferred from configuration alone.
Capability inventory updates for S4/S6 are dated **2026-08-09**; the S5 table
mode update is dated **2026-08-10**. They rely on the definitive gates and
operation probes recorded in `docs/STATUS.md`, rather than being folded into
the original run. This report answers one question —
*is LocalDocForge ready for use on this machine?* — and records the evidence.
Companion how-to:
`docs/GETTING_STARTED_WINDOWS.md`.

## Verdict

**Yes for everyday, non-sensitive local document work. No for sensitive
documents — unchanged from the project's own release decision.**

- The complete local release gate passed end-to-end today (exit 0, 344 s):
  locks, lint, types, the full 390-test suite twice (normally and with Python
  DNS/non-loopback sockets denied), reproducible wheel/sdist builds,
  sdist-to-wheel equivalence, artifact-manifest match, and clean
  Base/Lite/Standard/Full install/smoke/uninstall matrices.
- Every shipped capability was additionally exercised end-to-end with real
  files (15 CLI invocations, 27 structural verifications — all passed), and a
  live localhost API session confirmed token auth, Job-Object worker
  containment, download, and deletion behavior.
- The rebuilt wheel is **byte-identical to the canonical 2026-07-20 artifact**
  (SHA-256 `392d0952…`), a 14-day cross-session reproducibility confirmation.
- The blockers that keep `docs/STATUS.md` at *FAIL / not cleared for sensitive
  documents* are release-scope and platform-scope issues (§5); none of them
  affect correct operation for ordinary local files on this machine.

## 1. Machine and environment identity

| Item | Value |
|---|---|
| OS | Windows 11 Home Single Language, build 10.0.26200 (x64) |
| CPU / RAM | Intel Core Ultra 7 155H · 15.4 GB |
| Shell | PowerShell 7.6.4 |
| Repository | `E:\Sem-VI-Break\Pdf-Conversion-Tool` @ `7e49624` + release-hardening worktree changes |
| Working venv | `.venv` — CPython **3.14.4**, dev profile, `localdocforge 0.1.0` installed |
| Other interpreters (`py --list`) | 3.14.4 (default) · 3.13.5 (Astral/uv-managed) · 3.10.11 (below `requires-python`, unused) |
| uv | 0.11.26 at `.venv\Scripts\uv.exe` (not on system PATH — expected) |
| Free disk (E:) | ~136 GB |
| Canonical artifacts | `dist\windows-11-x64\` wheel 96,696 B / sdist 83,262 B (2026-08-03 compression-slice identity; sizes match `docs/PACKAGING.md`). The morning's 2026-07-20 set is archived in `dist\windows-11-x64-2026-07-20\` |

## 2. Executed verification — 2026-08-03

### 2.1 Full release gate — PASSED (exit 0, 344 s)

Command (the resume command documented in `docs/STATUS.md`):

```powershell
.venv\Scripts\python.exe scripts\release_gate.py `
  --profile-evidence packaging-evidence\windows-3.14.4.json
```

| Gate step | Result |
|---|---|
| `lock_profiles.py --check` (uv 0.11.26, offline, hash/profile drift) | passed |
| Ruff over `src tests scripts` | passed — "All checks passed!" |
| mypy (31 source files) | passed — no issues |
| `git diff --check` | passed (CRLF notices only) |
| `pip check` | passed — no broken requirements |
| `generate_release_artifacts.py --check` (SBOM/notices drift) | passed |
| Full test suite, normal | 390 collected — 388 passed, 2 expected platform skips |
| Full test suite, Python DNS + non-loopback sockets denied | same result |
| Reproducible builds (2× sdist + 2× wheel, isolated, hash-locked backend) | passed, byte-identical repeats |
| Twine check (wheel + sdist) | PASSED |
| Sdist→wheel rebuild equivalence | passed |
| Artifact manifest comparison | passed — wheel SHA-256 `392d0952313d89ee817139c90b388a79aa2d1c655126ee0876285e59f08176b2` (canonical at the time; superseded later the same day by the Phase 2 slice — §7) |
| Clean profile matrix: base / lite / standard / full (source + wheel install, doctor + core smoke, uninstall, `pip check`) | all four passed |

Final line: `release gate passed for this local platform/interpreter only`.

**Evidence-file note:** by design, this command refreshes
`packaging-evidence\windows-3.14.4.json` in place; that file now records the
2026-08-03 run (revision `7e49624`, `working_tree_changes: true`). The
2026-07-20 records `windows-3.14.4-final-gate.json`, `windows-3.13.5.json`,
`windows-pdf-render-2026-07-20.json`, and the artifact checksum file are
untouched, and the checksummed wheel/sdist themselves are unchanged.

### 2.2 End-to-end CLI smoke with real files — PASSED (15/15 commands, 27/27 checks)

Synthetic inputs were generated with Pillow (two PNGs, one JPEG carrying EXIF
Orientation=6, one two-frame multipage TIFF), then every shipped operation was
run against them and the outputs were reopened independently with pikepdf/Pillow:

| Command exercised | Exit | Structural verification |
|---|---|---|
| `images-to-pdf` 3 mixed images, `--page-size A4` | 0 | 3 pages |
| `images-to-pdf` multipage TIFF, `--page-size image` | 0 | 2 pages |
| `merge` two PDFs | 0 | 5 pages |
| `merge "a.pdf::1,3" b.pdf` per-input picks | 0 | 4 pages |
| `inspect` | 0 | — |
| `rotate --degrees 90 --pages odd` | 0 | `/Rotate 90` on pages 1,3,5 only; 0 on 2,4 |
| `crop --box 50,50,400,500` under `--strict-offline` | 0 | `/CropBox [50 50 400 500]` exact |
| `split --every 2` | 0 | `merged-part-001..003.pdf` (documented naming) |
| `extract-pages 1-2,last` | 0 | 3 pages |
| `remove-pages 2` | 0 | 4 pages |
| `organize --order reverse` | 0 | 5 pages |
| `pdf-to-images --format png --dpi 150` | 0 | 5 PNGs, all decode |
| repeat of remove-pages onto existing output | **5** | collision policy contract honored |
| `extract-pages --pages 0` | **2** | usage-error contract honored |
| `--json doctor` | 0 | see §3 |

### 2.3 Live localhost API session — PASSED

`ldf web --port 8477` was started, exercised, and stopped:

- `/api/health`: `status=ok`, `version=0.1.0`, `loopback_only=True`.
- `/api/capabilities`: exactly the shipped capabilities available (ten in
  this morning session; eleven after the same-day compression slice — §7).
- Request **without** `X-LDF-Token` → **401** (cookie alone cannot authorize).
- `POST /api/jobs/merge` (two uploaded PDFs) completed in a fresh spawned
  worker. Observed containment record of the finished job:
  `platform=win32 · document_gate=opened_after_containment ·
  process_tree=windows_job_object_kill_on_close ·
  memory=windows_job_object_job_memory ·
  cpu=windows_job_object_time_and_parent_accounting_watchdog ·
  child_processes=windows_job_object_active_process_limit ·
  network=application_policy_only; no OS network sandbox ·
  process_tree_exit=verified_empty ·
  private_cleanup=inputs_removed; outputs_retained`
- Output download (448,704 B) reopened with pikepdf: valid, 5 pages.
- `DELETE` the job, then re-request the output → **404**.

(The server process was force-terminated at the end of the session rather than
Ctrl+C'd; the graceful path is regression-tested by the suite, and startup
residue cleanup is lease-gated by design.)

### 2.4 `ldf doctor` — 2026-08-03 executed snapshot

merge · split · remove-pages · extract-pages · organize · rotate · crop ·
inspect · images-to-pdf · pdf-to-images — all `available: true`, each backed by
a passing live engine probe (compress joined this list the same day; §7).
Capabilities not listed here were not implemented in that snapshot.

**2026-08-09 update:** the current registry additionally implements
convert-images, pdf-to-markdown, and markdown-to-pdf. The S4/S6 operation
probes and definitive 616-outcome F1-remediation gate are recorded in
`docs/STATUS.md`.

**2026-08-10 update:** pdf-to-markdown additionally supports default-off,
Markdown-only `--tables` reconstruction for conservative explicit-line grids.
The S5 operation probes and definitive evidence are recorded in
`docs/STATUS.md`; borderless and merged-cell reconstruction remain unavailable.

## 3. Engine inventory on this machine

Python engines (in `.venv`, all probed available):

| Engine | Version | Role |
|---|---|---|
| pikepdf | 10.10.0 (qpdf library 12.3.2) | structural PDF operations |
| pypdfium2 | 5.12.1 | render validation, previews, PDF→image |
| pdfplumber | 0.11.10 | opt-in explicit-line table geometry/cell extraction |
| Pillow | 12.3.0 | imaging |
| pypdf | 6.14.2 | optional diagnostic adapter |

External executables (availability is still capability-gated; see
`docs/FEATURE_MATRIX.md` rules):

| Executable | Status | Install hint when needed |
|---|---|---|
| Typst 0.15.1 | **installed and wired** for bounded Markdown→PDF | `winget install Typst.Typst` on another machine |
| qpdf CLI | not installed | `winget install qpdf.qpdf` |
| Tesseract / OCRmyPDF | not installed | `winget install UB-Mannheim.TesseractOCR` |
| Ghostscript | not installed | `winget install ArtifexSoftware.GhostScript` |
| LibreOffice | not installed | `winget install TheDocumentFoundation.LibreOffice` |
| Pandoc | not installed | `winget install JohnMacFarlane.Pandoc` |
| veraPDF | not installed | installer from verapdf.org |

## 4. What "ready" means, by use case

| Use case | Ready? | Basis |
|---|---|---|
| Organize/split/merge/rotate/crop ordinary local PDFs (CLI) | **Yes** | §2.1–§2.2 |
| Images→PDF and PDF→images for ordinary files | **Yes** | §2.2 |
| Markdown→PDF for strict-UTF-8 CommonMark-subset files | **Yes on this machine** — Typst 0.15.1 probe passes | §3 and `docs/CLI.md` |
| Inspect PDFs read-only | **Yes** | §2.2 |
| Compress PDFs (lossless structural preset) | **Yes** | §7 |
| Localhost API / status page for the same operations | **Yes** | §2.3 |
| Scripted/automated use (`--json`, exit codes, reports) | **Yes** — contracts verified, including failure codes | §2.2 |
| Sensitive documents (privileged, regulated, high-consequence) | **No** — project release decision stands | §5 |
| OCR, Office↔PDF, lossy compression presets, repair, PDF/A, redaction, signatures, editing | **Not available** in this build | `docs/FEATURE_MATRIX.md` |
| Linux / macOS / CPython 3.12 | **Unverified** — no executed evidence anywhere yet | `docs/PACKAGING.md` |

## 5. Why sensitive documents remain blocked (unchanged today)

These are the open blockers recorded in `docs/STATUS.md`; today's verification
does not close any of them, and none is specific to this machine's health:

1. Bundled OpenJPEG 2.5.4 inside Pillow has an open advisory
   (`OSV-2025-219`); PDFium 152.0.7947.0 and 19 unversioned native children
   are advisory-unknown.
2. No OS-enforced outbound-plus-DNS network denial has been executed here.
   `--strict-offline` + the gate's socket-denial test run are application/
   Python-level controls, not a firewall. The opt-in probe
   (`scripts\run_windows_firewall_gate.ps1`) requires elevation and by design
   cannot prove DNS-service denial alone.
3. CLI parsing is in-process with cooperative timeouts; API workers are a
   contained *failure* boundary that still holds the user's filesystem
   authority — not an OS sandbox (`docs/THREAT_MODEL.md`).
4. Single-platform evidence: Windows 11 x64 on CPython 3.14.4/3.13.5 only.
5. Host-specific evidence gaps: no real mapped network drive was ever mounted
   for regression, and symbolic-link creation privilege is unavailable on this
   host (junction evidence is not relabelled).

Also permanent by design: crop is never redaction, deletion is not forensic
erasure, and a hard crash can leave a lease-protected session directory.

## 6. Re-verification recipe

Run these whenever the environment, dependencies, or code change (details:
`docs/PACKAGING.md`):

```powershell
cd E:\Sem-VI-Break\Pdf-Conversion-Tool
.venv\Scripts\ldf.exe --json doctor
.venv\Scripts\python.exe scripts\release_gate.py `
  --profile-evidence packaging-evidence\windows-3.14.4.json   # ~6 min; refreshes that evidence file
git diff --check
```

A passing result applies **only** to this OS build, architecture, and
interpreter — the project's standing rule, restated here.

## 7. Same-day addendum — Phase 2 compression slice (2026-08-03, afternoon)

After the morning verification above, the first Phase 2 feature — **lossless
PDF compression** — was implemented and verified on this machine following
the documented Phase 2 requirement to begin with compression:

- **Shipped in one change:** `operations/optimize.py` (pikepdf lossless
  optimization + PDFium pixel-comparison against the source), registry flip,
  `ldf compress` CLI command, API operation, 17 new tests (407 total), and
  the documentation set. Doctor now reports **eleven** capabilities.
- **Executed verification:** ruff and mypy clean (32 source files); the full
  407-test suite passed normally and network-blocked; live smokes:
  `outline-6page.pdf` 5,777 → 2,953 B (48.9 % smaller, outlines preserved,
  `--strict-offline`), `mixed-sizes.pdf` −38.27 % with
  `render_compare.identical: true` over `--json`; an image-heavy PDF showed
  the honest near-zero-reduction path with its `compress-no-reduction`
  report.
- **Gate behavior on an intentional package change:** the first full gate
  failed precisely at the artifact-manifest drift check (every earlier step
  green) — the designed refusal. The remediation gate with
  `--update-artifact-manifest --dist-dir dist\windows-11-x64` then passed
  end-to-end in 382 s, refreshed
  `packaging-evidence\windows-3.14.4.json` (`release_manifest_verified:
  true`), and retained the new canonical artifacts: wheel
  `540a36212cc2adbf21cb321ad0a684b806a26ef8fb7ddaae942d2fff0dccbff3`
  (96,696 B), sdist `7bd03dfbe3a817da05a26f490ededb67f8cd9482bfc190e7076be3222ca6ea91`
  (83,262 B). The superseded 2026-07-20 artifacts are archived in
  `dist\windows-11-x64-2026-07-20\` with their original checksum file.
  (This identity was itself superseded hours later by the Linux-mypy
  portability fix found by the first CI run — current identity:
  `docs/STATUS.md` and `docs/PACKAGING.md`; this set is archived in
  `dist\windows-11-x64-2026-08-03-superseded\`.)
- **Scope unchanged elsewhere:** the §5 blockers stand; Windows CPython
  3.13.5 was not re-run after the slice, and the release decision for
  sensitive documents remains FAIL.

## Related documents

`docs/GETTING_STARTED_WINDOWS.md` (how to use it) · `docs/CLI.md` (reference) ·
`docs/STATUS.md` (release truth) · `docs/PACKAGING.md` (profiles/locks/builds) ·
`docs/FEATURE_MATRIX.md` (capability truth) · `docs/THREAT_MODEL.md` (security
boundaries) · `docs/INDEPENDENT_AUDIT.md` (historical audit record) ·
`docs/DEVELOPMENT.md` (developer guide) · `docs/LIBRARY_API.md` (library
usage) · `docs/README.md` (documentation index)
