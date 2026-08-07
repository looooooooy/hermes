"""Control-extension bridge into the Runtime event boundary."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from .event_queue import RuntimeEventQueue
from .runtime_event import RuntimeEvent
from .session_authority import SessionAuthority


@dataclass(frozen=True, slots=True)
class ControlActionRequest:
    command_id: str
    runtime_generation: str
    session_id: str
    action: str
    payload: Mapping[str, object]


class ControlExtensionAdapter:
    """Validate session binding and enqueue an internal Runtime event."""

    def __init__(
        self,
        *,
        session_authority: SessionAuthority,
        event_queue: RuntimeEventQueue,
    ) -> None:
        self._session_authority = session_authority
        self._event_queue = event_queue

    def dispatch(self, request: ControlActionRequest) -> RuntimeEvent:
        binding = self._session_authority.resolve(
            request.session_id,
            request.runtime_generation,
        )
        event = RuntimeEvent.create(
            event_id=f"evt-{request.command_id}",
            command_id=request.command_id,
            runtime_generation=request.runtime_generation,
            session_id=binding.session_id,
            event_type=request.action,
            payload=request.payload,
        )
        if not self._event_queue.enqueue(event):
            raise ValueError("duplicate command")
        return event.queued()
