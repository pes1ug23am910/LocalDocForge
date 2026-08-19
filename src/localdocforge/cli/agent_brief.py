"""Registry-derived, deterministic usage guidance for document agents."""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Final, Protocol

from localdocforge import __version__
from localdocforge.domain.models import Capability
from localdocforge.engines.registry import (
    CAPABILITY_SPECS,
    CapabilitySpec,
    default_registry,
)
from localdocforge.security.paths import is_remote_path

SCHEMA_VERSION = 1

# Templates describe syntax only. Selection and ordering always come from
# CAPABILITY_SPECS, with live state joined from EngineRegistry.capabilities().
USAGE_BY_CAPABILITY_ID: Final[Mapping[str, str]] = MappingProxyType(
    {
        "merge": (
            "ldf merge INPUT.pdf [INPUT.pdf ...] -o OUTPUT.pdf [--pages RANGE ...] "
            "[--collision fail|rename|overwrite]"
        ),
        "split": (
            "ldf split INPUT.pdf -d OUTPUT_DIR [--pages RANGE | --every N] "
            "[--collision fail|rename|overwrite]"
        ),
        "remove-pages": (
            "ldf remove-pages INPUT.pdf --pages RANGE -o OUTPUT.pdf "
            "[--collision fail|rename|overwrite]"
        ),
        "extract-pages": (
            "ldf extract-pages INPUT.pdf --pages RANGE -o OUTPUT.pdf "
            "[--collision fail|rename|overwrite]"
        ),
        "organize": (
            "ldf organize INPUT.pdf --order RANGE -o OUTPUT.pdf [--collision fail|rename|overwrite]"
        ),
        "rotate": (
            "ldf rotate INPUT.pdf --degrees 90 [--pages RANGE] -o OUTPUT.pdf "
            "[--collision fail|rename|overwrite]"
        ),
        "crop": (
            "ldf crop INPUT.pdf --box X0,Y0,X1,Y1 [--pages RANGE] -o OUTPUT.pdf "
            "[--collision fail|rename|overwrite]"
        ),
        "inspect": "ldf inspect INPUT.pdf",
        "compress": "ldf compress INPUT.pdf -o OUTPUT.pdf [--collision fail|rename|overwrite]",
        "ocr": (
            "ldf ocr INPUT.pdf -o OUTPUT.pdf [--language eng] [--sidecar OUTPUT.txt] "
            "[--skip-text | --force-ocr] [--collision fail|rename|overwrite]"
        ),
        "images-to-pdf": (
            "ldf images-to-pdf IMAGE... -o OUTPUT.pdf [--page-size A4|image] "
            "[--collision fail|rename|overwrite]"
        ),
        "pdf-to-images": (
            "ldf pdf-to-images INPUT.pdf -d OUTPUT_DIR "
            "[--format png --dpi 300] [--preset llm] [--collision fail|rename|overwrite]"
        ),
        "pdf-to-markdown": (
            "ldf pdf-to-md INPUT.pdf -o OUTPUT.md [--pages RANGE] "
            "[--format md|txt|jsonl] [--no-page-anchors] [--tables] "
            "[--collision fail|rename|overwrite]"
        ),
        "markdown-to-pdf": (
            "ldf md-to-pdf INPUT.md -o OUTPUT.pdf "
            "[--paper A4|Letter|Legal] [--margin MM] [--toc] "
            "[--collision fail|rename|overwrite]"
        ),
        "convert-images": (
            "ldf convert-images IMAGE... -d OUTPUT_DIR [--preset llm] "
            "[--collision fail|rename|overwrite]"
        ),
    }
)

_EXIT_CODES: Final[tuple[tuple[int, str], ...]] = (
    (0, "success"),
    (1, "operation failed"),
    (2, "usage error (bad arguments, bad page range, or missing file)"),
    (3, "no engine available for the operation"),
    (4, "output validation or strict-fidelity policy failed before publication"),
    (5, "output exists and collision policy is fail"),
    (130, "cancelled or cooperative job timeout"),
)

_GOTCHAS: Final[tuple[tuple[str, str], ...]] = (
    (
        "encrypted-inputs",
        "Inspect encrypted inputs first. For non-interactive use, put global "
        "--password-stdin before the command or set LDF_PASSWORD; never put a password in argv.",
    ),
    (
        "collision-policy",
        "Existing outputs fail with exit 5 by default. --collision is a command-level option: "
        "put it after the subcommand, for example `ldf merge ... --collision rename`.",
    ),
    (
        "glob-expansion",
        "LocalDocForge expands input globs itself. Quote patterns when the shell would expand "
        "them before ldf receives them.",
    ),
    (
        "warning-codes",
        "Treat warning arrays (often shortened to warnings[]) as actionable. Check "
        "fidelity_status and fidelity_coverage first: an empty fidelity_warnings[] array is not a "
        "clean verdict when coverage is none or partial. Each fidelity warning has a stable code, "
        "basis, impact, and optional remedy.",
    ),
    (
        "fitness-check",
        "Originals are never modified and generated PDFs are validated before publication, "
        "but still spot-check whether the output is fit for the requested use. A 110 DPI PNG "
        "render is usually sufficient for a low-cost layout check.",
    ),
    (
        "strict-fidelity",
        "Use global --strict-fidelity before the command when every result other than "
        "complete/no-known-loss must be refused before publication.",
    ),
    (
        "mcp",
        "Use `ldf mcp` for the synchronous local stdio tool surface; it is generated from the "
        "same implemented capability registry and uses the same validation pipeline.",
    ),
)

_WORKFLOW: Final[tuple[tuple[str, str], ...]] = (
    (
        "verify",
        "Run ldf with global --json before the command, require exit 0, inspect "
        "fidelity_status and fidelity_coverage, then inspect both warning arrays (including "
        "fidelity basis, impact, and remedy) before checking page counts, file sizes, or a "
        "rendered sample. Warning silence is not an assessment unless coverage is complete.",
    ),
    (
        "fallback",
        "If the result is unsatisfactory, unsupported, or refused, use an appropriate fallback "
        "and record why it was needed.",
    ),
    (
        "review",
        "Append the outcome to the agent feedback log when the run failed, was unsatisfactory, "
        "or required fallback; a one-line smooth-success entry is optional.",
    ),
)

_FEEDBACK_RULES: Final[tuple[tuple[str, str], ...]] = (
    (
        "append-only",
        "Create the user-local file on the first recorded outcome; after that, append new "
        "entries and never edit or delete existing entries.",
    ),
    (
        "required-outcomes",
        "An entry is required for a failed or unsatisfactory run and whenever you fall back; "
        "a one-line smooth-success entry is welcome but optional.",
    ),
    (
        "write-scope",
        "Recording an outcome does not authorize changes to LocalDocForge source or "
        "documentation; make product changes only when the user requests them.",
    ),
    (
        "privacy",
        "Describe documents generically; do not include sensitive paths or document text. "
        "Filenames are acceptable.",
    ),
)

_FEEDBACK_FILENAME: Final[str] = "feedback.md"
_FEEDBACK_DIRECTORY: Final[str] = "localdocforge"
_UNSAFE_FEEDBACK_PATH_CHARACTERS: Final[frozenset[str]] = frozenset({"\r", "\n", "`"})


class AgentBriefError(RuntimeError):
    """The registry, usage templates, or local feedback path cannot form an honest brief."""


class CapabilityRegistry(Protocol):
    def capabilities(self) -> list[Capability]: ...


@dataclass(frozen=True)
class BriefItem:
    id: str
    text: str

    def to_dict(self) -> dict[str, str]:
        return {"id": self.id, "text": self.text}


@dataclass(frozen=True)
class ExitCodeEntry:
    code: int
    meaning: str

    def to_dict(self) -> dict[str, int | str]:
        return {"code": self.code, "meaning": self.meaning}


@dataclass(frozen=True)
class BriefCapability:
    id: str
    title: str
    category: str
    available: bool
    engines: tuple[str, ...]
    missing_requirements: tuple[str, ...]
    install_hint: str
    notes: str
    usage: str

    def _validate_implemented_identity(self) -> None:
        matching = tuple(spec for spec in CAPABILITY_SPECS if spec.id == self.id)
        if len(matching) != 1 or not matching[0].implemented:
            raise AgentBriefError(
                f"capability {self.id!r} is not an implemented CAPABILITY_SPECS entry"
            )
        spec = matching[0]
        actual_metadata = (self.title, self.category, self.install_hint, self.notes)
        expected_metadata = (spec.title, spec.category, spec.install_hint, spec.notes)
        usage_matches = self.usage == USAGE_BY_CAPABILITY_ID.get(self.id)
        if actual_metadata != expected_metadata or not usage_matches:
            raise AgentBriefError(
                f"renderable capability {self.id!r} does not match its authoritative spec/template"
            )
        if self.available == bool(self.missing_requirements):
            raise AgentBriefError(
                f"renderable capability {self.id!r} has contradictory live availability state"
            )

    def to_dict(self) -> dict[str, object]:
        self._validate_implemented_identity()
        return {
            "id": self.id,
            "title": self.title,
            "category": self.category,
            "implemented": True,
            "available": self.available,
            "engines": list(self.engines),
            "missing_requirements": list(self.missing_requirements),
            "install_hint": self.install_hint,
            "notes": self.notes,
            "usage": self.usage,
        }


@dataclass(frozen=True)
class FeedbackInfo:
    path: Path
    rules: tuple[BriefItem, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "path": str(self.path),
            "rules": [rule.to_dict() for rule in self.rules],
        }


@dataclass(frozen=True)
class AgentBrief:
    schema_version: int
    localdocforge_version: str
    generated_from: str
    capabilities: tuple[BriefCapability, ...]
    exit_codes: tuple[ExitCodeEntry, ...]
    gotchas: tuple[BriefItem, ...]
    workflow: tuple[BriefItem, ...]
    feedback: FeedbackInfo

    def _validate_capabilities(self) -> None:
        expected_ids = tuple(spec.id for spec in CAPABILITY_SPECS if spec.implemented)
        actual_ids = tuple(capability.id for capability in self.capabilities)
        if actual_ids != expected_ids:
            raise AgentBriefError(
                "renderable agent-brief capabilities must exactly match implemented "
                f"CAPABILITY_SPECS in registry order (expected={expected_ids}, actual={actual_ids})"
            )
        for capability in self.capabilities:
            capability._validate_implemented_identity()

    def __post_init__(self) -> None:
        self._validate_capabilities()

    def to_dict(self) -> dict[str, object]:
        self._validate_capabilities()
        return {
            "schema_version": self.schema_version,
            "localdocforge_version": self.localdocforge_version,
            "generated_from": self.generated_from,
            "capabilities": [capability.to_dict() for capability in self.capabilities],
            "exit_codes": [entry.to_dict() for entry in self.exit_codes],
            "gotchas": [gotcha.to_dict() for gotcha in self.gotchas],
            "workflow": [step.to_dict() for step in self.workflow],
            "feedback": self.feedback.to_dict(),
        }


def _resolve_feedback_path(path: Path, *, strict_offline: bool) -> Path:
    """Resolve one render-safe feedback path and enforce strict locality."""
    try:
        if _UNSAFE_FEEDBACK_PATH_CHARACTERS.intersection(os.fspath(path)):
            raise AgentBriefError("could not resolve the user-local feedback path")
        if strict_offline and is_remote_path(path):
            raise AgentBriefError(
                "strict-offline mode requires the user-local feedback path to be on a local drive"
            )
        resolved = path.resolve()
        if _UNSAFE_FEEDBACK_PATH_CHARACTERS.intersection(os.fspath(resolved)):
            raise AgentBriefError("could not resolve the user-local feedback path")
        if strict_offline and is_remote_path(resolved):
            raise AgentBriefError(
                "strict-offline mode requires the user-local feedback path to be on a local drive"
            )
        return resolved
    except AgentBriefError:
        raise
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise AgentBriefError("could not resolve the user-local feedback path") from exc


def resolve_feedback_log_path(*, strict_offline: bool = False) -> Path:
    """Return a deterministic user-local feedback path without creating it."""
    try:
        environment_root = (
            os.environ.get("LOCALAPPDATA")
            if sys.platform == "win32"
            else os.environ.get("XDG_STATE_HOME")
        )

        state_root: Path | None = None
        if environment_root is not None:
            candidate_root = Path(environment_root)
            remote_environment_root = is_remote_path(candidate_root)
            if strict_offline and remote_environment_root:
                raise AgentBriefError(
                    "strict-offline mode requires the user-local feedback path to be on a "
                    "local drive"
                )
            if (
                candidate_root.is_absolute()
                and not remote_environment_root
                and not _UNSAFE_FEEDBACK_PATH_CHARACTERS.intersection(environment_root)
            ):
                state_root = candidate_root

        if state_root is None:
            state_root = Path.home() / ".local" / "state"
            state_root_text = os.fspath(state_root)
            if (
                not state_root.is_absolute()
                or _UNSAFE_FEEDBACK_PATH_CHARACTERS.intersection(state_root_text)
            ):
                raise AgentBriefError("could not resolve the user-local feedback path")
        unresolved = state_root / _FEEDBACK_DIRECTORY / _FEEDBACK_FILENAME
    except AgentBriefError:
        raise
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise AgentBriefError("could not resolve the user-local feedback path") from exc
    return _resolve_feedback_path(unresolved, strict_offline=strict_offline)


def _validate_and_build_capabilities(
    specs: Sequence[CapabilitySpec],
    live_capabilities: Sequence[Capability],
    usage_by_id: Mapping[str, str],
) -> tuple[BriefCapability, ...]:
    spec_ids = [spec.id for spec in specs]
    if len(spec_ids) != len(set(spec_ids)):
        raise AgentBriefError("CAPABILITY_SPECS contains duplicate capability ids")

    live_ids = [capability.id for capability in live_capabilities]
    if len(live_ids) != len(set(live_ids)):
        raise AgentBriefError("the live capability probe returned duplicate capability ids")
    missing_live = sorted(set(spec_ids) - set(live_ids))
    extra_live = sorted(set(live_ids) - set(spec_ids))
    if missing_live or extra_live:
        raise AgentBriefError(
            "the live capability probe does not mirror CAPABILITY_SPECS "
            f"(missing={missing_live}, extra={extra_live})"
        )

    implemented_ids = [spec.id for spec in specs if spec.implemented]
    missing_templates = sorted(set(implemented_ids) - set(usage_by_id))
    stale_templates = sorted(set(usage_by_id) - set(implemented_ids))
    if missing_templates or stale_templates:
        raise AgentBriefError(
            "agent-brief usage templates do not match implemented capabilities "
            f"(missing={missing_templates}, stale={stale_templates})"
        )
    invalid_templates = sorted(
        capability_id
        for capability_id, usage in usage_by_id.items()
        if not usage.strip() or "\n" in usage or "\r" in usage or not usage.startswith("ldf ")
    )
    if invalid_templates:
        raise AgentBriefError(
            f"agent-brief usage templates must be one-line ldf commands: {invalid_templates}"
        )

    live_by_id = {capability.id: capability for capability in live_capabilities}
    rendered: list[BriefCapability] = []
    for spec in specs:
        capability = live_by_id[spec.id]
        actual_metadata = (
            capability.title,
            capability.category,
            capability.install_hint,
            capability.notes,
        )
        expected_metadata = (spec.title, spec.category, spec.install_hint, spec.notes)
        if actual_metadata != expected_metadata:
            raise AgentBriefError(
                f"live capability metadata does not match CAPABILITY_SPECS for {spec.id!r}"
            )
        if capability.available and capability.missing_requirements:
            raise AgentBriefError(f"available capability {spec.id!r} returned missing requirements")
        if not capability.available and not capability.missing_requirements:
            raise AgentBriefError(
                f"unavailable capability {spec.id!r} did not explain its missing requirements"
            )
        if not spec.implemented:
            continue
        rendered.append(
            BriefCapability(
                id=capability.id,
                title=capability.title,
                category=capability.category,
                available=capability.available,
                engines=tuple(capability.engines),
                missing_requirements=tuple(capability.missing_requirements),
                install_hint=capability.install_hint,
                notes=capability.notes,
                usage=usage_by_id[spec.id],
            )
        )
    return tuple(rendered)


def build_agent_brief(
    registry: CapabilityRegistry | None = None,
    *,
    feedback_path: Path | None = None,
    strict_offline: bool = False,
) -> AgentBrief:
    """Build one immutable brief snapshot from specs and one live probe snapshot."""
    active_registry = default_registry() if registry is None else registry
    live_capabilities = active_registry.capabilities()
    capabilities = _validate_and_build_capabilities(
        CAPABILITY_SPECS,
        live_capabilities,
        USAGE_BY_CAPABILITY_ID,
    )
    if feedback_path is None:
        resolved_feedback = resolve_feedback_log_path(strict_offline=strict_offline)
    else:
        resolved_feedback = _resolve_feedback_path(
            feedback_path,
            strict_offline=strict_offline,
        )
    return AgentBrief(
        schema_version=SCHEMA_VERSION,
        localdocforge_version=__version__,
        generated_from="CAPABILITY_SPECS + one live EngineRegistry.capabilities() probe",
        capabilities=capabilities,
        exit_codes=tuple(ExitCodeEntry(code, meaning) for code, meaning in _EXIT_CODES),
        gotchas=tuple(BriefItem(item_id, text) for item_id, text in _GOTCHAS),
        workflow=tuple(BriefItem(item_id, text) for item_id, text in _WORKFLOW),
        feedback=FeedbackInfo(
            path=resolved_feedback,
            rules=tuple(BriefItem(item_id, text) for item_id, text in _FEEDBACK_RULES),
        ),
    )


def render_markdown(brief: AgentBrief) -> str:
    """Render the same typed snapshot used by JSON as compact Markdown."""
    brief._validate_capabilities()
    lines = [
        "# LocalDocForge agent brief",
        "",
        f"Version {brief.localdocforge_version}. Generated from `{brief.generated_from}`.",
        "Only `implemented=True` capabilities render; live availability is shown explicitly.",
        "",
        "## Implemented commands",
        "",
    ]
    for capability in brief.capabilities:
        if capability.available:
            engine_text = ", ".join(capability.engines) or "engine-independent"
            status = f"available via {engine_text}"
        else:
            reason = "; ".join(capability.missing_requirements) or "live probe unavailable"
            status = f"unavailable: {reason}"
        lines.extend(
            (
                f"- `{capability.id}` — **{status}** — `{capability.usage}`",
                f"  {capability.title} ({capability.category}).",
            )
        )

    lines.extend(("", "## Exit codes", "", "| Code | Meaning |", "|---:|---|"))
    lines.extend(f"| {entry.code} | {entry.meaning} |" for entry in brief.exit_codes)

    lines.extend(("", "## Agent gotchas", ""))
    lines.extend(f"- **{gotcha.id}:** {gotcha.text}" for gotcha in brief.gotchas)

    lines.extend(("", "## Verify -> fallback -> review", ""))
    lines.extend(
        f"{index}. **{step.id}:** {step.text}" for index, step in enumerate(brief.workflow, 1)
    )

    lines.extend(("", "## Agent feedback", "", f"`{brief.feedback.path}`", "", "Rules:", ""))
    lines.extend(f"- **{rule.id}:** {rule.text}" for rule in brief.feedback.rules)
    return "\n".join(lines)
