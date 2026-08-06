"""Agent turn queue boundary.

Remote control commands are converted into turn events and consumed by the
existing agent lifecycle. This module intentionally does not execute models.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from collections import deque
from typing import Mapping


@dataclass(frozen=True, slots=True)
class AgentTurnEvent:
    event_id: str
    session_id: str
    event_type: str
    payload: Mapping[str, object] = field(default_factory=dict)


class AgentTurnQueue:
    def __init__(self) -> None:
        self._queue: deque[AgentTurnEvent] = deque()

    def push(self, event: AgentTurnEvent) -> None:
        self._queue.append(event)

    def pop(self) -> AgentTurnEvent | None:
        if not self._queue:
            return None
        return self._queue.popleft()

    def size(self) -> int:
        return len(self._queue)
