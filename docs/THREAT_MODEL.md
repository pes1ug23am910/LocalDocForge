# Threat Model

Scope: the current LocalDocForge core library, CLI, worker-backed FastAPI
service, and minimal browser status page. Controls below are implemented unless
explicitly marked planned. Unimplemented roadmap features are not assumed to
inherit these controls automatically.

## Assets

1. User documents and their filenames, metadata, annotations, attachments, and
   form values.
2. Derived data: converted documents, rendered pages, future extracted text or
   OCR data, reports, and temporary files.
3. Secrets: PDF passwords and future certificate passphrases/private keys.
4. The user's filesystem, processes, and machine integrity.

## Trust boundaries

- Every input document and upload is untrusted, regardless of extension.
- CLI operations invoke pikepdf/libqpdf, PDFium, and Pillow in-process. API
  operations invoke them in a fresh spawned child with process/resource
  containment. Both retain the user's filesystem authority; the worker is a
  failure boundary, not a restricted filesystem or kernel sandbox.
- User-installed external engines are semi-trusted. The runner constrains
  executable selection, arguments, environment, working directory, output,
  timeout, and process cleanup, but does not provide an OS/container sandbox.
- The local CLI user is trusted to choose ordinary local input/output paths.
  The optional CLI `allowed_output_roots` setting can narrow this authority.
- Browser requests are untrusted. The API accepts uploads and fixed operation
  parameters, not arbitrary server-side input/output paths.
- Other local processes with the same user privileges, administrators/root,
  kernel compromise, and forensic recovery from storage are outside the
  application boundary.

## Threats and current mitigations

### T1. Malicious or malformed documents

- `security/sniff.py` identifies supported leading-byte signatures before an
  engine opens a file and rejects extension/content mismatches. This is type
  sniffing, not a general polyglot detector or malware scanner.
- PDF inputs and generated PDFs are opened through pikepdf. Parser syntax
  warnings are rejected because repair is not an implemented operation.
  Generated PDFs are also rendered with PDFium before publication: all pages
  for selected high-risk operations, otherwise an evenly distributed sample of
  at most 20 pages. This detects structural/render failures; it does not prove
  semantic equivalence or absence of malicious content.
- Generated images must decode through Pillow before publication.
- Aggregate input bytes, aggregate output bytes, PDF page counts, image pixel
  counts, and decompressed image bytes are enforced in the implemented
  pipelines. Encrypted PDFs are page-counted again after opening. Output totals
  are generally checked after candidate generation, so the configured output
  cap is not a hard temporary-disk quota.
- CLI timeouts remain cooperative checks between operation steps, so a native
  parser stuck in the CLI cannot be preempted. Every API conversion instead
  runs in a fresh spawned process. The child waits for a parent gate before
  document parsing. On Windows that gate opens only after fail-closed Job Object
  assignment; after assignment, termination is checked and Job accounting must
  report zero active processes before `verified_empty` is published. If the
  bootstrap leader exits before assignment, the gate stays `never_opened` and
  only `pre_gate_leader_verified` is reported; every other unverified case fails
  closed. On POSIX the parent
  terminates and verifies the worker process group. Repository-launched tools
  remain in that group, but arbitrary same-user code can call `setsid()` and
  evade it. The worker boundary is therefore not a hostile-code sandbox.
- The parent enforces wall time and sampled aggregate temporary/output bytes,
  plus available platform memory, CPU, file-size, and child-count limits.
- Sampled directory monitors can overshoot between checks. macOS child-count
  control is reported unsupported and `RLIMIT_AS` is not claimed reliable
  there. Archive entry/expansion fields remain future controls because no
  archive/Office workflow is implemented.
- POSIX/Linux/macOS containment is implemented but was not executed in the
  Windows-only 2026-07-20 checkpoint. It is not cross-platform pass evidence.

### T2. Active or hidden content

- LocalDocForge does not execute PDF JavaScript, launch actions, links, form
  JavaScript, or attachments. `ldf inspect` reports a structural subset,
  including JavaScript, open actions, attachments, forms, outlines, and
  annotations.
- Page-moving operations warn when detected document actions/JavaScript,
  attachments, forms, signatures, or other document-level structures will not
  be preserved. `remove-pages` refuses page-referencing structures it cannot
  safely rewrite.
- Cropping changes CropBox visibility only; it leaves hidden content in the PDF
  and is always labelled **not redaction**. Secure redaction, sanitization,
  whiteout, and cover-and-replace are not implemented.
- Office macros/external-link handling is not a current control because Office
  conversion is unavailable.

### T3. Filesystem abuse and source integrity

- Upload names pass through Unicode normalization, control/format-character
  removal, reserved-name handling, length bounds, and path-separator removal.
  Job ids and temporary suffixes are constrained, and every workspace/API path
  is checked for containment.
- API inputs and outputs live under a private per-session root; artifacts are
  served only by the owning in-memory job and contained output index. Public API
  reports replace private server paths with basenames.
- CLI candidates are created in isolated per-job workspaces. An optional
  `allowed_output_roots` jail is enforced at publication; an explicitly empty
  list denies every destination. The API always chooses its contained output
  destinations server-side.
- Every candidate validates before publication begins. Publication copies each
  file to a hidden same-directory staging file and fsyncs it. Overwrite uses
  `os.replace`; fail/rename use atomic no-clobber linking. Each final file is
  individually atomic, and a concurrent collision cannot silently defeat the
  fail policy.
- A handled multi-output publication failure attempts to remove newly written
  files and restore overwrite backups. This is best-effort rollback, not an
  all-files transaction across process termination, power loss, filesystem
  failure, or hostile concurrent changes.
- A destination that equals or aliases an input (including an existing hard
  link) is refused, including under overwrite. Inputs are never intentional
  in-place targets.
- On Windows, lexical validation distinguishes local `\\?\C:\...` paths from
  extended UNC, rejects `\\.\` and unsupported device namespaces, NTFS ADS,
  reserved device components (including extensions), and trailing-dot/space
  aliases before access. Strict mode requires `GetDriveTypeW` to confirm a local
  drive and rejects UNC/mapped roots before metadata queries. Existing
  reparse-point components are rejected root-to-leaf at configuration,
  containment, publication, and download boundaries. Real Windows tests cover
  junction, case, 8.3, hard-link, extended, and long-path aliases where the host
  exposes them; mapped-drive behavior is mocked because no mapped drive is
  mounted, and symbolic-link creation is unavailable on this host.

### T4. Subprocess abuse

- External commands use argument arrays and `shell=False`. The logical engine
  name must be allowlisted, and executable search uses absolute, non-network
  PATH entries; the current directory is not an implicit search location.
- The child receives a small inherited environment. Child PATH replacement and
  remote filesystem values in path-like environment variables are refused.
  The default working directory is the resolved executable's directory; an
  explicit working directory must be absolute and non-network.
- Captured stdout/stderr retention is bounded while the pipe is still drained.
- Standalone CLI tool calls receive their own process group. Tool calls made by
  an API worker stay in its validated worker group so the outer supervisor owns
  their descendants. Timeout, `KeyboardInterrupt`, and other cancellation paths
  terminate the applicable tree/group, with a direct-kill fallback.
- These controls reduce command injection, executable hijacking, leakage, and
  orphaning. They do not make a user-installed binary trustworthy. None of the
  currently implemented conversion operations requires an optional executable.

### T5. Sensitive-data leakage

- CLI PDF passwords come from a hidden interactive prompt, never a command-line
  option. API passwords are multipart form values. Passwords are not included
  in conversion reports or external command arguments.
- Conversion reports contain artifact metadata, filenames, engine/version
  information, counts, warnings, errors, and user-selected CLI destinations;
  they do not intentionally contain extracted document text or passwords.
  API reports additionally scrub server-private paths. Unexpected API errors
  return a generic message and hardening headers rather than exception details.
- Admission precedes multipart parsing. API transport uses a random contained
  `.transport-*` directory beneath the private session and is aggregate-bounded
  by the upload, enabled input, and enabled temporary-byte ceilings. Handles and
  the spool are closed/removed before enqueue; malformed bodies and disconnects
  follow the same cleanup path. The spawn command line
  contains neither document bytes nor secrets. A required PDF password is sent
  only through the private spawn channel, never a command line or worker
  environment, and is cleared from parent state at completion. Worker IPC is
  size-capped JSON; progress, reports, errors, paths, and secrets are sanitized
  before they cross back. Worker Python/native stdout and stderr are redirected
  away from server logs, and unexpected parser errors become generic.
- Successful API jobs remove uploads, scratch, and worker-temp data before
  publishing their validated outputs and `success` together under the job lock.
  Downloads acquire a lifetime lease and reject every non-success state;
  DELETE/eviction cannot remove a file during streaming. Outputs are retained
  until explicit DELETE, 50-job eviction, or graceful shutdown. Failed/cancelled
  jobs remove their whole job root. A crash can leave a session directory;
  startup removes it only after acquiring its external OS-held session lease.
  Held, missing, and unreadable leases are preserved fail-closed.
- Workspace deletion uses retries and reports incomplete CLI cleanup as a
  critical security warning. API DELETE/eviction retains job state and reports
  an error if removal fails. No deletion is described as secure erase, and SSD
  or backup recovery remains possible.

### T6. Network and privacy boundary

- Source inspection and socket-denial tests found no outbound HTTP client,
  telemetry, update checker, CDN, remote font/script, cloud conversion, or
  automatic external-URI resolution in the shipped package. A synthetic PDF
  containing an external URI completed a strict operation without DNS or
  socket use.
- Strict-offline mode is preserved from `LDF_STRICT_OFFLINE`, recorded in job
  reports and API health, and overrides identified remote surfaces. It rejects
  UNC and Windows mapped-drive configured roots, inputs, outputs, report
  directories, and non-loopback web serving. The subprocess runner separately
  refuses remote executable-search, working-directory, and environment paths.
- Real UNC and local-drive behavior executed on this Windows host. Mapped-drive
  detection has mocked `GetDriveTypeW` evidence only because no mapped drive is
  mounted. The Python socket-denial suite passed, but the separate Windows
  Firewall/OS-enforced outbound-denial gate has not run; no OS-sandbox claim is
  inferred from application instrumentation.
- A strict API worker also blocks Python socket/DNS entry points after its
  environment has been reduced. The release wrapper chooses the repository
  guard path; the worker accepts it only when the exact marked `sitecustomize`
  file is the one loaded at startup, then preserves only that path for Python
  grandchildren. Tests exercise a spawned Python grandchild. This is test
  instrumentation and defense in depth, not an OS network sandbox; native code
  and non-Python children can bypass it.
- Network-path recognition is lexical plus Windows `GetDriveTypeW`. On POSIX,
  a remote filesystem mounted at an ordinary path is indistinguishable from a
  local filesystem to this application. Strict mode also cannot prevent a
  compromised parser or dependency from calling the OS network stack. Use a
  host firewall or a container/VM with no network for defense in depth.
- Outside strict mode, the CLI may intentionally access a path the OS exposes as
  a network filesystem. `ldf web` still binds to loopback by default;
  non-loopback inbound serving requires `--allow-nonlocal` and is prominently
  warned. Strict mode refuses that override.

### T7. Local API and browser surface

- Default bind is `127.0.0.1`; Host-header validation accepts loopback names
  only unless the dangerous nonlocal mode was explicitly selected.
- A random token is printed at startup. Every `/api` request must present it in
  `X-LDF-Token`; a token cookie alone is rejected, so a cross-origin form cannot
  authorize an operation. No CORS grants are emitted.
- Responses use CSP, frame denial, MIME sniffing denial, no-store caching,
  referrer restrictions, and camera/microphone/geolocation denial. The current
  status shell loads no remote code and contains no camera/scanner workflow.
- Request-body, upload, temporary-byte, file-count, field-count, and field-size caps are
  enforced. The HTTP ceiling remains active even if the general job input limit
  is explicitly disabled. Upload bytes are counted cumulatively per job.
  Operation parameters are type/range checked and failures clean their private
  job directory.
- Job ids are random UUID-derived values; the API never accepts a server-side
  artifact path. Output downloads acquire the owning job's active-download
  lease, require `success`, and re-check path containment. DELETE and eviction
  cannot race a live stream. History is memory-only, capped at 50, and
  disappears on shutdown.
- Admission occurs before multipart spooling and enforces a bounded queue,
  global worker count, per-client active-job cap, and sliding-window rate
  limit. Each job has queued/running/terminal states, bounded progress events,
  and a cancellation endpoint. Legacy requests wait for their worker and return
  `201`; explicit async requests return `202`. Worker crash, hang, malformed
  IPC, request cancellation, and server shutdown cannot permanently consume a
  worker slot. Cancelled queued entries are physically removed rather than
  retained as queue tombstones. Tree-exit verification and private finalization
  complete before a terminal event releases accounting. Non-loopback mode has
  no TLS and is not recommended for sensitive documents.

### T8. Unavailable security-sensitive capabilities

Compression, repair, OCR, Office/HTML/Markdown conversion, PDF/A or PDF/UA
validation/conversion, form editing, encryption/protection tools, secure
redaction, signatures, compare, scanner/camera acquisition, and a full browser
job UI are unavailable. UI capability lists and `ldf doctor` must keep them
unavailable even if a related optional executable happens to be installed.

## Residual risks

- CLI native parsers still handle hostile bytes in-process. API workers add
  hard failure/resource boundaries but retain the user's filesystem authority;
  they are not AppContainers, restricted tokens, seccomp sandboxes, or network
  namespaces.
- On POSIX, repository-managed tools remain in the worker process group, but
  arbitrary same-user code can create a new session and escape group-based
  descendant accounting and termination.
- PDF rendering is sampled for routine long outputs, and render success is not
  proof of fonts, reading order, links, forms, signatures, accessibility,
  compliance, or exact visual fidelity.
- Multi-output publication and cleanup have handled-failure recovery but cannot
  be made crash-transactional or forensically erasing at application level.
- Strict-offline is an application policy with platform-specific path
  detection, not an OS network sandbox.
- The 2026-07-20 checkpoint has no OS-enforced outbound-denial result, no real
  mapped-drive mount, and no symbolic-link creation privilege. Mapped-drive
  behavior is mocked and junction evidence does not substitute for a symlink.
- Linux and macOS hardening gates were not run; portable implementation and CI
  configuration are not evidence that either platform passed.
- Optional external tools are user-installed and must be reviewed by exact
  binary/version before enablement. Their diagnostic license labels are not
  provenance evidence.
- The dated advisory/license review is not a safety verdict: OpenJPEG 2.5.4 is
  affected, PDFium provenance cannot be mapped authoritatively to fixes, and
  the native inventory identifies incomplete/unversioned bundled subcomponents.

## Deployment modes

1. Local CLI under the user's account (implemented).
2. Loopback FastAPI service plus minimal status shell under the same account
   (implemented with per-job workers; strict-offline recommended).
3. Explicit non-loopback API bind outside strict mode (implemented but
   dangerous, plaintext transport with token authentication, and not
   recommended).
4. Container/VM deployment with OS-enforced network and resource isolation
   (example and packaging still planned).
