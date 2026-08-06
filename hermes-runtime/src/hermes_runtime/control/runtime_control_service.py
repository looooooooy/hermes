"""Public Runtime control orchestration service."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from .remote_control_flow import RemoteControlFlowCoordinator, RemoteControlResult


@dataclass(frozen=True, slots=True)
class RuntimeControlRequest:
    command_id: str
    runtime_generation: str
    session_id: str
    action: str
    payload: Mapping[str, object] = field(default_factory=dict)


class RuntimeControlService:
    """Stable service facade over the idempotent remote-control flow."""

    def __init__(self, coordinator: RemoteControlFlowCoordinator) -> None:
        self._coordinator = coordinator

    def dispatch(self, request: RuntimeControlRequest) -> RemoteControlResult:
        return self._coordinator.execute(
            command_id=request.command_id,
            runtime_generation=request.runtime_generation,
            session_id=request.session_id,
            action=request.action,
            payload=request.payload,
        )
