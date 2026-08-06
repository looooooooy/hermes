"""Runtime session action router.

Routes validated runtime actions to session-owned handlers.
The router intentionally does not invoke models directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


class SessionActionHandler(Protocol):
    def interrupt(self) -> None: ...

    def resume(self) -> None: ...

    def approve(self, payload: dict[str, Any]) -> None: ...


@dataclass(frozen=True, slots=True)
class SessionActionRequest:
    command_id: str
    action: str
    runtime_generation: str
    session_id: str
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class SessionActionResult:
    command_id: str
    action: str
    state: str
    detail: str | None = None


class SessionActionRouter:
    """Dispatch control actions to an already bound runtime session."""

    def dispatch(
        self,
        request: SessionActionRequest,
        session: SessionActionHandler,
    ) -> SessionActionResult:
        if request.action == "interrupt":
            session.interrupt()
        elif request.action == "resume":
            session.resume()
        elif request.action == "approve":
            session.approve(request.payload)
        else:
            return SessionActionResult(
                command_id=request.command_id,
                action=request.action,
                state="rejected",
                detail="unsupported_action",
            )

        return SessionActionResult(
            command_id=request.command_id,
            action=request.action,
            state="completed",
        )
