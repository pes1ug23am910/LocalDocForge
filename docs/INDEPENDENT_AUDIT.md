# LocalDocForge independent audit

Audit date: 2026-07-19  
Audited checkpoint: `main` at
`0966b924da41d10ec36798af77cb97e8c2b2db54`  
Starting tree: clean  
Audit scope: this repository, processes started from it on localhost, and
synthetic fixtures only  
Network use: none

## Executive summary

The starting checkpoint was a coherent early-alpha implementation, not an
empty scaffold: its 208 collected tests produced 207 passes and one expected
Windows skip. Privacy and localhost controls were tested before feature
completeness, as required.

The audit reproduced defects in strict-offline precedence, source/output
aliasing, executable discovery, API retention and request limits, atomic
publication, Unicode/job-path handling, encrypted and signed PDF disclosure,
malformed-PDF handling, page-reference integrity, image alpha/quality handling,
capability claims, API parameter parity, and release licensing evidence. The
confirmed product defects were fixed directly and receive focused regression
coverage. No confirmed critical defect remains, and no directly reproducible
high-severity product defect was knowingly left unfixed.

The final local gate collected 266 tests: **265 passed and one POSIX-only
permission test skipped on Windows**. Ruff, `git diff --check`, `pip check`, the
release-artifact drift check, and the full suite with DNS and non-loopback
Python sockets denied all passed. Ten representative output PDFs (24 pages)
were reopened, syntax-checked, fully rendered, and visually inspected without a
blank, corrupt, or transparency-damaged result.

**Release decision: FAIL for a sensitive-document release.** The remediated
implemented subset is suitable for continued local development and controlled
synthetic testing, but release evidence is incomplete: authoritative advisory
and upstream-license review was not authorized, a clean package build/install
could not be completed offline, dependency locks lack hashes/platform markers,
Lite/Standard/Full profiles do not exist, and hostile documents are still
parsed by native libraries in the long-lived application process.

## Environment and checkpoint

| Item | Evidence |
|---|---|
| Repository | `E:\Sem-VI-Break\Pdf-Conversion-Tool` |
| Branch / starting commit | `main` / `0966b924da41d10ec36798af77cb97e8c2b2db54` |
| Starting Git state | clean; only the audit edits described here are now present |
| OS | Microsoft Windows 11 Home Single Language, `10.0.26200`, x64 |
| Shell | PowerShell `7.6.3` |
| Python | CPython `3.14.4`, `.venv\Scripts\python.exe` |
| pip | `26.1.2` |
| Node / npm | `24.15.0` / `11.12.1` |
| Git | `2.55.0.windows.3` |
| .NET | `Microsoft.NETCore.App 8.0.21` runtime; no .NET SDK installed |
| Core engines | pikepdf `10.10.0` with qpdf `12.3.2`; pypdf `6.14.2`; pypdfium2 `5.12.1`; Pillow `12.3.0` |
| Optional probes | Typst `0.15.1` present but no implemented capability; qpdf CLI, Tesseract, OCRmyPDF, Ghostscript, LibreOffice, Pandoc, and veraPDF unavailable |

The pypdf package is intentionally no longer selectable as a production
fallback: current operation code is pikepdf-specific. Its successful import is
diagnostic evidence only.

## Tests performed

### Baseline and privacy boundary

- Recorded Git status, branch, history, exact runtime versions, engine probes,
  and the starting commit before editing.
- Ran `pytest tests -q`: 207 passed, one POSIX-permission check skipped.
- Ran Ruff before editing: clean.
- Ran the complete suite with Python DNS and every non-loopback socket denied.
- Started the real `ldf web` service on ephemeral port `38477`; verified its
  listener was only `127.0.0.1`, HTTP health returned 200 with the token, CSP
  and hardening headers were present, and the process stopped cleanly.
- Processed a synthetic PDF containing an external URI while socket and DNS
  primitives raised on use; no primitive was called.
- Inspected source and browser markup for outbound clients, telemetry, update
  checks, CDNs, remote fonts/assets, browser storage, and camera/scanner APIs.
  None is shipped.
- Tested token authentication, cookie-only CSRF rejection, no CORS grant, Host
  validation, strict/non-strict UI wording, nonlocal opt-in, strict-mode
  override, request/body/file/field bounds, job-id isolation, output-index
  containment, path scrubbing, cleanup failure, and generic 500 responses.

### Command, filesystem, and sensitive-data checks

- Searched for shell invocation, dynamic command construction, subprocess
  entry points, traversal, unsafe temporary paths, and collision handling.
- Used only a local marker file to verify that a planted current-directory
  `qpdf` is not discovered; it was never executed.
- Verified bounded child-output retention, pipe draining, timeout/base-exception
  process-tree cleanup, PATH override refusal, and absolute/local working-dir
  policy.
- Tested exact-path and hard-link output aliases under overwrite and verified
  source SHA-256 values were unchanged.
- Tested an atomic fail-policy collision introduced at publication time and a
  pre-existing second output in a multi-output job; the competing/pre-existing
  file survived and no earlier output remained.
- Tested traversal-shaped/empty/non-ASCII job ids, unsafe temp suffixes,
  exclusive workspace creation, an empty output-root allowlist, long astral and
  bidirectional Unicode filenames, Windows reserved device names, and strict
  UNC/default-temp/output rejection without contacting a share.
- Verified reports contain neither the synthetic password nor fixture text;
  public API reports and errors contain no server-private path.

### PDF integrity and fidelity

- Reopened and rendered every structurally openable repository PDF fixture.
  Twenty-four source pages rendered. The deliberate `fake.pdf` and
  `garbage.pdf` fixtures failed structural open and an explicit PDFium page-1
  render attempt as expected. `bad-xref.pdf`
  rendered but produced three qpdf syntax warnings and is now refused by all
  implemented PDF input paths rather than silently repaired.
- Generated representative merge, split, extract, organize, rotate, crop,
  encrypted-input rotate, images-to-PDF, and PDF-to-images results. Ten output
  PDFs (24 pages) passed signature, reopen, syntax, expected-page-count, and
  full PDFium render checks. Exported images decoded.
- Visually inspected contact sheets of all rendered source and output pages and
  the alpha test page. Rotations, crop visibility, order, page dimensions, and
  the opaque white/blue alpha result were consistent with the synthetic
  markers; no unexpected blank or corrupt page was observed.
- Verified source hashes were unchanged after every visual-audit operation.
- Built synthetic PDFs with outlines, internal links, forms, duplicate field
  names, attachments, XMP, page labels, JavaScript/open actions, and a
  signature-field structure. Page-moving loss warnings were checked; unsafe
  remove-pages was refused; signature invalidation is critical.
- Tested encrypted PDFs after password opening against page limits. Generated
  PDFs/images are intentionally unencrypted and now carry the critical
  `input-encryption-removed` warning.
- Verified transparent image compositing, impossible-margin refusal, WebP
  quality differences, render-pixel/decompressed/output bounds, malformed
  input refusal, crop-is-not-redaction warnings, and all-page validation for
  the high-risk synthetic outputs.
- Verified duplicate split/PDF-to-image selections receive deterministic
  unique names, long derived stems are bounded, and routine render sampling
  includes both the first and final page.

### Unavailable feature claims

Compression, repair, OCR, Office/HTML/Markdown conversion, PDF/A/PDF/UA,
interactive forms/editing, protection, secure redaction, cryptographic signing,
comparison, scanner/camera acquisition, and the full browser UI are not
implemented. Registry, API, UI, and documentation gating was checked instead
of treating a model field or installed executable as a working feature. Crop
content remained recoverable and is explicitly not described as redaction.

### Dependency, licensing, packaging, and documentation

- Added and verified the MIT `LICENSE`, `THIRD_PARTY_NOTICES.md`, licensing
  documentation, and a deterministic CycloneDX 1.6 SBOM.
- The offline generator inventories 42 exact Python distributions and 15
  bundled native components. Two generations were byte-identical; `--check`
  passed. Recorded hashes were:
  - notices: `dbeaaf6e34fece7aa815f10c10c5aef769617bf050dc2bc17ec6a583d1a4f501`
  - SBOM: `8bef279b038ed1f5e0cc2126ad2cdf72d63bdd85993ce89ed020242f5cedf9d0`
- `pip check` passed. Bootstrap PowerShell and shell scripts parsed without
  syntax errors. README `ldf doctor` and `ldf --json doctor` commands ran.
- Audited README, status, threat model, architecture, CLI/API reference,
  engine decisions, fidelity notes, and feature matrix against runtime probes
  and tests; stale API, validation, resource, privacy, fallback, and cleanup
  claims were corrected.
- Attempted an offline wheel build with
  `pip wheel . --no-deps --no-build-isolation`. It stopped at
  `BackendUnavailable: Cannot import 'setuptools.build_meta'`: neither the
  Python 3.14 environment nor its venv contains setuptools, the only cached
  wheel is unrelated (`pyserini`), and the locally installed Python 3.10
  setuptools is `65.5.0`, below the declared `setuptools>=75` build
  requirement. No dependency was downloaded.

## Findings and remediation

### Critical

No critical defect was confirmed.

### High — fixed

| ID | Reproduction/evidence at start | Fix | Regression evidence |
|---|---|---|---|
| H-01 strict-offline precedence | `LDF_STRICT_OFFLINE=true` was overwritten by a false CLI default; strict mode allowed explicit non-loopback serving and did not reject every effective network job path | Preserve environment state, report strict state, refuse recognized remote input/output/report/job/external-tool paths (including effective default temp), and make strict mode override `--allow-nonlocal` | `test_privacy_boundary.py`, `test_filesystem_regressions.py`, CLI strict-bind test, socket/DNS-denied full suite |
| H-02 source overwrite | Setting an output equal to an input, or to its hard link, with overwrite modified the source | Canonicalize destinations once and reject equality/`samefile` aliases before publication | exact-path and hard-link SHA-256 regressions |
| H-03 executable hijack/orphaning | Windows executable discovery could select a planted current-directory binary; `KeyboardInterrupt` could orphan a process and retained output was bounded only after capture | Search only absolute local PATH directories, forbid child PATH replacement/relative or remote cwd, drain with a retained-byte cap, and terminate the tree on every base exception with checked fallback | local inert marker, real bounded Python child, interrupting fake-process regression |
| H-04 private API retention/leakage | Successful API outputs survived graceful shutdown, invalid requests left uploads, reports/errors exposed private absolute paths, and cleanup failures were ignored | Private per-session roots, success/failure/shutdown cleanup, stale-session migration/sweep, checked delete/eviction, path-scrubbed public reports/errors, generic hardened 500 | shutdown, invalid-param, public-path, delete-failure, and generic-error privacy tests |
| H-05 unbounded/ambiguous API input | Multiple files bypassed a per-file limit; multipart spooling preceded the application limit; chunk overflow could become 500; invalid/unknown/duplicate parameters could be ignored or mishandled | Aggregate file limits, request receive cap plus bounded multipart overhead/counts/fields, 413 unwrapping, explicit per-operation parameter allowlists and range checks | cumulative, chunked, field/error, unknown-param, image-parity, and output-limit tests |
| H-06 silent protection/signature loss | Password-protected inputs produced unencrypted outputs without disclosure; rotate/crop rewrites invalidated signature semantics without a warning | Critical `input-encryption-removed` and `signature-invalidated` security warnings; passwords remain absent from reports | encrypted rotate/PDF-to-image and synthetic signature-field regressions |
| H-07 PDF reference/syntax integrity | Remove-pages could leave stale outlines/internal links/forms; page movers silently omitted more document-level features; malformed xrefs were accepted or repaired on some paths | Conservatively refuse remove-pages when references cannot be safely rewritten, enumerate stable loss warnings, reject any parser syntax warning before conversion/output publication | rich-feature/remove refusal and bad-xref validation/organize/PDF-to-image regressions |

### Medium/low — fixed

| ID | Defect | Remediation and proof |
|---|---|---|
| M-01 collision/publication races | Fail/rename publication now uses atomic no-clobber linking; every candidate validates before publication; handled multi-output failure rolls back; overwrite backups use bounded private names. Race, blocker, and collision suites pass. |
| M-02 workspace/path robustness | Job ids and suffixes are constrained, workspace creation is exclusive, existing POSIX roots are not chmod-mutated, new private directories use 0700 where applicable, cleanup returns a checked result, and an empty output allowlist denies all. |
| M-03 resource enforcement | Input/output totals are aggregate, encrypted PDFs are page-counted after opening, image page/pixel/decompressed/output limits are incremental where possible, and cooperative timeouts run at operation checkpoints. |
| M-04 image fidelity | Alpha is composited onto the requested background even for image-sized pages, impossible margins and oversized canvases are refused, temporary PIL objects are closed, and WebP uses the requested quality. Pixel and byte-difference tests pass. |
| M-05 capability/UI truthfulness | The unwired pypdf fallback was removed, machine-specific Typst wording was removed, doctor reports strict state, UI privacy text is conditional, interface coverage is qualified, and unknown/unavailable capabilities stay disabled. |
| M-06 licensing evidence | Added MIT license metadata, deterministic notices/SBOM generation, native wheel components, external-engine caveats, and four drift/shape/license tests. |
| M-07 derived naming/render sample edges | Duplicate page selections no longer collide, source-derived stems are bounded before adding suffixes, and routine validation samples both document endpoints. Real split/image outputs and a recorded six-page render sample pass. |
| L-01 Unicode edge cases | Format/control/surrogate characters, UTF-8 and code-point bounds, suffix retention, and additional Windows device names are covered. Long astral/bidi API paths no longer fail with a server error. |

## Open release blockers and residual risks

### High release-process blockers

1. **Authoritative advisory and upstream-license review not performed.** The
   audit requested permission before network access but received no approval.
   The SBOM is local evidence, not a vulnerability verdict. Every exact Python
   and native component remains advisory-unverified as of this audit.
2. **Clean build/install matrix not demonstrated.** The offline wheel build
   failed for the exact missing build backend evidence above. There are no
   clean Python 3.12/3.13/3.14 installs or macOS/Linux runs, no artifact hashes
   or platform markers in the lock, and no Lite/Standard/Full dependency
   profiles, locks, tests, or profile SBOMs.

### Technical residual risks

- pikepdf/libqpdf, PDFium, and Pillow parse hostile bytes in the API/CLI
  process with the user's privileges. Cooperative timeouts cannot interrupt a
  stuck native call, and no hard memory/CPU quota exists.
- API jobs are synchronous and have no worker isolation, rate limit,
  concurrency quota, background cancellation, or progress channel. This is a
  significant reason not to expose nonlocal mode to sensitive documents.
- The output byte cap prevents publication after candidate generation; it is
  not a workspace disk quota. A crash can retain an API session until the
  best-effort 24-hour sweep. Deletion is not secure erasure.
- Each output publication is atomic, but a multi-output job is not a
  transaction across power loss, process termination, filesystem failure, or
  hostile concurrent changes.
- Routine long PDFs render an evenly distributed sample of at most 20 pages.
  Rendering and syntax checks do not prove semantic equivalence, font
  correctness, reading order, accessibility, PDF/A/PDF/UA conformance, or
  cryptographic validity.
- Page-moving operations still lose documented document-level structures and
  warn. Rotate/crop retain active content; they do not sanitize a PDF.
- Strict-offline recognizes UNC/device paths and Windows mapped drives, but it
  cannot identify an ordinary-looking POSIX network mount or replace an OS
  firewall/container network namespace.
- Nonloopback mode remains available outside strict mode, uses plaintext HTTP,
  and is explicitly not recommended for sensitive documents.

## License findings

The repository now declares MIT and contains the standard license text.
`THIRD_PARTY_NOTICES.md` separates 29 runtime, nine development, and four
lock-only Python distributions and discloses 15 nested native components,
including qpdf, PDFium, and Pillow codecs. Optional external executables are
not bundled and their displayed license identities are not treated as binary
provenance.

The generated SBOM is CycloneDX 1.6 and specific to the inspected
Windows/Python 3.14 wheels. It deliberately does not guess versions for
unversioned pikepdf libjpeg/MSVC evidence. Redistributors must preserve wheel
license files and repeat inventory/advisory review for the exact artifacts they
ship. Ghostscript and veraPDF licensing choices require authoritative review
before enablement or redistribution.

## Final decision and checkpoint

**FAIL — do not designate this checkpoint as a sensitive-document release.**

The implementation is materially safer and its available-capability claims are
now evidence-backed, but the two high release-process blockers and in-process
parser architecture prevent a pass. The nonlocal web mode should not be used
for sensitive documents.

To reach a release candidate:

1. With explicit network authorization, check every SBOM component and exact
   optional binary against authoritative advisories and upstream license
   sources; record dates, sources, affected versions, and dispositions.
2. Produce wheel/sdist artifacts in a clean build environment; clean-install
   and run the suite/doctor on supported Python and OS combinations.
3. Define actual Lite/Standard/Full dependency profiles with hashed,
   platform-aware locks, profile installation tests, and profile SBOMs—or
   remove those profile expectations from the release plan.
4. Add worker-process isolation, hard resource termination, and API
   concurrency/rate controls before treating hostile-document web processing
   as hardened.
5. Re-run the blocked-network suite, full render audit, license generator, and
   release gate; only then revisit the sensitive-document decision.

No audit commit was created. The working tree is a coherent, reviewable patch
on the recorded starting commit. Final local gates passed, generated visual
and package-attempt intermediates were removed, and one verified synthetic
legacy API residue (`a6fa66ab7d9a439a931f7e08f1fc9642`, five fixture-marker
pages, SHA-256
`a1371505b2f898662b9d22e2ef8d7e0589437fcf95888a4710cbe523f9191883`) was
removed by ordinary filesystem deletion after containment verification.

## Release-hardening re-audit — 2026-07-20

This section is an append-only re-audit of the release-hardening work. The
historical audit above remains the evidence for the earlier checkpoint and has
not been rewritten to make its then-current findings appear newer.

### Scope, starting state, and evidence boundary

- The run started on branch `main` at
  `7e49624c4ad646f3998c4a204a4e8049c8748205`, tagged
  `audit-checkpoint-2026-07-19`.
- The starting working tree was already materially dirty. No claim of a clean
  checkout, clean-room source tree, or audit commit is made, and unrelated
  pre-existing work was preserved.
- Windows 11 x64 is the primary and first verified release platform. The
  evidence below was executed on Windows 11 build `10.0.26200`, AMD64. It does
  not establish macOS or Linux support.
- Phase 2 document features were explicitly out of scope and were not started.

The first all-default release-gate invocation reached the clean profile smoke
test and failed because `scripts/profile_smoke.py` read a nonexistent `pages`
field instead of the inventory contract's `page_count` field. Lock checks,
Ruff, mypy, diff checks, `pip check`, generated-artifact checks, the ordinary
and blocked-network test runs, repeated package builds, Twine checks,
reproducibility checks, and the sdist-to-wheel check had passed before that
failure. The field access was corrected and the affected profile/build checks
were rerun successfully.

### Executed local evidence

| Area | Evidence executed on 2026-07-20 | Result |
| --- | --- | --- |
| Local test gate | Lock validation, Ruff, mypy over 31 source files, diff checks, `pip check`, generated-artifact checks, ordinary tests, and blocked-network tests | PASS; 390 tests collected, with 388 passed and two expected platform skips in each test run |
| Worker/API containment | Real spawned-worker cancellation, client disconnect, timeout, leader-orphan, resource-limit, shutdown, Ctrl+Break, parent-death/restart-residue, API queue/rate/concurrency, spool cleanup, lease, and active-download race tests | PASS, with one explicitly platform-inapplicable test skipped |
| Windows path containment | Extended local paths, extended UNC rejection, device/ADS/reserved-name/trailing-dot-space rejection, reparse/junction ancestry, case variants, 8.3 aliases, long paths, and hard-link behavior | PASS |
| Firewall harness contract | Six non-mutating contract tests for the opt-in PowerShell harness | PASS; the administrator firewall experiment itself was not executed |
| Canonical package build | Hash-locked isolated PEP 517 wheel and sdist build, Twine checks, source-install and reproducibility checks | PASS |
| Python 3.13 matrix | Clean uv-managed CPython 3.13.5 Lite/Standard/Full installs, source install, full tests, doctor, profile checks, manifest and checksum checks | PASS |
| Python 3.14 matrix | Clean CPython 3.14.4 Lite/Standard/Full installs, source install, full tests, doctor, profile checks, manifest and checksum checks | PASS |
| Strict-offline PDF exercise | Synthetic crop, images-to-PDF, merge, and rotate outputs; pikepdf reopen; complete PDFium rendering; visual contact-sheet inspection | PASS; four PDFs and all 16 pages inspected |
| Advisory refresh | Exact-version OSV batch review plus focused authoritative source review | Completed; release blocker found |

The canonical Windows artifacts are:

- wheel: `localdocforge-0.1.0-py3-none-any.whl`, 92,228 bytes,
  SHA-256
  `392d0952313d89ee817139c90b388a79aa2d1c655126ee0876285e59f08176b2`;
- sdist: `localdocforge-0.1.0.tar.gz`, 80,014 bytes, SHA-256
  `4675fa04055353032a0ab55bb373f3d74068b46dad1b86bba82d7bd124582fef`;
- recorded source digest:
  `c39c91b599d1d8b050599a2e62bceaf5339818e613e56c6a4997be1021532f70`.

The signed-off local evidence files are
`packaging-evidence/windows-3.13.5.json`,
`packaging-evidence/windows-3.14.4.json`,
`packaging-evidence/windows-11-x64-SHA256SUMS.txt`, and
`packaging-evidence/windows-pdf-render-2026-07-20.json`.

### Worker, cancellation, and API findings

The earlier in-process parser finding is materially addressed for document
jobs. Each admitted job now uses a spawned worker process and a bounded IPC
channel. On Windows, the parent places that worker in a kill-on-close Job
Object before opening the document-processing gate. Cancellation, client
disconnect, wall-clock expiry, API shutdown, and resource-limit failures use
hard process-tree termination. If Job assignment or equivalent process-tree
containment cannot be established, document processing never opens.

Containment proof is fail-closed: a successful result requires verified-empty
Job accounting after Windows termination. A leader that exits before Job
assignment is recorded as a pre-gate leader exit rather than as proof that a
descendant tree was contained. Tests exercised an actual grandchild process,
leader-orphan behavior, assignment failure, worker-parent death, restart
cleanup, Ctrl+Break shutdown, output and temporary-byte limits, and active
download/deletion races.

The API now reserves a bounded admission slot before request-body spooling,
spools multipart bodies only into a request-scoped `.transport-*` directory,
and enforces the smallest applicable upload, enabled-input, and enabled-temp
budget. Rate, concurrency, and queue limits reject excess work before document
processing. External OS session leases prevent live sessions from being
treated as stale, and active-download leases prevent DELETE or eviction from
racing a response stream.

The POSIX process-group and resource-limit implementation was code-reviewed
and has platform-gated tests, but it was not executed on Linux or macOS in this
run. It is therefore not release evidence for those platforms.

### Windows filesystem matrix

| Case | Exercise | Evidence status |
| --- | --- | --- |
| Normal local drive and `\\?\\C:\\...` extended local syntax | Real filesystem tests | PASS |
| UNC, extended UNC, device namespaces, ADS, reserved device names, trailing dots/spaces | Real validation tests | PASS: rejected before processing |
| Existing junction/reparse ancestry and symlink-style escape handling | Real junction tests plus reparse-point validation | PASS for junctions; symbolic-link creation privilege was unavailable, so no real symlink case was claimed |
| Hard links | Real filesystem test | PASS |
| Case variants, 8.3 alias handling, and long local paths | Real filesystem tests | PASS |
| Mapped network drive | Drive-type branch tested with a controlled API result | No mapped drive was mounted; not claimed as a live mapped-drive test |

Strict-offline mode now requires a confirmed local Windows drive and rejects
unsupported namespace syntax or existing reparse ancestry before document
bytes are opened. This is path containment evidence, not proof of host-wide
network isolation.

### Windows firewall experiment

`scripts/run_windows_firewall_gate.ps1` is an explicit, administrator-only,
opt-in harness. Its contract verifies Windows 11 x64, PowerShell 7,
administrator membership, the Windows Firewall service and active profiles;
resolves one Python executable; installs a uniquely named outbound block rule
scoped to that exact executable; checks the ActiveStore application filter;
probes loopback and a self-hosted nonloopback endpoint; and removes and verifies
absence of the rule in `finally`.

No firewall rule was created or removed during this audit because explicit
approval for the elevated firewall mutation was not provided. More
importantly, the harness deliberately returns an incomplete result even after
the socket probes because an executable-scoped rule cannot prove that Windows
DNS Client-mediated `getaddrinfo` traffic is denied. Consequently the Windows
strict-offline network gate is not complete.

### Authorized dependency and advisory review

Only public package/component names and exact public versions were sent to
public services. No file name, file content, document hash, private path,
environment value, hostname, or user data was submitted.

| Source and access date | Exact-version coverage | Conclusion | Disposition |
| --- | --- | --- | --- |
| OSV batch API, 2026-07-20 | 29 Python distributions | Zero matches | No OSV match at query time; not a permanent no-vulnerability claim |
| OSV batch API, 2026-07-20 | 16 versioned native components | One match: OpenJPEG 2.5.4 / `OSV-2025-219` | Affected; release blocker |
| OSV vulnerability record, 2026-07-20 | OpenJPEG 2.5.4 | Version is in the affected range; fixed version is 2.5.4-1 in the tracked ecosystem record | Affected pending an upstream artifact that demonstrably contains the fix |
| Pillow 12.3.0 tagged Windows build source, 2026-07-20 | Bundled OpenJPEG configuration | Pillow's Windows build pins OpenJPEG 2.5.4 | Containing component remains affected for this review |
| PyPI JSON, 2026-07-20 | setuptools 83.0.0 build backend | Official wheel hash matches the new build-backend lock | Build input verified and hash-locked |

The complete 45 versioned queries, 18 unversioned native records, evidence
dates, source references, licenses, and dispositions are recorded in
`docs/ADVISORY_REPORT.json` and the regenerated SBOM/notice artifacts.
Unversioned or unprovenanced PDFium native children remain `unknown`; absence
of a version-specific match is not presented as a clean bill of health.

After this re-audit text was aligned, the all-default release gate was run once
more on CPython 3.14.4 and exited successfully in 299 seconds. It repeated
locks, Ruff, mypy, diff hygiene, `pip check`, generated-artifact checks, the
ordinary and blocked-network 390-test runs, reproducible isolated builds,
sdist-to-wheel equivalence, and the complete clean profile matrix. The
supplemental record is
`packaging-evidence/windows-3.14.4-final-gate.json`; the canonical
checksum-authenticated matrix remains
`packaging-evidence/windows-3.14.4.json`.

### Re-audit decision

**FAIL — do not designate this tree as a sensitive-document release.**

Worker isolation, Job Object containment, hard cancellation, admission-before-
spooling queue bounds, Windows path validation, hash-locked isolated builds,
two clean Python profile matrices, and a full Windows render exercise are now
backed by executed evidence. They resolve several of the earlier structural
findings, but they do not override the following release blockers and evidence
gaps:

1. OpenJPEG 2.5.4 is affected by `OSV-2025-219`, and the reviewed Pillow
   12.3.0 Windows build configuration pins that version.
2. PDFium provenance and some bundled native child versions remain unknown.
3. The administrator firewall experiment was not executed, and its exact-
   executable rule cannot establish denial of Windows DNS Client-mediated
   resolution.
4. Windows Python 3.12, a live mapped-drive case, and a privilege-enabled real
   symlink case were not exercised.
5. No macOS or Linux build, test, packaging, filesystem, cancellation, or
   render evidence was produced. No support claim for those platforms follows
   from this Windows run.

Phase 2 document features remain unstarted. The next release decision must be
based on a fixed and re-inventoried OpenJPEG-containing artifact, closed
PDFium/native provenance, a defensible Windows network-isolation result, and
the explicitly claimed platform matrix—not on extrapolation from the Windows
11 x64 results above.
