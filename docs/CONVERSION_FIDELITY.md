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

## Encrypted inputs and active content

- A supplied password authorizes reading an encrypted input. Generated PDFs
  and raster images are not password protected; reports carry the critical
  `input-encryption-removed` security warning.
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
