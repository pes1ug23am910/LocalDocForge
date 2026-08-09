# Using LocalDocForge as a Python Library

The CLI and the local API are thin layers over a typed Python library. This
reference covers that library surface for scripting and embedding. Every code
original sample set below was executed against this repository on 2026-08-03;
the S4 extraction and S6 Markdown-rendering samples are backed by 2026-08-09
integration coverage.
Import paths are stable within 0.x only in the sense that
`docs/STATUS.md` records interface decisions; this is an early-alpha project.

The same guarantees apply as everywhere else: sources are never modified,
candidates are validated for their media type before an atomic publish (PDF
reopen/render, image decode, or strict text schema/provenance checks), reports
never contain document text or passwords, and operations
raise instead of guessing.

## Install

Any profile works for the core library (`pip install localdocforge` from a
checkout — see `docs/PACKAGING.md` for the hash-locked reproducible recipe).
The API service extras are only needed for `ldf web`. `md_to_pdf` also requires
a separately installed Typst executable at version 0.15.1 or newer; its live
engine probe remains the availability authority.

## The result object: `ConversionReport`

Every operation returns a `localdocforge.domain.models.ConversionReport`:

| Field | Meaning |
|---|---|
| `status` | `"success"` — anything else arrives via exception, not return |
| `job_id` | hex job id (also used in report filenames) |
| `engine`, `engine_version` | what actually executed, e.g. `pikepdf 10.10.0` |
| `inputs` / `outputs` | artifacts with `path`, `size_bytes`, `page_count`, `media_type` |
| `input_bytes` / `output_bytes`, `input_page_count` / `output_page_count` | totals |
| `security_warnings` | e.g. `crop-is-not-redaction`, `input-encryption-removed` (codes: `docs/CONVERSION_FIDELITY.md`) |
| `fidelity_warnings` | e.g. `outlines-dropped`, `compress-no-reduction` |
| `validation` | the pre-publication check results (`passed`, per-check details) |
| `details` | operation-specific data (e.g. compression statistics or bounded text coverage) |

`report.to_human()` renders the CLI's summary; `report.model_dump_json()` is
the CLI's `--json` payload (it is a Pydantic v2 model).

## Operations

All operations live under `localdocforge.operations`. PDF-structural ones
accept an `OrganizeOptions` (collision policy, explicit `Settings`, progress
callback, input password); image operations use their own options dataclasses
with the same fields plus format knobs.

### Merge, split, and page surgery (`operations.organize`)

```python
from localdocforge.domain.pages import PageRange
from localdocforge.jobs.workspace import CollisionPolicy
from localdocforge.operations.organize import OrganizeOptions, merge_pdfs

report = merge_pdfs(
    [scans_pdf, tiff_pdf],
    out_dir / "merged.pdf",
    page_ranges=[PageRange(spec="1-2"), None],  # pages 1-2 of the first, all of the second
    options=OrganizeOptions(collision=CollisionPolicy.RENAME),
)
assert report.status == "success"
```

Also available with the same shape: `split_pdf(input, output_dir, pages=…,
every=…)`, `remove_pages(input, output, pages)`, `extract_pages(input,
output, pages)`, `organize_pdf(input, output, order)`, `rotate_pages(input,
output, degrees=…, pages=…)`, `crop_pages(input, output, box=(x0, y0, x1,
y1), pages=…)`.

`PageRange(spec="1-5,9,12-end")` implements the documented grammar (`odd`,
`even`, `reverse`, `last-5`, descending ranges…); it raises `PageRangeError`
on nonsense and resolves against the real page count at execution time.

### Lossless compression (`operations.optimize`)

```python
from localdocforge.operations.optimize import compress_pdf

report = compress_pdf(merged_pdf, out_dir / "smaller.pdf")
stats = report.details["compression"]     # input/output bytes, reduction_percent…
assert report.details["render_compare"]["identical"] is True
```

Only `preset="lossless"` exists; planned lossy presets raise `PipelineError`.
`compare_page_renders(a, b, page_count)` — the pixel-comparison helper — is
public and usable on its own.

### Images ↔ PDF and image → image (`operations.images`)

```python
from localdocforge.operations.images import (
    ConvertImagesOptions, ImagesToPdfOptions, PdfToImagesOptions,
    convert_images, images_to_pdf, pdf_to_images,
)

images_to_pdf([photo1, photo2], out_dir / "album.pdf",
              options=ImagesToPdfOptions(page_size="A4", fit="fit"))

report = pdf_to_images(merged_pdf, out_dir / "pages",
                       options=PdfToImagesOptions(image_format="png", dpi=72))
page_files = [artifact.path for artifact in report.outputs]

# iPhone HEIC (decoded via pi-heif) → AI-assistant-ready JPEGs: EXIF
# orientation applied, Display P3 converted to sRGB, GPS/EXIF stripped,
# long edge bounded to 1568 px.
report = convert_images([photo_heic], out_dir / "ready",
                        options=ConvertImagesOptions(preset="llm"))
```

### PDF text extraction (`operations.text`)

```python
from localdocforge.domain.pages import PageRange
from localdocforge.operations.text import PdfToMdOptions, pdf_to_md

report = pdf_to_md(
    source_pdf,
    out_dir / "content.jsonl",
    options=PdfToMdOptions(
        output_format="jsonl",
        pages=PageRange(spec="1-5,9"),
        page_anchors=True,  # accepted but semantically ignored by JSONL
    ),
)
coverage = report.details["coverage"]
assert coverage["pages_total"] == 6
assert len(coverage["per_page"]) == 6

table_report = pdf_to_md(
    source_pdf,
    out_dir / "content.md",
    options=PdfToMdOptions(tables=True),
)
assert table_report.details["tables"]["requested"] is True
```

`PdfToMdOptions` fields are `output_format` (`md`, `txt`, or `jsonl`, default
`md`), optional `pages`, `page_anchors` (default true), `tables` (default false),
`password`, `collision`, `settings`, and `progress`. `tables=True` is valid only
with Markdown; pairing it with `txt` or `jsonl` raises `PipelineError` before
the input is opened. Markdown uses exact `<!-- ldf:page N -->` anchors;
TXT uses exact `--- ldf:page N ---` anchors or form-feed separators when
anchors are disabled. JSONL always has one object per selected occurrence with
keys, in order, `page`, `text`, `char_count`, `has_text_layer`.
MD/TXT, with or without structural page anchors, escapes source lines matching
either reserved marker syntax; JSONL preserves those lookalikes as ordinary
`text` data.

The report never contains the `text` values. `details.coverage` has the exact
keys `pages_total`, `pages_with_text`, `pages_with_text_layer`,
`char_count_min`, `char_count_median`, `char_count_max`, and `per_page`.
Each ordered `per_page` record is `{page, char_count, has_text_layer,
warning_codes}`. Aggregate `fidelity_warnings` contains at most one entry for
each stable extraction code, while the per-page lists preserve exact
attribution. `details.tables` contains only `requested`, `engine_status`,
`emitted`, and `flattened_candidates`; no table cells or document text enter the
report.

Table mode uses pdfplumber's explicit-line strategy and emits only complete,
bounded rectangular grids. The first physical row becomes the inferred GFM
header; backslashes and pipes are escaped and cell newlines become `<br>`.
PDFium supplies ordinary regions and pdfplumber supplies accepted table regions,
never both for the same region. Borderless, merged-cell, rotated, dense-vector,
overlapping, and other low-confidence candidates remain flowed text with
`tables-flattened` when detected. An emitted table carries
`table-fidelity-best-effort`; absence of either warning does not prove that no
table exists. See `docs/CONVERSION_FIDELITY.md` for the confidence and resource
limits.

### Markdown rendering (`operations.markdown`)

```python
from localdocforge.jobs.workspace import CollisionPolicy
from localdocforge.operations.markdown import MdToPdfOptions, md_to_pdf

report = md_to_pdf(
    notes_md,
    out_dir / "notes.pdf",
    options=MdToPdfOptions(
        paper="Letter",
        margin_mm=18,
        toc=True,
        collision=CollisionPolicy.RENAME,
    ),
)
assert report.output_page_count == report.outputs[0].page_count
```

`MdToPdfOptions` fields are `paper` (`A4`, `Letter`, or `Legal`), finite
non-negative `margin_mm` (default 20), `toc`, `collision`, `settings`, and
`progress`. The primary input must be strict UTF-8 and end in `.md` or
`.markdown`. The parser supports a bounded CommonMark subset plus the built-in
GFM table rule. Relative raster-image references are resolved beneath the
Markdown file's directory, signature checked, included in input-byte/alias
limits, and normalized to private neutral PNGs before Typst runs. API adapters
may pass `image_inputs={"diagram.png": uploaded_path}` to map sanitized sibling
uploads; every supplied asset must be referenced.

Preprocessing admits at most the lowest of 16 MiB, `max_input_bytes`,
`max_memory_bytes / 512`, and `max_temporary_bytes / 64` (4 MiB by default),
then enforces 100,000 source lines, 250,000 parser tokens, and 256 image
occurrences. It happens before `run_pipeline` creates the cooperative CLI job
clock, so those explicit cardinality/byte bounds are the in-process protection;
the API parent watchdog covers the complete worker call.

All source text, code, link destinations, and image alt text become escaped
Typst string literals. Imports/packages and remote, absolute, traversing, or
reparse-point image paths are refused. Unsupported raw HTML, footnotes, math,
strikethrough, and unknown tokens produce `markdown-construct-dropped` with
1-based source-line metadata. `system-font-dependent` records the remaining
font-fallback variance. Reports omit Markdown text, link values, and tool
diagnostics. Detailed dropped-construct metadata is capped at 256 distinct
construct/line entries plus one aggregate summary; callers can inspect
`dropped_constructs_truncated`, `dropped_constructs_omitted`, and
`dropped_construct_report_limit`. The candidate remains private until page
limits and the standard
full-PDF reopen/syntax/page-count/render validation pass.

### Read-only inspection

```python
from localdocforge.operations.organize import inspect_pdf

info = inspect_pdf(merged_pdf)          # dict: page_count, encrypted,
info["has_outlines"], info["has_javascript"]  # annotations, docinfo, …
info["page_text_stats"], info["text_coverage"]  # PDFium counts, no page text
```

For a valid zero-page PDF, `page_text_stats` is `[]`; `text_coverage` reports
zero page counters and `None` for character-count min/median/max. Pass
`settings=Settings(...)` to override the configured page, decompressed-text,
or memory limits; inspection refuses text statistics that exceed them.

## Error handling

Failures raise `localdocforge.pipelines.runner.PipelineError`; the failed
`ConversionReport` (when one exists) is attached as `error.report`, already
carrying the errors and any validation details.

```python
from localdocforge.pipelines.runner import PipelineError

try:
    merge_pdfs([only_one_pdf], out_dir / "never.pdf")
except PipelineError as error:
    print(error)                # "merge needs at least two inputs (…)"
    failed_report = error.report  # may be None for pure-argument errors
```

Special cases:

- `operations.organize.EncryptedInputError` (a `PipelineError`) — the input
  needs a password. Retry with `OrganizeOptions(password=…)`. Passwords are
  never written to reports.
- `engines.base.EngineUnavailableError` — no probed engine supports the
  operation (its `hints` carry install commands).
- `jobs.workspace.OutputCollisionError` — surfaces as the `__cause__` of a
  `PipelineError` when the collision policy is `FAIL`.

## Configuration without environment variables

Library callers should pass explicit `Settings` instead of mutating the
process environment:

```python
from localdocforge.config.settings import Settings
from localdocforge.operations.organize import OrganizeOptions

tight = Settings(limits={"max_pages": 2})        # nested models accept dicts
compress_pdf(big_pdf, out, options=OrganizeOptions(settings=tight))
# PipelineError: Inputs total 4 pages, over the configured limit of 2. …
```

`Settings()` reads `LDF_*` environment variables exactly like the CLI
(`docs/GETTING_STARTED_WINDOWS.md` §6 lists the knobs); constructor arguments
outrank the environment. `strict_offline=True` enables the documented
application-level path/network policy — not an OS firewall.

## Capability discovery (what `ldf doctor` uses)

```python
from localdocforge.engines.registry import default_registry

registry = default_registry()
available = {c.id for c in registry.capabilities() if c.available}
engines = registry.all_infos()   # EngineInfo: name, version, license, hints
```

A capability is `available` only when its pipeline is implemented **and** a
live engine probe passed — the same honesty gate as everywhere else.

## Persisting reports

```python
from localdocforge.reporting.writers import write_report_files

json_path, text_path = write_report_files(report, reports_dir, "compress-demo")
```

Writes `<basename>.report.json` and `.report.txt` — the same files
`--report-dir` produces. Reports name the user-selected files but never
contain document text or passwords.

## Progress and cancellation

Every options object accepts `progress` — a callable receiving
`domain.models.ProgressEvent` (`stage`, `current`, `total`, `message`). CLI
timeouts are cooperative (`ResourceLimits.timeout_seconds`); hard
cancellation of a runaway native parse exists only in the API's spawned
workers, not in-process (`docs/THREAT_MODEL.md`).

## Related documents

`docs/CLI.md` (grammar, exit codes, HTTP contract) ·
`docs/CONVERSION_FIDELITY.md` (warning codes) · `docs/ARCHITECTURE.md`
(pipeline lifecycle) · `docs/DEVELOPMENT.md` (adding operations)
