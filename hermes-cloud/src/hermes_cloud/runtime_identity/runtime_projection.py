"""Runtime identity projection facade.

Keeps gateway adapters independent from registry storage details.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .registry import RuntimeIdentityRegistry
from .service import RuntimeIdentityService


@dataclass(frozen=True, slots=True)
class RuntimeProjectionResult:
    active: bool
    runtime_id: str
    runtime_generation: str


class RuntimeProjectionFacade:
    def __init__(self, registry: RuntimeIdentityRegistry) -> None:
        self._service = RuntimeIdentityService(registry)

    def accept_handshake(self, payload: dict[str, Any]) -> RuntimeProjectionResult:
        result = self._service.verify(payload)
        return RuntimeProjectionResult(
            active=result.accepted,
            runtime_id=result.runtime_id,
            runtime_generation=result.runtime_generation,
        )
