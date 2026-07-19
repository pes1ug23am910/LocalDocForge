"""Engine adapter contract.

Every engine — in-process Python library or external executable — is wrapped
in an adapter. The CLI, API, and UI never touch a library or binary directly;
they ask the registry for an engine that supports the operation, and the
registry only hands out engines whose runtime probe succeeded.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from localdocforge.domain.models import EngineInfo


class EngineUnavailableError(Exception):
    """No installed engine supports the requested operation."""

    def __init__(self, operation: str, hints: list[str] | None = None) -> None:
        self.operation = operation
        self.hints = hints or []
        message = f"No available engine supports operation {operation!r}."
        if self.hints:
            message += " Install one of: " + "; ".join(self.hints)
        super().__init__(message)


class EngineAdapter(ABC):
    """Common contract for all engines."""

    #: Stable identifier used in reports, config, and --engine flags.
    name: str

    @abstractmethod
    def probe(self) -> EngineInfo:
        """Check availability at runtime. Must never raise; failure is an
        EngineInfo with ``available=False`` and an install hint."""

    @abstractmethod
    def supported_operations(self) -> frozenset[str]:
        """Operation ids this engine can execute when available."""

    def supports(self, operation: str) -> bool:
        return operation in self.supported_operations()

    def version(self) -> str | None:
        return self.probe().version

    def license_info(self) -> str | None:
        return self.probe().license
