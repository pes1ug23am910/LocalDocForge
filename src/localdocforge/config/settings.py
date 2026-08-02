"""Runtime configuration.

Sources, in precedence order: explicit constructor args (CLI flags), then
``LDF_``-prefixed environment variables, then defaults. Everything defaults
to the private, local, bounded choice.
"""

from __future__ import annotations

import math
from pathlib import Path

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from localdocforge.domain.models import ResourceLimits
from localdocforge.jobs.workspace import CollisionPolicy, default_jobs_root
from localdocforge.security.paths import (
    PathSecurityError,
    is_remote_path,
    validate_path_before_access,
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="LDF_", env_nested_delimiter="__")

    #: Application privacy policy. This rejects configured remote paths and
    #: enables Python-level network guards as defense in depth; it is not an OS
    #: network sandbox for native engines.
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

    #: Background worker admission controls. Every API conversion uses one
    #: fresh spawned process; these values bound how many may run or wait.
    api_max_concurrent_jobs: int = 2
    api_max_queued_jobs: int = 16
    api_max_active_jobs_per_client: int = 4
    api_max_upload_bytes: int = 2 * 1024**3
    api_rate_limit_jobs: int = 30
    api_rate_limit_window_seconds: float = 60.0
    api_max_progress_events: int = 256

    verbose: bool = False

    @model_validator(mode="after")
    def strict_offline_paths_are_local(self) -> Settings:
        positive = {
            "api_max_concurrent_jobs": self.api_max_concurrent_jobs,
            "api_max_active_jobs_per_client": self.api_max_active_jobs_per_client,
            "api_max_upload_bytes": self.api_max_upload_bytes,
            "api_rate_limit_jobs": self.api_rate_limit_jobs,
            "api_rate_limit_window_seconds": self.api_rate_limit_window_seconds,
            "api_max_progress_events": self.api_max_progress_events,
        }
        invalid = [
            name
            for name, value in positive.items()
            if value <= 0 or (isinstance(value, float) and not math.isfinite(value))
        ]
        if self.api_max_queued_jobs < 0:
            invalid.append("api_max_queued_jobs")
        if invalid:
            raise ValueError(f"API admission settings must be positive: {', '.join(invalid)}")
        configured = [self.jobs_root or default_jobs_root()]
        configured.extend(self.allowed_output_roots or [])
        for path in configured:
            if path is None:
                continue
            try:
                validate_path_before_access(
                    path,
                    what="configured workspace or output root",
                    require_local=self.strict_offline,
                    reject_reparse=self.strict_offline or not is_remote_path(path),
                )
            except PathSecurityError as exc:
                raise ValueError(str(exc)) from exc
        return self


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
