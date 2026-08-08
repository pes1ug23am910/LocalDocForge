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
