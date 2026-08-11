# Engine Decisions

Decisions recorded when made; revisit points listed at the bottom.
Probed state on this machine is always visible via `ldf doctor`.

## Selected, installed, probed, and in use

| Engine | Version (this env) | License | Role | Why |
|---|---|---|---|---|
| pikepdf (libqpdf) | 10.10.0 / qpdf 12.3.2 | MPL-2.0 (pikepdf) / Apache-2.0 (qpdf) | Primary structural engine: merge/split/remove/extract/organize/rotate/crop/inspect | Mature, actively maintained, binds qpdf (the reference structural tool), preserves objects faithfully, handles encryption, robust against malformed files |
| pypdf | 6.14.2 | BSD-3-Clause | Installed library used for independent text assertions in tests; no production operation is wired to it | It remains probed for diagnostics but is not advertised or selected as a fallback |
| pypdfium2 (PDFium) | 5.12.1 / PDFium 152.0.7947.0 | Apache-2.0 OR BSD-3-Clause (wrapper); bundled PDFium uses BSD-style and `BUILD_LICENSES` notices | Rendering: validation renders and pdf-to-images; text/geometry extraction for pdf-to-md and inspect coverage | Chrome's widely deployed PDF engine: robust on hostile files, exposes page-scoped text rectangles/font geometry without another runtime dependency, permissive license, abi3 wheels |
| pdfplumber | 0.11.10 | MIT | Opt-in, Markdown-only explicit-line table detection and cell extraction for `pdf-to-md --tables` | Its line strategy exposes table/cell geometry without an AGPL dependency. LocalDocForge adds strict rectangular confidence and resource bounds, uses pdfplumber text only inside an accepted table region, and falls back to PDFium flowed text rather than emitting a doubtful table |
| Pillow | 12.3.0 | MIT-CMU; bundled codecs have per-component terms | Image decode/encode, images-to-pdf composition, convert-images transcoding | The standard Python imaging library; built-in decompression-bomb guard which we wire to `ResourceLimits.max_image_pixels` |
| pi-heif (libheif) | 1.4.0 / libheif 1.23.0, libde265 1.1.1 | BSD-3-Clause wrapper; LGPL-3.0-or-later libheif + libde265 | HEIF/HEIC **decode-only** Pillow plugin: iPhone photo input for convert-images and images-to-pdf | The decode-only distribution of pillow-heif — its wheels bundle no GPLv2 x265 encoder, keeping the runtime license ceiling at LGPLv3; the full pillow-heif package is a dev-profile fixture-encoding tool only |
| MCP Python SDK | 1.28.1 | MIT SDK; exact closure remains weak-copyleft-or-lighter | Local `ldf mcp` JSON-RPC server over UTF-8 stdio | The official SDK provides maintained protocol models, negotiation, error mapping, and stdio lifecycle behavior for a modest reviewed closure; HTTP/SSE/auth features remain unused |
| Typst | 0.15.1 | Apache-2.0 | Separately installed executable for Markdown→PDF; not bundled in Python profiles | Fast deterministic PDF generation, explicit project root, dependency manifest, bounded subprocess runner, and a permissive license. Availability requires a parseable version ≥0.15.1; generated source uses only application-controlled code and escaped strings |

Rationale for the split: structural edits (pikepdf) and rendering/text
extraction (PDFium) are different failure domains; no single library is trusted for
both. A pypdf production fallback remains a future implementation task; an
installed library alone is not reported as an executable operation engine.

### MCP SDK dependency decision (S8)

The official `mcp` SDK was selected instead of a hand-written JSON-RPC subset.
Repo-pinned uv 0.11.26, using the global `exclude-newer = 2026-07-19`
cutoff, resolves stable `mcp==1.28.1`; later stable 1.29.0 and 2.0.0 releases
postdate that cutoff, and the resolver does not select pre-release 2.0 builds.
The reviewed SDK supports protocol versions `2024-11-05`, `2025-03-26`,
`2025-06-18`, and `2025-11-25`, with `2025-11-25` as its latest generation.
LocalDocForge constrains the SDK to `>=1.28.1,<2`; an SDK major, cutoff, or
supported-protocol change requires a fresh compatibility, closure, advisory,
and framing review. It also declares `pywin32>=311; sys_platform == 'win32'`
directly because first-party MCP stdio and shared worker-spawn code import
those bindings; runtime correctness must not rely only on the SDK's transitive
platform marker.

The ten new universal-lock nodes and their compatible CPython 3.14 / Windows
x86-64 wheel sizes are:

| Package | License conclusion | Wheel bytes |
|---|---|---:|
| attrs 26.1.0 | MIT | 67,548 |
| httpx-sse 0.4.3 | MIT | 8,960 |
| jsonschema 4.26.0 | MIT | 90,630 |
| jsonschema-specifications 2025.9.1 | MIT | 18,437 |
| mcp 1.28.1 | MIT | 222,620 |
| PyJWT 2.13.0 | MIT | 31,274 |
| pywin32 312 | BSD-3-Clause AND HPND AND LGPL-2.1-or-later AND MIT AND Python-2.0.1 | 7,024,157 |
| referencing 0.37.0 | MIT | 26,766 |
| rpds-py 2026.6.3 | MIT top level; supplier Cargo expressions are MIT/Apache-2.0/Unicode-3.0 combinations | 220,380 |
| sse-starlette 3.4.5 | BSD-3-Clause | 16,518 |

That lock delta is 7,727,290 bytes (7.369 MiB), 90.9% of it pywin32. The
complete standalone SDK closure on this platform is 31 packages and 15,341,779
wheel bytes (14.631 MiB). Relative to the previous profile locks, MCP adds 20
packages / 8,537,799 bytes (8.142 MiB) to Lite and 13 packages / 8,012,880
bytes (7.642 MiB) to Standard and Full; the latter already carried HTTPX and
the ASGI stack. `cryptography==50.0.0` and `pdfminer-six==20260107` do not move.
Exact-tag and installed-wheel review found no license above the program's
weak-copyleft ceiling. The pywin32 composite license directory and MAPI notice
must be preserved. The rpds-py supplier SBOM lists 15 required Cargo children
but no child copyright/license texts, so notice coverage and CycloneDX
composition remain explicitly incomplete under the existing native-boundary
convention.

For stdio-only v1, the installed HTTP/SSE/JWT portion is dormant overhead;
calls are synchronous and serialized with no progress streaming. Even so, the
closure is reasonable compared with implementing and maintaining negotiation,
typed protocol messages, lifecycle/cancellation semantics, and JSON-RPC error
behavior by hand. The stdio surface must not silently enable the SDK's network
transports or HTTP authorization features.

PDF→Markdown/text uses PDFium's page-scoped text and geometry APIs. It was
chosen because PDFium is already shipped, probed, worker-contained for API
jobs, and licensed Apache-2.0/BSD-3-Clause; it supports deterministic
one-page-at-a-time extraction without expanding the dependency closure.
Layout and heading reconstruction remain documented heuristics. **PyMuPDF and
pymupdf4llm are banned from core**: linking/importing their AGPL runtime would
exceed this project's weak-copyleft-or-lighter license ceiling. They are not a
fallback and are not optional adapters. The opt-in pdfplumber path emits only
confident explicit-line rectangles. PDFium remains the source for ordinary
regions; within an accepted table region, pdfplumber alone supplies cell text,
so the two extractors are never interleaved for the same content.

## Other optional executable probes (capability availability varies)

| Engine | License | Role | Install hint (Windows) |
|---|---|---|---|
| qpdf CLI | Apache-2.0 | Repair second-opinion, JSON introspection | `winget install qpdf.qpdf` |
| Tesseract | Apache-2.0 | Implemented OCR recognition engine; ≥4.1.1 probe and requested language packs required | `winget install UB-Mannheim.TesseractOCR` |
| OCRmyPDF | MPL-2.0 core; bundled Occulta font Apache-2.0, Noto Sans font OFL-1.1, and sRGB profile Zlib | Locked ≥17.8.1 OCR orchestration executable; ordinary PDF output, one worker, optimization disabled | Shipped in LocalDocForge's locked Python dependency profiles (+ separate Tesseract and Ghostscript) |
| Ghostscript | AGPL-3.0 / commercial | OCRmyPDF-mediated live OCR gate; it is never a direct LocalDocForge child. Also a possible future PDF/A engine | Install 64-bit Ghostscript from the official Artifex release page; never bundled |
| LibreOffice | MPL-2.0 | Office↔PDF in isolated headless mode | `winget install TheDocumentFoundation.LibreOffice` |
| Pandoc | GPL-2.0+ | Markdown/Office conversions (invoked, not linked) | `winget install JohnMacFarlane.Pandoc` |
| veraPDF | GPL-3.0+ / MPL | Authoritative PDF/A validation | installer from verapdf.org |

## OCRmyPDF / Tesseract / Ghostscript boundary

The implemented `ocr` operation selects locked OCRmyPDF ≥17.8.1 as its primary
engine and requires live Tesseract ≥4.1.1 (excluding upstream-incompatible exact
5.4.0) and an OCRmyPDF-mediated Ghostscript compatibility probe as secondary
gates. On the 2026-08-11 Windows development host, OCRmyPDF 17.8.1,
Tesseract 5.4.0.20240606 (`eng`, `osd`), and Ghostscript 10.07.1 all pass their
live probes, so the OCR capability is available there.

LocalDocForge launches only the OCRmyPDF console entry point for conversion and
for Ghostscript compatibility probing. Its hardened executable allowlist
deliberately excludes direct Ghostscript execution. The compatibility probe asks
OCRmyPDF to process a private 64×64 synthetic image PDF with null OCR, the
Ghostscript rasterizer, lossless PDF/A-2, one job, and optimization disabled.
Success requires exit 0, a link-free workspace under 16 MiB, and a structurally
valid one-page output under 4 MiB. OCRmyPDF performs the Ghostscript ≥9.54
check, rasterization, and PDF/A generation. The probed OCRmyPDF, Tesseract, and
Ghostscript paths are rebound immediately before launch; the child PATH contains
only the unambiguous Tesseract/Ghostscript directories. Windows discovery
requires the native `tesseract.exe` and `gswin64c.exe` names and additionally
uses `NoDefaultCurrentDirectoryInExePath=1` and child-only `PATHEXT=.EXE`.
Thus the platform's
Ghostscript executable (`gswin64c.exe` on Windows) is an OCRmyPDF child, never
a LocalDocForge child. LocalDocForge never imports, links,
bundles, redistributes, or directly invokes Ghostscript. Document conversion
requests ordinary `pdf` output with optimization 0, so it does not request
OCRmyPDF's PDF/A conversion or optional lossy optimizer paths.
The live result is cached only within one registry instance; each new CLI
process reruns the bounded probe so an earlier installation result cannot stale.

OCRmyPDF itself is shipped as a locked MPL-2.0 dependency and is executed out
of process. Its wheel also contains the Apache-2.0 Occulta font, OFL-1.1 Noto
Sans font, and Zlib-licensed sRGB profile; those asset grants and notices are
represented in release artifacts rather than relying on the wheel's
MPL-only core metadata. Its new closure remains at weak copyleft or lighter.
The fpdf2 renderer is LGPL-3.0-only, img2pdf is LGPL-3.0-or-later, FontTools
includes MIT/BSD-3-Clause/Apache-2.0 code, and uharfbuzz's Apache-2.0 wrapper
uses the already inventoried HarfBuzz 14.2.1 native component.

## Rules
- An installed binary alone never lights a feature: `supported_operations()`
  stays empty until the pipeline using it lands with tests.
- External tools run only through the hardened subprocess runner
  (allowlist in `security/subproc.py`).
- The selected primary operation engine name + version is recorded in every
  `ConversionReport`. Imported secondary parsers such as pdfplumber are pinned
  in the release locks/SBOM; their safe status and counters may appear in report
  details without replacing the primary engine identity.
- `--engine` lets users override selection where multiple engines support an
  operation (registry enforces support + availability).

## Revisit points
- Compression (Phase 2): pikepdf image recompression vs Ghostscript
  pipelines — decide with benchmarks and quality-floor checks.
- Markdown→PDF follow-up: Typst is the implemented primary. Revisit themes and
  a WeasyPrint fallback only with equivalent isolation, licensing, and output
  validation evidence; Typst's project root is defense in depth, not an OS
  filesystem or network sandbox.
- PDF→Markdown follow-up: evaluate richer borderless and merged-cell semantics
  only with equally conservative confidence and resource thresholds. Keep
  PDFium text as the source for ordinary regions; do not interleave competing
  text engines or weaken the PyMuPDF ban.
- Semantic PDF→DOCX (Phase 2/3): candidate pdf2docx; verify license and
  output honesty before adoption.
