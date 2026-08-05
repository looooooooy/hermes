from __future__ import annotations

from hermes_connector.adapters.contract_codec import InvalidEnvelope
from hermes_connector.adapters.platform.macos.agent_discovery import (
    MacOSAgentDiscovery,
)
from hermes_connector.adapters.platform.macos.local_gateway_transport import (
    MacOSLocalGatewayTransport,
)
from hermes_connector.domain.local_gateway import AgentEndpoint


class MacOSLocalRuntimePreflight:
    """Read-only Local runtime descriptor and kernel peer proof gate."""

    def __init__(
        self,
        *,
        discovery: MacOSAgentDiscovery,
        transport: MacOSLocalGatewayTransport,
        timeout_seconds: float,
    ) -> None:
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
        except (InvalidEnvelope, OSError, TimeoutError):
            return None
        return endpoint
