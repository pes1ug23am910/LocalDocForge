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
- Page labels (roman/appendix numbering) — not yet rebuilt.
- `form-field-name-conflict` — merge detects identically named fields across
  inputs and warns what that would mean.

`remove-pages` and `rotate`/`crop` operate on the original document object
model in memory, so outlines/forms/attachments of the *single* input remain
intact in the output; only cross-document page moves lose them today.

## rotate
Sets `/Rotate` relative to the page's existing rotation. Lossless.

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
  opaque RGB).

## pdf-to-images
- Rasterization at the requested DPI; vector content and text become pixels
  (inherently lossy in editability, faithful in appearance).
- JPEG output is lossy (quality configurable); PNG/TIFF lossless; WebP lossy
  per Pillow defaults at the configured quality.

## Validation floor for every operation
Every generated PDF is reopened structurally; risky outputs render every
page; blank/zero-page results are caught before publish. Page-count
expectations are asserted per candidate.
