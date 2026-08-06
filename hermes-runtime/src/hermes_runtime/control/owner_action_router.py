"""Owner action routing boundary for runtime control."""

from __future__ import annotations

from dataclasses import dataclass

from .event_queue import RuntimeEventQueue
from .runtime_event import RuntimeEvent


@dataclass(frozen=True, slots=True)
class OwnerActionRequest:
    command_id: str
    runtime_generation: str
    action: str
    payload: dict[str, object]
    session_id: str | None = None


class OwnerActionRouter:
    def __init__(self, queue: RuntimeEventQueue) -> None:
        self._queue = queue

    def route(self, request: OwnerActionRequest) -> bool:
        event = RuntimeEvent.create(
            event_id=f"event-{request.command_id}",
            command_id=request.command_id,
            runtime_generation=request.runtime_generation,
            session_id=request.session_id,
            event_type=request.action,
            payload=request.payload,
        )
        return self._queue.enqueue(event)
