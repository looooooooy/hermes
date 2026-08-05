from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Protocol
from uuid import UUID, uuid4

from hermes_connector.domain.contract_messages import CloudEnvelope
from hermes_connector.domain.session_catalog import (
    SessionCatalogAck,
    SessionCatalogEvent,
    SessionCatalogNack,
    SessionCatalogSnapshotPage,
)
from hermes_connector.domain.storage import (
    IdempotencyConflict,
    SessionCatalogOutboxRecord,
)


class _Codec(Protocol):
    def encode_envelope(self, message: CloudEnvelope) -> bytes: ...

    def encode_session_catalog_snapshot_page(
        self, message: SessionCatalogSnapshotPage
    ) -> bytes: ...

    def encode_session_catalog_event(self, message: SessionCatalogEvent) -> bytes: ...

    def session_catalog_snapshot_page_payload(
        self, message: SessionCatalogSnapshotPage
    ) -> Mapping[str, object]: ...

    def session_catalog_event_payload(
        self, message: SessionCatalogEvent
    ) -> Mapping[str, object]: ...


class _Storage(Protocol):
    async def get_session_catalog_fact(
        self, **values: object
    ) -> SessionCatalogOutboxRecord | None: ...

    async def append_session_catalog_outbox(
        self, **values: object
    ) -> SessionCatalogOutboxRecord: ...

    async def pending_session_catalog_outbox(
        self, **values: object
    ) -> tuple[SessionCatalogOutboxRecord, ...]: ...

    async def ack_session_catalog_outbox(
        self, **values: object
    ) -> SessionCatalogOutboxRecord: ...

    async def nack_session_catalog_outbox(
        self, **values: object
    ) -> SessionCatalogOutboxRecord: ...

    async def retire_session_catalog_outbox(self) -> None: ...


class SessionCatalogOutboundLane:
    """Durably stage catalog facts and settle only from business ACK/NACK."""

    def __init__(
        self,
        *,
        storage: _Storage,
        codec: _Codec,
        tenant_id: str,
        device_id: str,
        utc_now: Callable[[], datetime] = lambda: datetime.now(UTC),
        message_id_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        self._storage = storage
        self._codec = codec
        self._tenant_id = tenant_id
        self._device_id = device_id
        self._utc_now = utc_now
        self._message_id_factory = message_id_factory

    async def stage_snapshot_page(
        self,
        page: SessionCatalogSnapshotPage,
        *,
        connector_sequence: int,
        force_new_attempt: bool = False,
        transport_epoch_id: str | None = None,
    ) -> SessionCatalogOutboxRecord:
        return await self._stage(
            message_type="session.catalog.snapshot.page",
            profile=page.profile,
            runtime_generation=page.runtime_generation,
            snapshot_id=str(page.snapshot_id),
            catalog_revision=page.catalog_revision,
            page_index=page.page_index,
            is_last=page.is_last,
            catalog_sequence=None,
            payload=self._codec.encode_session_catalog_snapshot_page(page),
            payload_mapping=self._codec.session_catalog_snapshot_page_payload(page),
            connector_sequence=connector_sequence,
            force_new_attempt=force_new_attempt,
            transport_epoch_id=transport_epoch_id,
        )

    async def stage_event(
        self,
        event: SessionCatalogEvent,
        *,
        connector_sequence: int,
        force_new_attempt: bool = False,
        transport_epoch_id: str | None = None,
    ) -> SessionCatalogOutboxRecord:
        return await self._stage(
            message_type="session.catalog.event",
            profile=event.profile,
            runtime_generation=event.runtime_generation,
            snapshot_id=None,
            catalog_revision=None,
            page_index=None,
            is_last=None,
            catalog_sequence=event.catalog_sequence,
            payload=self._codec.encode_session_catalog_event(event),
            payload_mapping=self._codec.session_catalog_event_payload(event),
            connector_sequence=connector_sequence,
            force_new_attempt=force_new_attempt,
            transport_epoch_id=transport_epoch_id,
        )

    async def pending(
        self,
        *,
        limit: int,
        after_sequence: int | None = None,
        include_settled: bool = False,
    ) -> tuple[SessionCatalogOutboxRecord, ...]:
        return await self._storage.pending_session_catalog_outbox(
            limit=limit,
            after_sequence=after_sequence,
            include_settled=include_settled,
        )

    async def acknowledge(
        self,
        ack: SessionCatalogAck,
    ) -> SessionCatalogOutboxRecord:
        return await self._storage.ack_session_catalog_outbox(
            profile=ack.profile,
            runtime_generation=ack.runtime_generation,
            acked_message_id=str(ack.acked_message_id),
            acked_payload_digest=ack.acked_payload_digest,
            acked_connector_sequence=ack.acked_connector_sequence,
            ack_kind=ack.ack_kind,
            snapshot_id=str(ack.snapshot_id) if ack.snapshot_id is not None else None,
            catalog_revision=ack.catalog_revision,
            page_index=ack.page_index,
            is_last=ack.is_last,
            catalog_sequence=ack.catalog_sequence,
        )

    async def reject(
        self,
        nack: SessionCatalogNack,
    ) -> SessionCatalogOutboxRecord:
        return await self._storage.nack_session_catalog_outbox(
            profile=nack.profile,
            runtime_generation=nack.runtime_generation,
            rejected_message_id=str(nack.rejected_message_id),
            rejected_payload_digest=nack.rejected_payload_digest,
            rejected_connector_sequence=nack.rejected_connector_sequence,
            reason=nack.reason,
            snapshot_id=(
                str(nack.snapshot_id) if nack.snapshot_id is not None else None
            ),
            expected_page_index=nack.expected_page_index,
            expected_catalog_sequence=nack.expected_catalog_sequence,
        )

    async def transport_sent(self, _record: SessionCatalogOutboxRecord) -> None:
        """Transport acceptance deliberately has no business-settlement effect."""

    async def retire_pending(self) -> None:
        await self._storage.retire_session_catalog_outbox()

    async def _stage(
        self,
        *,
        message_type: str,
        profile: str,
        runtime_generation: str,
        snapshot_id: str | None,
        catalog_revision: int | None,
        page_index: int | None,
        is_last: bool | None,
        catalog_sequence: int | None,
        payload: bytes,
        payload_mapping: Mapping[str, object],
        connector_sequence: int,
        force_new_attempt: bool,
        transport_epoch_id: str | None,
    ) -> SessionCatalogOutboxRecord:
        identity = {
            "transport_epoch_id": transport_epoch_id,
            "message_type": message_type,
            "profile": profile,
            "runtime_generation": runtime_generation,
            "snapshot_id": snapshot_id,
            "catalog_revision": catalog_revision,
            "page_index": page_index,
            "catalog_sequence": catalog_sequence,
        }
        existing = await self._storage.get_session_catalog_fact(**identity)
        digest = hashlib.sha256(payload).hexdigest()
        if existing is not None:
            if existing.payload_digest != digest or existing.payload != payload:
                raise IdempotencyConflict()
            if existing.state not in {"rejected", "retired"} and not force_new_attempt:
                return existing

        message_id = self._message_id_factory()
        envelope = CloudEnvelope(
            contract_version=1,
            message_id=message_id,
            message_type=message_type,
            tenant_id=self._tenant_id,
            device_id=self._device_id,
            sequence=connector_sequence,
            sent_at=self._utc_now(),
            payload=MappingProxyType(dict(payload_mapping)),
            idempotency_key=str(message_id),
        )
        frame = self._codec.encode_envelope(envelope)
        return await self._storage.append_session_catalog_outbox(
            message_id=str(message_id),
            connector_sequence=connector_sequence,
            transport_epoch_id=transport_epoch_id,
            message_type=message_type,
            profile=profile,
            runtime_generation=runtime_generation,
            snapshot_id=snapshot_id,
            catalog_revision=catalog_revision,
            page_index=page_index,
            is_last=is_last,
            catalog_sequence=catalog_sequence,
            payload=payload,
            frame=frame,
        )


__all__ = ["SessionCatalogOutboundLane"]
