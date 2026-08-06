"""Control extension bridge into Hermes runtime execution boundary.

The adapter intentionally does not execute model calls directly.
It converts external control actions into runtime events.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

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
    """Adapter used by connector/control extensions.

    Boundary rule:
    connector -> adapter -> runtime event queue -> agent runtime

    No direct Agent invocation is allowed here.
    """

    def __init__(
        self,
        *,
        session_authority: SessionAuthority,
        event_queue: RuntimeEventQueue,
    ) -> None:
        self._session_authority = session_authority
        self._event_queue = event_queue

    def dispatch(self, request: ControlActionRequest) -> RuntimeEvent:
        session = self._session_authority.resolve(
            request.session_id,
            request.runtime_generation,
        )

        event = RuntimeEvent(
            event_id=f"evt-{request.command_id}",
            command_id=request.command_id,
            runtime_generation=request.runtime_generation,
            session_id=session.session_id,
            event_type=request.action,
            payload=dict(request.payload),
        )

        self._event_queue.enqueue(event)
        return event
