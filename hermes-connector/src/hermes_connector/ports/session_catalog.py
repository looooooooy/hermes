from __future__ import annotations

from typing import Protocol

from hermes_connector.domain.session_catalog import (
    SessionCatalogAck,
    SessionCatalogEvent,
    SessionCatalogNack,
    SessionCatalogSnapshotPage,
)
from hermes_connector.domain.storage import SessionCatalogOutboxRecord


class SessionCatalogOutboundLanePort(Protocol):
    async def stage_snapshot_page(
        self,
        page: SessionCatalogSnapshotPage,
        *,
        connector_sequence: int,
        force_new_attempt: bool = False,
        transport_epoch_id: str | None = None,
    ) -> SessionCatalogOutboxRecord: ...

    async def stage_event(
        self,
        event: SessionCatalogEvent,
        *,
        connector_sequence: int,
        force_new_attempt: bool = False,
        transport_epoch_id: str | None = None,
    ) -> SessionCatalogOutboxRecord: ...

    async def pending(
        self,
        *,
        limit: int,
        after_sequence: int | None = None,
        include_settled: bool = False,
    ) -> tuple[SessionCatalogOutboxRecord, ...]: ...

    async def transport_sent(self, record: SessionCatalogOutboxRecord) -> None: ...

    async def acknowledge(
        self, ack: SessionCatalogAck
    ) -> SessionCatalogOutboxRecord: ...

    async def reject(
        self, nack: SessionCatalogNack
    ) -> SessionCatalogOutboxRecord: ...

    async def retire_pending(self) -> None: ...


class SessionCatalogSyncPort(Protocol):
    async def acknowledge(self, ack: SessionCatalogAck) -> None: ...

    async def recover(self, nack: SessionCatalogNack) -> None: ...


__all__ = ["SessionCatalogOutboundLanePort", "SessionCatalogSyncPort"]
