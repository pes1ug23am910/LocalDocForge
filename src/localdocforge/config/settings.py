"""Runtime configuration.

Sources, in precedence order: explicit constructor args (CLI flags), then
``LDF_``-prefixed environment variables, then defaults. Everything defaults
to the private, local, bounded choice.
"""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

from localdocforge.domain.models import ResourceLimits
from localdocforge.jobs.workspace import CollisionPolicy


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="LDF_", env_nested_delimiter="__")

    #: Hard privacy switch. When on, every code path that could touch the
    #: network (none exist today outside pip-installed engines) must refuse,
    #: regardless of any other setting.
    strict_offline: bool = False

    #: Where per-job scratch directories live. None = system temp.
    jobs_root: Path | None = None

    #: Default collision behavior for outputs; every CLI command can override.
    collision: CollisionPolicy = CollisionPolicy.FAIL

    #: Optional whitelist of directories outputs may be written to.
    #: None (default) = anywhere the OS user can write, since CLI users chose
    #: the path themselves. The HTTP API layers its own, stricter containment.
    allowed_output_roots: list[Path] | None = None

    #: Per-job resource bounds.
    limits: ResourceLimits = ResourceLimits()

    #: Web API bind address; anything but loopback requires explicit opt-in.
    bind_host: str = "127.0.0.1"
    bind_port: int = 8477

    verbose: bool = False


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def set_settings(settings: Settings) -> None:
    """Install CLI-resolved settings as the process-wide configuration."""
    global _settings
    _settings = settings
