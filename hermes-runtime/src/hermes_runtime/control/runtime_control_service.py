"""Runtime control orchestration service."""

from __future__ import annotations

from dataclasses import dataclass

from .agent_turn_queue import AgentTurnEvent, AgentTurnQueue


@dataclass(frozen=True, slots=True)
class RuntimeControlRequest:
    command_id: str
    session_id: str
    action: str
    payload: dict[str, object]


class RuntimeControlService:
    """Final boundary before events enter the agent lifecycle."""

    def __init__(self, turn_queue: AgentTurnQueue) -> None:
        self._turn_queue = turn_queue

    def dispatch(self, request: RuntimeControlRequest) -> AgentTurnEvent:
        event = AgentTurnEvent(
            event_id=f"turn-{request.command_id}",
            session_id=request.session_id,
            event_type=request.action,
            payload=request.payload,
        )
        self._turn_queue.push(event)
        return event
