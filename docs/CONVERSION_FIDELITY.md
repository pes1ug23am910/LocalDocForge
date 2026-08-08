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
- `--preset llm` resolves to JPEG quality 85 and a 1568-px long-edge bound.
  Each page is rendered at up to the ordinary 150-DPI default, then only pages
  that would exceed the bound receive a lower per-page scale; smaller pages
  are not enlarged to fill the bound. A capped job carries `image-downscaled`
  (info). Explicit `--format`/`--quality` values replace those preset values;
  explicit `--dpi` requests fixed-DPI output and disables the pixel cap.
- Report details record the resolved format, configured quality, and applied
  quality (`null` for lossless PNG/TIFF) plus an ordered `dimensions` entry for
  every output (zero-based output index, source page/occurrence, actual pixel
  width/height, and effective DPI). The stable index corresponds to the same
  position in `report.outputs`, including when collision policy renames a
  published file. The preflight uses PDFium's upward pixel rounding and
  verifies the actual image edge before publication, preventing a nominal
  1568-px cap from producing a 1569-px image.
- Inputs with parser-reported structural syntax damage are refused rather
  than silently repaired by PDFium.

## pdf-to-md

`pdf-to-md` is text-layer extraction, not OCR and not semantic reconstruction.
It uses the text and geometry APIs in pypdfium2 5.12.1 / PDFium
152.0.7947.0, owns only one selected page at a time, and writes the requested
artifact explicitly as UTF-8 with LF line endings. Unicode is normalized to
NFC. For clean text formats, non-newline whitespace runs (including tabs,
form-feed, and NBSP) are collapsed to one ASCII space, trailing space and outer
blank lines are trimmed, and geometry supplies paragraph breaks. Inter-fragment
spacing is also heuristic: rectangles separated by at most 1 pt or 10% of the
smaller line height concatenate; a larger gap inserts one ASCII space. PDFium's mapped visible characters and
explicit line/hyphen boundaries are otherwise preserved: LocalDocForge performs
no silent dehyphenation, compatibility normalization, bidi repair, or guessed
ligature replacement.

Format fidelity and provenance:

- Markdown begins every selected occurrence with the exact anchor
  `<!-- ldf:page N -->` unless `--no-page-anchors` is selected. With anchors
  disabled, blank lines separate occurrences.
- TXT uses the exact `--- ldf:page N ---` anchor by default. With anchors
  disabled, one form-feed (`U+000C`) separates occurrences.
- JSONL has one LF-terminated object per selected occurrence and the exact keys
  `page`, `text`, `char_count`, and `has_text_layer`. The `page` key is always
  authoritative, so the page-anchor option is accepted for API/CLI shape parity
  but is ignored semantically for JSONL.
- Markdown and TXT, with or without structural page anchors, escape source
  lines matching either reserved syntax (`<!-- ldf:page N -->` and
  `--- ldf:page N ---`) so text cannot forge provenance. JSONL preserves both
  lookalikes unchanged as data.
- `has_text_layer` is based on raw PDFium character/text-object presence;
  `char_count` and `pages_with_text` use normalized non-whitespace extracted
  content. A whitespace-only text layer may therefore have
  `has_text_layer=true` and `char_count=0`. An image-only page is false.
  `char_count` equals Python's Unicode-code-point length of the normalized
  extracted page text before anchors/Markdown markup (and equals the JSONL
  record's text length), not the UTF-8 byte length or grapheme-cluster count.
  A single selected empty page with anchors disabled may therefore produce a
  valid zero-byte MD/TXT artifact; the report still carries its coverage and
  text-layer distinction.

Markdown reading order is a deterministic best-effort baseline: text rectangles
are ordered top-to-bottom, then left-to-right. Larger-font clustering may turn a
line into a heading, but that is explicitly a heuristic. Multi-column pages,
rotated or angled text, and RTL scripts can be ordered incorrectly. Tables are
flattened into ordinary text in this slice; conservative ruled-grid detection
can identify some cases, but no warning is proof that a page has no table.

Stable codes (at most one aggregate `fidelity_warnings` entry per code):

- `no-text-layer` — one or more selected pages have no PDF text objects. Use
  `pdf-to-images --preset llm` for vision input; OCR is not implemented.
- `headings-inferred` — Markdown headings were produced through font-size
  clustering rather than document semantics.
- `reading-order-uncertain` — columns, rotation, angled text, or RTL content
  makes the baseline reading order uncertain.
- `tables-flattened` — conservative ruled-grid detection suggests a table was
  emitted as flowed text; structured table extraction is deferred to S5.

The aggregate warning message reports how many selected occurrences were
affected. Exact attribution remains bounded in
`details.coverage.per_page[]`, whose ordered records are
`{"page": N, "char_count": N, "has_text_layer": bool,
"warning_codes": [...]}`. `details.coverage` also contains
`pages_total`, `pages_with_text`, `pages_with_text_layer`,
`char_count_min`, `char_count_median`, and `char_count_max`. These values count
selected occurrences, including repeats and reverse order. The report never
contains extracted text.

Per-page work is bounded before layout materialization. These are memory and
cardinality bounds, not a speed guarantee: per-page wall time scales with
PDFium text-rectangle count, and a dense page below the 50,000-rectangle cutoff
can consume much of `pdf-to-md`'s cooperative timeout. PDFium's raw character
count is compared conservatively with the remaining
`max_decompressed_bytes` budget (before whitespace cleanup) and with
`max_memory_bytes // 64`; a zero decompressed budget therefore rejects even a
whitespace-only text object. More than 50,000 text rectangles skips rectangle
layout and falls back to full-page bounded text with
`reading-order-uncertain`. The page-object inventory examines at most 4,096
objects, descends through at most 15 nested Form levels, and retains at most
512 horizontal and 512 vertical ruling candidates. If either bounded traversal
stops while PDFium reports zero characters and no text object has been found,
extraction refuses with `[reading-order-uncertain]` rather than falsely
asserting `no-text-layer`. Pages above one million raw
characters are also preflighted against the remaining output budget; the
streaming writer remains authoritative for exact UTF-8/framing bytes.

The related read-only `inspect` inventory reports no document text and skips
the font-size/angle sampling used only for extraction warnings and Markdown
headings. It still walks and extracts accepted PDFium text rectangles, so a
rectangle-dense page can be slow. A valid zero-page PDF has an empty
`page_text_stats` list; its `text_coverage` page
counters are zero and `char_count_min`, `char_count_median`, and
`char_count_max` are JSON `null` because no page population exists. Inspection
uses the same configured `max_pages`, cumulative `max_decompressed_bytes`, and
per-page `max_memory_bytes // 64` preflights as the text pipeline, so an
over-limit document is refused rather than materialized for statistics.

Pre-publication validation is format-specific. Every candidate must decode as
strict UTF-8 and carry the required coverage schema. Markdown/TXT anchor counts
must equal selected occurrence count when anchors are enabled; JSONL must have
exactly one record per occurrence, the exact schema above, and counts that agree
with the report. Validation failure blocks atomic publication. Unlike generated
PDF validation, this proves encoding, framing, provenance cardinality, and
report consistency — not linguistic correctness or visual equivalence.

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
- `image-downscaled` (info) — `convert-images --max-dimension`, either
  operation's `llm` preset, or the PDF per-page render cap shrank at least one
  image/render relative to its ordinary size; preset processing never
  upscales.
- `color-profile-converted` (info) — the sRGB conversion above happened.
- `color-profile-retained` (info) — a profile could not be parsed or
  converted, so it was kept in the output rather than silently dropped.

The `llm` preset (JPEG quality 85, long edge ≤ 1568 px, metadata stripped)
is sized so current AI assistants ingest the file without further
server-side downscaling; it is a convenience default, not a fidelity claim.

## Encrypted inputs and active content

- A supplied password authorizes reading an encrypted input. Generated PDFs,
  raster images, and extracted-text artifacts are not password protected;
  reports carry the critical
  `input-encryption-removed` security warning.
- CLI credential source does not change conversion semantics: global
  `--password-stdin` outranks `LDF_PASSWORD`, which outranks the hidden TTY
  prompt. One password is tried against all encrypted inputs in an invocation;
  differing passwords are refused rather than guessed or requested through an
  argv value.
- No PDF JavaScript, launch action, attachment, or form script is executed.
  Single-document rotate/crop can retain active objects, while page-moving
  operations warn when document-level active content is dropped.

## agent-brief (read-only diagnostics)

`ldf agent-brief` opens and converts no document, publishes no output, and
therefore introduces no fidelity warning code. It takes one normal live
capability-probe snapshot and reports which registry-defined operations are
implemented and whether their engines are currently available. Its guidance to
inspect `warnings[]` uses that term as shorthand for the real conversion-report
arrays, `security_warnings[]` and `fidelity_warnings[]`, whose entries carry
stable `code` values.

## Validation floor for every operation

Every generated PDF is reopened with pikepdf/libqpdf, parser syntax warnings
are rejected, expected page counts are checked, and pages are rendered through
PDFium (all pages for high-risk/small outputs, a documented sample for routine
large outputs). Zero-page and render failures block publication. Blank pages
are reported and may be legitimate; only callers that explicitly forbid an
all-blank result make blankness a hard failure. These checks do not establish
PDF/A or PDF/UA conformance. Generated images must decode. Generated
Markdown/TXT/JSONL follows the strict UTF-8, anchor/record-cardinality,
exact-schema, and coverage-consistency validator described above.
