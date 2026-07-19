# Threat Model

Scope: the current LocalDocForge core library, CLI, synchronous FastAPI
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
- pikepdf/libqpdf, PDFium, and Pillow parse untrusted data in-process with the
  user's authority. They are dependencies, not a security sandbox.
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
- In-process timeouts are cooperative checks between operation steps. A native
  parser call that hangs or exhausts memory cannot currently be forcibly
  stopped. Worker-process isolation and hard per-process memory/CPU limits are
  planned.
- Archive entry/expansion and subprocess-count limit fields exist for future
  pipelines but are not active defenses for an archive/Office workflow today.

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

### T4. Subprocess abuse

- External commands use argument arrays and `shell=False`. The logical engine
  name must be allowlisted, and executable search uses absolute, non-network
  PATH entries; the current directory is not an implicit search location.
- The child receives a small inherited environment. Child PATH replacement and
  remote filesystem values in path-like environment variables are refused.
  The default working directory is the resolved executable's directory; an
  explicit working directory must be absolute and non-network.
- Captured stdout/stderr retention is bounded while the pipe is still drained.
  Timeout, `KeyboardInterrupt`, and other cancellation paths terminate the
  process tree (`taskkill /T` on Windows or a process group on POSIX), with a
  direct-kill fallback.
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
- Uploaded API inputs are removed after a successful operation. Outputs remain
  in the private session directory until explicit DELETE, 50-job eviction, or
  graceful shutdown. A crash can leave a session directory; startup removes
  sessions older than 24 hours on a best-effort basis.
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
- Request-body, upload, file-count, field-count, and field-size caps are
  enforced. Upload bytes are counted cumulatively per job. Operation parameters
  are type/range checked and failures clean their private job directory.
- Job ids are random UUID-derived values; the API never accepts a server-side
  artifact path, and output downloads re-check containment under the owning
  job. History is memory-only, capped at 50, and disappears on shutdown.
- Jobs run synchronously in the server process. There is no per-client rate
  limit, concurrency quota, background isolation, or cancellation endpoint.
  Non-loopback mode has no TLS and is not recommended for sensitive documents.

### T8. Unavailable security-sensitive capabilities

Compression, repair, OCR, Office/HTML/Markdown conversion, PDF/A or PDF/UA
validation/conversion, form editing, encryption/protection tools, secure
redaction, signatures, compare, scanner/camera acquisition, and a full browser
job UI are unavailable. UI capability lists and `ldf doctor` must keep them
unavailable even if a related optional executable happens to be installed.

## Residual risks

- In-process native parsers handle hostile bytes with user privileges and lack
  hard memory/CPU isolation.
- PDF rendering is sampled for routine long outputs, and render success is not
  proof of fonts, reading order, links, forms, signatures, accessibility,
  compliance, or exact visual fidelity.
- Multi-output publication and cleanup have handled-failure recovery but cannot
  be made crash-transactional or forensically erasing at application level.
- Strict-offline is an application policy with platform-specific path
  detection, not an OS network sandbox.
- Optional external tools are user-installed and must be reviewed by exact
  binary/version before enablement. Their diagnostic license labels are not
  provenance evidence.
- The offline SBOM/license inventory has not been checked against current
  authoritative security advisories without user-approved network access.

## Deployment modes

1. Local CLI under the user's account (implemented).
2. Loopback FastAPI service plus minimal status shell under the same account
   (implemented; strict-offline recommended for sensitive work).
3. Explicit non-loopback API bind outside strict mode (implemented but
   dangerous, plaintext transport with token authentication, and not
   recommended).
4. Container/VM deployment with OS-enforced network and resource isolation
   (example and packaging still planned).
