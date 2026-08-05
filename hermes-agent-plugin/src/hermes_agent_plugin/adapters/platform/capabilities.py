"""Shared shape for truthful platform capability declarations."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class LocalGatewayPlatformCapabilities:
    """Availability facts used by the Plugin composition root."""

    platform: str
    available: bool
    transport: str | None
    features: frozenset[str]
    unavailable_reason: str | None = None


class PlatformLocalGatewayUnavailable(RuntimeError):
    """Raised when no verified Local Gateway exists for the host platform."""


class UnavailableLocalRelayBackend:
    """Explicit fail-closed backend for an unimplemented host platform."""

    def __init__(
        self,
        reason: str,
        *,
        error_type: type[RuntimeError] = PlatformLocalGatewayUnavailable,
    ) -> None:
        self._reason = reason
        self._error_type = error_type

    def _raise(self) -> None:
        raise self._error_type(self._reason)

    def start_control_endpoint(self, **_kwargs: object) -> None:
        self._raise()

    def list_control_endpoints(self) -> list[object]:
        self._raise()

    def create_control_relay_hub(self, *, current_pid: int | None) -> None:
        del current_pid
        self._raise()

    def start_observer_endpoint(self, **_kwargs: object) -> None:
        self._raise()

    def list_observer_endpoints(self) -> list[object]:
        self._raise()

    def create_observer_relay_hub(self, *, current_pid: int | None) -> None:
        del current_pid
        self._raise()
