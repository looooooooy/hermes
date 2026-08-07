"""Command receipt synchronization boundary.

This module bridges runtime execution receipts to external control-plane
transport. It deliberately does not own transport implementation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class ReceiptSyncEvent:
    command_id: str
    runtime_generation: str
    session_id: str
    state: str
    detail: str | None = None


class ReceiptPublisher(Protocol):
    def publish(self, event: ReceiptSyncEvent) -> None: ...


class CommandReceiptSync:
    """Publishes completed runtime effects to the control plane."""

    def __init__(self, publisher: ReceiptPublisher) -> None:
        self._publisher = publisher

    def sync(
        self,
        *,
        command_id: str,
        runtime_generation: str,
        session_id: str,
        state: str,
        detail: str | None = None,
    ) -> ReceiptSyncEvent:
        event = ReceiptSyncEvent(
            command_id=command_id,
            runtime_generation=runtime_generation,
            session_id=session_id,
            state=state,
            detail=detail,
        )
        self._publisher.publish(event)
        return event
