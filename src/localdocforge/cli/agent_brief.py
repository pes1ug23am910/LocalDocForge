"""Registry-derived, deterministic usage guidance for document agents."""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path
from types import MappingProxyType
from typing import Final, Protocol
from urllib.parse import urlsplit
from urllib.request import url2pathname

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
        "merge": "ldf merge INPUT.pdf [INPUT.pdf ...] -o OUTPUT.pdf [--pages RANGE ...]",
        "split": "ldf split INPUT.pdf -d OUTPUT_DIR [--pages RANGE | --every N]",
        "remove-pages": "ldf remove-pages INPUT.pdf --pages RANGE -o OUTPUT.pdf",
        "extract-pages": "ldf extract-pages INPUT.pdf --pages RANGE -o OUTPUT.pdf",
        "organize": "ldf organize INPUT.pdf --order RANGE -o OUTPUT.pdf",
        "rotate": "ldf rotate INPUT.pdf --degrees 90 [--pages RANGE] -o OUTPUT.pdf",
        "crop": "ldf crop INPUT.pdf --box X0,Y0,X1,Y1 [--pages RANGE] -o OUTPUT.pdf",
        "inspect": "ldf inspect INPUT.pdf",
        "compress": "ldf compress INPUT.pdf -o OUTPUT.pdf",
        "images-to-pdf": "ldf images-to-pdf IMAGE... -o OUTPUT.pdf [--page-size A4]",
        "pdf-to-images": (
            "ldf pdf-to-images INPUT.pdf -d OUTPUT_DIR "
            "[--format png --dpi 300] [--preset llm]"
        ),
        "pdf-to-markdown": (
            "ldf pdf-to-md INPUT.pdf -o OUTPUT.md [--pages RANGE] "
            "[--format md|txt|jsonl] [--no-page-anchors] [--tables]"
        ),
        "markdown-to-pdf": (
            "ldf md-to-pdf INPUT.md -o OUTPUT.pdf "
            "[--paper A4|Letter|Legal] [--margin MM] [--toc]"
        ),
        "convert-images": "ldf convert-images IMAGE... -d OUTPUT_DIR [--preset llm]",
    }
)

_EXIT_CODES: Final[tuple[tuple[int, str], ...]] = (
    (0, "success"),
    (1, "operation failed"),
    (2, "usage error (bad arguments, bad page range, or missing file)"),
    (3, "no engine available for the operation"),
    (4, "generated-output validation failed before publication"),
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
        "Existing outputs fail with exit 5 by default; choose --collision rename or overwrite "
        "explicitly on writing commands.",
    ),
    (
        "glob-expansion",
        "LocalDocForge expands input globs itself. Quote patterns when the shell would expand "
        "them before ldf receives them.",
    ),
    (
        "warning-codes",
        "Treat warning arrays (often shortened to warnings[]) as actionable. Current conversion "
        "JSON reports expose security_warnings[] and fidelity_warnings[]; each entry has a stable "
        "code value.",
    ),
    (
        "fitness-check",
        "Originals are never modified and generated PDFs are validated before publication, "
        "but still spot-check whether the output is fit for the requested use.",
    ),
)

_WORKFLOW: Final[tuple[tuple[str, str], ...]] = (
    (
        "verify",
        "Run ldf with global --json before the command, require exit 0, inspect both warning "
        "arrays and their code values, then check page counts, file sizes, or a rendered sample.",
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
    ("append-only", "Append new entries at the end; never edit or delete existing entries."),
    (
        "required-outcomes",
        "An entry is required for a failed or unsatisfactory run and whenever you fall back; "
        "a one-line smooth-success entry is welcome but optional.",
    ),
    (
        "write-scope",
        "Do not change anything else in the LocalDocForge repository unless the user explicitly "
        "commissioned development work.",
    ),
    (
        "privacy",
        "Describe documents generically; do not include sensitive paths or document text. "
        "Filenames are acceptable.",
    ),
)

_FEEDBACK_RELATIVE_PATH: Final[Path] = Path("docs") / "AGENT_FEEDBACK.md"


class AgentBriefError(RuntimeError):
    """The registry, usage templates, or feedback path cannot form an honest brief."""


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


def _direct_url_root(*, strict_offline: bool) -> Path | None:
    """Return a local checkout recorded by pip's direct_url metadata, if any."""
    try:
        raw = distribution("localdocforge").read_text("direct_url.json")
    except (OSError, PackageNotFoundError):
        return None
    if not raw:
        return None
    try:
        url = json.loads(raw).get("url")
    except (AttributeError, json.JSONDecodeError):
        return None
    if not isinstance(url, str):
        return None
    parsed = urlsplit(url)
    if parsed.scheme != "file" or parsed.netloc not in ("", "localhost"):
        return None
    root = Path(url2pathname(parsed.path))
    if strict_offline and is_remote_path(root):
        return None
    return root


def _feedback_candidate_roots(*, strict_offline: bool) -> Iterator[str | Path]:
    """Yield checkout roots lazily so a valid early candidate short-circuits."""
    module_path = Path(__file__)
    if len(module_path.parents) > 3:
        yield module_path.parents[3]
    direct_url_root = _direct_url_root(strict_offline=strict_offline)
    if direct_url_root is not None:
        yield direct_url_root
    yield Path(sys.prefix).parent
    yield from (entry for entry in sys.path if entry)
    current = Path.cwd()
    yield current
    yield from current.parents


def resolve_feedback_log_path(*, strict_offline: bool = False) -> Path:
    """Locate the tracked feedback log without a machine-specific hardcoded path."""
    for raw_root in _feedback_candidate_roots(strict_offline=strict_offline):
        try:
            root = Path(raw_root)
            if strict_offline and is_remote_path(root):
                continue
            root = root.resolve()
            candidate = (root / _FEEDBACK_RELATIVE_PATH).resolve()
            if strict_offline and is_remote_path(candidate):
                continue
            if candidate.is_file() and (root / "src" / "localdocforge").is_dir():
                return candidate
        except (OSError, RuntimeError, TypeError, ValueError):
            continue

    raise AgentBriefError(
        "could not locate an existing docs/AGENT_FEEDBACK.md in a LocalDocForge source "
        "checkout; detached wheel/VCS installs must run from a checkout because the writable "
        "feedback log is intentionally not packaged"
    )


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
            raise AgentBriefError(
                f"available capability {spec.id!r} returned missing requirements"
            )
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
    resolved_feedback = (
        resolve_feedback_log_path(strict_offline=strict_offline)
        if feedback_path is None
        else feedback_path.resolve()
    )
    if not resolved_feedback.is_file():
        raise AgentBriefError(f"agent feedback log does not exist: {resolved_feedback}")
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
