"""Runtime command receipt receiver.

Receives execution receipts from connectors and updates cloud-side
command lifecycle state.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ReceiptUpdate:
    command_id: str
    runtime_generation: str
    state: str
    detail: str | None = None


class RuntimeReceiptReceiver:
    """Cloud boundary for runtime execution receipts."""

    def __init__(self, status_store: Any) -> None:
        self._status_store = status_store

    def receive(self, payload: dict[str, object]) -> ReceiptUpdate:
        update = ReceiptUpdate(
            command_id=str(payload["command_id"]),
            runtime_generation=str(payload["runtime_generation"]),
            state=str(payload["state"]),
            detail=(
                str(payload["detail"])
                if payload.get("detail") is not None
                else None
            ),
        )

        self._status_store.update(
            command_id=update.command_id,
            runtime_generation=update.runtime_generation,
            state=update.state,
            detail=update.detail,
        )

        return update
