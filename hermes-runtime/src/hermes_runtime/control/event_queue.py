"""Thread-safe runtime control-event queue with command idempotency."""

from __future__ import annotations

from collections import deque
from threading import RLock

from .runtime_event import RuntimeEvent


class RuntimeEventQueue:
    def __init__(self) -> None:
        self._events: deque[RuntimeEvent] = deque()
        self._seen_commands: set[str] = set()
        self._lock = RLock()

    def enqueue(self, event: RuntimeEvent) -> bool:
        with self._lock:
            if event.command_id in self._seen_commands:
                return False
            queued = event.queued()
            self._seen_commands.add(event.command_id)
            self._events.append(queued)
            return True

    def pop(self) -> RuntimeEvent | None:
        with self._lock:
            if not self._events:
                return None
            return self._events.popleft()

    def contains(self, command_id: str) -> bool:
        with self._lock:
            return command_id in self._seen_commands

    def size(self) -> int:
        with self._lock:
            return len(self._events)
