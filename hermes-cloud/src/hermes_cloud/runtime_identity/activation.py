"""Runtime identity activation workflow."""

from __future__ import annotations

from dataclasses import dataclass

from .projection import RuntimeIdentityProjection
from .registry import RuntimeIdentityRegistry


@dataclass(frozen=True, slots=True)
class RuntimeActivationResult:
    active: bool
    runtime_id: str


class RuntimeActivationService:
    def __init__(self, registry: RuntimeIdentityRegistry) -> None:
        self._registry = registry

    def activate(self, projection: RuntimeIdentityProjection) -> RuntimeActivationResult:
        self._registry.upsert(projection)
        return RuntimeActivationResult(
            active=True,
            runtime_id=projection.runtime_id,
        )
