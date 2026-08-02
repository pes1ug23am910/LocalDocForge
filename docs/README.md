# Documentation Index

Where to look, by question. Truth hierarchy: runtime probes and tests outrank
every document; among documents, `STATUS.md` and `FEATURE_MATRIX.md` outrank
prose that merely explains.

## Start here

| Doc | Answers |
|---|---|
| [`../README.md`](../README.md) | What this project is, what works today, quick start |
| [`GETTING_STARTED_WINDOWS.md`](GETTING_STARTED_WINDOWS.md) | Task-oriented usage on the primary Windows workstation — recipes, web API session, config, troubleshooting |
| [`CLI.md`](CLI.md) | Complete `ldf` reference: commands, page-range grammar, exit codes, and the local HTTP API contract |

## What is true right now

| Doc | Answers |
|---|---|
| [`STATUS.md`](STATUS.md) | The release decision, executed evidence, and remaining blockers — updated every checkpoint |
| [`FEATURE_MATRIX.md`](FEATURE_MATRIX.md) | Per-capability status, engine, verifying tests, and honest limitations |
| [`MACHINE_READINESS.md`](MACHINE_READINESS.md) | Dated is-it-ready verification runs on the primary machine, with full evidence |

## Design and guarantees

| Doc | Answers |
|---|---|
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | Layering, the pipeline lifecycle, worker containment, and key contracts |
| [`THREAT_MODEL.md`](THREAT_MODEL.md) | Assets, trust boundaries, mitigations — and their stated limits |
| [`CONVERSION_FIDELITY.md`](CONVERSION_FIDELITY.md) | Per-operation preservation/loss behavior and every `fidelity_warnings` / security-warning code |
| [`ENGINE_DECISIONS.md`](ENGINE_DECISIONS.md) | Which engines were chosen, why, and the revisit points |
| [`IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md) | The phased engineering roadmap and release constraints |

## Building on it

| Doc | Answers |
|---|---|
| [`DEVELOPMENT.md`](DEVELOPMENT.md) | Dev setup, repo map, quality gates, and the golden path for adding a capability |
| [`LIBRARY_API.md`](LIBRARY_API.md) | Using `localdocforge` as a Python library, with executed examples |

## Supply chain and audits

| Doc | Answers |
|---|---|
| [`PACKAGING.md`](PACKAGING.md) | Install profiles, hash locks, reproducible builds, the release gate, CI contract |
| [`LICENSING.md`](LICENSING.md) | Project license and dependency/engine licensing posture |
| [`SBOM.lite.cdx.json`](SBOM.lite.cdx.json) / [`SBOM.standard.cdx.json`](SBOM.standard.cdx.json) / [`SBOM.full.cdx.json`](SBOM.full.cdx.json) | Profile-specific CycloneDX SBOMs (generated; do not edit) |
| [`ADVISORY_REPORT.json`](ADVISORY_REPORT.json) | Dependency advisory review record (dated) |
| [`INDEPENDENT_AUDIT.md`](INDEPENDENT_AUDIT.md) | The 2026-07-19 independent audit and its 2026-07-20 closure appendix — historical record, kept intact |

`Transcripts/` holds historical working-session transcripts; like the audit,
they are records of what happened, not living documentation — never "fix"
them retroactively.
