"""Runtime events used by the remote control-plane boundary."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import Enum
from types import MappingProxyType
from typing import Final


class RuntimeEventState(str, Enum):
    RECEIVED = "received"
    VALIDATED = "validated"
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


_ALLOWED_TRANSITIONS: Final[Mapping[RuntimeEventState, frozenset[RuntimeEventState]]] = (
    MappingProxyType(
        {
            RuntimeEventState.RECEIVED: frozenset(
                {
                    RuntimeEventState.VALIDATED,
                    RuntimeEventState.QUEUED,
                    RuntimeEventState.FAILED,
                }
            ),
            RuntimeEventState.VALIDATED: frozenset(
                {RuntimeEventState.QUEUED, RuntimeEventState.FAILED}
            ),
            RuntimeEventState.QUEUED: frozenset(
                {RuntimeEventState.PROCESSING, RuntimeEventState.FAILED}
            ),
            RuntimeEventState.PROCESSING: frozenset(
                {RuntimeEventState.COMPLETED, RuntimeEventState.FAILED}
            ),
            RuntimeEventState.COMPLETED: frozenset(),
            RuntimeEventState.FAILED: frozenset(),
        }
    )
)


class InvalidRuntimeEventTransition(ValueError):
    """Raised when an event attempts an edge outside the frozen state graph."""


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

    def __post_init__(self) -> None:
        for name, value in (
            ("event_id", self.event_id),
            ("command_id", self.command_id),
            ("runtime_generation", self.runtime_generation),
            ("event_type", self.event_type),
        ):
            if not value or value != value.strip():
                raise ValueError(f"{name} must be canonical non-empty text")
        if self.session_id is not None and (
            not self.session_id or self.session_id != self.session_id.strip()
        ):
            raise ValueError("session_id must be canonical text when present")
        if self.created_at.tzinfo is None:
            raise ValueError("created_at must be timezone-aware")

    @classmethod
    def create(
        cls,
        *,
        event_id: str,
        command_id: str,
        runtime_generation: str,
        event_type: str,
        payload: Mapping[str, object],
        session_id: str | None = None,
    ) -> "RuntimeEvent":
        return cls(
            event_id=event_id,
            command_id=command_id,
            runtime_generation=runtime_generation,
            session_id=session_id,
            event_type=event_type,
            payload=dict(payload),
            created_at=datetime.now(UTC),
        )

    def transition(self, target: RuntimeEventState) -> "RuntimeEvent":
        if target not in _ALLOWED_TRANSITIONS[self.state]:
            raise InvalidRuntimeEventTransition(
                f"runtime event transition is not allowed: {self.state.value} -> {target.value}"
            )
        return replace(self, state=target)

    def validated(self) -> "RuntimeEvent":
        return self.transition(RuntimeEventState.VALIDATED)

    def queued(self) -> "RuntimeEvent":
        return self.transition(RuntimeEventState.QUEUED)

    def processing(self) -> "RuntimeEvent":
        return self.transition(RuntimeEventState.PROCESSING)

    def completed(self) -> "RuntimeEvent":
        return self.transition(RuntimeEventState.COMPLETED)

    def failed(self) -> "RuntimeEvent":
        return self.transition(RuntimeEventState.FAILED)
