# Licensing and release evidence

LocalDocForge source code is licensed under the MIT License. The complete
license grant is in the repository-root `LICENSE` file and is declared through
the `license-files` project metadata in `pyproject.toml`.

This document describes the evidence available in the audited local
environment. It is an engineering inventory, not legal advice.

## Generated release artifacts

- `THIRD_PARTY_NOTICES.md` inventories the locked Python distributions,
  locally evidenced license labels, bundled native components, build
  requirements, and optional external engines.
- `docs/SBOM.cdx.json` is a CycloneDX 1.6 software bill of materials for the
  single locked environment. It includes PyPI package URLs and explicit qpdf,
  PDFium, and Pillow codec-bundle components.
- `scripts/generate_release_artifacts.py` regenerates both files using only
  `requirements-lock.txt`, `pyproject.toml`, `importlib.metadata`, and
  license evidence already installed on the machine. It makes no network
  requests.

Regenerate and verify drift with:

```powershell
.venv\Scripts\python.exe scripts\generate_release_artifacts.py
.venv\Scripts\python.exe scripts\generate_release_artifacts.py --check
```

The generated inventory does not replace upstream license texts. Wheel license
and notice files must be preserved when dependencies are redistributed, and a
standalone bundler or installer needs a fresh review of the files it actually
contains.

## Dependency categories

`pyproject.toml` declares runtime dependencies and a `dev` extra. The current
`requirements-lock.txt` combines their transitive closures and also contains
several Uvicorn-standard extras not requested by the project metadata. The
generated notices label packages as `runtime`, `development`, or `lock-only`
instead of implying that every locked package is a production dependency.

The lock contains exact versions but no artifact hashes or platform markers.
It was derived from Windows/Python 3.14 and is not proof that the same wheels
exist or behave correctly on Python 3.12/3.13, macOS, or Linux.

Lite, Standard, and Full installation profiles are **not implemented**. Until
profile-specific dependency definitions, locks, installation tests, and SBOMs
exist, the combined lock must not be described as any of those profiles.

## Bundled native code

The repository does not bundle optional qpdf, Tesseract, OCRmyPDF,
Ghostscript, LibreOffice, Pandoc, Typst, or veraPDF executables. However, core
Python wheels do contain native code and their associated licensing evidence:

- pikepdf includes libqpdf; the inspected wheel reports qpdf 12.3.2,
  Apache-2.0 qpdf notices, libjpeg/IJG terms, and Microsoft Visual C++ Runtime
  redistributable terms.
- pypdfium2 includes a PDFium DLL. The inspected build reports PDFium
  152.0.7947.0 and supplies per-component `BUILD_LICENSES` files.
- Pillow supplies native image/codec libraries. Their exact versions and the
  wheel's license-bundle evidence are recorded in the generated notices and
  SBOM.

The generated nested-component list is environment-specific. A different
wheel or standalone build must be inventoried again.

## Optional external engines

External engines remain user-installed subprocess candidates and are absent
from the Python dependency lock. Their license strings in diagnostics are
expected upstream identities, not proof of the terms governing an arbitrary
binary found on `PATH`.

Copyleft, dual-licensed, and commercial terms depend on the exact engine,
version, distribution method, and use. In particular, Ghostscript's
AGPL/commercial choices and veraPDF's GPL/MPL choices require authoritative
upstream review before redistribution or release enablement. Merely invoking a
separate process is not used here as a categorical conclusion about every
licensing scenario.

## Checks still requiring approved network access

No online license, wheel-availability, or security-advisory lookup was
performed during offline generation. Before a sensitive-document release,
review every exact Python and nested native version against authoritative
upstream license files and security advisories, then record the date and
results in the independent audit. Repeat that review for each optional engine
that is enabled or distributed.
