"""Operating-system adapters selected by the composition root."""

from .capabilities import (
    LocalGatewayPlatformCapabilities,
    PlatformLocalGatewayUnavailable,
)

__all__ = [
    "LocalGatewayPlatformCapabilities",
    "PlatformLocalGatewayUnavailable",
]
