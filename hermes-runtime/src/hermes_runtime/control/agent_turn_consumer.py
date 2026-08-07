"""Consume queued runtime control events and forward them to the agent loop."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class AgentTurnResult:
    event_id: str
    session_id: str
    state: str
    detail: str | None = None


class AgentLoopPort(Protocol):
    def handle_event(self, event: object) -> str: ...


class AgentTurnConsumer:
    """Runtime boundary between control events and the agent loop."""

    def __init__(self, queue: object, agent_loop: AgentLoopPort) -> None:
        self._queue = queue
        self._agent_loop = agent_loop

    def consume_once(self) -> AgentTurnResult | None:
        event = self._queue.pop()
        if event is None:
            return None

        try:
            detail = self._agent_loop.handle_event(event)
            return AgentTurnResult(
                event_id=event.event_id,
                session_id=event.session_id,
                state="completed",
                detail=detail,
            )
        except Exception as error:
            return AgentTurnResult(
                event_id=event.event_id,
                session_id=event.session_id,
                state="failed",
                detail=str(error),
            )
