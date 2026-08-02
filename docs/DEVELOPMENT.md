# Developer Guide

How to set up, navigate, extend, and verify this codebase without violating
its central rule: **nothing is claimed that was not executed.** Written
2026-08-03, immediately after the lossless-compression slice was added using
exactly the workflow below.

## 1. Environment setup

```powershell
# One command: creates .venv (dev profile), installs hash-locked deps,
# installs LocalDocForge editable, runs tests + ruff + mypy + doctor.
pwsh -File scripts\bootstrap.ps1                 # dev is the default profile
pwsh -File scripts\bootstrap.ps1 -Profile standard   # or lite / full
```

What that does (and what to know when doing it by hand):

- Picks CPython 3.14/3.13/3.12 via the `py` launcher (3.14 preferred).
- Installs dependencies from the matching **hash lock**
  (`requirements\locks\dev.txt`) — never from loose ranges.
- Installs the package with `pip install -e ".[dev]" --no-deps`: **editable**,
  so source edits take effect without reinstalling, and `--no-deps` so pip
  cannot silently re-resolve anything past the lock.
- Native-command failures are terminating (`$PSNativeCommandUseErrorActionPreference`),
  so the success banner cannot print over a failed step.

The dev profile adds pytest, ReportLab (fixture generation), Ruff, mypy,
HTTPX, build, and Twine on top of the full runtime set.

## 2. Repository map

| Path | Responsibility |
|---|---|
| `src/localdocforge/domain/` | Typed models (`ConversionReport`, `ResourceLimits`, warnings) and the page-range grammar. No I/O. |
| `src/localdocforge/security/` | Magic-byte sniffing, Windows-aware path containment, filename sanitization, hardened subprocess runner (executable allowlist). |
| `src/localdocforge/jobs/` | Per-job private workspaces, atomic publish, collision policies, stale-workspace sweep. |
| `src/localdocforge/engines/` | Engine adapters + live probes (`adapters.py`), registry and honest capability gating (`registry.py`). `CAPABILITY_SPECS` is the single source of what the product claims. |
| `src/localdocforge/pipelines/runner.py` | The one job lifecycle every operation runs through: sniff → limits → workspace → execute → validate all → publish atomically → report → cleanup. |
| `src/localdocforge/operations/` | The operations themselves (`organize.py` structural, `optimize.py` compression, `images.py` image conversion). Each builds an `execute()` closure and hands it to the runner. |
| `src/localdocforge/validation/` | Structural reopen (pikepdf), syntax rejection, PDFium render checks. |
| `src/localdocforge/reporting/` | JSON + human report writers. |
| `src/localdocforge/cli/main.py` | Typer CLI; stable exit codes 0/1/2/3/4/5/130. |
| `src/localdocforge/api/` | FastAPI app (`app.py`: admission, transport, routes) and the spawned-worker containment layer (`worker.py`). |
| `tests/` | `unit/`, `integration/`, `security/`, `packaging/` + synthetic fixture generator (`tests/fixtures/make_fixtures.py` — never third-party documents). |
| `scripts/` | Lock management, SBOM/notices generation, profile matrix, release gate, blocked-network harness. |
| `packaging-evidence/` | Executed-run records. Treat as evidence, not config. |

## 3. Quality gates and how to run them

```powershell
.venv\Scripts\python.exe -m pytest tests -q          # full suite (407 tests)
.venv\Scripts\python.exe -m pytest tests/integration/test_optimize_ops.py -q   # one file
.venv\Scripts\python.exe -m ruff check src tests scripts
.venv\Scripts\python.exe -m mypy                     # config in pyproject.toml
.venv\Scripts\python.exe scripts\lock_profiles.py --check       # lock drift (offline)
.venv\Scripts\python.exe scripts\generate_release_artifacts.py --check  # SBOM/notices drift

# Everything at once (~6 min), exactly what the release decision cites:
.venv\Scripts\python.exe scripts\release_gate.py `
  --profile-evidence packaging-evidence\windows-3.14.4.json
```

Notes that save time:

- mypy runs over `src/localdocforge` with per-module debt overrides listed in
  `pyproject.toml`; new modules get the strict baseline — don't add overrides
  to make new code pass.
- Ruff enforces the `S` (bandit) rules outside tests; subprocess use outside
  `security/subproc.py` will (rightly) get flagged.
- The full suite runs again with Python DNS/non-loopback sockets denied in
  the gate (`scripts\run_blocked_network.py`) — code that quietly reaches the
  network fails there.
- `tests/unit/test_documentation_consistency.py` asserts that README/STATUS/
  FEATURE_MATRIX/CLI docs match shipped reality. If you change what ships,
  the docs are part of the change, and this test is the reminder.

## 4. How to add a capability (the golden path)

This is the exact sequence the compression slice followed. "Flip + pipeline +
tests in the same change" is enforced by `tests/unit/test_registry.py`.

1. **Operation module** under `src/localdocforge/operations/`: a function
   that validates its parameters, builds an `execute(context, artifacts) ->
   ExecuteResult` closure, and calls `pipelines.runner.run_pipeline`.
   Candidates are written only inside `context.workspace`; destinations are
   declared, never written directly. Emit `SecurityWarning`/`FidelityWarning`
   for every loss or hazard you can detect — silence is a bug here.
2. **Operation id + engine**: add an `OP_*` constant in
   `engines/adapters.py` and include it in the `supported_operations()` of
   every engine that truly implements it (an installed binary alone never
   qualifies).
3. **Capability flip**: move/add the `CapabilitySpec` in
   `engines/registry.py` with `implemented=True` and honest `notes`.
4. **CLI**: a command in `cli/main.py` using the `_run(...)` helper (it owns
   password prompting, report emission, and exit-code mapping). Validate
   pure-argument errors in the command itself and exit `EXIT_USAGE`.
5. **API**: a `_run_<op>` runner in `api/app.py` plus entries in
   `_OPERATIONS` and `_OPERATION_PARAMS` (the allowlist of multipart form
   fields — anything else is 422). The server chooses all output paths.
6. **Tests**: integration tests for the operation (use
   `tests/fixtures/make_fixtures.py` synthetics; add a generator if needed),
   CLI contract tests (exit codes!), an API job test, and update
   `IMPLEMENTED_IDS` in `tests/unit/test_registry.py`.
7. **Docs, same change**: `FEATURE_MATRIX.md` row (status, engine, verified-by,
   limitations), `CLI.md` (command + API table + remove from the planned
   list), `CONVERSION_FIDELITY.md` (what is preserved/lost + warning codes),
   `STATUS.md`, `README.md`, and a string assertion in
   `test_documentation_consistency.py` so the claims can't silently rot.
8. **Verify**: focused tests → ruff → mypy → full suite → the complete
   release gate, plus one real end-to-end run of the new command on files you
   generated (the machine-readiness report shows the shape).

What is *not* acceptable: enabling a capability whose engine is missing,
wiring a probe-only external tool into `supported_operations()` "because it's
installed", catching validation failures to publish anyway, or writing docs
for behavior that has no test.

## 5. Standing rules (enforced, not aspirational)

- **Honest gating** — `available = implemented && probe passed`. Doctor, the
  API capability endpoint, and the status page all read the same registry.
- **Sources immutable; outputs atomic** — inputs are never opened for write;
  every output validates before an atomic publish; input/output aliasing is
  refused; collisions are explicit (`fail`/`rename`/`overwrite`).
- **Reports carry no document text and no secrets** — tested in
  `tests/security/`. Passwords cross only private channels and are cleared.
- **Refuse rather than guess** — syntax-damaged PDFs are rejected (repair is
  a future explicit operation), `remove-pages` refuses structures it cannot
  rewrite, unknown presets/params are errors, never warnings.
- **Words mean things** — crop is never called redaction; "lossless" is
  verified by pixel comparison; PDF/A will only ever be claimed after an
  authoritative validator passes.
- **Strict-offline is application policy** — path rejection plus Python
  socket guards; documentation must never call it an OS firewall.
- **Windows-first, portable-by-design** — PowerShell examples, no
  Administrator requirement, `spawn` (never `fork`), Job Objects for worker
  containment; POSIX code paths exist but are unverified until executed
  (`docs/PACKAGING.md` table is the truth).

## 6. Evidence discipline

A version marker, classifier, lock resolution, or configured CI job is a
declared contract — not proof. Executed proof lives in:

- `packaging-evidence/*.json` — gate/profile matrix runs (refreshed by
  `release_gate.py --profile-evidence …`).
- `docs/MACHINE_READINESS.md` — dated per-machine verification runs.
- `docs/STATUS.md` — the running release decision and its blockers.

When you run the gate, it rewrites the evidence file you point it at; the
historical side records (e.g. `windows-3.14.4-final-gate.json`) stay put. Do
not edit evidence files by hand, and never update
`packaging/release-artifact-manifest.json` to hide drift.

## 7. Dependency changes

```powershell
# Intentional update flow (network access to official PyPI):
.venv\Scripts\python.exe -m pip install --require-hashes -r requirements\uv-bootstrap.txt
.venv\Scripts\python.exe scripts\lock_profiles.py --write
git diff -- uv.lock requirements\locks
.venv\Scripts\python.exe scripts\lock_profiles.py --check   # offline verification
.venv\Scripts\python.exe scripts\generate_release_artifacts.py --all-profiles
```

Never hand-edit `uv.lock`, the lock exports, or the SBOM/notices — they are
generated, checked for drift, and advisory-reviewed (`docs/ADVISORY_REPORT.json`,
review dates in `docs/PACKAGING.md`).

## 8. Related documents

`docs/ARCHITECTURE.md` (contracts in depth) · `docs/LIBRARY_API.md` (the
Python surface) · `docs/THREAT_MODEL.md` (what the boundaries actually hold)
· `docs/PACKAGING.md` (profiles, locks, reproducible builds) ·
`docs/STATUS.md` (what is true right now)
