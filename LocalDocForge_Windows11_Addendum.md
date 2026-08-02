# LocalDocForge — Windows 11 Primary-Platform Addendum

This addendum narrows LocalDocForge implementation and release verification to the
user's Windows 11 x64 workstation without weakening the cross-platform design.

## Platform and shell

- Treat Windows 11 x64 as the primary development and first release target.
- Work from `E:\Sem-VI-Break\Pdf-Conversion-Tool`.
- Use PowerShell 7 syntax and native Windows paths in user-facing commands.
- Do not assume Bash, POSIX signals, `fork`, `/tmp`, executable permission bits,
  or POSIX-only filesystem semantics.
- Keep portable code based on `pathlib`, but write and run Windows-specific
  regression tests for Windows path and process behavior.
- Do not require Administrator privileges for normal installation, startup,
  conversion, cleanup, or testing.
- Do not make Hyper-V, Windows Sandbox, WSL, or Docker mandatory. They may be
  optional defense-in-depth or CI environments only.

## Python environments and packaging

- Preserve the existing audited Python 3.14 virtual environment as evidence;
  do not delete or silently replace it.
- Create at least one clean side-by-side release-test environment on a second
  supported Python version, preferably Python 3.13, and test Python 3.14 as an
  additional target.
- Install the declared build backend explicitly in clean build environments.
- Build both wheel and sdist with `python -m build`.
- Install the wheel into a fresh virtual environment, run `ldf doctor`, the
  focused smoke suite, strict-offline tests, and representative PDF render
  validation.
- Generate hashes and Windows-specific lock evidence for exact artifacts.
- Do not claim macOS or Linux support from Windows-only evidence; use CI or a
  separate environment later for those platforms.
- Do not make a standalone EXE/MSIX installer the first packaging milestone.
  Establish reproducible wheel/sdist installation first, then evaluate a
  Windows installer or standalone bundle with fresh SBOM and license evidence.

## Windows worker-process isolation

- Use the multiprocessing `spawn` model; never rely on `fork` behavior.
- Move untrusted PDF and image parsing out of the long-lived API process into
  dedicated worker child processes before enabling sensitive-document release
  claims.
- Prefer Windows Job Objects for worker containment. Assign each worker and all
  descendants to one job object immediately after creation.
- Implement hard termination of the full job on cancellation, timeout, API
  shutdown, or worker failure. `taskkill /T` may remain a fallback, not the
  primary containment primitive.
- Enforce practical per-job controls where Windows supports them: process-tree
  lifetime, active-process count, memory ceiling, CPU-time ceiling, wall-clock
  timeout, and bounded temporary/output storage.
- Treat a worker crash, access violation, forced termination, and out-of-memory
  termination as bounded job failures with sanitized reports.
- Never reuse a worker that processed a malformed or hostile document unless
  the worker model is demonstrably reset-safe; one job per worker process is an
  acceptable initial design.
- Keep passwords and document text out of process command lines, environment
  variables, crash messages, Windows Event Log messages, and parent-process
  diagnostics.

## Windows API queue and cancellation

- Keep Uvicorn/FastAPI on `127.0.0.1` by default.
- Add a bounded queue, configurable worker count, per-client and global
  concurrency limits, rate limits, progress events, cancellation endpoints,
  and graceful shutdown behavior.
- The request process must not directly parse untrusted document bytes.
- Cancellation must terminate the worker job object and wait for verified
  process-tree exit before marking the job cancelled.
- Test abrupt browser disconnects, server shutdown, Ctrl+C, worker crashes,
  and Windows restart residue cleanup.

## Windows filesystem and path security

Test all relevant operations against:

- local drive-letter paths
- UNC paths
- mapped network drives
- extended-length paths beginning with `\\?\`
- device paths beginning with `\\.\`
- Windows reserved device names such as `CON`, `PRN`, `AUX`, `NUL`, `COM1`,
  and `LPT1`, including names with extensions or trailing dots/spaces
- NTFS alternate data stream syntax such as `file.pdf:stream`
- hard links, symbolic links, directory junctions, and reparse points
- case-insensitive aliases and short/long path aliases where available
- trailing dots/spaces, bidirectional controls, astral Unicode, normalization
  variants, and long paths

Requirements:

- Strict-offline mode must reject UNC and mapped-network roots before opening
  them.
- Resolve and contain reparse-point destinations at publication and download
  boundaries.
- Refuse source/output aliases even when represented through links, casing,
  or alternate path forms.
- Keep private workspaces on a confirmed local filesystem in strict mode.
- Do not claim secure deletion; ordinary Windows deletion is best effort.

## Windows network isolation

- Application strict-offline mode is required but is not an OS network
  sandbox.
- For the release gate, run a second test under OS-enforced outbound network
  denial using an appropriate Windows Firewall, VM, or container setup.
- Do not modify global firewall policy without explicit user approval. Prefer a
  narrowly scoped rule or isolated test environment for the exact executable.
- Record the exact isolation mechanism and verify that loopback API operation
  still works while outbound DNS and non-loopback sockets fail.

## Windows external engines

- Discover executables only from explicit absolute local paths or approved
  local PATH entries. Never search the current working directory implicitly.
- Use argument arrays and `shell=False`.
- Quote examples for PowerShell but do not manually build shell command
  strings.
- Use isolated local profiles for LibreOffice and terminate its complete
  process tree on timeout.
- Treat WIA and TWAIN as the primary future scanner adapters on Windows;
  scanner acquisition must remain optional and capability-gated.
- Do not bundle Ghostscript, LibreOffice, Tesseract, Pandoc, veraPDF, or other
  optional executables until the exact Windows binary, provenance, license,
  redistribution terms, and advisories are reviewed.

## Windows-specific release gate

A Windows-sensitive-document release remains blocked until all of the
following are evidenced on a clean Windows 11 x64 machine:

1. Wheel and sdist build successfully.
2. The wheel installs into clean supported-Python virtual environments.
3. `ldf doctor`, CLI smoke tests, API tests, strict-offline tests, worker crash
   and cancellation tests, and full PDF render validation pass.
4. Untrusted parsing occurs only in bounded worker processes.
5. Windows Job Object termination is verified for the complete process tree.
6. UNC, mapped-drive, reparse-point, reserved-name, ADS, alias, and long-path
   regressions pass.
7. Exact Windows artifacts receive hashes, SBOMs, notices, upstream-license
   verification, and advisory review.
8. No feature is enabled solely because an executable is installed.
9. The documentation clearly identifies Windows as verified and other
   platforms as unverified until their own CI evidence exists.
10. A new independent audit changes the sensitive-document decision from FAIL
    only on the basis of executed evidence.
