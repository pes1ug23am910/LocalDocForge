# Third-Party Notices

This is the profile index. Use the notice and SBOM matching the installed LocalDocForge profile.

| Profile | Python components | Versioned native records | Known unversioned children | Notice | SBOM |
|---|---:|---:|---:|---|---|
| Lite | 27 | 52 | 19 | [notices](THIRD_PARTY_NOTICES.lite.md) | [SBOM](docs/SBOM.lite.cdx.json) |
| Standard | 35 | 52 | 19 | [notices](THIRD_PARTY_NOTICES.standard.md) | [SBOM](docs/SBOM.standard.cdx.json) |
| Full | 36 | 52 | 19 | [notices](THIRD_PARTY_NOTICES.full.md) | [SBOM](docs/SBOM.full.cdx.json) |

`docs/SBOM.cdx.json` is a byte-identical compatibility alias of the Full profile SBOM.

All profiles currently bundle affected OpenJPEG 2.5.4 ([OSV-2025-219](https://osv.dev/vulnerability/OSV-2025-219)) through Pillow, affected libheif 1.23.0 ([OSV-2020-2308](https://osv.dev/vulnerability/OSV-2020-2308), [OSV-2023-1129](https://osv.dev/vulnerability/OSV-2023-1129)) through pi-heif, and advisory-unknown PDFium 152.0.7947.0 through pypdfium2. See the profile notice and `docs/ADVISORY_REPORT.json` for applicability, remediation, residual risk, and authoritative sources.
The 52 versioned native records are not exhaustive: 19 known unversioned PDFium, libavif, and libffi children remain advisory-unknown, and each SBOM's CycloneDX composition is `incomplete`.
The pre-existing pydantic-core 2.46.4 embedded Cargo SBOM remains unflattened and is an explicit inventory gap, not an absence claim.

These summaries are not legal advice. Redistributors must retain the full upstream license and notice texts shipped in wheel metadata.
