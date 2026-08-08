# Agent Feedback Log

Reviews from AI coding agents (Claude Code, Codex, …) that delegated document
work to the `ldf` CLI instead of writing their own conversion code. Entries
here are maintainer input: real-world failures, refusals, missing features,
and friction feed the roadmap.

## Rules for agents

- **Append entries at the end of this file.** Never edit or delete existing
  entries.
- An entry is **required** whenever a run failed, the output was
  unsatisfactory, or you fell back to doing the work yourself. A one-line
  entry for a smooth success is welcome but optional.
- **Do not change anything else in this repository.** It is release-gated;
  this file is the only file agents may write to. Exception: sessions the
  user explicitly commissioned to develop the tool may edit files within that
  commissioned scope.
- Describe documents generically ("a 40-page scanned PDF"), never their
  contents. Filenames are acceptable; sensitive paths and document text are
  not — the same policy the tool's own reports follow.

## Entry template

```text
### YYYY-MM-DD — <agent, model> — <ok | unsatisfactory | failed | fell back>
- Task: <what the user needed>
- Command: <exact ldf invocation>
- Version: 0.1.0 @ <output of: git -C E:\Sem-VI-Break\Pdf-Conversion-Tool rev-parse --short HEAD>
- Observed: <exit code, warning codes from the JSON report, what verification showed>
- Fallback: <what you did instead, or "none">
- Suggestion: <what would have made the tool succeed, or a feature request>
```

---

## Entries

*(none yet)*

### 2026-08-09 — Codex, GPT-5.6 Sol (Ultra reasoning) — ok
- Task: Take the live capability snapshot before implementing the commissioned S4 `pdf-to-md` slice.
- Command: `& 'E:\Sem-VI-Break\Pdf-Conversion-Tool\.venv\Scripts\ldf.exe' --json agent-brief`
- Version: 0.1.0 @ f8a8b96
- Observed: The first sandboxed launcher attempt could not access the venv's external Python interpreter; the approved read-only rerun exited 0 and returned valid registry-derived JSON with all 12 baseline capabilities.
- Fallback: none; the same command succeeded with the required filesystem permission.
- Suggestion: none for `ldf`; the initial failure was the agent sandbox boundary, not a LocalDocForge defect.

### 2026-08-09 — Codex sub-agent, GPT-5.6 Sol — failed
- Task: Read the live capability brief before adding S4 synthetic fixtures and focused tests.
- Command: `ldf --json agent-brief`
- Version: 0.1.0 @ f8a8b96
- Observed: Exit 1 before LocalDocForge started because the sandbox denied access to the venv's external Python interpreter (`Access is denied`); no JSON report or warning code was produced.
- Fallback: none; the root executor had already completed the identical read-only brief successfully with approved filesystem access.
- Suggestion: none for `ldf`; route repository-venv launchers through the approved read-only permission when the interpreter lives outside the workspace sandbox.
