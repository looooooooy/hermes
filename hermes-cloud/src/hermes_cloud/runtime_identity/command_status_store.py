"""Runtime command lifecycle status storage."""

from __future__ import annotations

from dataclasses import dataclass
from time import time


@dataclass(slots=True)
class CommandStatus:
    command_id: str
    runtime_generation: str
    state: str
    detail: str | None = None
    updated_at: float = 0.0


class RuntimeCommandStatusStore:
    """Tracks remote command lifecycle independent from transport state."""

    def __init__(self) -> None:
        self._items: dict[str, CommandStatus] = {}

    def update(
        self,
        command_id: str,
        runtime_generation: str,
        state: str,
        detail: str | None = None,
    ) -> CommandStatus:
        status = CommandStatus(
            command_id=command_id,
            runtime_generation=runtime_generation,
            state=state,
            detail=detail,
            updated_at=time(),
        )
        self._items[command_id] = status
        return status

    def get(self, command_id: str) -> CommandStatus | None:
        return self._items.get(command_id)
