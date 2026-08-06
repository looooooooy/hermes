"""Remote control flow coordinator.

Connects validated runtime commands with session actions and receipt creation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class RemoteControlResult:
    command_id: str
    action: str
    state: str
    detail: str | None = None


class RemoteControlFlowCoordinator:
    """Final orchestration boundary before session execution.

    This component intentionally does not invoke models directly.
    It coordinates runtime-owned session actions only.
    """

    def __init__(self, session_authority: Any, action_router: Any) -> None:
        self._session_authority = session_authority
        self._action_router = action_router

    def execute(
        self,
        *,
        command_id: str,
        runtime_generation: str,
        session_id: str,
        action: str,
        payload: Mapping[str, object] | None = None,
    ) -> RemoteControlResult:
        session = self._session_authority.resolve(
            session_id,
            runtime_generation,
        )

        result = self._action_router.dispatch(
            action=action,
            session=session,
            payload=dict(payload or {}),
        )

        return RemoteControlResult(
            command_id=command_id,
            action=action,
            state=result.state,
            detail=result.detail,
        )
