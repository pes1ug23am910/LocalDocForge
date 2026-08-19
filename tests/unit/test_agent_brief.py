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
FEEDBACK_PATH = ROOT / ".localdocforge" / "feedback.md"


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
    assert "--preset llm" in USAGE_BY_CAPABILITY_ID["pdf-to-images"]
    assert USAGE_BY_CAPABILITY_ID["pdf-to-markdown"] == (
        "ldf pdf-to-md INPUT.pdf -o OUTPUT.md [--pages RANGE] "
        "[--format md|txt|jsonl] [--no-page-anchors] [--tables] "
        "[--collision fail|rename|overwrite]"
    )
    assert USAGE_BY_CAPABILITY_ID["markdown-to-pdf"] == (
        "ldf md-to-pdf INPUT.md -o OUTPUT.pdf "
        "[--paper A4|Letter|Legal] [--margin MM] [--toc] "
        "[--collision fail|rename|overwrite]"
    )
    assert USAGE_BY_CAPABILITY_ID["ocr"] == (
        "ldf ocr INPUT.pdf -o OUTPUT.pdf [--language eng] [--sidecar OUTPUT.txt] "
        "[--skip-text | --force-ocr] [--collision fail|rename|overwrite]"
    )
    assert "--page-size A4|image" in USAGE_BY_CAPABILITY_ID["images-to-pdf"]
    assert all(
        "--collision fail|rename|overwrite" in usage
        for capability_id, usage in USAGE_BY_CAPABILITY_ID.items()
        if capability_id != "inspect"
    )


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
    verify = next(item for item in payload["workflow"] if item["id"] == "verify")
    assert "fidelity_status and fidelity_coverage" in verify["text"]
    assert "Warning silence is not an assessment" in verify["text"]
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
    assert "110 DPI" in first
    assert "global --strict-fidelity before the command" in first
    assert "complete/no-known-loss" in first
    assert "ldf mcp" in first
    assert "command-level option" in first
    assert "fidelity_status and fidelity_coverage" in first
    assert "Warning silence is not an assessment" in first
    assert "basis, impact, and remedy" in first


def test_feedback_path_is_user_local_and_independent_of_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_root = tmp_path / "state"
    monkeypatch.setattr(agent_brief.sys, "platform", "linux")
    monkeypatch.setenv("XDG_STATE_HOME", str(state_root))
    monkeypatch.chdir(tmp_path)
    resolved = resolve_feedback_log_path()
    assert resolved == (state_root / "localdocforge" / "feedback.md").resolve()
    assert resolved.is_absolute()
    assert not resolved.exists()


def test_feedback_path_uses_local_app_data_on_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root = tmp_path / "LocalAppData"
    monkeypatch.setattr(agent_brief.sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(state_root))
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    assert resolve_feedback_log_path() == (
        state_root / "localdocforge" / "feedback.md"
    ).resolve()


def test_windows_does_not_treat_xdg_state_home_as_its_platform_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    monkeypatch.setattr(agent_brief.sys, "platform", "win32")
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-must-not-be-used"))
    monkeypatch.setattr(agent_brief.Path, "home", classmethod(lambda cls: home))

    assert resolve_feedback_log_path() == (
        home / ".local" / "state" / "localdocforge" / "feedback.md"
    ).resolve()


def test_feedback_path_falls_back_to_home_state_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(agent_brief.sys, "platform", "linux")
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setattr(agent_brief.Path, "home", classmethod(lambda cls: tmp_path))
    assert resolve_feedback_log_path() == (
        tmp_path / ".local" / "state" / "localdocforge" / "feedback.md"
    ).resolve()


@pytest.mark.parametrize(
    ("platform", "environment_name"),
    (("win32", "LOCALAPPDATA"), ("linux", "XDG_STATE_HOME")),
)
def test_relative_environment_feedback_root_falls_back_without_using_cwd(
    platform: str,
    environment_name: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    first_cwd = tmp_path / "first"
    second_cwd = tmp_path / "second"
    first_cwd.mkdir()
    second_cwd.mkdir()
    monkeypatch.setattr(agent_brief.sys, "platform", platform)
    monkeypatch.setenv(environment_name, "relative-state")
    monkeypatch.setattr(agent_brief.Path, "home", classmethod(lambda cls: home))

    monkeypatch.chdir(first_cwd)
    first = resolve_feedback_log_path(strict_offline=True)
    monkeypatch.chdir(second_cwd)
    second = resolve_feedback_log_path(strict_offline=True)

    expected = (home / ".local" / "state" / "localdocforge" / "feedback.md").resolve()
    assert first == second == expected


@pytest.mark.parametrize("unsafe_character", ("\n", "\r", "`"))
def test_markup_unsafe_environment_feedback_root_falls_back(
    unsafe_character: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    monkeypatch.setattr(agent_brief.sys, "platform", "linux")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / f"state{unsafe_character}injected"))
    monkeypatch.setattr(agent_brief.Path, "home", classmethod(lambda cls: home))

    assert resolve_feedback_log_path() == (
        home / ".local" / "state" / "localdocforge" / "feedback.md"
    ).resolve()


def test_invalid_home_feedback_fallback_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(agent_brief.sys, "platform", "linux")
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setattr(
        agent_brief.Path,
        "home",
        classmethod(lambda cls: Path("relative-home")),
    )

    with pytest.raises(AgentBriefError, match="could not resolve"):
        resolve_feedback_log_path()


def test_feedback_path_fails_closed_for_remote_state_in_strict_offline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root = tmp_path / "remote-state"
    monkeypatch.setattr(agent_brief.sys, "platform", "linux")
    monkeypatch.setenv("XDG_STATE_HOME", str(state_root))
    monkeypatch.setattr(agent_brief, "is_remote_path", lambda _path: True)
    with pytest.raises(AgentBriefError, match="local drive"):
        resolve_feedback_log_path(strict_offline=True)


def test_remote_environment_feedback_root_falls_back_outside_strict_offline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root = tmp_path / "remote-state"
    home = tmp_path / "home"
    monkeypatch.setattr(agent_brief.sys, "platform", "linux")
    monkeypatch.setenv("XDG_STATE_HOME", str(state_root))
    monkeypatch.setattr(agent_brief.Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(agent_brief, "is_remote_path", lambda path: path == state_root)

    assert resolve_feedback_log_path() == (
        home / ".local" / "state" / "localdocforge" / "feedback.md"
    ).resolve()


def test_remote_home_feedback_fallback_fails_closed_in_strict_offline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(agent_brief.sys, "platform", "linux")
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setattr(agent_brief.Path, "home", classmethod(lambda cls: tmp_path / "home"))
    monkeypatch.setattr(agent_brief, "is_remote_path", lambda _path: True)

    with pytest.raises(AgentBriefError, match="local drive"):
        resolve_feedback_log_path(strict_offline=True)


def test_explicit_feedback_path_does_not_have_to_exist(tmp_path: Path) -> None:
    feedback_path = tmp_path / "new" / "feedback.md"
    brief = build_agent_brief(
        StubRegistry(_live_capabilities()), feedback_path=feedback_path
    )
    assert brief.feedback.path == feedback_path.resolve()
    assert not feedback_path.exists()


def test_explicit_feedback_path_fails_closed_when_remote_in_strict_offline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(agent_brief, "is_remote_path", lambda _path: True)
    with pytest.raises(AgentBriefError, match="local drive"):
        build_agent_brief(
            StubRegistry(_live_capabilities()),
            feedback_path=tmp_path / "feedback.md",
            strict_offline=True,
        )


@pytest.mark.parametrize("unsafe_character", ("\n", "\r", "`"))
def test_explicit_feedback_path_rejects_markup_injection(
    unsafe_character: str,
    tmp_path: Path,
) -> None:
    with pytest.raises(AgentBriefError, match="could not resolve"):
        build_agent_brief(
            StubRegistry(_live_capabilities()),
            feedback_path=tmp_path / f"feedback{unsafe_character}injected.md",
        )


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
