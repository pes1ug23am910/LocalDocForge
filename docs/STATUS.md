# STATUS — LocalDocForge

Last updated: 2026-08-03 (first Phase 2 slice — lossless compression — on the
Windows release-hardening baseline).

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
SBOM/notices drift, the complete collected test suite (407 tests as of
2026-08-03) normally and with Python DNS/non-loopback sockets denied,
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
- Canonical package identity (2026-08-03): source
  `32b645769366b28516860eb19c3e10859e7549dd94a03bf7b4a0dd83f634a4b8`;
  wheel `540a36212cc2adbf21cb321ad0a684b806a26ef8fb7ddaae942d2fff0dccbff3`
  (96,696 bytes); sdist
  `7bd03dfbe3a817da05a26f490ededb67f8cd9482bfc190e7076be3222ca6ea91`
  (83,262 bytes). `packaging-evidence/windows-11-x64-SHA256SUMS.txt` was
  regenerated from the retained files and matches the manifest.
- Live compression smokes: a deliberately bloated 5-page PDF compressed with
  pixel-identical sampled renders; `outline-6page.pdf` 5,777 → 2,953 bytes
  (48.9 % smaller) under `--strict-offline` with outlines preserved;
  `mixed-sizes.pdf` 2,992 → 1,847 bytes (38.27 %) over the `--json` contract.
- Windows CPython 3.13.5 was **not** re-run after the compression slice; its
  retained evidence (`windows-3.13.5.json`) still describes the 2026-07-20
  package identity. The platform scope remains Windows 11 x64 only.
- Known flake, recorded honestly: in one full-suite run,
  `test_live_uvicorn_sync_disconnect_cancels_and_finalizes_job` failed on a
  marker-file comparison, then passed 3/3 in isolation and in both complete
  gate suite runs. Tracked as a timing-sensitivity issue in the live-server
  test, not a shipped-code defect; it deserves hardening.

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

1. affected bundled OpenJPEG 2.5.4 (and Pillow aggregates that contain it);
2. advisory-unknown PDFium 152.0.7947.0 and 18 unversioned native children;
3. no complete Windows OS-enforced outbound-plus-DNS denial result;
4. no executed Linux or macOS release matrix, and no Windows CPython 3.12 run;
5. no real mounted mapped-drive regression and no symbolic-link privilege on
   this host.

CLI parsing remains in-process and cooperative. API workers retain the user's
filesystem authority and are not AppContainers, restricted tokens, filesystem
sandboxes, or native network sandboxes. POSIX containment code exists but was
not executed here.

Phase 2 began on 2026-08-03 with the lossless compression slice: pipeline,
CLI, API, tests, documentation, and sampled render-identity verification
against the source landed together, and the capability is engine-gated like
every other. Lossy compression presets, repair, OCR, Office/HTML/Markdown
conversion, PDF/A/PDF/UA, editing/forms, protection, secure redaction,
signatures, compare, scanner/camera acquisition, and the React UI remain
unavailable.

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
