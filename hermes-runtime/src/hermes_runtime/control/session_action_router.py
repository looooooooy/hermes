"""Routes validated Runtime actions to Runtime-owned session controllers."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from .session_authority import SessionController


@dataclass(frozen=True, slots=True)
class SessionActionRequest:
    command_id: str
    action: str
    runtime_generation: str
    session_id: str
    payload: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class SessionActionResult:
    command_id: str
    action: str
    state: str
    detail: str | None = None


class SessionActionRouter:
    """Dispatch an allowed action without bypassing the session authority."""

    def dispatch(
        self,
        request: SessionActionRequest,
        session: SessionController,
    ) -> SessionActionResult:
        if request.action == "interrupt":
            session.interrupt()
        elif request.action == "resume":
            session.resume()
        elif request.action == "approve":
            pending_request_id = request.payload.get("pending_request_id")
            if not isinstance(pending_request_id, str) or not pending_request_id:
                return SessionActionResult(
                    command_id=request.command_id,
                    action=request.action,
                    state="rejected",
                    detail="pending_request_id_required",
                )
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
