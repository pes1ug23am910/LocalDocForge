# LocalDocForge — Architecture

## Layering

```text
┌────────────────────────────────────────────────────────────┐
│ interfaces: cli/ (Typer, shipped)                          │
│             api/ (FastAPI, shipped; worker-backed jobs)    │
│             minimal HTML status shell (shipped)            │
│             full browser job UI (planned)                  │
├────────────────────────────────────────────────────────────┤
│ operations/  organize, edit, compress, image conversion    │
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
   between meaningful units of work. CLI operations currently execute this
   lifecycle in-process. API operations execute it in a fresh spawned worker,
   so the parent can preempt a native parser by terminating the contained
   process tree.
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
The API admits a request before multipart parsing, creates a random
`.transport-*` spool beneath the private API session, and applies cumulative
upload/file/field bounds. The aggregate transport limit is the lower of the
non-disableable API upload ceiling, the enabled job input limit, and the enabled
temporary-byte limit. Upload handles are closed and the transport root is
removed before worker ownership begins. A parent wall-clock watchdog surrounds
every spawned job.

On Windows each worker is assigned to a fail-closed Job Object before the
document-processing gate opens. The object applies kill-on-close, job memory,
job CPU time, and active-process limits. Once that boundary exists, termination
checks `TerminateJobObject` and queries Job accounting until `ActiveProcesses`
is zero; only then is `process_tree_exit=verified_empty` published. If the
bootstrap leader exits before Job assignment, the document gate remains
`never_opened` and the narrower `pre_gate_leader_verified` proof is reported,
never an empty-Job claim. Any other unverifiable exit fails closed.

The portable implementation creates a POSIX session/process group and applies
available `RLIMIT_AS`, `RLIMIT_CPU`, and `RLIMIT_FSIZE`; Linux additionally
monitors descendant count through `/proc`. Because `RLIMIT_FSIZE` (derived
from `max_output_bytes`) can fire before the sampled parent monitor, both of
its surfaces are classified as the output limit rather than an anonymous
crash: a `SIGXFSZ`-killed worker is recognized by its exit signal, and on
hosts where `SIGXFSZ` is inherited-ignored (observed on WSL and GitHub's
Ubuntu runners) the child reports the resulting `EFBIG` write failure as a
typed limit message over IPC. Repository-launched external tools
remain in that group, but arbitrary same-user code can call `setsid()` and
escape it. These POSIX/macOS paths were not executed in the Windows-only
2026-07-20 checkpoint and are not cross-platform pass evidence.

The parent also samples the contained job tree for aggregate temporary and
output bytes. Those directory monitors can overshoot between samples, macOS has
no portable child-count control here, and `RLIMIT_AS` is not claimed as a
reliable macOS memory ceiling. None of these limits turns the worker into a
filesystem or network sandbox.

`max_archive_entries` and `max_archive_expansion_ratio` remain reserved for
future archive/Office pipelines. `max_subprocesses` is now enforced for API
workers where the platform mechanism above supports it. CLI native parsing
remains in-process and its timeout checkpoints remain cooperative.

### API lifecycle (`api/app.py`)

`ldf web` defaults to `127.0.0.1`. A non-loopback bind requires
`--allow-nonlocal`; strict-offline mode overrides and refuses that option. The
application additionally checks the request Host unless nonlocal mode was
explicitly selected.

At process startup the API creates a private `api-data/ldf-api-<uuid>` session
root and holds an external OS-released exclusive lease for its lifetime. Each
request job receives contained `in/` and `out/` directories beneath a random job
id. Upload inputs are removed after success. Failed/cancelled jobs remove their
whole job root. Successful outputs remain available by job id and artifact index
until DELETE, eviction above 50 remembered jobs, or graceful shutdown. A later
startup removes crash residue only when it can acquire the corresponding
external lease; a held, missing, or unreadable lease is preserved fail-closed.
Cleanup failure is surfaced rather than silently dropping the only reference to
retained files.

Every `/api` route requires the per-session `X-LDF-Token` header. A cookie is
set for the status page but is insufficient for API authorization. The server
emits no CORS grant and adds CSP, no-store, frame, MIME-sniffing, referrer, and
browser-permission restrictions. Public report serialization changes artifact
paths to basenames and recursively scrubs the server-private root.

Every conversion is dispatched to a fresh multiprocessing `spawn` child; the
long-lived API process performs only admission, request-contained bounded
multipart transport, filename/form validation, and result serving. The child
cannot start document
parsing until the parent has established Windows Job Object or POSIX process-
group containment. Its inherited environment is reduced, temporary/home paths
are redirected into the job tree, Python and native stdout/stderr are discarded,
and IPC is JSON-framed and capped. Passwords cross into the child only when an
operation requires one and are cleared from the parent record at completion;
they never cross back in IPC.

The manager enforces a bounded queue, global worker count, per-client active-job
cap, and sliding-window submission rate. The legacy request flow waits for the
worker and returns `201`; `Prefer: respond-async` or `?async=true` returns `202`.
Job detail, bounded progress events, output download, cancellation, and delete
endpoints are available. Cancellation, parent wall timeout, worker crash, IPC
failure, and shutdown terminate the contained tree. The manager does not set
the terminal event or release admission accounting until tree-exit verification
and finalization complete. Generated paths are validated and private inputs,
scratch, and worker temp are removed before outputs and `success` are published
together under the job lock. A successful download acquires an active-download
lease before `FileResponse` opens the file and releases it only after streaming
closes; DELETE and eviction refuse or defer while that lease is active. Running,
failed, or cancelled output is not exposed. Failed/cancelled jobs remove their
whole root. Incomplete containment, output validation, or cleanup becomes a
generic terminal failure and a critical report warning rather than being
silently ignored.

### Error model

Operations raise `PipelineError` with the failed `ConversionReport` where one
exists. The CLI resolves an explicitly selected stdin credential lazily and
once, after subcommand parsing but before password-capable operation setup;
this leaves help paths non-consuming. `EncryptedInputError` then lets the CLI
apply its configured stdin/environment credential or perform one hidden
interactive retry. POSIX uses `isatty()` for that decision; Windows additionally
requires `GetConsoleMode` to accept the stdin OS handle, so NUL and other
non-console character devices cannot enter an unanswerable prompt. Missing
credentials in a non-interactive invocation map to usage exit 2. Interfaces map
documented cases to stable exit codes or bounded
HTTP error responses. The worker boundary generalizes parser-derived errors,
scrubs paths/passwords from reports and progress, and validates every IPC
message. Unexpected API exceptions return a generic 500 response without
exception values, private paths, document fragments, or parser details.

## Runtime and state characteristics

- CLI structural and image work runs in-process through pikepdf/libqpdf,
  PDFium, and Pillow. API work uses one contained process per job. Both still
  execute with the user's filesystem authority: process/resource containment
  limits crashes and denial of service but is not a restricted-token,
  filesystem, or kernel sandbox. Optional executable probes and future
  pipelines must use the allowlisted subprocess runner.
- The package has no shipped outbound network client, telemetry, update check,
  or remote browser asset. Strict mode adds application-level rejection of
  recognizable network filesystem paths and non-loopback serving; it is not an
  OS network sandbox.
- Windows 11 x64 is the only locally executed release-hardening platform for
  the 2026-07-20 checkpoint. Portable/POSIX code and CI configuration do not
  establish Linux or macOS support without their own retained runner evidence.
- CLI state is ephemeral except user-requested outputs and optional report
  files. API job history is memory-only, while successful output bytes occupy
  the private session directory for the lifecycle described above.
- The minimal HTML page is a capability/status shell. A React/TypeScript job UI,
  local PDF.js previews, camera/scanner flow, and browser storage design are not
  shipped.
