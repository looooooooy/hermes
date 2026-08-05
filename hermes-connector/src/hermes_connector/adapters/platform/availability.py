from __future__ import annotations

from dataclasses import dataclass


class PlatformUnavailable(RuntimeError):
    """Raised when no verified Connector adapters exist for a host platform."""


@dataclass(frozen=True)
class PlatformAvailability:
    platform_name: str
    available: bool
    capabilities: frozenset[str]
    unavailable_reason: str | None = None

    def require_available(self) -> None:
        if self.available:
            return
        reason = self.unavailable_reason or "no verified adapters are available"
        raise PlatformUnavailable(
            f"Hermes Connector is unavailable on {self.platform_name}: {reason}"
        )
