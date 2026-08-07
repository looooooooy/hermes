"""Adapter boundary between runtime control events and agent execution.

Remote commands must enter the Agent runtime through internal events.
This module intentionally does not call model/tool execution directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Protocol


class AgentEventSink(Protocol):
    def submit_event(self, event: "AgentRuntimeEvent") -> str: ...


@dataclass(frozen=True, slots=True)
class AgentRuntimeEvent:
    event_id: str
    session_id: str
    runtime_generation: str
    event_type: str
    payload: Mapping[str, object]


class AgentEventAdapter:
    """Converts validated runtime events into Agent lifecycle events."""

    def __init__(self, sink: AgentEventSink) -> None:
        self._sink = sink

    def dispatch(
        self,
        *,
        event_id: str,
        session_id: str,
        runtime_generation: str,
        event_type: str,
        payload: Mapping[str, object],
    ) -> str:
        event = AgentRuntimeEvent(
            event_id=event_id,
            session_id=session_id,
            runtime_generation=runtime_generation,
            event_type=event_type,
            payload=payload,
        )
        return self._sink.submit_event(event)
