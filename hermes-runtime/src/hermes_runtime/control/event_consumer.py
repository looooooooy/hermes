"""Runtime control event consumer.

Consumes validated runtime events and forwards them to the runtime effect handler.
The consumer deliberately does not call models directly; it preserves the
Runtime Authority boundary.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .runtime_event import RuntimeEvent
from .event_queue import RuntimeEventQueue


@dataclass(frozen=True, slots=True)
class EffectReceipt:
    event_id: str
    command_id: str
    state: str
    detail: str | None = None


class RuntimeEventConsumer:
    """Bridge between the runtime queue and effect execution."""

    def __init__(
        self,
        queue: RuntimeEventQueue,
        effect_handler: Callable[[RuntimeEvent], str],
    ) -> None:
        self._queue = queue
        self._effect_handler = effect_handler

    def consume_once(self) -> EffectReceipt | None:
        event = self._queue.pop()
        if event is None:
            return None

        event.processing()
        try:
            result = self._effect_handler(event)
            event.completed()
            return EffectReceipt(
                event_id=event.event_id,
                command_id=event.command_id,
                state="completed",
                detail=result,
            )
        except Exception as error:  # noqa: BLE001
            event.failed(str(error))
            return EffectReceipt(
                event_id=event.event_id,
                command_id=event.command_id,
                state="failed",
                detail=str(error),
            )


__all__ = ["EffectReceipt", "RuntimeEventConsumer"]
