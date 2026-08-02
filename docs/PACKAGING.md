# Packaging, dependency profiles, and release gate

This is the packaging source of truth for LocalDocForge 0.1.0. A version
marker, lock resolution, package classifier, or configured CI job is a declared
contract—not proof that an operating system passed.

## Declared metadata and executed runners

Package metadata declares CPython 3.12, 3.13, and 3.14
(`Requires-Python: >=3.12,<3.15`) and carries the Windows 11 classifier for this
primary release checkpoint. The universal lock resolves CPython markers for
Windows, Linux, and macOS, but only these rows are evidence:

| Runner | Python | Result | Retained evidence |
|---|---:|---|---|
| Windows 11 x64 build 26200 | 3.14.4 | **Passed** Base/Lite/Standard/Full source+wheel install, smoke, uninstall, Ruff, mypy, normal/blocked full tests, drift, manifest and checksum | `packaging-evidence/windows-3.14.4.json` |
| Windows 11 x64 build 26200 | 3.13.5 | **Passed** same side-by-side matrix | `packaging-evidence/windows-3.13.5.json` |
| Windows 11 x64 | 3.12 | Not run | none |
| Linux | 3.12–3.14 | Not run after hardening | configured CI only |
| macOS | 3.12–3.14 | Not run after hardening | configured CI only |

The preserved `.venv` CPython 3.14.4 environment was not deleted or replaced.
CPython 3.13.5 was selected from a separate uv-managed installation and every
matrix environment was temporary. Do not infer Windows 3.12, Linux, or macOS
from these Windows results.

## Honest installation profiles

Profiles describe shipped Python dependencies, not the feature roadmap.

| Install | Shipped behavior | Direct additions over base |
|---|---|---|
| default / `lite` | Library and CLI; current PDF organization, inspection, render validation, and image conversion | none; Lite is an explicit base alias |
| `standard` | Lite plus localhost FastAPI service and status page | FastAPI, Uvicorn, python-multipart |
| `full` | Standard plus optional pypdf diagnostic adapter | pypdf |
| `dev` | Full plus test, lint, type, build, and artifact tools | pytest, ReportLab, HTTPX, Ruff, mypy, build, Twine |

“Full” means all shipped Python adapters. It does not add OCR,
Office/Markdown conversion, PDF/A/PDF/UA, editing, signatures, scanner support,
external executables, or the planned React UI. `ldf doctor` is the live
capability authority.

The Windows-primary reproducible Standard install is:

```powershell
py -3.14 -m venv .venv-standard
.venv-standard\Scripts\python.exe -m pip install --require-hashes `
  -r requirements\locks\standard.txt
.venv-standard\Scripts\python.exe -m pip install --no-deps ".[standard]"
.venv-standard\Scripts\ldf.exe --json doctor
```

Ordinary resolver-driven source forms are also tested in isolated staged
sources: `.`, `.[lite]`, `.[standard]`, and `.[full]`. For an audited install,
dependencies come from the matching hash lock first and LocalDocForge is then
installed with `--no-deps`, preventing silent re-resolution.

The equivalent POSIX recipe is structurally supported but unverified in this
checkpoint:

```bash
python3 -m venv .venv-standard
.venv-standard/bin/python -m pip install --require-hashes \
  -r requirements/locks/standard.txt
.venv-standard/bin/python -m pip install --no-deps ".[standard]"
.venv-standard/bin/ldf --json doctor
```

## Universal lock and regeneration

`uv.lock` is the canonical universal resolution. Auditable, marker-aware,
SHA-256-enforced exports are:

- `requirements/locks/lite.txt`
- `requirements/locks/standard.txt`
- `requirements/locks/full.txt`
- `requirements/locks/dev.txt`

`requirements-lock.txt` is only a compatibility include for Dev. The resolver
is pinned to uv 0.11.26. `requirements/uv-bootstrap.txt` contains hashes for
the official PyPI uv artifacts. Resolution uses the official PyPI simple index,
a 2026-07-19 cutoff, and no insecure/trusted-host bypass.

```powershell
.venv\Scripts\python.exe -m pip install --require-hashes `
  -r requirements\uv-bootstrap.txt
.venv\Scripts\python.exe scripts\lock_profiles.py --check
```

To intentionally update after reviewing dependency/advisory changes:

```powershell
.venv\Scripts\python.exe scripts\lock_profiles.py --write
git diff -- uv.lock requirements\locks
.venv\Scripts\python.exe scripts\lock_profiles.py --check
```

`--write` may contact official PyPI. `--check` forces uv offline and rejects
canonical-lock, export, hash, profile-set, and approved-host drift.

## Hash-locked isolated build backend

PEP 517 declares only `setuptools==83.0.0`. The separate
`requirements/build-backend.txt` records official PyPI release hashes:

- wheel:
  `29b23c360f22f414dc7336bb39178cc7bcbf6021ed2733cde173f09dba19abb3`
- sdist:
  `025bccbbf0fa05b6192bc64ae1e7b16e001fd6d6d4d5de03c97b1c1ade523bef`

For each build gate, LocalDocForge validates lock/`pyproject.toml` agreement,
downloads the exact wheel only from HTTPS `files.pythonhosted.org`, bounds its
size, rejects an off-host redirect, and verifies SHA-256. It then keeps normal
PEP 517 build isolation but strips ambient `PIP_*` configuration and forces
backend installation from the one-use wheelhouse with `PIP_NO_INDEX=1`,
`PIP_FIND_LINKS`, and binary-only mode. `--no-build-isolation` is not used.

PowerShell bootstrap sets `$PSNativeCommandUseErrorActionPreference = $true`
before native environment/install/lint/test/doctor commands, so a failing native
command terminates the script instead of continuing with false evidence.

## Reproducible artifacts

The gate stages two independent minimal source trees, builds direct wheel and
sdist twice with `SOURCE_DATE_EPOCH=1704067200`, canonicalizes sdist metadata,
requires byte-identical repeats, rebuilds the wheel from the sdist, and requires
that wheel to match the direct wheel. Twine, metadata, member, and pure-wheel tag
checks run before comparison with `packaging/release-artifact-manifest.json`.

The canonical 2026-08-03 artifacts (first Phase 2 slice — lossless
compression — plus the Linux-mypy portability fix caught by the first CI run)
were generated by the complete gate with:

```powershell
.venv\Scripts\python.exe scripts\release_gate.py --update-artifact-manifest `
  --dist-dir dist\windows-11-x64 `
  --profile-evidence packaging-evidence\windows-3.14.4.json
```

| Identity | SHA-256 | Bytes |
|---|---|---:|
| package source inputs | `2b732915fd7e635359f52f9982db21a0b33a9bb5db7da7f37435d69edf19c8d8` | — |
| `localdocforge-0.1.0-py3-none-any.whl` | `972a1e82e662dac0618518aa45bc8f3ac7e0b1cd4f2c179c6a665f222d0b56d8` | 96,834 |
| `localdocforge-0.1.0.tar.gz` | `faf90ac7df2461921ad9d3a372fc90abd553132718dcde63fcc9950e06cfe57b` | 83,397 |

`packaging-evidence/windows-11-x64-SHA256SUMS.txt` authenticates both retained
files. Superseded artifact sets were not deleted: the 2026-07-20 set (wheel
`392d0952…`, 92,228 B) is archived in `dist/windows-11-x64-2026-07-20/`, and
the same-day pre-portability-fix set (wheel `540a3621…`, 96,696 B) in
`dist/windows-11-x64-2026-08-03-superseded/`, each with its checksum file.
Refresh the manifest only after inspecting intentional package-source
changes; never update it merely to hide drift. (Both 2026-08-03 refreshes
followed intentional changes: the drift check first failed exactly as
designed, and the manifest was regenerated only after the new sources passed
the full gate.)

## Clean profile/full-test matrices

Both executed interpreters used the same authenticated wheel, package-source
identity, manifest, profile locks, and checksum file:

```powershell
.venv\Scripts\python.exe scripts\profile_matrix.py `
  --wheel-dir dist\windows-11-x64 `
  --python .venv\Scripts\python.exe `
  --install-source --full-tests `
  --checksum-file packaging-evidence\windows-11-x64-SHA256SUMS.txt `
  --evidence packaging-evidence\windows-3.14.4.json

$python313 = & .venv\Scripts\uv.exe python find 3.13
.venv\Scripts\python.exe scripts\profile_matrix.py `
  --wheel-dir dist\windows-11-x64 `
  --python $python313 `
  --install-source --full-tests `
  --checksum-file packaging-evidence\windows-11-x64-SHA256SUMS.txt `
  --evidence packaging-evidence\windows-3.13.5.json
```

Each profile gets a fresh venv, matching hash lock, source install/import/
uninstall, wheel install, `pip check`, `ldf doctor` plus focused core smoke, and
wheel uninstall. The additional fresh Dev venv runs Ruff, mypy, all 390 tests,
the same complete suite with Python DNS/non-loopback sockets denied, and
generated-artifact drift.

## SBOMs, notices, and the complete gate

Profile-specific artifacts are:

- `docs/SBOM.lite.cdx.json` / `THIRD_PARTY_NOTICES.lite.md`
- `docs/SBOM.standard.cdx.json` / `THIRD_PARTY_NOTICES.standard.md`
- `docs/SBOM.full.cdx.json` / `THIRD_PARTY_NOTICES.full.md`

Regenerate/check them offline:

```powershell
.venv\Scripts\python.exe scripts\generate_release_artifacts.py --all-profiles
.venv\Scripts\python.exe scripts\generate_release_artifacts.py --check
```

The complete local command is:

```powershell
.venv\Scripts\python.exe scripts\release_gate.py `
  --profile-evidence packaging-evidence\windows-3.14.4.json
```

With all default steps selected, the profile phase also uses `--full-tests`.
The independently runnable Python-level network instrumentation is:

```powershell
.venv\Scripts\python.exe scripts\run_blocked_network.py
```

It permits loopback and local non-IP sockets while denying Python DNS and
non-loopback socket primitives, including spawned Python workers. It is test
instrumentation, not an OS firewall.

The opt-in OS-level Windows probe is:

```powershell
# Requires explicit approval and an elevated PowerShell 7 session.
pwsh -File scripts\run_windows_firewall_gate.ps1
```

It temporarily creates one unique outbound Block rule scoped to the exact
Python executable, verifies the effective rule, tests local loopback and a
self-hosted non-loopback TCP listener, and removes/verifies removal in `finally`.
It was **not executed** in this checkpoint. It deliberately exits 2 even after
successful socket proof because an executable-scoped rule cannot prove denial
of Windows DNS Client-mediated `getaddrinfo`; therefore it cannot satisfy the
complete addendum network gate by itself.

## CI contract (not local pass evidence)

`.github/workflows/packaging.yml` is configured to build on Ubuntu CPython 3.14
and to download/check that artifact across `windows-latest`, `ubuntu-latest`,
and `macos-latest` for Python 3.12, 3.13, and 3.14. It retains distributions,
checksums, profile evidence, SBOMs, and notices. No workflow run was executed or
retained here; all CI rows remain unverified until their actual artifacts exist.

## Authoritative packaging sources

Base sources were accessed 2026-07-19 and are recorded in
`docs/ADVISORY_REPORT.json`: PyPA packaging guidance, uv lock/resolution/export
documentation, and official PyPI metadata for pinned release tools. On
2026-07-20, official `https://pypi.org/pypi/setuptools/83.0.0/json` was checked
for the exact build-backend files and hashes above. PyPI's current classifier
list was also checked before replacing the OS-independent classifier with
`Operating System :: Microsoft :: Windows :: Windows 11`.
