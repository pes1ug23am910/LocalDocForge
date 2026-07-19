# LocalDocForge — Architecture

## Layering

```
┌────────────────────────────────────────────────────────┐
│ interfaces:  cli/ (Typer)   api/ (FastAPI, planned)    │
│              browser UI (React, planned)               │
├────────────────────────────────────────────────────────┤
│ operations/  merge, split, rotate, images, …           │
│   thin functions: build an execute() closure and hand  │
│   it to the pipeline runner                            │
├────────────────────────────────────────────────────────┤
│ pipelines/runner.py — the one lifecycle every job uses │
│   sniff → limits → workspace → execute → validate →    │
│   atomic publish → report → cleanup                    │
├──────────────┬─────────────────────┬───────────────────┤
│ engines/     │ validation/         │ reporting/        │
│ adapters +   │ reopen via pikepdf, │ JSON + human      │
│ registry +   │ render via PDFium,  │ report writers    │
│ capability   │ blank/zero-page     │                   │
│ gating       │ detection           │                   │
├──────────────┴─────────────────────┴───────────────────┤
│ jobs/ workspaces, atomic publish   security/ sniffing, │
│ collision policies, stale sweep    paths, filenames,   │
│                                    subprocess hardening│
├────────────────────────────────────────────────────────┤
│ domain/ typed models + page-range grammar              │
│ config/ pydantic-settings (LDF_* env), strict-offline  │
└────────────────────────────────────────────────────────┘
```

## Key contracts

### Engine adapters (`engines/base.py`)
`EngineAdapter.probe() -> EngineInfo` must never raise; unavailability is
data, not an exception. `supported_operations()` declares operation ids. The
registry (`engines/registry.py`) resolves `engine_for(operation, preferred)`
and only returns engines whose probe passed. The CLI/API/UI never import
pikepdf/PDFium/Pillow directly for document work — they go through
operations, which consult the registry.

### Capability honesty (`engines/registry.py::CAPABILITY_SPECS`)
A capability is `available` iff `implemented` (set in code, in the same
change as its tests) AND a live engine probe passed. `tests/unit/
test_registry.py` pins the implemented set so nothing can be advertised by
accident.

### Pipeline lifecycle (`pipelines/runner.py`)
1. `require_media_type` — magic bytes decide, extensions never do.
2. Input size and total-page limits from `ResourceLimits`.
3. `JobWorkspace` — isolated per-job dir under the jobs root; all
   intermediate files stay inside; `workspace.contain()` re-checks every
   candidate path.
4. `execute(context, inputs)` — operation body; emits progress; checks
   cancellation via `JobContext.check_cancelled()`.
5. Validation — every candidate PDF is reopened with pikepdf and rendered
   with PDFium (all pages for high-risk candidates, a 20-page sample
   otherwise); generated images must actually decode.
6. `atomic_publish` — copy to a hidden staging name in the destination
   directory, fsync, `os.replace`. Collision policies: fail | rename |
   overwrite.
7. `ConversionReport` — status, engines, versions, sizes, page counts,
   elapsed, warnings (security + fidelity), validation checks, details. No
   document text, no secrets.
8. Cleanup — workspace removed on success, failure, and cancellation;
   `cleanup_stale_workspaces` sweeps leftovers at CLI startup.

### Error model
Operations raise `PipelineError` carrying the failed report.
`EncryptedInputError` (subclass) lets interfaces prompt for a password and
retry. The CLI maps causes to stable exit codes (see `docs/CLI.md`).

## Runtime characteristics
- In-process engines (pikepdf/PDFium/Pillow) for Phase 0/1; external tools
  run only through `security/subproc.run_tool` (allowlist, argv arrays,
  minimal env, output caps, process-tree kill).
- No network code paths exist anywhere in the package; `--strict-offline`
  records the pledge in settings/reports and will gate future engines that
  could touch the network.
- State: none persisted. Jobs are ephemeral; recent-job history (in-memory)
  arrives with the API layer.
