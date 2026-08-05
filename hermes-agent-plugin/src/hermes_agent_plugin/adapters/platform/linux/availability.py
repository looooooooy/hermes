"""Linux capabilities; no Local Gateway implementation is verified yet."""

from ..capabilities import LocalGatewayPlatformCapabilities

LOCAL_GATEWAY_CAPABILITIES = LocalGatewayPlatformCapabilities(
    platform="linux",
    available=False,
    transport=None,
    features=frozenset(),
    unavailable_reason="linux_local_gateway_not_implemented",
)
LOCAL_GATEWAY_AVAILABLE = LOCAL_GATEWAY_CAPABILITIES.available

__all__ = [
    "LOCAL_GATEWAY_AVAILABLE",
    "LOCAL_GATEWAY_CAPABILITIES",
]
