# Documentation Index

Where to look, by question. Truth hierarchy: runtime probes and tests outrank
every document; among documents, `FEATURE_MATRIX.md` and the executable CLI
reference outrank prose that merely explains.

## Start here

| Doc | Answers |
|---|---|
| [`../README.md`](../README.md) | What this project is, what works today, quick start |
| [`GETTING_STARTED_WINDOWS.md`](GETTING_STARTED_WINDOWS.md) | Task-oriented Windows usage — recipes, web API session, config, troubleshooting |
| [`CLI.md`](CLI.md) | Complete `ldf` reference: commands, page-range grammar, exit codes, and the local HTTP API contract |

## What is true right now

| Doc | Answers |
|---|---|
| [`FEATURE_MATRIX.md`](FEATURE_MATRIX.md) | Per-capability status, engine, verifying tests, and honest limitations |
| [`PACKAGING.md`](PACKAGING.md) | Release gates, platform scope, reproducible builds, and retained package identities |

## Design and guarantees

| Doc | Answers |
|---|---|
| [`TECHNICAL_REFERENCE.md`](TECHNICAL_REFERENCE.md) | **The consolidated technical reference** — every subsystem, constant, contract, and invariant in one document |
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | Layering, the pipeline lifecycle, worker containment, and key contracts |
| [`THREAT_MODEL.md`](THREAT_MODEL.md) | Assets, trust boundaries, mitigations — and their stated limits |
| [`CONVERSION_FIDELITY.md`](CONVERSION_FIDELITY.md) | Per-operation preservation/loss behavior and every `fidelity_warnings` / security-warning code |
| [`ENGINE_DECISIONS.md`](ENGINE_DECISIONS.md) | Which engines were chosen, why, and the revisit points |

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
