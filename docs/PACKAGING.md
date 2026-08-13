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
| default / `lite` | Library and CLI; current PDF organization, inspection, render validation, image conversion, PDF text extraction, and Markdown parsing/render orchestration | none; Lite is an explicit base alias |
| `standard` | Lite plus localhost FastAPI service and status page | FastAPI, Uvicorn, python-multipart |
| `full` | Standard plus optional pypdf diagnostic adapter | pypdf |
| `dev` | Full plus test, lint, type, build, and artifact tools | pytest, ReportLab, HTTPX, Ruff, mypy, build, Twine |

“Full” means all shipped Python adapters. It does not add OCR,
Office/HTML-to-PDF conversion, PDF/A/PDF/UA, editing, signatures, scanner
support, external executables, or the planned React UI. PDF-to-Markdown text
extraction and the `markdown-it-py>=4.2` side of Markdown-to-PDF are part of
every profile. Opt-in PDF-to-Markdown table extraction also ships in every
profile through `pdfplumber==0.11.10` and its locked parser/cryptography
closure. Typst ≥0.15.1 is separately installed and never bundled by these
profiles, so `ldf doctor` remains the live capability authority.

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
the official PyPI uv artifacts. Resolution uses the official PyPI simple index
and a 2026-07-19 global cutoff, with one package-scoped exception:
cryptography uses 2026-08-01 so the locks can select 50.0.0, the first release
fixing CVE-2026-69247. The exception does not move any other package's cutoff,
and no insecure/trusted-host bypass is used.

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

The manifest is **platform-scoped** (schema 3). Byte-identical artifacts exist
only per build platform: measured Windows↔Linux on 2026-08-03, package
`METADATA` newlines (CRLF vs LF), archive external attributes (`fat` 0666 vs
`unx` 0644 modes), and deflate output (zlib variants) all legitimately differ.
Each platform's gate therefore compares against its own recorded identity;
`--allow-unrecorded-platform` — used by CI until non-Windows identities are
recorded — skips only that comparison, with a printed notice, never the
reproducibility, Twine, metadata, or sdist-to-wheel checks. `profile_matrix.py`
matches a wheel against *any* recorded platform identity because the CI matrix
verifies the Ubuntu-built artifact on every OS; an unmatched wheel is refused
unless the same flag turns it into an explicit
`skipped-no-recorded-platform-identity` marker in the evidence.

The retained 2026-08-03 artifacts (first Phase 2 slice — lossless compression
— plus the Linux-mypy portability fix caught by the first CI run) were
generated by the complete gate with:

```powershell
.venv\Scripts\python.exe scripts\release_gate.py --update-artifact-manifest `
  --dist-dir dist\windows-11-x64 `
  --profile-evidence packaging-evidence\windows-3.14.4.json
```

| Identity | SHA-256 | Bytes |
|---|---|---:|
| package source inputs | `150b4aeb882d0b6c8b09787fed94d7df8d460ad2522aac61c37395f3fa504db2` | — |
| `localdocforge-0.1.0-py3-none-any.whl` | `049345cb2dd1f6d23da011a95984eede0e5d64e2d34a052f8e408132dee2b2b0` | 98,083 |
| `localdocforge-0.1.0.tar.gz` | `531c4084dd8071cae8c66e2408c1ed388a1c419c48d3e12b799b32311e3d69ca` | 84,593 |

`packaging-evidence/windows-11-x64-SHA256SUMS.txt` authenticates both retained
files. Superseded artifact sets were not deleted: the 2026-07-20 set (wheel
`392d0952…`, 92,228 B) is archived in `dist/windows-11-x64-2026-07-20/`, the
pre-mypy-portability set (wheel `540a3621…`, 96,696 B) in
`dist/windows-11-x64-2026-08-03-superseded/`, and the
pre-Linux-limit-classification set (wheel `972a1e82…`, 96,834 B) in
`dist/windows-11-x64-2026-08-03-superseded-2/`, each with its checksum file.
Refresh the manifest only after inspecting intentional package-source
changes; never update it merely to hide drift. (Both 2026-08-03 refreshes
followed intentional changes: the drift check first failed exactly as
designed, and the manifest was regenerated only after the new sources passed
the full gate.)

### 2026-08-08 S1 manifest identity

The non-interactive-password slice and its review remediation intentionally
changed packaged sources. Every gate used fresh temporary dist, checksum, and
profile-evidence paths because the multi-model execution plan protects retained
evidence from executor writes. Consequently, the live manifest below is the
authoritative identity for the remediated S1 drift checks, while
`dist/windows-11-x64/` and
`packaging-evidence/windows-11-x64-SHA256SUMS.txt` remain honest historical
2026-08-03 records and are not claimed as S1 artifacts.

| Identity | SHA-256 | Bytes |
|---|---|---:|
| package source inputs | `d4b78a0b520f029eb94f6aba4e7ecdc1fc9dc2f4ffa625a9b12ea692ce34ff86` | — |
| `localdocforge-0.1.0-py3-none-any.whl` | `7f3e6689ece12b7eba18ec6ec20d7546f49ad67cb4ae3af5bac1c54a7d934476` | 104,614 |
| `localdocforge-0.1.0.tar.gz` | `f7ad3852530f1ff6ca6d1f701ddaadf94fb2775d4aa5f97f403474aee53c978d` | 92,911 |

The first full S1 refresh passed on Windows-AMD64 CPython 3.14.4 with 460
collected outcomes. After the packaged README count and two internal-review
findings were corrected, the pre-review refresh repeated the complete gate on
the 463-outcome tree in 339.2 seconds. The required cross-family review then
found that the Windows NUL device was misclassified as interactive. The F1
remediation adds a real `stdin=subprocess.DEVNULL` regression, bringing the
tree to 464 outcomes.

The first remediation-gate attempt passed the ordinary and blocked-environment
suites, quality, reproducible/Twine/sdist-equivalence checks, and the installed
Base/Lite/Standard/Full profile checks. It refreshed the table above, then the
final Dev full-suite correctly rejected this document's still-previous hashes.
This ordering failure left the protected artifacts untouched and required a
definitive complete-gate rerun after synchronizing the documented identity.
That rerun passed end to end in 341.1 seconds on Windows-AMD64 CPython 3.14.4:
both 464-outcome suite modes, all quality and build checks, every clean install
profile, the fresh Dev full-suite, and `release_manifest_verified: true`. The
table is that reproducibly verified final identity.

### 2026-08-08 S2 manifest identity

The PDF-to-images LLM-preset slice intentionally changes packaged sources and
the packaged README. Its live Windows-AMD64 manifest was refreshed only after
the focused behavior, documentation, Ruff, mypy, generated-artifact, and full
test checks passed. The refresh used a fresh temporary dist path; it did not
modify the retained `dist/windows-11-x64/` or `packaging-evidence/` records,
which remain historical evidence rather than S2 artifacts.

| Identity | SHA-256 | Bytes |
|---|---|---:|
| package source inputs | `6e0707f41f1a22be8876b4fdc1f1593f27063b09e1badc29be49df28366dc063` | — |
| `localdocforge-0.1.0-py3-none-any.whl` | `5d2dd0ceb978665fc4d89fb90d7ace35c00b98dd920267ab7cfe1d6bcb8b1b2f` | 106,628 |
| `localdocforge-0.1.0.tar.gz` | `1b2630a8adb11082954038e81ea5189c619422e7a14dc14f7405be14db7581c0` | 94,998 |

An initial S2 gate passed end to end in 316.206 seconds, but its package
identity was intentionally superseded after the independent diff audit found
and the executor corrected a typed-library compatibility defect. The table is
the post-remediation build identity. The definitive post-audit gate verified
it without updating in 392.978 seconds on Windows-AMD64 CPython 3.14.4, using
another fresh temporary dist directory and profile-evidence file. Both
476-outcome suite modes, reproducible builds, all four clean profiles, fresh
Dev checks, and `release_manifest_verified: true` passed. This keeps manifest
drift an independent assertion.

### 2026-08-08 S3 post-S2 integration manifest identity

The registry-derived agent-brief slice was rebased onto the shipped S2
PDF-to-images preset before merge review. Packaged inputs now contain both
features, including the F1 integration update that advertises
`pdf-to-images --preset llm` in the registry-keyed agent usage template and
the merged 504-outcome inventory in the packaged README. A build-only
`--update-artifact-manifest` run used a fresh system-temporary dist directory;
it did not modify retained `dist/windows-11-x64/` or `packaging-evidence/`
records.

| Identity | SHA-256 | Bytes |
|---|---|---:|
| package source inputs | `69354c7f9bb4d092661f0b29430de563362b3abfba34b4282f548aa49455a28a` | — |
| `localdocforge-0.1.0-py3-none-any.whl` | `a6831020d1e60321af4626b654d6739b17d616fc0eb4b4a9698d354bdca6c47a` | 112,760 |
| `localdocforge-0.1.0.tar.gz` | `3ed23059b2ff8e503ade9dfb2d416c2f95049f75007cb486f7bfe349f0f3b1e9` | 100,287 |

The fresh build-only run passed reproducible wheel/sdist construction, Twine,
member/metadata checks, and sdist-to-wheel equivalence in 29.3 seconds. A
separate definitive verify-mode full gate then reproduced this identity without
updating it and passed in 331.7 seconds on Windows-AMD64 CPython 3.14.4. Both
ordinary and blocked-network 504-outcome suites, locks, Ruff, three-platform
mypy, generated-artifact drift, reproducible builds, all four source/wheel
profiles, and the clean Dev full-suite passed; profile evidence records
`full_tests.status: passed`, `release_manifest_verified: true`, and SHA-256
`f2299b86791e6885a2775a1b4404e9bb80c5871cd17a744ee43f3d4f1efdb26a`.

### 2026-08-09 S4 PDF text-extraction manifest identity

S4 adds the packaged `localdocforge/operations/text.py` pipeline plus its
registry, CLI, API, runner-validation, and inspect integrations. No runtime
dependency or lock changed. The live Windows-AMD64 identity was refreshed by a
build-only gate using disposable system-temporary artifacts; retained `dist/`
and `packaging-evidence/` records were not modified.

| Identity | SHA-256 | Bytes |
|---|---|---:|
| package source inputs | `a9ad99e5ba9ed3c59f8076334074bb1e377b5a93d64bafe5fe074673f546909d` | — |
| `localdocforge-0.1.0-py3-none-any.whl` | `f8f032b845f954d68ec5d194ca0c34f522b2ff722b425254adaad229c5331856` | 124,962 |
| `localdocforge-0.1.0.tar.gz` | `43670e82a7f990a8c00f890dd77291605175b1bce99f9a6f17b755cb30b0b234` | 111,797 |

The build-only refresh passed reproducible direct builds, Twine/member/metadata
checks, and sdist-to-wheel equivalence in 32.935 seconds. The definitive
verify-mode gate and its fresh temporary profile-evidence identity are recorded
in `docs/STATUS.md` and the S4 executor log.

#### 2026-08-09 S4 F1/N1 remediation identity

On 2026-08-09, the two non-blocking findings were addressed on
S4. The inspect path now skips style sampling that its count-only result never
consumes; package dependencies and locks remain unchanged. A build-only refresh
again used disposable system-temporary artifacts and left retained `dist/` and
`packaging-evidence/` records untouched.

| Identity | SHA-256 | Bytes |
|---|---|---:|
| package source inputs | `b3384fe440a96881a96d9617d4a5dd2a35ac2f03d8f7146055ff989024ee0f5f` | — |
| `localdocforge-0.1.0-py3-none-any.whl` | `1f6a5ed319f51f2ad793a9593eaedadd3bb3ab08f62191eb1497bebf09aaa097` | 125,012 |
| `localdocforge-0.1.0.tar.gz` | `42481a9948b469213a5d78317d905618818e51ae4c4b381d38daf14a1e0c8fd4` | 111,839 |

The remediation build-only refresh passed reproducible direct builds,
Twine/member/metadata checks, and sdist-to-wheel equivalence in 25.3 seconds.
The separate clean-tree verify-mode gate then passed in 434.112 seconds at
revision `961e3e54bf9274d3f533bdce835aef9097e85206`; fresh temporary evidence
records `release_manifest_verified: true`, `source_install_syntax_tested: true`,
`full_tests.status: passed`, and SHA-256
`626d4885463d005d5a3611cdd625baee7f4d4b47bf7a396623da85375e74d3c9`.

### 2026-08-09 S6 Markdown-to-PDF manifest identity

S6 packages the bounded `operations/markdown.py` pipeline and makes
`markdown-it-py>=4.2` a direct base dependency; Typst remains a separately
installed subprocess and is not bundled. A build-only reproducibility gate used
disposable system-temporary artifacts and left retained `dist/` and
`packaging-evidence/` records untouched.

| Identity | SHA-256 | Bytes |
|---|---|---:|
| package source inputs | `b2d8dfe9abe5bfea5c99ace8504d505e0b29cf5988e045a86cc768f8677688a9` | — |
| `localdocforge-0.1.0-py3-none-any.whl` | `98d2701037611e2313ddd89efd4ec6f5247cc3089b4c7a8aaf05d1bc11b3d432` | 140,233 |
| `localdocforge-0.1.0.tar.gz` | `29aa62fae063b934f6fb539ddee3ef71f55195913a040668f81c4a01589c369d` | 126,744 |

The 34.6-second refresh passed two byte-identical direct builds, Twine/member/
metadata checks, and sdist-to-wheel equivalence. A separate 552.2-second
verify-mode full gate reproduced the identity, passed both 615-outcome suite
modes and every source/wheel profile, and recorded
`release_manifest_verified: true`, `source_install_syntax_tested: true`, and
`full_tests.status: passed`. Its disposable evidence SHA-256 is
`8216c2d879f0a7af04ca9a2d6b5f281750777de2502340e0dd13b156796887dc`;
retained artifacts/evidence remained untouched.

#### F1 literal-dollar remediation identity

The S6 review remediation removes the custom inline-dollar detector so ordinary
CommonMark currency prose remains literal. A 30.4-second build-only gate used a
fresh system-temporary directory, reproduced both direct builds and the
sdist-to-wheel build, and refreshed only the live manifest below. Retained
`dist/` and `packaging-evidence/` records were not modified.

| Identity | SHA-256 | Bytes |
|---|---|---:|
| package source inputs | `d5d841401632212c646159f347aa2b5d1271ca2b71623870f4812e13be62b22e` | — |
| `localdocforge-0.1.0-py3-none-any.whl` | `5cdc94118b52d2fe5bc14245287044060014aa0beb856f98bc834b1e83fe0d7b` | 140,192 |
| `localdocforge-0.1.0.tar.gz` | `41a8d07447859e5d7acbcfd5d4904cc3db8190425185171f7ddab99f91a1a5d5` | 126,706 |

A subsequent 491.6-second clean-tree verify-mode gate at remediation commit
`f83a2b7d4c20c11aa5d56a3fcedef6a1a31e6863` reproduced this identity, passed
both 616-outcome suite modes and every source/wheel profile, and recorded
`release_manifest_verified: true`, `source_install_syntax_tested: true`,
`full_tests.status: passed`, and `source.working_tree_changes: false`.
Disposable artifacts/evidence were removed after verification; retained
artifacts/evidence remained untouched.

### 2026-08-10 S5 PDF-to-Markdown table identity

S5 packages the bounded opt-in pdfplumber table path, adds `pdfplumber` and the
fixed `cryptography>=50.0.0` floor as direct base requirements, and carries the
reviewed six-package Python closure in every profile. A 29.066-second build-only
gate used a fresh system-temporary directory, reproduced both direct builds and
the sdist-to-wheel build, and refreshed only the live manifest below. Retained
`dist/` and `packaging-evidence/` records were not modified.

| Identity | SHA-256 | Bytes |
|---|---|---:|
| package source inputs | `66e25069bc8db079746ddc609ab07ce7162982b31e3a6bf4d42f8a5d4130cef9` | — |
| `localdocforge-0.1.0-py3-none-any.whl` | `7076cabda6baaa9a6b9bda69ccbd4533c8cb7c61e3ab5cbf2cffb652f3bedae6` | 145,443 |
| `localdocforge-0.1.0.tar.gz` | `0ad3304e7342721bdf67b6f079c0881e3979fc865ae50042c63e4b468ab781d0` | 132,183 |

The build-only refresh passed byte-identical direct builds, Twine/member/
metadata checks, and sdist-to-wheel equivalence. A subsequent 485.3-second
clean-tree verify-mode gate at S5 implementation commit
`073b7b32812ad08af86eed261dbaf776bb6a92bd` reproduced the identity, passed both
639-outcome suite modes and every source/wheel profile, and recorded
`release_manifest_verified: true`, `source_install_syntax_tested: true`,
`full_tests.status: passed`, and `source.working_tree_changes: false`.
Disposable evidence SHA-256 was
`9c4e25226330d3e8ff8206d7067edc744f82291d8c8bc45b5d2ab68b4553596b`;
temporary output was removed and retained artifacts/evidence stayed untouched.

### 2026-08-14 public CI and history-remediation identity

The public-snapshot remediation installs the separately required Typst 0.15.1
compiler in both GitHub Actions jobs, makes ANSI-coloured help assertions
semantic, and removes stale development-plan wording from the packaged agent
brief. The documented manifest-refresh path reproduced both direct builds and
the sdist-to-wheel build before updating the live Windows-AMD64 identity.
The follow-up Linux gate established that Typst reports relative dependency
paths on that platform; these are now anchored to the private compile workspace
before the same containment and exact input/output checks run. CI evidence
uploads are also scoped to the current runner's JSON record, so a failed runner
cannot publish unrelated retained records under its artifact name.

| Identity | SHA-256 | Bytes |
|---|---|---:|
| package source inputs | `161c290f452a9f8334ea0c4bd7eef75a858baec434569fa8c8d21675a87d49f8` | — |
| `localdocforge-0.1.0-py3-none-any.whl` | `bae9a1b9a9bf8992af913002f232a6e4e794f2098a842cf85f7411f828c8cf8b` | 145,515 |
| `localdocforge-0.1.0.tar.gz` | `8f5362a202dae7b239b0d47c77f98d14b975b0118d43cae8f3c9b4d215fb7954` | 132,231 |

The history rewrite preserves the prior release records as historical facts;
this table is the current manifest identity for the remediated public source.

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
wheel uninstall. The additional fresh Dev venv runs Ruff, mypy, the complete
collected suite (639 outcomes as of 2026-08-10), the same suite with Python
DNS/non-loopback sockets denied, and generated-artifact drift.

## SBOMs, notices, and the complete gate

Profile-specific artifacts are:

- `docs/SBOM.lite.cdx.json` / `THIRD_PARTY_NOTICES.lite.md`
- `docs/SBOM.standard.cdx.json` / `THIRD_PARTY_NOTICES.standard.md`
- `docs/SBOM.full.cdx.json` / `THIRD_PARTY_NOTICES.full.md`

The 2026-08-10 S5 inventory contains 36 unique runtime Python records, 52
versioned bundled-native records, and 19 enumerated version-unknown native
children. Profile component totals are 98 Lite, 106 Standard, and 107 Full.
Cryptography's supplier SBOM boundary retains its aggregate plus all 32
`scope=required` Cargo children and OpenSSL 4.0.1, while excluding exactly seven
supplier-marked build dependencies and a duplicate target record. CFFI's
embedded libffi remains version-unknown. Composition is still explicitly
`incomplete`: the pre-existing pydantic-core 2.46.4 embedded Cargo inventory is
disclosed but not flattened, and other static/platform-specific children may
exist.

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
for the exact build-backend files and hashes above. On 2026-08-08, the HEIF
input dependency was added and reviewed: pi-heif 1.4.0 (decode-only; bundled
libheif 1.23.0 and libde265 1.1.1) was queried against OSV and GitHub reviewed
advisories with exact versions, its exact-tag license texts were verified, and
the results — including two applicable OSS-Fuzz records against libheif — are
recorded in the report's 2026-08-08 verification run. The full pillow-heif
encoder package (GPLv2 wheels) is a dev-profile fixture tool only and is
excluded from every runtime profile, SBOM, and notice. PyPI's current classifier
list was also checked before replacing the OS-independent classifier with
`Operating System :: Microsoft :: Windows :: Windows 11`.
On 2026-08-10, S5 added pdfplumber 0.11.10 and its exact locked closure. The
review constrained cryptography to 50.0.0 because 49.0.0 falls in the affected
range for GHSA-g6cj-pr64-35w5/CVE-2026-69247, even though pdfminer.six does not
call the affected PKCS#7 API. Exact PyPI, crates.io, OpenSSL, supplier-SBOM, and
source-license evidence—including pdfminer.six's omitted pyHanko notice and
MongoDB/PyMongo Apache-2.0 SASLprep attribution/terms, plus CFFI's omitted
libffi notice—is recorded in the report's 2026-08-10 verification run. Empty
advisory results remain time-bounded findings, not safety guarantees.
