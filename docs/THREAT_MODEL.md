# Threat Model

Scope: LocalDocForge as of Phase 0/1 (core library + CLI). The HTTP API and
browser UI sections describe controls that MUST hold when those layers land;
they are design constraints, not shipped features.

## Assets
1. User documents (content, filenames, metadata) — the primary asset.
2. Derived data: extracted text, thumbnails, OCR output, reports.
3. Credentials: PDF passwords, future certificate passphrases.
4. The user's filesystem and machine integrity.

## Trust boundaries
- **Every input document is untrusted**, whatever its extension claims.
- External engine binaries are semi-trusted (allowlisted, bounded, isolated).
- The local user is trusted (CLI runs with their authority).
- (Future) The browser is untrusted-ish: same-origin, CSRF, and token rules
  apply; payloads never carry absolute filesystem paths.

## Threats and mitigations (implemented now unless marked planned)

### T1. Malicious/malformed documents
- Polyglot or mislabeled files → magic-byte sniffing before any engine
  touches bytes (`security/sniff.py`); mismatches rejected with the detected
  type named. *Tested with a PNG-as-.pdf fixture.*
- Parser exploits / crashes → structural engine is pikepdf/libqpdf (memory-
  safe wrapper over hardened C++); renders go through PDFium (Chrome's
  hardened renderer). Failures surface as clean `PipelineError`s; partial
  outputs never reach the destination. *Tested with a garbage-body PDF and a
  corrupted-xref PDF.*
- Decompression bombs (images) → `ResourceLimits.max_image_pixels` wired to
  Pillow's bomb guard. *Tested.* Office/archive bombs: planned with the
  Office pipeline (bounded entry count + expansion ratio already modeled in
  `ResourceLimits`).
- Resource exhaustion → input-size and total-page limits enforced before
  work starts; per-tool timeouts on external processes. *Tested.*

### T2. Active content in documents
- PDF JavaScript, launch actions, embedded files are never executed or
  opened; `ldf inspect` reports their presence read-only. Office macros:
  LibreOffice pipeline (planned) runs macro-disabled, isolated profile,
  link updates suppressed.

### T3. Filesystem abuse
- Path traversal via crafted names → `sanitize_filename` (traversal,
  reserved device names, control chars, Unicode normalization) and
  `ensure_contained` for every workspace path. *Tested.*
- Writes outside intended locations → outputs staged in per-job workspaces;
  optional `allowed_output_roots` jail enforced at publish. *Tested.*
- Partial/corrupt outputs → atomic publish (staging file + fsync +
  `os.replace`); validation before publish. *Tested.*
- Source damage → sources opened read-only; nothing ever writes to an input
  path. In-place operations do not exist; a future `--in-place` requires an
  explicit flag + automatic backup per spec.

### T4. Subprocess abuse
- Command injection → argv arrays only, `shell=True` nowhere; executables
  resolved from a hard allowlist; document-derived strings never become
  executable names. *Tested (allowlist refusal).*
- Runaway processes → timeouts with full process-tree termination
  (`taskkill /T` on Windows, killpg on POSIX); bounded output capture.
- Environment leakage → minimal inherited env (PATH/SYSTEMROOT/TEMP…);
  secrets like `*_KEY` never inherited. *Tested.*

### T5. Secret exposure
- Passwords accepted via interactive no-echo prompt (CLI) or API parameter;
  never via argv of external tools, never logged, never in reports.
  *Tested: report JSON contains neither password nor document text.*

### T6. Network exfiltration
- No telemetry, no update checks, no remote assets: **no network code paths
  exist in the package**. `--strict-offline` records the pledge and will
  hard-gate any future engine that could reach the network (e.g. signature
  timestamping). Planned: strict-offline integration test that fails on any
  socket use; container example with `--network none`.

### T7. Local API attack surface (design constraints for the planned API)
- Bind 127.0.0.1/::1 only by default; non-loopback requires explicit opt-in.
- Random per-session token in a header (never URLs); CSRF protection on
  state-changing routes; restrictive CORS; CSP; unguessable job ids; request
  and file-size caps; job-scoped file serving only; no user-controlled
  absolute paths in payloads.

## Residual risks (stated honestly)
- pikepdf/PDFium/Pillow parse untrusted bytes in-process; a zero-day in
  those libraries executes with user privileges. Mitigation: keep pinned
  versions current; worker-process isolation is planned with the job queue.
- Deleted temp files are not guaranteed unrecoverable on SSDs; we describe
  cleanup as best-effort deletion, never "secure erase".
- Windows per-user temp ACLs protect workspaces; multi-user shared-temp
  scenarios on POSIX get 0700 dirs, but a root-level adversary is out of scope.

## Supported deployment modes
1. Local CLI, user privileges (current).
2. Localhost web app, same user (planned; T7 controls mandatory).
3. Container with `--network none` (planned example; strongest isolation).
