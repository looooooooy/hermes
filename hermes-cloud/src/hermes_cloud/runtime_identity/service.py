"""Runtime identity application service boundary.

Keeps connector/runtime identity reconciliation outside transport handlers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from .projection import RuntimeIdentityProjection
from .registry import RuntimeIdentityRegistry


@dataclass(frozen=True, slots=True)
class RuntimeIdentityVerificationResult:
    accepted: bool
    reason: str
    projection: RuntimeIdentityProjection | None = None


class RuntimeIdentityService:
    """Validate and register connector supplied runtime identity."""

    def __init__(self, registry: RuntimeIdentityRegistry) -> None:
        self._registry = registry

    def verify_and_register(
        self,
        payload: Mapping[str, object],
    ) -> RuntimeIdentityVerificationResult:
        required = (
            "connector_id",
            "runtime_id",
            "runtime_generation",
            "profile",
            "descriptor_hash",
        )
        missing = [key for key in required if not payload.get(key)]
        if missing:
            return RuntimeIdentityVerificationResult(
                accepted=False,
                reason=f"missing_runtime_identity_fields:{','.join(missing)}",
            )

        projection = RuntimeIdentityProjection.from_mapping(payload)
        self._registry.upsert(projection)
        return RuntimeIdentityVerificationResult(
            accepted=True,
            reason="runtime_identity_verified",
            projection=projection,
        )


__all__ = [
    "RuntimeIdentityService",
    "RuntimeIdentityVerificationResult",
]
