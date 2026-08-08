"""Truthful Windows Local Relay capability boundary."""

from ..capabilities import LocalGatewayPlatformCapabilities

# All three Host endpoint roles now have concrete Named Pipe implementations in
# stacked slices. Overall availability remains false until the Observer/Catalog
# Connector clients and the Windows Host endpoint opener are transaction-tested.
LOCAL_GATEWAY_CAPABILITIES = LocalGatewayPlatformCapabilities(
    platform="windows",
    available=False,
    transport="named-pipe",
    features=frozenset(
        {
            "control.endpoint",
            "local-gateway.handshake",
            "observer.endpoint",
            "session-catalog.endpoint",
        }
    ),
    unavailable_reason="windows_observer_connector_and_host_opener_not_implemented",
)
LOCAL_GATEWAY_AVAILABLE = LOCAL_GATEWAY_CAPABILITIES.available

__all__ = [
    "LOCAL_GATEWAY_AVAILABLE",
    "LOCAL_GATEWAY_CAPABILITIES",
]
