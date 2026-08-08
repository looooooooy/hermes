"""Platform-neutral read-only local runtime preflight."""

from __future__ import annotations

from typing import Protocol

from hermes_connector.adapters.contract_codec import InvalidEnvelope
from hermes_connector.domain.local_gateway import AgentEndpoint


class _Discovery(Protocol):
    def discover_now(self, profile: str) -> tuple[AgentEndpoint, ...]: ...


class _Transport(Protocol):
    def probe_peer(self, endpoint: AgentEndpoint, *, timeout_seconds: float) -> None: ...


class LocalRuntimePreflight:
    """Require one descriptor plus kernel peer proof without sending a protocol frame."""

    def __init__(
        self,
        *,
        discovery: _Discovery,
        transport: _Transport,
        timeout_seconds: float,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._discovery = discovery
        self._transport = transport
        self._timeout_seconds = timeout_seconds

    def verify(self, profile: str) -> AgentEndpoint | None:
        endpoints = self._discovery.discover_now(profile)
        if len(endpoints) != 1:
            return None
        endpoint = endpoints[0]
        try:
            self._transport.probe_peer(
                endpoint,
                timeout_seconds=self._timeout_seconds,
            )
        except (InvalidEnvelope, OSError, TimeoutError, PermissionError, ValueError):
            return None
        return endpoint


__all__ = ["LocalRuntimePreflight"]
