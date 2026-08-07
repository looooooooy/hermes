"""Truthful Windows Local Gateway capability boundary."""

from ..capabilities import LocalGatewayPlatformCapabilities

# Discovery + LocalHello/Welcome Named Pipes are implemented in this slice, but
# Host activation still requires observer and control endpoints. Keep overall
# availability false until those roles share the same verified transport.
LOCAL_GATEWAY_CAPABILITIES = LocalGatewayPlatformCapabilities(
    platform="windows",
    available=False,
    transport="named-pipe",
    features=frozenset({"local-gateway.handshake"}),
    unavailable_reason="windows_observer_control_not_implemented",
)
LOCAL_GATEWAY_AVAILABLE = LOCAL_GATEWAY_CAPABILITIES.available

__all__ = [
    "LOCAL_GATEWAY_AVAILABLE",
    "LOCAL_GATEWAY_CAPABILITIES",
]
