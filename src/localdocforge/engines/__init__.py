"""Engine adapters and the registry that gates every capability claim."""

from localdocforge.engines.base import EngineAdapter, EngineUnavailableError
from localdocforge.engines.registry import EngineRegistry, default_registry

__all__ = [
    "EngineAdapter",
    "EngineRegistry",
    "EngineUnavailableError",
    "default_registry",
]
