# STATUS — LocalDocForge

Last updated: 2026-08-08 (`pdf-to-images --preset llm` and the
registry-derived agent brief on the Windows release-hardening baseline).

**Release decision: FAIL / NOT CLEARED for sensitive documents.** Windows 11
x64 is the primary and only platform with executed local release evidence in
this checkpoint. Linux and macOS are unverified. The historical 2026-07-19
audit remains intact in `docs/INDEPENDENT_AUDIT.md`; its dated 2026-07-20
appendix records which former blockers were closed and which remain.

## Resume commands

```powershell
.venv\Scripts\python.exe scripts\release_gate.py `
  --profile-evidence packaging-evidence\windows-3.14.4.json
.venv\Scripts\python.exe scripts\generate_release_artifacts.py --check
.venv\Scripts\ldf.exe --json doctor
git diff --check
```

The full gate checks lock drift, Ruff, mypy, `pip check`, generated
SBOM/notices drift, the complete collected test suite (504 tests as of
2026-08-08) normally and with Python DNS/non-loopback sockets denied,
reproducible isolated wheel/sdist builds, sdist-to-wheel equivalence,
artifact-manifest drift, and clean profile install/smoke/uninstall. A local
result applies only to the recorded OS, architecture, and interpreter.

## Checkpoint and evidence boundary

- Starting branch/HEAD: `main` at
  `7e49624c4ad646f3998c4a204a4e8049c8748205`, tagged
  `audit-checkpoint-2026-07-19`.
- The starting worktree was **not clean**. It already contained tracked and
  untracked release-hardening/profile/worker changes plus technical inputs and
  Windows addendum. Those user-owned changes were preserved; no clean-start
  claim is made.
- Primary environment preserved: Windows 11 x64, build `10.0.26200`,
  PowerShell 7.6.3, CPython 3.14.4. It was not deleted or replaced.
- Side-by-side release environment: uv-managed Windows x64 CPython 3.13.5.
- Windows CPython 3.12: declared by metadata, not run here.
- Linux: WSL/CI definitions may be available, but no retained hardening run was
  executed in this checkpoint. macOS: not run. Configuration is not pass
  evidence.

## Executed evidence

- The initial complete local gate failed only in clean profile smoke because
  `profile_smoke.py` read obsolete key `pages` instead of `page_count`. Lock,
  quality, artifact, normal/blocked test, reproducible build, Twine, and
  sdist-to-wheel checks had passed. The key was corrected and the focused
  build/profile gate then passed.
- Post-hardening pre-packaging gate passed on CPython 3.14.4: 390 tests were
  collected and both the normal and blocked-network runs completed with two
  expected platform/capability skips; Ruff, mypy over 31 source files,
  `git diff --check`, `pip check`, locks, and generated artifacts passed.
- After implementation and documentation reconciliation, the all-default gate
  passed again in 299 seconds on CPython 3.14.4. It repeated the lock, quality,
  artifact, ordinary/blocked 390-test, reproducible build, sdist-to-wheel, and
  full clean-profile checks. Its supplemental record is
  `packaging-evidence/windows-3.14.4-final-gate.json`; the checksum-authenticated
  canonical record remains `packaging-evidence/windows-3.14.4.json`.
- Authenticated clean Base/Lite/Standard/Full source and wheel installs,
  doctor/core smokes, dependency checks, uninstalls, Ruff, mypy, normal tests,
  blocked-network tests, and artifact drift passed on both Windows CPython
  3.14.4 and 3.13.5. Evidence:
  `packaging-evidence/windows-3.14.4.json` and
  `packaging-evidence/windows-3.13.5.json`.
- Canonical package identity at that checkpoint: source
  `c39c91b599d1d8b050599a2e62bceaf5339818e613e56c6a4997be1021532f70`;
  wheel `392d0952313d89ee817139c90b388a79aa2d1c655126ee0876285e59f08176b2`
  (92,228 bytes); sdist
  `4675fa04055353032a0ab55bb373f3d74068b46dad1b86bba82d7bd124582fef`
  (80,014 bytes). Superseded on 2026-08-03 by the compression-slice identity
  below; the 2026-07-20 artifacts and their checksum file are archived in
  `dist/windows-11-x64-2026-07-20/`.
- Four strict-offline synthetic outputs covering crop, images-to-PDF, merge,
  and rotate (16 pages) reopened with pikepdf, fully rendered with PDFium, and
  passed visual inspection. Sanitized hashes/results are retained in
  `packaging-evidence/windows-pdf-render-2026-07-20.json`.

## Executed evidence — 2026-08-03 (readiness verification and Phase 2 start)

- Morning readiness verification on the unchanged checkpoint: the complete
  default gate passed in 344 s on CPython 3.14.4 and reproduced the 2026-07-20
  wheel byte-for-byte; an end-to-end CLI smoke (15 invocations, 27 structural
  verifications) and a live localhost API session (token auth, Job Object
  containment with `verified_empty`, download lease, delete) all passed.
  Details: `docs/MACHINE_READINESS.md`.
- The lossless compression slice then landed: pipeline, registry flip, CLI,
  API, 17 new tests (407 total), and documentation in one change.
- The first post-slice full gate failed exactly at the artifact-manifest
  comparison ("release artifact drift detected") after every prior step
  passed — the designed refusal for changed package sources.
- The remediation gate
  (`release_gate.py --update-artifact-manifest --dist-dir dist\windows-11-x64
  --profile-evidence packaging-evidence\windows-3.14.4.json`) passed
  end-to-end in 382 s: locks, Ruff, mypy over 32 source files, `pip check`,
  generated-artifact drift, 407 tests normally and with Python
  DNS/non-loopback sockets denied (two expected platform skips each),
  reproducible double builds, Twine, sdist-to-wheel equivalence, manifest
  refresh, and clean Base/Lite/Standard/Full install/smoke/uninstall plus the
  dev full-test venv. Evidence: `packaging-evidence/windows-3.14.4.json`
  (`release_manifest_verified: true`).
- Compression-slice package identity (superseded the same day): source
  `32b645769366b28516860eb19c3e10859e7549dd94a03bf7b4a0dd83f634a4b8`;
  wheel `540a36212cc2adbf21cb321ad0a684b806a26ef8fb7ddaae942d2fff0dccbff3`
  (96,696 bytes); archived with its checksum file in
  `dist/windows-11-x64-2026-08-03-superseded/` after the portability fix
  below.
- The repository was published to GitHub
  (`pes1ug23am910/LocalDocForge`, private) and the packaging workflow
  executed for the first time. Its Linux release-gate job failed in mypy:
  `security/paths.py` used `ctypes.WinDLL`, an attribute that exists only on
  Windows — previously invisible because mypy had only ever run on Windows.
  This is recorded as the first real cross-platform CI signal; CI remains
  non-evidence until a green run's artifacts are retained.
- The fix adds an explicit `sys.platform` guard (runtime behavior unchanged —
  the non-Windows path already returned `None` via `AttributeError`), and the
  local quality gate now also runs mypy with `--platform linux` and
  `--platform darwin` so Windows-only attributes fail locally before CI. The
  full gate then passed end-to-end again with a refreshed manifest.
- Portability-fix package identity (superseded the same day by the Linux
  limit-classification fix): wheel
  `972a1e82e662dac0618518aa45bc8f3ac7e0b1cd4f2c179c6a665f222d0b56d8`
  (96,834 bytes), archived with its checksum file in
  `dist/windows-11-x64-2026-08-03-superseded-2/`.
- Retained package identity (2026-08-03; superseded in the release manifest
  by the 2026-08-08 S1 identity below): source
  `150b4aeb882d0b6c8b09787fed94d7df8d460ad2522aac61c37395f3fa504db2`;
  wheel `049345cb2dd1f6d23da011a95984eede0e5d64e2d34a052f8e408132dee2b2b0`
  (98,083 bytes); sdist
  `531c4084dd8071cae8c66e2408c1ed388a1c419c48d3e12b799b32311e3d69ca`
  (84,593 bytes). The complete gate passed with this identity after the
  Linux limit-classification fix. The historical
  `packaging-evidence/windows-11-x64-SHA256SUMS.txt` authenticates those
  retained files; it is not claimed to authenticate the S1 build outputs.
- Cross-platform artifact identity is now honestly platform-scoped. The CI
  Ubuntu gate reached the artifact-manifest comparison and failed
  byte-identity against the Windows-recorded manifest; measurement showed
  three structural causes (package `METADATA` CRLF vs LF, zip external
  attributes with platform modes, and differing deflate streams). The
  manifest moved to a per-platform schema 3 — only `Windows-AMD64` is
  recorded at this checkpoint, with the identity above unchanged — the gate
  gained `--allow-unrecorded-platform` (CI uses it; reproducibility, Twine,
  and sdist-to-wheel checks still run everywhere), and `profile_matrix.py`
  now matches a wheel against any recorded identity, writing an explicit
  `skipped-no-recorded-platform-identity` marker into evidence otherwise.
  The WSL2 Ubuntu simulation of the CI build gate then passed end-to-end
  with the printed unrecorded-platform notice.
- **First executed CI matrix evidence** (GitHub Actions run 30771517389 on
  commit `ee2de18`, artifacts retained per job): the Ubuntu 3.14 runner
  passed the complete release gate — locks, quality, both full suites,
  reproducible builds, and the clean profile matrix — and the
  install/full-test matrix passed on ubuntu-latest, macos-latest, and
  windows-latest across CPython 3.12, 3.13, and 3.14, including the first
  execution anywhere of the suite on macOS and of CPython 3.12. macOS
  surfaced one honest gap on first execution: it has no portable
  child-process count control (containment already reported
  `unsupported`), so that probe test now skips on darwin like the
  documented memory-ceiling skip. The windows-latest 3.13 job failed once
  on a test-side race (the tree probe's pid marker was read between file
  creation and content write) and was rerun; the marker read is now
  parse-until-integer in both polling sites. CI evidence is per-run and
  retained for 30 days; it does not replace the local Windows release
  evidence and the release decision is unchanged.
- Live compression smokes: a deliberately bloated 5-page PDF compressed with
  pixel-identical sampled renders; `outline-6page.pdf` 5,777 → 2,953 bytes
  (48.9 % smaller) under `--strict-offline` with outlines preserved;
  `mixed-sizes.pdf` 2,992 → 1,847 bytes (38.27 %) over the `--json` contract.
- Windows CPython 3.13.5 was **not** re-run after the compression slice; its
  retained evidence (`windows-3.13.5.json`) still describes the 2026-07-20
  package identity. The platform scope remains Windows 11 x64 only.
- Flake root-caused and fixed: two worker-isolation tests intermittently
  failed (one local full-suite run; twice on the windows-latest 3.13 CI
  job) by reading synthetic marker files between creation and content write
  — `write_text` is not atomic, so `read_text` could observe an empty file.
  The test harness now publishes markers via temp-file + `os.replace` and
  reads them with content-aware waits (`_wait_marker_text` /
  `_wait_child_pid`). No shipped code was involved.
- **First executed Linux evidence.** After the pip-seeding workflow fix, the
  CI Ubuntu release-gate job ran the full suite on Linux for the first time:
  405 of 407 outcomes were correct, including the POSIX process-group
  containment tests. The two failures shared one root cause: the POSIX
  `RLIMIT_FSIZE` boundary (derived from `max_output_bytes`) fired before the
  parent's sampled monitor and surfaced as an unclassified crash. Reproduced
  locally on WSL2 Ubuntu-24.04 (CPython 3.12.3), which also showed the host
  variance: `SIGXFSZ` is inherited-ignored there and on GitHub's runners, so
  the write fails with `OSError(EFBIG)` instead of a kill.
- The classification fix landed with the evidence: a `SIGXFSZ`-killed worker
  and a child-reported `EFBIG` failure both terminate as `limit_exceeded`
  with the aggregate-output message (HTTP 422), matching the Windows monitor
  path; forged or unrelated `EFBIG` cannot upgrade any state, and the typed
  limit message carries no parser-derived text. After the fix, the complete
  407-test suite passed on WSL2 Ubuntu-24.04 (Windows-only tests skipping)
  and again on Windows. WSL execution is real Linux-kernel-interface
  evidence but is not a native-Linux release gate; the CI runner remains the
  designated Linux executor and its rows stay non-evidence until a green
  run's artifacts are retained.

## Executed evidence — 2026-08-08 (HEIC input and convert-images slice)

- The dependency change followed the documented lock flow: `pi-heif` 1.4.0
  (the decode-only distribution of pillow-heif) joined the base dependency
  set, `uv` re-resolved under the existing `exclude-newer` policy with wheel
  coverage across every required environment (win/linux/musllinux/macOS ×
  CPython 3.12–3.14), and `lock_profiles.py --check` verified the exports
  offline. The full pillow-heif package — whose binary wheels are GPLv2
  because they bundle the x265 HEVC encoder — was deliberately kept out of
  the runtime closure and added to the dev profile only, where the test
  fixture generator uses it to encode synthetic HEIC inputs (the same role
  ReportLab plays for PDF fixtures). The pi-heif wheel ceiling is LGPLv3
  (libheif 1.23.0 + libde265 1.1.1, verified from the installed wheel's
  license inventory and runtime probe).
- The advisory review was executed the same day (recorded as the 2026-08-08
  verification run in `docs/ADVISORY_REPORT.json`): exact-version OSV and
  GitHub reviewed-advisory queries for pi-heif 1.4.0, libheif 1.23.0, and
  libde265 1.1.1, plus exact-tag license-text verification. OSV returned two
  applicable OSS-Fuzz records for libheif 1.23.0 — OSV-2020-2308 and
  OSV-2023-1129, MEDIUM read-class memory-safety crashes with introducing
  commits and no fixed release enumerated — so libheif is recorded
  `affected`, pi-heif `contains-affected-component`, and the release
  blockers below gained an entry. SBOMs and notices were regenerated
  (48 versioned review records: 30 runtime Python + 18 versioned native).
- The convert-images slice landed as one change: pipeline
  (`operations/images.py::convert_images`), the pi-heif engine probe and
  `convert-images` capability flip (gated on both pillow and pi-heif), HEIC
  input for images-to-pdf, CLI command, API operation with its multipart
  field allowlist, synthetic HEIC/ICC fixtures generated from code, and 35
  new tests (operation, CLI contract, API job, registry honesty,
  release-artifact counts).
- Real end-to-end smokes on this machine: four genuine iPhone HEIC files
  (heix brand, Display P3, EXIF/XMP) converted through `ldf convert-images
  --preset llm` — orientation applied, P3→sRGB conversion, metadata
  stripped with the GPS-aware report codes, long edge bounded to 1568 px,
  ~100 KB JPEG outputs, validation reopening every output — and one HEIC
  composed into a PDF page via `ldf images-to-pdf`.

## Executed evidence — 2026-08-08 (non-interactive PDF passwords, S1)

- S1 adds two non-argv credential sources for encrypted CLI inputs: global
  `--password-stdin` consumes exactly one strict-UTF-8 line, while
  `LDF_PASSWORD` is the lower-precedence automation fallback. The existing
  hidden TTY prompt remains last. One resolved password is tried for every
  encrypted input, differing input passwords fail clearly, and outputs remain
  unencrypted.
- Eighteen focused CLI outcomes cover UTF-8, CRLF, a significant trailing
  space, source precedence in both directions, EOF, invalid UTF-8, explicit
  raw-TTY input, hidden-prompt JSON purity and multi-input guidance,
  wrong/empty passwords, state/environment cleanup,
  `inspect`, `pdf-to-images`, and multi-input failure. Secret canaries are
  absent from stdout, stderr, JSON/human reports, and report-file bytes.
- The complete suite collected 464 outcomes: 462 passed and two expected
  platform/capability skips. Ruff, three-platform mypy over 32 source files,
  generated release-artifact drift, reproducible build, Twine,
  sdist-to-wheel equivalence, and clean Base/Lite/Standard/Full profile checks
  also passed on Windows-AMD64 CPython 3.14.4.
- The first complete S1 gate exposed one inherited release-script mismatch:
  `pi-heif` was already shipped and locked by the prior HEIC slice, but the
  hard-coded base-dependency expectation had not been updated. After explicit
  user approval, the expectation was synchronized and a packaging-contract
  regression now derives and compares the exact declared base set. The
  remediation gate then passed end-to-end in 351.7 seconds with
  `release_manifest_verified: true`.
- Internal pre-review then caught and drove fixes for explicit raw-TTY flag
  semantics, subcommand-help stdin consumption, and prompted multi-input
  guidance. The full gate on that 463-outcome tree passed in 339.2 seconds.
- The required independent cross-family review requested changes after proving
  that the Windows NUL device reports `isatty()` true despite having no console,
  which could leave a non-interactive caller blocked in the hidden prompt. The
  remediation now requires both `isatty()` and a successful `GetConsoleMode`
  call for the actual stdin handle, and a real `stdin=subprocess.DEVNULL`
  subprocess regression verifies exit 2 plus both actionable mechanisms. It
  also documents that a present-but-empty `LDF_PASSWORD` deliberately supplies
  the empty password and removes an unreachable prompt-then-discard path.
- The first remediation-gate attempt refreshed the source/wheel/sdist identity
  to `d4b78a0b520f029eb94f6aba4e7ecdc1fc9dc2f4ffa625a9b12ea692ce34ff86`,
  `7f3e6689ece12b7eba18ec6ec20d7546f49ad67cb4ae3af5bac1c54a7d934476`,
  and `f7ad3852530f1ff6ca6d1f701ddaadf94fb2775d4aa5f97f403474aee53c978d`.
  It passed all preceding checks and installed profiles, then the final Dev
  full-suite correctly rejected the still-previous hashes in this document.
  Protected evidence remained untouched. After documentation synchronization,
  the definitive complete-gate rerun passed end to end in 341.1 seconds on
  Windows-AMD64 CPython 3.14.4, including the fresh Dev full-suite and
  `release_manifest_verified: true`.
- Per the multi-model plan's protected-evidence rule, S1's gates used fresh
  temporary dist and profile-evidence paths. Existing `packaging-evidence/`
  and `dist/windows-11-x64/` records were neither overwritten nor relabelled;
  the new S1 comparison identity is recorded in
  `packaging/release-artifact-manifest.json` and `docs/PACKAGING.md`.

## Executed evidence — 2026-08-08 (pdf-to-images LLM preset, S2)

- S2 extends the already-shipped PDFium `pdf-to-images` pipeline with
  `--preset llm` across the library, CLI, and local API: JPEG quality 85 and a
  per-page long-edge bound of 1568 px, using the same preset mapping as
  `convert-images`. The ordinary 150-DPI render is a never-upscaled ceiling;
  explicit format/quality values replace only their preset fields, while an
  explicit DPI requests fixed-DPI output and disables the cap.
- PDFium's upward dimension rounding now drives both resource preflight and
  rendering. A synthetic fractional-size page that previously could compute
  1568.0000000000002 is adjusted by ULPs before allocation and verified at an
  actual 1568-px edge. Reports retain the established scalar fields and add
  preset/cap mode plus ordered actual dimensions and effective DPI per output;
  capped pages reuse the documented `image-downscaled` info code.
- Eleven new behavior outcomes cover mixed A4 portrait, landscape Letter, and
  a small 300-point square; no-upscale behavior; independent format, quality,
  and DPI precedence; the fractional rounding adversary; JSON CLI output;
  library/CLI/API invalid-preset refusal; API downloads; and report/file
  dimension agreement. The expanded focused image/API/CLI regression command
  completed with 53 passing outcomes.
- The capability registry remains unchanged because PDF-to-images was already
  honestly implemented and engine-gated. No dependency, lock, SBOM, notice,
  advisory, or engine-license change was needed.
- The standalone tree checks passed with 476 outcomes (474 passed and two
  expected skips), Ruff over `src` and `tests`, mypy over 32 source files,
  generated-release-artifact drift, documentation consistency, and diff
  hygiene. An initial all-default gate also passed every phase in 316.206
  seconds. Independent diff review then found that nullable public option
  annotations weakened typed-library compatibility and that semantically
  different preset configurations compared equal. Concrete annotations and
  semantic equality were restored; the focused S2/docs selection passed 60/60
  and the complete 476-outcome suite passed again in 53.217 seconds. The
  definitive post-audit gate then passed from
  2026-08-08T21:48:23.2302079+05:30 through
  2026-08-08T21:54:56.2085350+05:30 (392.978 seconds).
- The final Base, Lite, Standard, and Full source/wheel profile smokes all
  report `status: passed`; the fresh Dev record reports
  `full_tests.status: passed`, `release_manifest_verified: true`, and platform
  `Windows-AMD64`. The gate's ordinary, blocked-network, and two fresh-Dev
  suite modes each exercised the complete 476-outcome tree. Linux and Darwin
  mypy modes passed, but no native Linux or macOS execution is claimed.
- The package identity is recorded in `docs/PACKAGING.md`: source inputs
  `6e0707f41f1a22be8876b4fdc1f1593f27063b09e1badc29be49df28366dc063`,
  wheel `5d2dd0ceb978665fc4d89fb90d7ace35c00b98dd920267ab7cfe1d6bcb8b1b2f`
  (106,628 bytes), and sdist
  `1b2630a8adb11082954038e81ea5189c619422e7a14dc14f7405be14db7581c0`
  (94,998 bytes). Build and profile evidence use fresh temporary paths;
  retained `dist/` and `packaging-evidence/` records remained unchanged and
  are not claimed as S2 evidence.

## Executed evidence — 2026-08-08 (registry-derived agent brief, S3 pre-rebase)

- `ldf agent-brief` now builds one immutable Markdown/JSON snapshot by
  iterating `CAPABILITY_SPECS` in registry order and joining exactly one live
  `EngineRegistry.capabilities()` result. Every `implemented=True` command is
  present with one-line usage and live availability/reasons; every
  `implemented=False` capability is structurally excluded even if a hostile
  test result falsely marks it available.
- Both output forms contain stable exit codes, five agent gotchas, the
  `verify` -> `fallback` -> `review` workflow, and the existing absolute
  `docs/AGENT_FEEDBACK.md` path plus its append-only, required-outcome,
  write-scope, and privacy rules. JSON fields mirror the live registry, while
  Markdown renders that same typed snapshot.
- The command performs no conversion or engine execution beyond that normal
  live probe path, opens no document, writes only stdout on success, creates no
  report/output/job directory, does not consume `--password-stdin`, and
  bypasses the inherited stale-workspace sweep so its read-only claim is
  literal.
- Thirty-four focused unit/CLI/documentation outcomes passed, including
  missing/stale templates, duplicate/missing/extra registry ids, hostile
  metadata and false-implementation state, implemented-but-unavailable state,
  lazy CWD-independent feedback-path resolution, strict-offline remote-path
  avoidance, byte stability, global option composition, and no-read/no-write
  guards. Ruff over `src tests` and mypy over 33 source files also passed.
- The definitive literal-invariant verify-mode full release gate passed in 382.7 seconds on
  Windows-AMD64 CPython 3.14.4. It reproduced the tracked source/wheel/sdist
  identity, passed both ordinary and blocked-network 492-outcome suites (490
  passed plus two expected skips), Ruff, three-platform mypy, artifact drift,
  reproducible builds, Twine, sdist-to-wheel equivalence, every source/wheel
  install profile, and the clean Dev full-suite; fresh profile evidence records
  `full_tests.status: passed` and `release_manifest_verified: true`. Its fresh
  profile-evidence SHA-256 is
  `83758a01cf851eee80f7d5597ae638e0e91aff74052076a6568f8d630cbfa878`.
  Protected retained evidence and archived dist checkpoints were unchanged.

## Executed evidence — 2026-08-08 (S3 integration onto shipped S2)

- S3 rebased onto `main` `6b8f40b` after S2 merged as `6481fb7`. Shared CLI,
  test, and documentation surfaces retain both features; main's S1/S2 DONE
  ledger rows and full audit trail remain intact, while S3 remains IN-REVIEW
  for a pending independent verification.
- Review nit F1 is addressed: the registry-keyed `pdf-to-images` usage
  template now includes `[--preset llm]`, with the existing template-coverage
  test extended in place. Explicit numeric collection confirms exactly 504
  merged outcomes (502 passed and two expected skips in the pre-refresh full
  suite); the focused agent-brief/CLI/documentation set passed 35 outcomes,
  and Ruff, mypy, and generated-artifact drift checks passed.
- A fresh build-only manifest update produced combined package-source identity
  `69354c7f9bb4d092661f0b29430de563362b3abfba34b4282f548aa49455a28a`,
  wheel `a6831020d1e60321af4626b654d6739b17d616fc0eb4b4a9698d354bdca6c47a`
  (112,760 bytes), and sdist
  `3ed23059b2ff8e503ade9dfb2d416c2f95049f75007cb486f7bfe349f0f3b1e9`
  (100,287 bytes). The build used only a fresh system-temp dist; retained
  `dist/` and `packaging-evidence/` records remain historical and untouched.
- The separate definitive verify-mode full gate reproduced that identity
  without updating and passed in 331.7 seconds on Windows-AMD64 CPython 3.14.4.
  It passed locks, Ruff, Windows/Linux/macOS mypy modes, generated-artifact
  drift, ordinary and blocked-network 504-outcome suites, reproducible builds,
  Twine, sdist-to-wheel equivalence, all Base/Lite/Standard/Full source/wheel
  profiles, and the clean Dev full-suite. Fresh profile evidence records
  `full_tests.status: passed`, `release_manifest_verified: true`,
  `source_install_syntax_tested: true`, and SHA-256
  `f2299b86791e6885a2775a1b4404e9bb80c5871cd17a744ee43f3d4f1efdb26a`.
  Retained `dist/` and `packaging-evidence/` records remained untouched.

## Implemented hardening

### Worker, cancellation, and API admission

- Every API conversion uses a fresh multiprocessing `spawn` child. The API
  process performs admission, bounded transport, basic filename/form checks,
  and result serving; it does not parse uploaded document bytes.
- Admission precedes multipart parsing. A random `.transport-*` spool is
  contained under the private API session and aggregate-bounded by the API
  upload, enabled input, and enabled temporary-byte limits. Handles/spool are
  removed before enqueue and on malformed body or disconnect.
- Queue length, global concurrency, per-client active jobs, submission rate,
  and progress history are bounded. Queued cancellation physically removes the
  entry. Async status/events/cancel endpoints coexist with the synchronous 201
  contract.
- On Windows the document gate opens only after Job Object assignment.
  Kill-on-close, memory, CPU-time, and active-process controls apply. After an
  established boundary, `verified_empty` requires Job accounting to report zero
  active processes. A pre-assignment bootstrap exit keeps the gate
  `never_opened` and reports only `pre_gate_leader_verified`; every other
  unverifiable exit fails closed.
- Cancellation, timeout, crash, malformed IPC, browser disconnect, graceful
  shutdown, and real Windows Ctrl+Break terminate/finalize without permanently
  consuming admission. The real CLI server Ctrl+Break regression exits 0 and
  removes the leased session root.
- Successful downloads hold an active lease until streaming finishes;
  DELETE/eviction cannot remove the file concurrently. Crash residue is removed
  only when a later process acquires its external OS-released session lease.
  Held, missing, or unreadable leases are preserved fail-closed.
- Passwords/document bytes are absent from process command lines. Required
  passwords cross only the private spawn channel, never return in IPC, and are
  cleared from parent state. IPC/public errors/reports are bounded and scrubbed.

### Windows path and privacy boundary

- Strict mode distinguishes local extended paths (`\\?\C:\...`) from extended
  UNC, rejects UNC/mapped roots before metadata access, and requires a confirmed
  local Windows drive.
- Device namespaces, ADS, reserved device components (including extensions),
  trailing-dot/space aliases, and existing reparse ancestors are rejected at
  configuration, input, containment, publication, and download boundaries.
- Real Windows regressions passed for local/UNC/extended/device/ADS/reserved/
  trailing forms, hard links, junctions, case aliases, 8.3 aliases, and paths
  longer than 260 characters. No mapped drive was mounted, so that API path has
  mocked evidence only. Symbolic-link creation privilege was unavailable;
  junction evidence is not relabelled as symlink evidence.
- Application strict-offline and Python socket guards passed, but they are not
  an OS firewall. `scripts/run_windows_firewall_gate.ps1` is an opt-in,
  exact-executable rule/probe with fail-closed cleanup; it was not executed
  because firewall mutation needs explicit approval/elevation. It also reports
  DNS proof incomplete because Windows DNS Client can mediate `getaddrinfo`.

### Packaging, licensing, and advisories

- Default/Lite, Standard, Full, and Dev are real dependency sets with
  marker-aware SHA-256 locks. Full means shipped Python adapters, not roadmap
  features.
- PowerShell bootstrap now makes native-command failures terminating. The PEP
  517 backend is `setuptools==83.0.0`, authenticated against official PyPI
  hashes, downloaded into a one-use wheelhouse, and installed by isolated builds
  with the wheelhouse forced offline; build isolation was not weakened.
- Profile-specific CycloneDX 1.6 SBOMs/notices remain deterministic: 29 runtime
  Python components, 16 versioned bundled-native components, and 18 separately
  enumerated version-unknown children.
- The base advisory/license review date is 2026-07-19. A 2026-07-20 OSV refresh
  returned no matches for 29 exact PyPI queries and one match among 16 versioned
  native queries: OpenJPEG 2.5.4 / `OSV-2025-219`. Empty results are not safety
  guarantees.

## Release posture and remaining blockers

**FAIL / NOT CLEARED for a sensitive-document release.** Engineering blockers
for profiles, reproducible builds, Windows worker containment, cancellation,
queueing, transport containment, and the two executed Windows Python versions
are closed. Release remains blocked by:

1. affected bundled OpenJPEG 2.5.4 (and Pillow aggregates that contain it),
   and — since 2026-08-08 — affected bundled libheif 1.23.0 (OSS-Fuzz
   OSV-2020-2308 and OSV-2023-1129, no fixed release enumerated), which
   untrusted HEIC inputs reach by design of the HEIF input feature;
2. advisory-unknown PDFium 152.0.7947.0 and 18 unversioned native children;
3. no complete Windows OS-enforced outbound-plus-DNS denial result;
4. partially closed on 2026-08-03 by the first executed CI runs (see the
   dated evidence section): the Ubuntu runner passed the complete release
   gate, and the install/full-test matrix passed on ubuntu/windows/macos
   runners across CPython 3.12–3.14 with retained evidence artifacts. Still
   open: no native-runner *release gate* on macOS or Windows CI (the matrix
   runs the profile/full-test verification, not the build gate), and no
   local Windows 3.12 hardening run on the primary workstation;
5. no real mounted mapped-drive regression and no symbolic-link privilege on
   this host.

CLI parsing remains in-process and cooperative. API workers retain the user's
filesystem authority and are not AppContainers, restricted tokens, filesystem
sandboxes, or native network sandboxes. POSIX containment code exists but was
not executed here.

Phase 2 began on 2026-08-03 with the lossless compression slice: pipeline,
CLI, API, tests, documentation, and sampled render-identity verification
against the source landed together, and the capability is engine-gated like
every other. The 2026-08-08 convert-images slice followed the same golden
path: iPhone HEIC/HEIF input (decode-only via pi-heif) for image conversion
and images-to-pdf, with an `llm` preset producing AI-assistant-ready JPEGs
and privacy-default GPS/EXIF stripping. Lossy compression presets, repair,
OCR, Office/HTML/Markdown conversion, PDF/A/PDF/UA, editing/forms,
protection, secure redaction, signatures, compare, scanner/camera
acquisition, and the React UI remain unavailable.

## Stable interface decisions

- Engines: pikepdf for structural operations, PDFium for rendering, Pillow for
  imaging, and optional executables only through the allowlisted runner.
- `last-5` means the final five pages.
- Split naming is `stem-page-NNN.pdf`, `stem-part-NNN.pdf`, or
  `stem-pages-token.pdf`.
- Reports are human-readable by default and optionally JSON; they omit document
  text and passwords. Public API serialization also omits private server paths.
- Exit codes remain `0/1/2/3/4/5/130` as documented in `docs/CLI.md`.
- `compress` means lossless structural optimization until lossy presets exist;
  presets that would degrade images are refused, never silently approximated,
  and a candidate that renders differently from its source is never published.
