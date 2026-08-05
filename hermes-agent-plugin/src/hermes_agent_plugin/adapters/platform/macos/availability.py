"""Capabilities of the verified macOS Local Gateway adapter."""

from ..capabilities import LocalGatewayPlatformCapabilities

LOCAL_GATEWAY_CAPABILITIES = LocalGatewayPlatformCapabilities(
    platform="macos",
    available=True,
    transport="unix-domain-socket",
    features=frozenset(
        {
            "private-discovery-descriptor-v1",
            "private-unix-domain-socket",
        }
    ),
)
LOCAL_GATEWAY_AVAILABLE = LOCAL_GATEWAY_CAPABILITIES.available

__all__ = [
    "LOCAL_GATEWAY_AVAILABLE",
    "LOCAL_GATEWAY_CAPABILITIES",
]
