# Conversion Fidelity — honest notes

LocalDocForge never advertises "perfect conversion" or "zero quality loss".
This file records what each implemented operation preserves, what it loses,
and how losses are reported. Reports carry `fidelity_warnings` with stable
codes; nothing is dropped silently.

## Structural operations (merge, split, remove, extract, organize)

Preserved:
- Page content streams, resources, fonts, images — byte-faithful via pikepdf.
- Page-level annotations and links (they live in the page tree).
- Document info dictionary (title/author/…): copied from the (first) source.
- Page boxes and rotation flags.

Not yet preserved when pages move between documents (reported per input):
- `outlines-dropped` — bookmarks/outline trees.
- `form-fields-detached` — AcroForm field tree (widget appearances remain on
  pages; interactivity is lost).
- `attachments-dropped` — document-level embedded files.
- `xmp-metadata-dropped` — document-level XMP metadata.
- `page-labels-dropped` — roman/appendix numbering is not rebuilt.
- `document-actions-dropped` — document-level open/additional actions and
  JavaScript name trees are not carried into page-moving outputs.
- `signature-semantics-dropped` — signature fields cannot remain valid after
  page copying. This is a **critical** warning.
- `form-field-name-conflict` — merge detects identically named fields across
  inputs and warns what that would mean.

`remove-pages` refuses a document when this build cannot safely rewrite page
references held by outlines, forms/signatures, page labels, open actions,
tagged structure, named destinations, or internal links. This conservative
policy prevents a successful-looking PDF with stale references.

`rotate` and `crop` operate on the original document object model in memory,
so outlines/forms/attachments and active content remain. Saving a modified
PDF nevertheless invalidates cryptographic signatures and does not retain
input password protection. Reports use the critical `signature-invalidated`
and `input-encryption-removed` security warning codes when applicable.

## rotate
Sets `/Rotate` relative to the page's existing rotation without re-encoding
page graphics. Calling it wholly "lossless" would be misleading because the
rewrite invalidates cryptographic signatures and removes input encryption.

## crop
Sets `/CropBox` only; `MediaBox` and content untouched. **Cropping is not
redaction** — every report carries the `crop-is-not-redaction` security
warning, and the CLI echoes it. Boxes are clamped to the page (`crop-clamped`
warning) and non-intersecting boxes are refused.

## compress (lossless preset)

Rewrites the document container it opened — the same in-memory object model —
so document-level structures travel with the file: outlines, form fields,
attachments, XMP metadata, page labels, and annotations are preserved, unlike
page-moving operations. What changes is representation only: generalized
filters (Flate/LZW/RLE/ASCII) are decoded and recompressed, object streams are
generated, and page resources qpdf proves unreferenced are pruned. DCT/JPX
image data is never decoded, re-encoded, or downsampled.

Verification beyond the standard floor: sampled pages of the candidate are
rendered through PDFium and compared pixel-for-pixel against the source; any
difference blocks publication (`render_compare` in the report records the
compared pages and maximum channel delta, which is 0 on success).

Codes:
- `compress-no-reduction` (info) — the output is not smaller; the input was
  already tightly compressed. Reported, never hidden.
- `resource-cleanup-skipped` (info) — qpdf could not analyze resource usage
  safely, so unused-resource pruning was skipped for that document.
- `signature-invalidated` / `input-encryption-removed` — same critical
  semantics as rotate/crop: the rewrite invalidates cryptographic signatures
  and the output is not password protected.

Lossy presets (`balanced`, `aggressive`, `archival`) do not exist in this
build and are refused; nothing labelled "compress" silently degrades images.

## images-to-pdf
- EXIF orientation honored; multipage TIFF expands to one page per frame.
- Pages composed on a raster canvas at the configured `--dpi` (default 200),
  so images are re-encoded (`images-reencoded` info warning); photographs go
  through one JPEG generation at quality 95 by default.
- `--page-size image` keeps the source pixel grid (no canvas compositing) but
  still re-encodes through Pillow's PDF writer.
- Alpha channels are flattened onto the background color (PDF pages here are
  opaque RGB), including `--page-size image`.
- Margins that leave no drawable area and canvases exceeding the pixel limit
  are refused before allocation/publication.

## pdf-to-images
- Rasterization at the requested DPI; vector content and text become pixels
  (inherently lossy in editability, faithful in appearance).
- JPEG output is lossy (quality configurable); PNG/TIFF lossless; WebP uses
  the configured quality.
- Inputs with parser-reported structural syntax damage are refused rather
  than silently repaired by PDFium.

## convert-images

Every output is a re-encode (`image-reencoded`, info; lossy for JPEG/WebP at
the configured quality, lossless for PNG/TIFF). HEIC/HEIF inputs decode
through the decode-only pi-heif engine; HEIF output is never offered.

Preserved:
- Pixel geometry after EXIF orientation is applied (the orientation tag is
  consumed, not carried forward pointing at unrotated pixels).
- Color appearance: pixels tagged with a non-sRGB ICC profile (iPhone photos
  are typically Display P3) are converted to sRGB before the profile is
  dropped, so viewers that assume sRGB see the intended colors.
- Alpha channels, for output formats that support them (PNG/WebP/TIFF).

Intentionally not preserved (defaults chosen for sharing, each reported):
- `metadata-stripped` (info) — EXIF metadata, explicitly including any GPS
  position, is removed by default. `--keep-metadata` retains EXIF; when that
  keeps GPS data the report carries the `location-metadata-retained`
  **security** warning instead.
- `xmp-metadata-dropped` (info) — XMP blocks are never carried into outputs,
  with or without `--keep-metadata`.
- `alpha-flattened` (info) — JPEG output composites transparency onto the
  chosen background color.
- `image-downscaled` (info) — `--max-dimension` (and the `llm` preset's
  1568 px bound) shrank at least one image; images are never upscaled.
- `color-profile-converted` (info) — the sRGB conversion above happened.
- `color-profile-retained` (info) — a profile could not be parsed or
  converted, so it was kept in the output rather than silently dropped.

The `llm` preset (JPEG quality 85, long edge ≤ 1568 px, metadata stripped)
is sized so current AI assistants ingest the file without further
server-side downscaling; it is a convenience default, not a fidelity claim.

## Encrypted inputs and active content

- A supplied password authorizes reading an encrypted input. Generated PDFs
  and raster images are not password protected; reports carry the critical
  `input-encryption-removed` security warning.
- CLI credential source does not change conversion semantics: global
  `--password-stdin` outranks `LDF_PASSWORD`, which outranks the hidden TTY
  prompt. One password is tried against all encrypted inputs in an invocation;
  differing passwords are refused rather than guessed or requested through an
  argv value.
- No PDF JavaScript, launch action, attachment, or form script is executed.
  Single-document rotate/crop can retain active objects, while page-moving
  operations warn when document-level active content is dropped.

## Validation floor for every operation
Every generated PDF is reopened with pikepdf/libqpdf, parser syntax warnings
are rejected, expected page counts are checked, and pages are rendered through
PDFium (all pages for high-risk/small outputs, a documented sample for routine
large outputs). Zero-page and render failures block publication. Blank pages
are reported and may be legitimate; only callers that explicitly forbid an
all-blank result make blankness a hard failure. These checks do not establish
PDF/A or PDF/UA conformance.
