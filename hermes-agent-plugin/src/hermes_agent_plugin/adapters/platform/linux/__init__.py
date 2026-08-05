"""Linux platform boundary."""

from .availability import (
    LOCAL_GATEWAY_AVAILABLE,
    LOCAL_GATEWAY_CAPABILITIES,
)
from .local_gateway_transport import create_local_gateway_resource

__all__ = [
    "LOCAL_GATEWAY_AVAILABLE",
    "LOCAL_GATEWAY_CAPABILITIES",
    "create_local_gateway_resource",
]
