"""Runtime events used by the remote control plane boundary.

The control plane never executes an agent directly. Incoming actions are
converted into runtime events and consumed by the owning runtime.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum


class RuntimeEventState(str, Enum):
    RECEIVED = "received"
    VALIDATED = "validated"
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class RuntimeEvent:
    event_id: str
    command_id: str
    runtime_generation: str
    session_id: str | None
    event_type: str
    payload: dict[str, object]
    created_at: datetime
    state: RuntimeEventState = RuntimeEventState.RECEIVED

    @classmethod
    def create(
        cls,
        *,
        event_id: str,
        command_id: str,
        runtime_generation: str,
        event_type: str,
        payload: dict[str, object],
        session_id: str | None = None,
    ) -> "RuntimeEvent":
        return cls(
            event_id=event_id,
            command_id=command_id,
            runtime_generation=runtime_generation,
            session_id=session_id,
            event_type=event_type,
            payload=payload,
            created_at=datetime.now(UTC),
        )
