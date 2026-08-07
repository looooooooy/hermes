"""Runtime control-event consumer."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .event_queue import RuntimeEventQueue
from .runtime_event import RuntimeEvent


@dataclass(frozen=True, slots=True)
class EffectReceipt:
    event_id: str
    command_id: str
    runtime_generation: str
    session_id: str | None
    state: str
    detail: str | None = None


class RuntimeEventConsumer:
    """Bridge between the runtime queue and its Runtime-owned effect handler."""

    def __init__(
        self,
        queue: RuntimeEventQueue,
        effect_handler: Callable[[RuntimeEvent], str | None],
    ) -> None:
        self._queue = queue
        self._effect_handler = effect_handler

    def consume_once(self) -> EffectReceipt | None:
        event = self._queue.pop()
        if event is None:
            return None

        processing = event.processing()
        try:
            result = self._effect_handler(processing)
            processing.completed()
            return EffectReceipt(
                event_id=event.event_id,
                command_id=event.command_id,
                runtime_generation=event.runtime_generation,
                session_id=event.session_id,
                state="completed",
                detail=result,
            )
        except Exception:  # noqa: BLE001 - safe remote boundary
            processing.failed()
            return EffectReceipt(
                event_id=event.event_id,
                command_id=event.command_id,
                runtime_generation=event.runtime_generation,
                session_id=event.session_id,
                state="failed",
                detail="runtime_effect_failed",
            )


__all__ = ["EffectReceipt", "RuntimeEventConsumer"]
