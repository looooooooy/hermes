"""Verified macOS platform adapters."""

from .availability import (
    LOCAL_GATEWAY_AVAILABLE,
    LOCAL_GATEWAY_CAPABILITIES,
)
from .local_gateway_paths import (
    MacOSLocalGatewayPaths,
    ensure_distinct_local_gateway_directories,
    load_local_gateway_paths,
)
from .local_gateway_transport import (
    MacOSLocalGatewayResource,
    create_local_gateway_resource,
)

__all__ = [
    "LOCAL_GATEWAY_AVAILABLE",
    "LOCAL_GATEWAY_CAPABILITIES",
    "MacOSLocalGatewayPaths",
    "MacOSLocalGatewayResource",
    "create_local_gateway_resource",
    "ensure_distinct_local_gateway_directories",
    "load_local_gateway_paths",
]
