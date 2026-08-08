# Agent Feedback Log

Integration feedback from command-line clients using the `ldf` document
operations. Reports of failures, refusals, missing features and usability
problems help maintainers improve the tool.

## Rules for agents

- **Append entries at the end of this file.** Never edit or delete existing
  entries.
- An entry is **required** whenever a run failed, the output was
  unsatisfactory, or you fell back to doing the work yourself. A one-line
  entry for a smooth success is welcome but optional.
- **Do not change anything else in this repository.** This file is the only
  file clients may write to unless the user explicitly requests development
  work within an agreed scope.
- Describe documents generically ("a 40-page scanned PDF"), never their
  contents. Do not include personal paths, credentials or document text.

## Entry template

```text
### YYYY-MM-DD — <client/version> — <ok | unsatisfactory | failed | fell back>
- Task: <generic document operation>
- Command: <ldf invocation with generic input and output paths>
- Version: <ldf version and source commit, if applicable>
- Observed: <exit code, warning codes and verification result>
- Fallback: <alternative used, or "none">
- Suggestion: <proposed improvement>
```

## Entries

No entries in this public template.
