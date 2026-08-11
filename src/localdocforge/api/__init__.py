"""Local HTTP API (localhost only).

The application factory is loaded lazily so worker and operation-model imports
remain independent of FastAPI.
"""

from __future__ import annotations

from typing import Any

__all__ = ["create_app"]


def __getattr__(name: str) -> Any:
    if name == "create_app":
        from localdocforge.api.app import create_app as app_factory

        return app_factory
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
