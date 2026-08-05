"""Compatibility aliases for the former POSIX transport import path."""

from hermes_connector.adapters.platform.macos.local_gateway_transport import (
    MAX_LOCAL_BODY_BYTES,
    MacOSLocalGatewayConnection,
    MacOSLocalGatewayTransport,
)

PosixLocalGatewayConnection = MacOSLocalGatewayConnection
PosixLocalGatewayTransport = MacOSLocalGatewayTransport

__all__ = [
    "MAX_LOCAL_BODY_BYTES",
    "PosixLocalGatewayConnection",
    "PosixLocalGatewayTransport",
]
