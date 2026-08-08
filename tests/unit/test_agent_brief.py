"""Registry-derived agent brief construction and rendering contracts."""

from __future__ import annotations

from dataclasses import fields, replace
from pathlib import Path

import pytest

from localdocforge.cli import agent_brief
from localdocforge.cli.agent_brief import (
    USAGE_BY_CAPABILITY_ID,
    AgentBriefError,
    BriefCapability,
    _validate_and_build_capabilities,
    build_agent_brief,
    render_markdown,
    resolve_feedback_log_path,
)
from localdocforge.domain.models import Capability
from localdocforge.engines.registry import CAPABILITY_SPECS, CapabilitySpec

ROOT = Path(__file__).resolve().parents[2]
FEEDBACK_PATH = ROOT / "docs" / "AGENT_FEEDBACK.md"


class StubRegistry:
    def __init__(self, capabilities: list[Capability]) -> None:
        self._capabilities = capabilities
        self.calls = 0

    def capabilities(self) -> list[Capability]:
        self.calls += 1
        return list(self._capabilities)


def _live_capabilities(
    *,
    unavailable_id: str | None = None,
    force_available_id: str | None = None,
) -> list[Capability]:
    capabilities: list[Capability] = []
    for spec in CAPABILITY_SPECS:
        available = spec.implemented
        if spec.id == unavailable_id:
            available = False
        if spec.id == force_available_id:
            available = True
        capabilities.append(
            Capability(
                id=spec.id,
                title=spec.title,
                category=spec.category,
                available=available,
                engines=["stub-engine"] if available else [],
                missing_requirements=[] if available else ["synthetic probe unavailable"],
                install_hint=spec.install_hint,
                notes=spec.notes,
            )
        )
    return capabilities


def _capability(capability_id: str, *, available: bool = True) -> Capability:
    return Capability(
        id=capability_id,
        title=capability_id.title(),
        category="Synthetic",
        available=available,
        engines=["stub"] if available else [],
        missing_requirements=[] if available else ["missing stub"],
    )


def test_usage_templates_exactly_cover_implemented_specs() -> None:
    implemented_ids = {spec.id for spec in CAPABILITY_SPECS if spec.implemented}
    assert set(USAGE_BY_CAPABILITY_ID) == implemented_ids
    for usage in USAGE_BY_CAPABILITY_ID.values():
        assert usage.startswith("ldf ")
        assert "\n" not in usage and "\r" not in usage


def test_brief_uses_spec_order_one_probe_and_keeps_unavailable_implemented() -> None:
    first_implemented = next(spec.id for spec in CAPABILITY_SPECS if spec.implemented)
    planned = next(spec.id for spec in CAPABILITY_SPECS if not spec.implemented)
    live = _live_capabilities(
        unavailable_id=first_implemented,
        force_available_id=planned,
    )
    registry = StubRegistry(list(reversed(live)))

    brief = build_agent_brief(registry, feedback_path=FEEDBACK_PATH)

    expected_ids = [spec.id for spec in CAPABILITY_SPECS if spec.implemented]
    assert registry.calls == 1
    assert [capability.id for capability in brief.capabilities] == expected_ids
    by_id = {capability.id: capability for capability in brief.capabilities}
    assert by_id[first_implemented].available is False
    assert by_id[first_implemented].missing_requirements == ("synthetic probe unavailable",)
    assert planned not in by_id  # implemented=False wins over a hostile available=True result


def test_json_snapshot_mirrors_live_registry_fields() -> None:
    live = _live_capabilities()
    brief = build_agent_brief(StubRegistry(live), feedback_path=FEEDBACK_PATH)
    payload = brief.to_dict()
    entries = payload["capabilities"]
    assert isinstance(entries, list)
    live_by_id = {capability.id: capability for capability in live}
    for entry in entries:
        assert isinstance(entry, dict)
        capability = live_by_id[entry["id"]]
        expected = capability.model_dump()
        for field in (
            "id",
            "title",
            "category",
            "available",
            "engines",
            "missing_requirements",
            "install_hint",
            "notes",
        ):
            assert entry[field] == expected[field]
        assert entry["implemented"] is True
        assert entry["usage"] == USAGE_BY_CAPABILITY_ID[capability.id]


def test_markdown_is_deterministic_and_uses_the_same_snapshot() -> None:
    planned = next(spec.id for spec in CAPABILITY_SPECS if not spec.implemented)
    brief = build_agent_brief(
        StubRegistry(_live_capabilities(force_available_id=planned)),
        feedback_path=FEEDBACK_PATH,
    )
    first = render_markdown(brief)
    second = render_markdown(brief)

    assert first == second
    positions = [first.index(f"`{capability.id}`") for capability in brief.capabilities]
    assert positions == sorted(positions)
    assert f"`{planned}`" not in first
    assert str(FEEDBACK_PATH.resolve()) in first
    assert "Verify -> fallback -> review" in first


def test_feedback_path_is_absolute_existing_and_independent_of_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    resolved = resolve_feedback_log_path()
    assert resolved == FEEDBACK_PATH.resolve()
    assert resolved.is_absolute()
    assert resolved.is_file()


def test_feedback_path_short_circuits_before_malformed_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class MalformedPath:
        def __fspath__(self) -> str:
            raise OSError("synthetic inaccessible sys.path entry")

    monkeypatch.setattr(agent_brief.sys, "path", [MalformedPath()])
    assert resolve_feedback_log_path() == FEEDBACK_PATH.resolve()


def test_feedback_path_fails_closed_without_a_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package_file = tmp_path / "venv" / "site-packages" / "localdocforge" / "cli" / "agent_brief.py"
    package_file.parent.mkdir(parents=True)
    package_file.touch()
    unrelated_cwd = tmp_path / "unrelated"
    unrelated_cwd.mkdir()
    monkeypatch.setattr(agent_brief, "__file__", str(package_file))
    monkeypatch.setattr(agent_brief, "_direct_url_root", lambda **_kwargs: None)
    monkeypatch.setattr(agent_brief.sys, "prefix", str(tmp_path / "venv"))
    monkeypatch.setattr(agent_brief.sys, "path", [str(package_file.parent)])
    monkeypatch.chdir(unrelated_cwd)

    with pytest.raises(AgentBriefError, match="detached wheel/VCS installs"):
        resolve_feedback_log_path()


def test_feedback_path_skips_remote_fallback_in_strict_offline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    remote = Path(r"\\server\share")
    monkeypatch.setattr(
        agent_brief,
        "_feedback_candidate_roots",
        lambda **_kwargs: iter((remote, ROOT)),
    )
    assert resolve_feedback_log_path(strict_offline=True) == FEEDBACK_PATH.resolve()


def test_exit_code_table_matches_cli_constants() -> None:
    from localdocforge.cli import main as cli_main

    brief = build_agent_brief(StubRegistry(_live_capabilities()), feedback_path=FEEDBACK_PATH)
    assert [entry.code for entry in brief.exit_codes] == [
        cli_main.EXIT_OK,
        cli_main.EXIT_FAILED,
        cli_main.EXIT_USAGE,
        cli_main.EXIT_NO_ENGINE,
        cli_main.EXIT_VALIDATION,
        cli_main.EXIT_COLLISION,
        cli_main.EXIT_CANCELLED,
    ]


def test_implemented_false_is_impossible_to_render_even_if_live_available() -> None:
    specs = (CapabilitySpec("planned", "Planned", "Synthetic", None, False),)
    rendered = _validate_and_build_capabilities(
        specs,
        [_capability("planned", available=True)],
        {},
    )
    assert rendered == ()


def test_rendered_capability_has_no_false_implemented_state() -> None:
    brief = build_agent_brief(StubRegistry(_live_capabilities()), feedback_path=FEEDBACK_PATH)
    capability = brief.capabilities[0]
    assert "implemented" not in {field.name for field in fields(BriefCapability)}
    assert capability.to_dict()["implemented"] is True


def test_agent_brief_rejects_forged_unimplemented_and_unknown_ids() -> None:
    valid = build_agent_brief(StubRegistry(_live_capabilities()), feedback_path=FEEDBACK_PATH)
    planned = next(spec.id for spec in CAPABILITY_SPECS if not spec.implemented)
    for invalid_id in (planned, "future-unknown-capability"):
        forged = BriefCapability(
            id=invalid_id,
            title="Forged capability",
            category="Synthetic",
            available=True,
            engines=("stub",),
            missing_requirements=(),
            install_hint="",
            notes="",
            usage=f"ldf {invalid_id} INPUT",
        )
        with pytest.raises(AgentBriefError, match="must exactly match implemented"):
            replace(valid, capabilities=(*valid.capabilities, forged))

        tampered = build_agent_brief(
            StubRegistry(_live_capabilities()), feedback_path=FEEDBACK_PATH
        )
        object.__setattr__(tampered, "capabilities", (*tampered.capabilities, forged))
        with pytest.raises(AgentBriefError, match="must exactly match implemented"):
            tampered.to_dict()
        with pytest.raises(AgentBriefError, match="must exactly match implemented"):
            render_markdown(tampered)
        with pytest.raises(AgentBriefError, match="not an implemented"):
            forged.to_dict()

    forged_metadata = replace(valid.capabilities[0], title="OCR PDF")
    with pytest.raises(AgentBriefError, match="authoritative spec/template"):
        replace(valid, capabilities=(forged_metadata, *valid.capabilities[1:]))


def test_implemented_unavailable_is_rendered_with_probe_reason() -> None:
    specs = (CapabilitySpec("ready", "Ready", "Synthetic", None, True),)
    rendered = _validate_and_build_capabilities(
        specs,
        [_capability("ready", available=False)],
        {"ready": "ldf ready INPUT"},
    )
    assert len(rendered) == 1
    assert rendered[0].available is False
    assert rendered[0].missing_requirements == ("missing stub",)


@pytest.mark.parametrize(
    ("specs", "live", "templates", "message"),
    [
        (
            (CapabilitySpec("ready", "Ready", "Synthetic", None, True),),
            [_capability("ready")],
            {},
            "usage templates",
        ),
        (
            (CapabilitySpec("ready", "Ready", "Synthetic", None, True),),
            [_capability("ready")],
            {"ready": "ldf ready", "stale": "ldf stale"},
            "usage templates",
        ),
        (
            (CapabilitySpec("ready", "Ready", "Synthetic", None, True),),
            [_capability("ready"), _capability("ready")],
            {"ready": "ldf ready"},
            "duplicate capability ids",
        ),
        (
            (CapabilitySpec("ready", "Ready", "Synthetic", None, True),),
            [],
            {"ready": "ldf ready"},
            "does not mirror",
        ),
        (
            (CapabilitySpec("ready", "Ready", "Synthetic", None, True),),
            [_capability("ready"), _capability("extra")],
            {"ready": "ldf ready"},
            "does not mirror",
        ),
        (
            (
                CapabilitySpec("ready", "Ready", "Synthetic", None, True),
                CapabilitySpec("ready", "Ready again", "Synthetic", None, True),
            ),
            [_capability("ready")],
            {"ready": "ldf ready"},
            "CAPABILITY_SPECS contains duplicate",
        ),
        (
            (CapabilitySpec("ready", "Ready", "Synthetic", None, True),),
            [_capability("ready")],
            {"ready": "ldf ready\nsecond line"},
            "one-line ldf commands",
        ),
        (
            (CapabilitySpec("ready", "Ready", "Synthetic", None, True),),
            [_capability("ready").model_copy(update={"title": "OCR PDF"})],
            {"ready": "ldf ready"},
            "metadata does not match",
        ),
        (
            (CapabilitySpec("ready", "Ready", "Synthetic", None, True),),
            [_capability("ready").model_copy(update={"missing_requirements": ["contradiction"]})],
            {"ready": "ldf ready"},
            "available capability.*missing requirements",
        ),
    ],
)
def test_contract_drift_fails_loudly(
    specs: tuple[CapabilitySpec, ...],
    live: list[Capability],
    templates: dict[str, str],
    message: str,
) -> None:
    with pytest.raises(AgentBriefError, match=message):
        _validate_and_build_capabilities(specs, live, templates)
