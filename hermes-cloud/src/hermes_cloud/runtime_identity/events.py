"""Runtime identity lifecycle events.

Small domain events used to project connector/runtime binding changes
without coupling transport and persistence layers.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass(frozen=True, slots=True)
class RuntimeIdentityEvent:
    runtime_id: str
    runtime_generation: str
    event_type: str
    created_at: str

    @classmethod
    def create(
        cls,
        *,
        runtime_id: str,
        runtime_generation: str,
        event_type: str,
    ) -> RuntimeIdentityEvent:
        return cls(
            runtime_id=runtime_id,
            runtime_generation=runtime_generation,
            event_type=event_type,
            created_at=datetime.now(UTC).isoformat(),
        )


__all__ = ["RuntimeIdentityEvent"]
