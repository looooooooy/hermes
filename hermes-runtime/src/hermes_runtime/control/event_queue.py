"""Durable boundary queue abstraction for runtime control events."""

from __future__ import annotations

from collections import deque

from .runtime_event import RuntimeEvent, RuntimeEventState


class RuntimeEventQueue:
    def __init__(self) -> None:
        self._events: deque[RuntimeEvent] = deque()
        self._seen_commands: set[str] = set()

    def enqueue(self, event: RuntimeEvent) -> bool:
        if event.command_id in self._seen_commands:
            return False
        self._seen_commands.add(event.command_id)
        self._events.append(
            RuntimeEvent(
                **{
                    **event.__dict__,
                    "state": RuntimeEventState.QUEUED,
                }
            )
        )
        return True

    def pop(self) -> RuntimeEvent | None:
        if not self._events:
            return None
        return self._events.popleft()

    def size(self) -> int:
        return len(self._events)
