# LocalDocForge — Architecture

## Layering

```text
┌────────────────────────────────────────────────────────────┐
│ interfaces: cli/ (Typer, shipped)                          │
│             api/ (FastAPI, shipped; synchronous jobs)      │
│             minimal HTML status shell (shipped)            │
│             full browser job UI (planned)                  │
├────────────────────────────────────────────────────────────┤
│ operations/  merge, split, rotate, crop, image conversion  │
│   build an execute() closure and hand it to the runner     │
├────────────────────────────────────────────────────────────┤
│ pipelines/runner.py — shared job lifecycle                 │
│   sniff/limits → workspace → execute → validate all →      │
│   publish each → report → cleanup                          │
├──────────────┬─────────────────────┬───────────────────────┤
│ engines/     │ validation/         │ reporting/            │
│ adapters,    │ pikepdf syntax +    │ JSON + human report   │
│ probes, and  │ structure; PDFium   │ writers               │
│ capability   │ render; image decode│                       │
│ gating       │                     │                       │
├──────────────┴─────────────────────┴───────────────────────┤
│ jobs/ private workspaces, publication, cleanup             │
│ security/ sniffing, containment, filenames, subprocesses  │
├────────────────────────────────────────────────────────────┤
│ domain/ typed models, reports, resource limits, ranges     │
│ config/ LDF_* settings and strict-offline path policy      │
└────────────────────────────────────────────────────────────┘
```

## Key contracts

### Engine adapters (`engines/base.py`)

`EngineAdapter.probe() -> EngineInfo` must return unavailable data rather than
raise. `supported_operations()` declares operation ids. The registry resolves
`engine_for(operation, preferred)` only from probes that passed. Operations,
not interfaces, own document-engine calls.

External executable presence is not an implementation claim. For example,
Typst may probe successfully while Markdown-to-PDF remains unavailable because
its capability implementation bit is false.

### Capability honesty (`engines/registry.py::CAPABILITY_SPECS`)

A capability is available only when both conditions hold:

1. its implementation bit is true in the same code/test slice; and
2. a compatible engine probe succeeds at runtime.

The CLI, API, and status shell consume that same registry result. Missing
engines and planned capabilities remain data, not placeholder actions.

### Pipeline lifecycle (`pipelines/runner.py`)

1. **Input boundary.** In strict mode, recognizable network paths are refused
   before file inspection. `require_media_type` uses leading bytes rather than
   extensions. Input byte totals are cumulative across the job.
2. **Early resource inventory.** Page counts are collected where possible and
   checked cumulatively. Encrypted PDFs are counted again by the operation
   after password-based opening, so encryption cannot bypass the page cap.
3. **Private workspace.** `JobWorkspace` creates an exclusive, constrained
   `ldf-job-<uuid>` directory. Candidate and temporary paths are re-contained
   beneath it.
4. **Execution.** The operation writes candidate files only to the workspace,
   emits progress events, and calls cooperative cancellation/timeout checks
   between meaningful units of work. This is not preemptive interruption of a
   native parser call.
5. **Pre-publication boundaries.** Candidate paths and destinations are
   canonicalized once; strict network-output policy, output-root containment,
   duplicate destinations, and input/output aliases are checked. Aggregate
   candidate bytes are compared with `max_output_bytes`. This check occurs
   after candidate generation, so it limits publication rather than acting as
   a workspace filesystem quota.
6. **Validate every candidate.** PDFs must be nonempty, reopen through pikepdf,
   contain at least one page, have no reported parser syntax warning, match an
   expected page count when supplied, and render through PDFium. Selected
   high-risk candidates render every page; routine candidates render at most
   20 evenly sampled pages. Blank-page metrics are recorded, but blank output
   is not rejected unless a caller explicitly requests that policy. Generated
   images must fully decode.
7. **Publish.** No destination is touched until every candidate passes. Each
   candidate is copied to a hidden staging file in its destination directory
   and fsynced. Overwrite uses `os.replace`; fail and rename use `os.link` as an
   atomic no-clobber operation. Each final name is individually atomic. For a
   handled later-artifact failure, newly published paths are removed and
   overwritten files are restored from private backups where possible. This is
   best-effort rollback, not a multi-file transaction across a crash.
8. **Report and cleanup.** `ConversionReport` records status, engine/version,
   artifact metadata, counts, elapsed time, stable security/fidelity warnings,
   validation checks, details, and strict-offline state. It intentionally omits
   document text and passwords. Workspace removal is retried on success,
   failure, and cancellation; an incomplete removal adds a critical warning.
   Startup sweeps CLI workspaces older than 24 hours.

### Resource-limit coverage

Implemented operations wire aggregate input/output bytes, PDF page counts,
image pixels, decompressed image bytes, and cooperative timeout checks.
PDF-to-image export also checks rendered pixels and output bytes incrementally.
The API caps the received multipart body, cumulative uploaded bytes, file
count, field count, and field size.

`max_archive_entries`, `max_archive_expansion_ratio`, and
`max_subprocesses` are model fields reserved for future pipelines; they are not
evidence that archive/Office processing or subprocess concurrency control is
implemented. There is currently no hard per-job memory quota or preemptive
timeout for in-process native libraries.

### API lifecycle (`api/app.py`)

`ldf web` defaults to `127.0.0.1`. A non-loopback bind requires
`--allow-nonlocal`; strict-offline mode overrides and refuses that option. The
application additionally checks the request Host unless nonlocal mode was
explicitly selected.

At process startup the API creates a private `api-data/ldf-api-<uuid>` session
root. Each request job receives contained `in/` and `out/` directories beneath
a random job id. Upload inputs are removed after success. Failed/cancelled jobs
remove their whole job root. Successful outputs remain available by job id and
artifact index until DELETE, eviction above 50 remembered jobs, or graceful
shutdown. A later startup best-effort removes crashed API sessions older than
24 hours. Cleanup failure is surfaced rather than silently dropping the only
reference to retained files.

Every `/api` route requires the per-session `X-LDF-Token` header. A cookie is
set for the status page but is insufficient for API authorization. The server
emits no CORS grant and adds CSP, no-store, frame, MIME-sniffing, referrer, and
browser-permission restrictions. Public report serialization changes artifact
paths to basenames and recursively scrubs the server-private root.

Jobs execute synchronously inside the Uvicorn process. A background queue,
worker isolation, concurrency/rate caps, progress streaming, and cancellation
endpoints are planned rather than implied by the job-shaped API.

### Error model

Operations raise `PipelineError` with the failed `ConversionReport` where one
exists. `EncryptedInputError` allows an interactive CLI password prompt and a
single retry. Interfaces map documented cases to stable exit codes or bounded
HTTP error responses. Unexpected API exceptions return a generic 500 response
without exception values, private paths, or parser details.

## Runtime and state characteristics

- Current structural and image work runs in-process through
  pikepdf/libqpdf, PDFium, and Pillow. Those libraries execute with the user's
  privileges. Optional executable probes and future pipelines must use the
  allowlisted subprocess runner.
- The package has no shipped outbound network client, telemetry, update check,
  or remote browser asset. Strict mode adds application-level rejection of
  recognizable network filesystem paths and non-loopback serving; it is not an
  OS network sandbox.
- CLI state is ephemeral except user-requested outputs and optional report
  files. API job history is memory-only, while successful output bytes occupy
  the private session directory for the lifecycle described above.
- The minimal HTML page is a capability/status shell. A React/TypeScript job UI,
  local PDF.js previews, camera/scanner flow, and browser storage design are not
  shipped.
