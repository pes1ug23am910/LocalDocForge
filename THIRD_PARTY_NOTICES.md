# Third-Party Notices

This is the profile index. Use the notice and SBOM matching the installed LocalDocForge profile.

| Profile | Python components | Versioned native records | Known unversioned children | Notice | SBOM |
|---|---:|---:|---:|---|---|
| Lite | 21 | 18 | 18 | [notices](THIRD_PARTY_NOTICES.lite.md) | [SBOM](docs/SBOM.lite.cdx.json) |
| Standard | 29 | 18 | 18 | [notices](THIRD_PARTY_NOTICES.standard.md) | [SBOM](docs/SBOM.standard.cdx.json) |
| Full | 30 | 18 | 18 | [notices](THIRD_PARTY_NOTICES.full.md) | [SBOM](docs/SBOM.full.cdx.json) |

`docs/SBOM.cdx.json` is a byte-identical compatibility alias of the Full profile SBOM.

All profiles currently bundle affected OpenJPEG 2.5.4 ([OSV-2025-219](https://osv.dev/vulnerability/OSV-2025-219)) through Pillow, affected libheif 1.23.0 ([OSV-2020-2308](https://osv.dev/vulnerability/OSV-2020-2308), [OSV-2023-1129](https://osv.dev/vulnerability/OSV-2023-1129)) through pi-heif, and advisory-unknown PDFium 152.0.7947.0 through pypdfium2. See the profile notice and `docs/ADVISORY_REPORT.json` for applicability, remediation, residual risk, and authoritative sources.
The 18 versioned native records are not exhaustive: 18 known unversioned PDFium/libavif children remain advisory-unknown, and each SBOM's CycloneDX composition is `incomplete`.

These summaries are not legal advice. Redistributors must retain the full upstream license and notice texts shipped in wheel metadata.
