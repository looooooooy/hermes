"""Gateway boundary adapter for runtime identity handshake integration.

This module intentionally keeps transport concerns outside the runtime identity
module. Existing gateway code can call this adapter after decoding a connector
hello payload.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .handshake import RuntimeHandshake
from .service import RuntimeIdentityService


@dataclass(frozen=True, slots=True)
class RuntimeBindingResponse:
    status: str
    runtime_id: str
    runtime_generation: str
    profile: str


class RuntimeIdentityGatewayAdapter:
    """Connect connector hello handling with runtime identity service."""

    def __init__(self, service: RuntimeIdentityService) -> None:
        self._service = service

    def handle_connector_hello(
        self,
        payload: Mapping[str, Any],
    ) -> RuntimeBindingResponse:
        handshake = RuntimeHandshake.from_mapping(payload)
        result = self._service.verify_and_register(handshake)
        return RuntimeBindingResponse(
            status="active" if result.accepted else "rejected",
            runtime_id=handshake.runtime_id,
            runtime_generation=handshake.runtime_generation,
            profile=handshake.profile,
        )
