from __future__ import annotations

from typing import Protocol

from hermes_connector.domain.observer import (
    SessionEvent,
    SessionObserveClose,
    SessionObserveOpen,
    SessionSnapshot,
    StreamAck,
    StreamNack,
)
from hermes_connector.domain.storage import ObserverOutboxRecord


class ObserverOutboundLanePort(Protocol):
    async def stage_snapshot(
        self,
        snapshot: SessionSnapshot,
        *,
        connector_sequence: int,
        force_new_attempt: bool = False,
        transport_epoch_id: str | None = None,
    ) -> ObserverOutboxRecord: ...

    async def stage_event(
        self,
        event: SessionEvent,
        *,
        connector_sequence: int,
        transport_epoch_id: str | None = None,
    ) -> ObserverOutboxRecord: ...

    async def pending(
        self,
        *,
        limit: int,
        after_sequence: int | None = None,
        include_settled: bool = False,
    ) -> tuple[ObserverOutboxRecord, ...]: ...

    async def transport_sent(self, record: ObserverOutboxRecord) -> None: ...

    async def acknowledge(self, ack: StreamAck) -> ObserverOutboxRecord: ...

    async def reject(self, nack: StreamNack) -> ObserverOutboxRecord: ...


class ObserverIntentLanePort(Protocol):
    def raise_if_failed(self) -> None: ...

    async def open(self, intent: SessionObserveOpen) -> None: ...

    async def close(self, intent: SessionObserveClose) -> None: ...

    async def recover(self, nack: StreamNack) -> None: ...

    async def acknowledge(self, ack: StreamAck) -> None: ...

    async def shutdown(self) -> None: ...


__all__ = ["ObserverIntentLanePort", "ObserverOutboundLanePort"]
