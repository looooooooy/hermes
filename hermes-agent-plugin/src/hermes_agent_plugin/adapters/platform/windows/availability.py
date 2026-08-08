"""Truthful Windows Local Relay capability boundary."""

from ..capabilities import LocalGatewayPlatformCapabilities

# Local Gateway discovery/handshake and the Control endpoint are implemented and
# Windows-runner validated. Host activation still requires the Observer role and
# a Windows endpoint opener, so keep overall availability false until that full
# lifecycle is transaction-tested.
LOCAL_GATEWAY_CAPABILITIES = LocalGatewayPlatformCapabilities(
    platform="windows",
    available=False,
    transport="named-pipe",
    features=frozenset(
        {
            "control.endpoint",
            "local-gateway.handshake",
        }
    ),
    unavailable_reason="windows_observer_endpoint_not_implemented",
)
LOCAL_GATEWAY_AVAILABLE = LOCAL_GATEWAY_CAPABILITIES.available

__all__ = [
    "LOCAL_GATEWAY_AVAILABLE",
    "LOCAL_GATEWAY_CAPABILITIES",
]
