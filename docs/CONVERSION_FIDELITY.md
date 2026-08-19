# Conversion Fidelity — honest notes

LocalDocForge never advertises "perfect conversion" or "zero quality loss".
This file records what each implemented operation preserves, what it loses,
and how losses are reported. Reports carry `fidelity_warnings` with stable
codes and an explicit assessment envelope. Warning silence is not treated as a
clean verdict outside that envelope.

## Machine-readable fidelity contract

Every conversion report carries both coverage and a derived status:

- `fidelity_coverage` is `none`, `partial`, or `complete`. It states how much of
  the operation's implemented fidelity contract was assessed, not how “good”
  the output is.
- `fidelity_status` is `unassessed`, `no-known-loss`, `review-required`, or
  `known-loss`. It is derived from coverage and warning impacts rather than
  supplied independently.

Derivation is deliberately worst-case and deterministic:

1. Any `known-loss` warning impact derives `known-loss`.
2. Otherwise, any `review` impact derives `review-required`.
3. Otherwise, `complete` coverage derives `no-known-loss` (advisories may still
   be present).
4. Otherwise, the result is `unassessed`.

Consequently, an empty `fidelity_warnings` array under `none` or `partial`
coverage does **not** mean no loss. `no-known-loss` means only that the declared
contract was completely assessed and no review/known-loss observation was
found; it does not claim perfect conversion, exact equivalence, or fitness for
every use. Each published `OutputArtifact` repeats the conservative run-level
status until an operation provides a narrower per-artifact assessor.

Every fidelity warning contains:

- stable `code` and human `message`;
- `severity` (`info`, `warning`, or `critical`) for presentation and urgency;
- `basis`: `declared` for behavior guaranteed by the operation design,
  `structural` for an observed source/output structure, or `heuristic` for an
  inference;
- `impact`: `advisory`, `review`, or `known-loss`, which drives the run-level
  status; and
- optional `page` and actionable `remedy` fields.

Severity and impact are intentionally separate. A security-urgent condition is
not automatically proof of fidelity loss, and a known transform can prove loss
without being a security event. Heuristic evidence may require review but
cannot claim `known-loss`; that verdict requires a declared or structural
basis. Legacy reports missing the new classification fields remain readable,
but every in-tree warning producer now supplies its classification explicitly.

Current coverage declarations are conservative. `rotate` and `crop` completely
assess their implemented contracts. Pipeline-backed API/MCP `inspect` is
complete/no-known-loss within the explicit scope “non-mutating structural
inventory; no document conversion,” so server-wide strict fidelity does not
disable it. Direct CLI `inspect` is read-only and has no publication step.
Page-moving operations and `compress` are partial. `images-to-pdf` is partial at
run level but completely measures its placement sub-contract for every decoded
frame. Other operations default to none until a broader assessor is implemented;
their warning impacts still derive `review-required` or `known-loss` when
applicable.

### Strict fidelity publication policy

Use global `--strict-fidelity` before a CLI command, or set
`LDF_STRICT_FIDELITY=true`, when every result other than
complete/no-known-loss must be refused:

```powershell
ldf --strict-fidelity rotate input.pdf --degrees 90 -o output.pdf
```

The operation may create a private candidate first. The pipeline then performs
candidate path containment, alias, duplicate-destination, collision, and total
size safety checks so strict policy cannot mask an unsafe candidate. It next
applies the fidelity gate, before content validation and publication. A refusal
publishes nothing and returns the failed report with `validation: null`: CLI
exit 4, HTTP API 422, or an MCP tool error whose structured content retains the
bounded report and a decisive warning.

API and MCP operation schemas also expose strict boolean
`strict_fidelity=true`. Per-call false or omission cannot weaken a process-wide
setting inherited from `LDF_STRICT_FIDELITY=true` or global
`--strict-fidelity`.

## Structural operations (merge, split, remove, extract, organize)

Preserved:
- Page content streams, resources, fonts, images — byte-faithful via pikepdf.
- Page-level annotation objects are copied with their pages. Internal link
  targets may cease to resolve after pages move and are reported separately.
- Document info dictionary (title/author/…): copied from the (first) source.
- Page boxes and rotation flags.

Not yet preserved when pages move between documents (reported per input):
- `docinfo-not-copied` — copying the selected document-information dictionary
  failed.
- `outlines-dropped` — bookmarks/outline trees.
- `form-fields-detached` — AcroForm field tree (widget appearances remain on
  pages; interactivity is lost).
- `attachments-dropped` — document-level embedded files.
- `xmp-metadata-dropped` — document-level XMP metadata.
- `page-labels-dropped` — roman/appendix numbering is not rebuilt.
- `tagged-structure-dropped` — tagged-PDF structure is not rebuilt.
- `named-destinations-dropped` — document-level named destinations are not
  rebuilt.
- `document-actions-dropped` — document-level open/additional actions and
  JavaScript name trees are not carried into page-moving outputs.
- `signature-semantics-dropped` — signature fields cannot remain valid after
  page copying. This is a **critical** warning.
- `internal-links-may-break` — copied internal link annotations may reference
  pages/destinations that moved or were omitted. This is a structural review
  warning with a link-verification remedy rather than an unconditional loss
  claim.
- `form-field-name-conflict` — merge detects identically named fields across
  inputs and marks the result for review.

`remove-pages` refuses a document when this build cannot safely rewrite page
references held by outlines, forms/signatures, page labels, open actions,
tagged structure, named destinations, or internal links. This conservative
policy prevents a successful-looking PDF with stale references.

`rotate` and `crop` operate on the original document object model in memory,
so outlines/forms/attachments and active content remain. Saving a modified
PDF nevertheless invalidates cryptographic signatures and does not retain
input password protection. `signature-invalidated` is reported both as a
critical security warning and as a structural/known-loss fidelity warning with
a re-signing remedy. `input-encryption-removed` remains a critical security
warning when applicable. Signature inspection covers catalog permissions,
AcroForm field/kid trees, widget parents, and page annotations. If malformed
signature-related structures prevent a complete determination,
`signature-presence-uncertain` is heuristic/review with a source-inspection
remedy; strict fidelity refuses that uncertainty instead of failing open.

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
- `resource-cleanup-skipped` (info; structural/advisory) — qpdf could not
  analyze resource usage safely, so unused-resource pruning was skipped for
  that document.
- `signature-invalidated` / `input-encryption-removed` — same critical
  semantics as rotate/crop: the rewrite invalidates cryptographic signatures
  and the output is not password protected. Signature invalidation also carries
  a structural/known-loss fidelity warning; encryption removal is a security
  warning.

Lossy presets (`balanced`, `aggressive`, `archival`) do not exist in this
build and are refused; nothing labelled "compress" silently degrades images.

## images-to-pdf

- EXIF orientation honored; multipage TIFF expands to one page per frame.
- Pages composed on a raster canvas at the configured `--dpi` (default 200),
  and every page is re-encoded through Pillow's PDF writer. The declared,
  known-loss `images-reencoded` warning is therefore always present;
  photographs go through one JPEG generation at quality 95 by default.
- `--page-size image` keeps the source pixel dimensions and avoids fixed-canvas
  resizing, but still performs that re-encode. Native page size is the remedy
  for unintended fit downscaling, not a lossless-copy mode.
- Alpha channels are flattened onto the background color (PDF pages here are
  opaque RGB), including `--page-size image`.
- Margins that leave no drawable area and canvases exceeding the pixel limit
  are refused before allocation/publication.

Run-level `fidelity_coverage` is `partial`: placement is exhaustively measured,
but this release does not yet inventory every source-image metadata/profile
transform. Because the declared re-encode is known loss, run-level status is
always `known-loss`; strict fidelity therefore refuses publication. The nested
`details.placement_analysis.coverage` is independently `complete`.

Placement analysis measures every decoded frame in the explicitly DPI-sensitive
space `output-raster-pixels/source-pixels`. The ratio describes retained raster
sample dimensions, not physical print scale: raising output DPI can retain more
source pixels and can change the measurement. `linear_scale` is the smaller of
the placed width/source width and placed height/source height ratios.

- `image-fit-downscaled` is structural/known-loss when a fixed-page frame's
  `linear_scale` is strictly below `0.5`; exactly `0.5` is not warned. Its
  remedy is `--page-size image` or a higher `--dpi` within resource limits.
- `image-aspect-distorted` is structural/known-loss when `--fit stretch`
  changes aspect ratio beyond `1.01`. Its remedy is `--fit fit` or
  `--fit center`.

The report records source/placed dimensions, axis/linear scales, aspect-ratio
distortion, one-based input index, and frame index without paths or image text.
Thresholds use the same six-decimal metric values exposed in the report, and
`aspect_distortion_warning_scope="stretch-only"` distinguishes stretch warnings
from harmless integer quantization under aspect-preserving fits. Detailed frame
entries are deterministically capped at 256. `frames_total`,
`severe_downscale_frames`, `aspect_distorted_frames`, and placement coverage
still include every frame; `frames_reported` and `truncated` disclose the bounded
detail list.

## pdf-to-images
- Rasterization at the requested DPI; vector content and text become pixels
  (inherently lossy in editability, faithful in appearance).
- JPEG output is lossy (quality configurable); PNG/TIFF lossless; WebP uses
  the configured quality.
- `--preset llm` resolves to JPEG quality 85 and a 1568-px long-edge bound.
  Each page is rendered at up to the ordinary 150-DPI default, then only pages
  that would exceed the bound receive a lower per-page scale; smaller pages
  are not enlarged to fill the bound. A capped job carries `image-downscaled`
  (info severity; structural/known-loss impact) with a higher-resolution/no-cap
  remedy. Explicit `--format`/`--quality` values replace those preset values;
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
  combined plain page text before anchors/Markdown markup. With an accepted
  table, pdfplumber supplies that region's cell text and PDFium supplies the
  ordinary regions; for JSONL, where table mode is unavailable, the count
  equals the record's text length. It is not the UTF-8 byte length or
  grapheme-cluster count.
  A single selected empty page with anchors disabled may therefore produce a
  valid zero-byte MD/TXT artifact; the report still carries its coverage and
  text-layer distinction.

Markdown reading order is a deterministic best-effort baseline: text rectangles
are ordered top-to-bottom, then left-to-right. Larger-font clustering may turn a
line into a heading, but that is explicitly a heuristic. Multi-column pages,
rotated or angled text, and RTL scripts can be ordered incorrectly.

Table reconstruction is opt-in through `--tables`/`PdfToMdOptions(tables=True)`
and valid only for Markdown; the default remains flowed PDFium text. The
pdfplumber strategy requires explicit horizontal and vertical ruling lines.
Only a bounded, non-overlapping rectangular grid with at least two rows and two
columns, nonempty header cells, nonempty body rows, complete cell geometry, and
matching region/cell character content is accepted. The first physical row is
rendered as an **inferred** GFM header; this is not a claim about PDF semantics.
Backslashes are doubled, `|` becomes `\|`, and newlines become `<br>` inside
GFM cells.

Each accepted region has one text source: pdfplumber cell text replaces wholly
contained PDFium fragments, while PDFium remains authoritative outside it.
Partial overlaps or coordinate disagreement reject the table, preventing the
two engines from duplicating or interleaving one region. Borderless grids,
merged/spanning cells, rotated pages, overlapping detections, dense vector
graphics, parser errors, resource-limit cases, and every other low-confidence
candidate remain flowed text. If bounded evidence identifies a rejected
candidate, `tables-flattened` is emitted. Absence of a table warning is not
proof that no table exists; it means only that the heuristics found no evidence.

Stable codes (at most one aggregate `fidelity_warnings` entry per code):

- `no-text-layer` — one or more selected pages have no PDF text objects. Use
  `pdf-to-images --preset llm` for vision input, or the separately engine-gated
  `ocr` command to create a best-effort searchable layer.
- `headings-inferred` — Markdown headings were produced through font-size
  clustering rather than document semantics.
- `reading-order-uncertain` — columns, rotation, angled text, or RTL content
  makes the baseline reading order uncertain.
- `table-fidelity-best-effort` — one or more accepted explicit-line grids were
  emitted as GFM. Verify the inferred header, cell order, and spanning-cell
  fidelity.
- `tables-flattened` (heuristic/review) — table output was disabled, or a
  candidate was emitted as flowed text because confidence, geometry, parser,
  or resource checks refused a rectangular GFM table.

For non-table codes, the aggregate warning message reports how many selected
occurrences were affected. The two table codes instead count emitted tables or
flattened candidates, because one selected occurrence may contain more than one
table region. Exact per-page attribution remains bounded in
`details.coverage.per_page[]`, whose ordered records are
`{"page": N, "char_count": N, "has_text_layer": bool,
"warning_codes": [...]}`. `details.coverage` also contains
`pages_total`, `pages_with_text`, `pages_with_text_layer`,
`char_count_min`, `char_count_median`, and `char_count_max`. These values count
selected occurrences, including repeats and reverse order. The report never
contains extracted text. `details.tables` contains only `requested`,
`engine_status` (`not-requested`, `available`, or `fallback`), `emitted`, and
`flattened_candidates`; these are status/counters, never cells or document
content.

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

Table work has a second fixed set of fail-safe bounds. Structured table finding
is skipped once the PDFium scan exceeds 8,192 PDF path segments. Structured
output is refused above 1,024 pdfplumber edges,
4,096 vertical×horizontal edge pairs, 32 detected tables per page, or 4,096
cumulative cells per page. Normalized table-cell UTF-8 is capped at 4 MiB per
page and further constrained by the remaining extraction-byte budget and
`max_memory_bytes // 64`. Crossing a bound keeps flowed text; it never publishes
a truncated table. These cardinality limits bound work and memory, not the wall
time of every third-party parser call.

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

## OCR (`ocr`)

OCR produces a best-effort machine-recognized text layer; it does not recover
the source document's semantic structure, fonts, reading order, or guaranteed
spelling. Every successful output therefore carries
`ocr-text-approximate`, even when the sampled extracted tokens match. The
operation writes ordinary PDF output, not PDF/A, with OCRmyPDF optimization
disabled. It does not claim byte identity or pixel identity with the input.

Mode fidelity is deliberately explicit:

- Default mode refuses the whole input when PDFium finds any page text object,
  including a whitespace-only layer. This avoids silently duplicating or
  replacing text in a document that may already be searchable.
- `--skip-text` OCRs only image-only pages. Existing page content is retained,
  but an optional sidecar omits text copied from skipped pages. That case carries
  `ocr-sidecar-omits-existing-text`.
- `--force-ocr` rasterizes and re-OCRs every page. Vector graphics, selectable
  text, annotations as rendered, and image compression can be flattened or
  re-encoded. Every force result carries critical `ocr-force-rasterized`.

Sidecars are normalized in a streaming pass to strict UTF-8 with LF newlines
and join the PDF in one validate-before-publish transaction. Engine diagnostics
are bounded and not copied verbatim into reports because they may contain
document text or private paths. A diagnostic indicating that an oversized or
timed-out page was skipped becomes critical `ocr-engine-page-skipped`; callers
must visually inspect that page. If skip markers cover every OCR-eligible page,
the operation fails rather than misreporting an image-only result as searchable;
partial skips may publish only with that critical warning. A truly blank scan
without a skip marker remains valid. Missing language packs and engine failures
are hard errors, not fidelity warnings.

Sidecars preserve literal form-feed (`U+000C`) page separators. An intentionally
skipped record may be the exact `[OCR skipped on page(s) N]` control marker (or
a contiguous `N-M` range); exact `[skipped page]` is the engine-failure control
record. Neither marker is recognized document text.

An OCRmyPDF zero exit is only a candidate. LocalDocForge reopens the PDF,
rejects syntax damage, checks the expected page count, renders every page with
PDFium, samples up to sixteen distinct normalized sidecar tokens, and requires
all sampled tokens to be extractable from the candidate text layer. That
all-page text pass first enforces the configured cumulative decompressed-text
and per-page extraction-memory ceilings; finding the expected tokens early does
not bypass later-page limits. Finite page geometry is checked against the
stricter of the image-pixel ceiling and a conservative memory-derived render
ceiling before standard all-page PDFium rendering allocates bitmaps. An
empty/whitespace-only sidecar is valid because a genuinely blank scan page has
no expected words. Non-marker sidecar content that yields no bounded token
(for example, punctuation alone or one overlong token) fails closed instead of
using that blank-scan exception.
Real-engine fixtures additionally require known rendered marker strings to be
extractable. No PDF or sidecar is published unless every validation passes.

OCR rewrites the document and invalidates existing cryptographic signatures;
both the critical security warning and structural/known-loss fidelity warning
`signature-invalidated` are emitted when a signature field is found. Passwords
only unlock input. Output is unencrypted and carries critical
`input-encryption-removed` when applicable.

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
- `image-downscaled` (info severity; structural/known-loss impact) —
  `convert-images --max-dimension`, either operation's `llm` preset, or the PDF
  per-page render cap shrank at least one image/render relative to its ordinary
  size; preset processing never upscales. Increase/omit the cap or choose a
  higher-resolution preset within resource limits when those pixels matter.
- `color-profile-converted` (info) — the sRGB conversion above happened.
- `color-profile-retained` (info) — a profile could not be parsed or
  converted, so it was kept in the output rather than silently dropped.

The `llm` preset (JPEG quality 85, long edge ≤ 1568 px, metadata stripped)
is sized so current AI assistants ingest the file without further
server-side downscaling; it is a convenience default, not a fidelity claim.

## Markdown → PDF (`md-to-pdf`)

Preserved within the implemented subset:

- headings, paragraphs, emphasis/strong text, ordered/unordered lists, block
  quotes, horizontal rules, inline/fenced code, and GFM tables;
- allowlisted `http`, `https`, `mailto`, and `tel` links; and
- contained relative single-frame raster images, decoded and normalized to PNG
  before Typst sees a neutral workspace name.

The selected A4/Letter/Legal dimensions, finite millimetre margin, and optional
table of contents are recorded in report details. Markdown source, link values,
image paths, and Typst diagnostics are never copied into the report. All
untrusted text/code/destinations/alt text are emitted as inert Typst strings;
the input is not treated as Typst source.

Known losses are explicit:

- `markdown-construct-dropped` — raw HTML markup, footnotes, math blocks,
  recognized backslash-delimited inline math, strikethrough, an unsafe/local
  link destination, or an unknown parser token
  was omitted. Each warning message and `details.dropped_constructs` entry names
  the construct and its 1-based source line, without copying its contents.
  Text that Markdown exposes separately between inline HTML tags remains text;
  no HTML styling or behavior is preserved. Dropping strikethrough or an unsafe
  link removes the formatting/destination but retains the enclosed label text.
  Details retain at most 256 distinct construct/line entries, followed by one
  aggregate summary when needed. `dropped_constructs_truncated`,
  `dropped_constructs_omitted`, and `dropped_construct_report_limit` make that
  bounded reporting explicit.
- `system-font-dependent` (info) — Typst uses embedded fonts first but may use
  installed system fonts for missing glyphs. Glyph choice, line wrapping, and
  page count can therefore differ across machines.

Detected footnotes and math are intentionally unsupported rather than passed
through as misleading notation. The CommonMark parser enables no math extension.
Dollar signs in ordinary CommonMark text are rendered literally. Remote images,
absolute/traversing paths, multi-frame images, Typst imports/plugins/packages,
and invalid/binary Markdown are refused instead of downgraded. Images are
re-encoded, so original image metadata, compression, color-profile bytes, and
animation are not preserved.
The converter offers no CSS/theme compatibility and does not claim pixel parity
with another Markdown renderer.

The input snapshot has a hard 16 MiB ceiling, further reduced by enabled input,
memory/512, and temporary/64 limits (4 MiB by default), and refuses more than
100,000 source lines, 250,000 parser tokens, or 256 image occurrences. These
are availability limits, not fidelity degradation: an over-limit document is
refused rather than partially rendered.

After compilation, `max_pages` is enforced and every page goes through the
normal pikepdf/libqpdf syntax/page checks and PDFium render validation before
atomic publication. Those checks establish structural/renderability fitness,
not PDF/A, PDF/UA, typography equivalence, accessibility, or semantic identity.

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

## agent-brief (metadata diagnostics)

`ldf agent-brief` opens and converts no document, publishes no output, and
therefore introduces no fidelity warning code. It takes one normal live
capability-probe snapshot and reports which registry-defined operations are
implemented and whether their engines are currently available. The
Ghostscript gate may convert a private synthetic one-page probe under a
validated local temporary root, then attempts to remove that scratch tree; it
never opens a user document or publishes the probe as an output. Cleanup
failure makes the gate unavailable but may leave OS-locked scratch residue. Its guidance to
inspect `warnings[]` uses that term as shorthand for the real conversion-report
arrays, `security_warnings[]` and `fidelity_warnings[]`, whose entries carry
stable `code` values.

The generated brief currently has seven gotchas: encrypted inputs;
command-level `--collision` placement after the subcommand; glob expansion;
warning/status interpretation; output fitness; strict fidelity; and the local
MCP surface. Its low-cost visual-review suggestion is a 110 DPI PNG spot-check.
It places global `--strict-fidelity` before the command, exposes
`--page-size A4|image` and command-level collision choices in usage, and points
agents to synchronous `ldf mcp`. Its warning guidance requires status and
coverage first, then stable warning code, basis, impact, and optional remedy.

## Validation floor for every operation

Every generated PDF permitted past the optional strict-fidelity gate is reopened
with pikepdf/libqpdf, parser syntax warnings are rejected, expected page counts
are checked, and pages are rendered through PDFium (all pages for high-risk or
small outputs, a documented sample for routine large outputs). Zero-page and
render failures block publication. Blank pages are reported and may be
legitimate; only callers that explicitly forbid an all-blank result make
blankness a hard failure. These checks do not establish PDF/A or PDF/UA
conformance. Generated images must decode. Generated Markdown/TXT/JSONL follows
the strict UTF-8, anchor/record-cardinality, exact-schema, and coverage-
consistency validator described above.
