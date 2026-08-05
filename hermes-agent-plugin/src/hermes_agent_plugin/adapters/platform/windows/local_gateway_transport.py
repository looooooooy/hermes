"""Fail-closed Windows Local Gateway boundary."""

from __future__ import annotations

from typing import NoReturn

from ..capabilities import PlatformLocalGatewayUnavailable
from .availability import (
    LOCAL_GATEWAY_AVAILABLE,
    LOCAL_GATEWAY_CAPABILITIES,
)


def create_local_gateway_resource(**_kwargs: object) -> NoReturn:
    """Reject Windows activation until a verified adapter is implemented."""
    reason = LOCAL_GATEWAY_CAPABILITIES.unavailable_reason
    raise PlatformLocalGatewayUnavailable(reason)


__all__ = [
    "LOCAL_GATEWAY_AVAILABLE",
    "LOCAL_GATEWAY_CAPABILITIES",
    "create_local_gateway_resource",
]
