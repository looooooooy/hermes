"""Windows capabilities; no Named Pipe implementation exists yet."""

from ..capabilities import LocalGatewayPlatformCapabilities

LOCAL_GATEWAY_CAPABILITIES = LocalGatewayPlatformCapabilities(
    platform="windows",
    available=False,
    transport=None,
    features=frozenset(),
    unavailable_reason="windows_local_gateway_not_implemented",
)
LOCAL_GATEWAY_AVAILABLE = LOCAL_GATEWAY_CAPABILITIES.available

__all__ = [
    "LOCAL_GATEWAY_AVAILABLE",
    "LOCAL_GATEWAY_CAPABILITIES",
]
