# Licensing and release evidence

LocalDocForge source code is licensed under the MIT License. The complete grant is in
the repository-root `LICENSE` file and is declared through the `license-files` project
metadata in `pyproject.toml`.

This document records engineering evidence, not legal advice. Redistributors remain
responsible for the exact artifacts they ship and for retaining every applicable
upstream license, copyright notice, attribution, and source-offer obligation.

## Profile-specific release artifacts

The release artifacts are generated offline from exact, marker-aware, SHA-256-bearing
profile exports and the curated machine-readable review:

| Profile | Python | Versioned native | Unversioned children | Notices | CycloneDX 1.6 SBOM |
|---|---:|---:|---:|---|---|
| Lite | 27 | 52 | 19 | `THIRD_PARTY_NOTICES.lite.md` | `docs/SBOM.lite.cdx.json` |
| Standard | 35 | 52 | 19 | `THIRD_PARTY_NOTICES.standard.md` | `docs/SBOM.standard.cdx.json` |
| Full | 36 | 52 | 19 | `THIRD_PARTY_NOTICES.full.md` | `docs/SBOM.full.cdx.json` |

The profile exports are universal Python locks, but every native record in these SBOMs
comes from inspected Windows x86-64 / CPython 3.14 wheels. They do not claim identical
native composition on Linux or macOS. Each SBOM has CycloneDX composition aggregate
`incomplete`: 52 native records have reviewed versions, 19 known children do not,
and additional statically linked or platform-specific children may exist. In
particular, the pre-existing pydantic-core 2.46.4 supplier Cargo inventory is
disclosed but not flattened.
`docs/SBOM.cdx.json` is a byte-identical compatibility alias of the Full SBOM.

`requirements-lock.txt` remains a compatibility include for the development lock. It
is not used to infer a production profile. The authoritative profile exports are:

- `requirements/locks/lite.txt`
- `requirements/locks/standard.txt`
- `requirements/locks/full.txt`

Regenerate or verify all committed artifacts with:

```powershell
.venv\Scripts\python.exe scripts\generate_release_artifacts.py --all-profiles
.venv\Scripts\python.exe scripts\generate_release_artifacts.py --check --all-profiles
```

A single profile can be generated or checked with `--profile lite`, `--profile
standard`, or `--profile full`. Generation makes no network requests. It fails if a
profile lock disagrees with the reviewed component/version/profile mapping or if a pin
lacks SHA-256 artifact hashes. Root direct and transitive Python dependency edges are
derived from `uv.lock`; the application root depends only on the ten base declarations
plus the selected Standard/Full direct extras.

## Authoritative review record

`docs/ADVISORY_REPORT.json` is the source-attributed, machine-readable review for 36
exact runtime Python distributions and 52 versioned nested native records. It separately
enumerates 19 known version-unknown children rather than representing the versioned set
as exhaustive. Sources were first accessed **2026-07-19**; dependency-specific
verification runs extend through **2026-08-10**. Each versioned component records:

- exact version and profile membership;
- its runtime role and untrusted-input reachability;
- concluded license, verification status, and exact-tag or exact-artifact evidence;
- advisory disposition and affected ranges where known;
- applicability/preconditions, remediation, residual risk, and source references.

The pdfminer.six 20260107 aggregate conclusion is `MIT AND Apache-2.0`: its
published wheel declares and installs only the top-level MIT license, but the
exact installed `_saslprep.py` retains MongoDB/PyMongo Apache-2.0 attribution
and pyHanko-licensed changes. Generated notices therefore restore that header,
the canonical Apache-2.0 terms, and the exact-tag pyHanko MIT notice rather than
silently inheriting the incomplete wheel metadata.

License conclusions were checked against authoritative upstream/vendor texts. Where
an upstream release tag exists, the report pins the license URL to that exact tag and
pairs Python records with exact PyPI release metadata. PDFium is the one unavoidable
moving-text exception: the wheel records no source hash, so its upstream license is
paired with the exact wheel's `BUILD_LICENSES` files and an explicit limitation.

Security review used authoritative upstream release/security material and exact-version
OSV and GitHub reviewed-advisory queries as corroboration. Empty database results are
recorded as `no-known-applicable-advisory`, never as proof that a component is safe.
The 2026-07-20 baseline OSV refresh submitted only public names and exact public
versions: all then-current 29 exact PyPI queries returned no matches, while the 16
versioned bundled-native queries returned one match, OpenJPEG 2.5.4 /
`OSV-2025-219`. Later runs cover the HEIF, Markdown, and S5 table-parser closures.
The 2026-08-10 run records cryptography's fixed 50.0.0 floor, its supplier-required
Cargo/OpenSSL inventory, and the advisory-unknown libffi child. Exact endpoints,
versions, conclusions, and dispositions are retained under `verificationRuns` in the
machine-readable report; empty results are never a safety guarantee.

The isolated PEP 517 build backend is a release-tool dependency, not a runtime SBOM
component. `requirements/build-backend.txt` pins `setuptools==83.0.0` to official PyPI
metadata and both published SHA-256 values: wheel
`29b23c360f22f414dc7336bb39178cc7bcbf6021ed2733cde173f09dba19abb3` and sdist
`025bccbbf0fa05b6192bc64ae1e7b16e001fd6d6d4d5de03c97b1c1ade523bef`. The release
gate downloads the exact official wheel into a one-use wheelhouse, verifies its host,
name, size, and hash, then keeps PEP 517 isolation while forcing backend installation
offline from that wheelhouse.

## Release-blocking and unknown findings

The reviewed component set is **not cleared for release** on advisory evidence alone:

1. **OpenJPEG 2.5.4 is affected by OSV-2025-219.** OSV enumerates v2.5.4 as affected
   by a heap-buffer-overflow in JPEG2000 tile decoding. The upstream fix is commit
   `d33cbecc148d3affcdf403211fddc2cc5d442379`. Pillow's 12.3.0 tag selects OpenJPEG
   2.5.4; the Windows build downloads the exact upstream v2.5.4 archive and declares
   no OpenJPEG patch. A same-version dependency tar is not credited as patched without
   checksum or equivalent provenance evidence.
2. **libheif 1.23.0 is affected by OSV-2020-2308 and OSV-2023-1129.** Untrusted
   HEIC/HEIF inputs reach this decoder through pi-heif; neither OSS-Fuzz record
   enumerates a fixed release.
3. **PDFium 152.0.7947.0 is advisory-unknown.** The pypdfium2 wheel records
   `origin=pdfium-binaries`, `n_commits=0`, and `hash=null`. Public PDFium/Chromium
   numbering cannot authoritatively establish which private security fixes are in the
   binary. PDFium directly parses and renders untrusted PDFs.
4. **All 19 enumerated unversioned native children are advisory-unknown.** These are 14
   children named by PDFium's Windows `BUILD_LICENSES` bundle, libavif's `LOCAL` AOM,
   dav1d, libyuv, and libsharpyuv children, and CFFI's embedded libffi. Aggregate or
   source-header notices are preserved, but no affected/not-affected conclusion is
   possible until exact source/build provenance is obtained.

Pillow 12.3.0 and the Pillow codec-bundle aggregate are marked
`contains-affected-component`; pi-heif is likewise marked for libheif. pypdfium2
5.12.1 and CFFI 2.1.0 are marked `contains-unknown-component`.

Current LocalDocForge media gates reject JP2/J2K, and PDF-contained JPEG2000 is rendered
through PDFium rather than Pillow's OpenJPEG. That reduces the declared OpenJPEG attack
path but does not remediate an affected bundled component. Before release, use a Pillow
build whose OpenJPEG contains the upstream fix (or a later fixed release), keep JP2/J2K
rejected, and retain hard worker/resource isolation. Re-inventory the native contents
of every non-Windows target wheel.

For PDFium, obtain the exact source revision/build provenance from pdfium-binaries and
map it to security fixes, or upgrade/rebuild from a vetted revision. Do not present the
current binary as not affected merely because public exact-version database queries
returned no match.

## License retention and native code

The Python and native conclusions include permissive licenses, MPL-2.0, multiple-license
expressions, and nested notice terms. In particular:

- pikepdf is MPL-2.0 and its inspected Windows wheel includes qpdf 12.3.2 under
  Apache-2.0 plus `msvcp140.dll` 14.44.35211.0 under Microsoft Visual C++ Runtime
  2015-2022 Software License Terms; retain the wheel's complete distributable-code
  notice;
- pypdfium2's own code is Apache-2.0 or BSD-3-Clause, its documentation/examples include
  CC-BY-4.0 terms, and the PDFium binary carries a platform-specific dependency notice
  bundle;
- Pillow is MIT-CMU, while its codec bundle includes distinct Brotli, FreeType,
  HarfBuzz, Little CMS, libavif, libjpeg-turbo, libpng, libwebp, OpenJPEG, libtiff, xz,
  and zlib-ng terms;
- packaging is Apache-2.0 or BSD-2-Clause; FreeType is FTL or GPL-2.0-or-later; and xz
  contains multiple terms whose complete upstream `COPYING` governs.

Summary labels in an SBOM or notice do not replace license text. Preserve the complete
license/notice directories from redistributed wheels, and review the final standalone
bundle or installer rather than assuming the Python environment inventory is complete.

## Optional external engines

No optional external executable is distributed by the reviewed Python profiles.
Typst 0.15.1 is separately installed on the reviewed machine and is enabled for
Markdown-to-PDF when its minimum-version probe passes; its executable is Apache-2.0 and
is invoked, never linked or bundled. It therefore remains outside the profiles' 88
versioned review records and 19-child unversioned inventory. qpdf CLI, Tesseract,
OCRmyPDF, Ghostscript, LibreOffice, Pandoc, and veraPDF are neither enabled nor shipped.

If an engine is enabled or redistributed later, repeat exact-version license,
provenance, and advisory review. Copyleft, dual-license, commercial, plugin, and bundled
dependency terms depend on the exact build and distribution method; subprocess
separation alone is not a universal licensing conclusion.
