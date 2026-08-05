"""Fail-closed Windows relay boundary."""

from ..capabilities import (
    PlatformLocalGatewayUnavailable,
    UnavailableLocalRelayBackend,
)

LOCAL_RELAY_BACKEND = UnavailableLocalRelayBackend(
    "windows_local_relay_not_implemented",
    error_type=PlatformLocalGatewayUnavailable,
)


def create_local_relay_backend() -> UnavailableLocalRelayBackend:
    return LOCAL_RELAY_BACKEND


__all__ = ["LOCAL_RELAY_BACKEND", "create_local_relay_backend"]
