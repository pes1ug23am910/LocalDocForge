# Using LocalDocForge as a Python Library

The CLI and the local API are thin layers over a typed Python library. This
reference covers that library surface for scripting and embedding. Every code
sample below was executed against this repository on 2026-08-03 before being
written down. Import paths are stable within 0.x only in the sense that
`docs/STATUS.md` records interface decisions; this is an early-alpha project.

The same guarantees apply as everywhere else: sources are never modified,
candidates are validated (structural reopen + PDFium render) before an atomic
publish, reports never contain document text or passwords, and operations
raise instead of guessing.

## Install

Any profile works for the core library (`pip install localdocforge` from a
checkout — see `docs/PACKAGING.md` for the hash-locked reproducible recipe).
The API service extras are only needed for `ldf web`.

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
| `details` | operation-specific data (e.g. compression statistics) |

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

### Images ↔ PDF (`operations.images`)

```python
from localdocforge.operations.images import (
    ImagesToPdfOptions, PdfToImagesOptions, images_to_pdf, pdf_to_images,
)

images_to_pdf([photo1, photo2], out_dir / "album.pdf",
              options=ImagesToPdfOptions(page_size="A4", fit="fit"))

report = pdf_to_images(merged_pdf, out_dir / "pages",
                       options=PdfToImagesOptions(image_format="png", dpi=72))
page_files = [artifact.path for artifact in report.outputs]
```

### Read-only inspection

```python
from localdocforge.operations.organize import inspect_pdf

info = inspect_pdf(merged_pdf)          # dict: page_count, encrypted,
info["has_outlines"], info["has_javascript"]  # annotations, docinfo, …
```

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
