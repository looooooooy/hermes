from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Protocol
from uuid import UUID, uuid4

from hermes_connector.domain.contract_messages import CloudEnvelope
from hermes_connector.domain.observer import (
    SessionEvent,
    SessionSnapshot,
    StreamAck,
    StreamNack,
)
from hermes_connector.domain.storage import IdempotencyConflict, ObserverOutboxRecord


class _Codec(Protocol):
    def encode_envelope(self, message: CloudEnvelope) -> bytes: ...

    def encode_session_event(self, message: SessionEvent) -> bytes: ...

    def encode_session_snapshot(self, message: SessionSnapshot) -> bytes: ...

    def session_event_payload(
        self,
        message: SessionEvent,
    ) -> Mapping[str, object]: ...

    def session_snapshot_payload(
        self,
        message: SessionSnapshot,
    ) -> Mapping[str, object]: ...


class _Storage(Protocol):
    async def get_observer_fact(
        self,
        *,
        transport_epoch_id: str | None = None,
        message_type: str,
        profile: str,
        session_key: str,
        runtime_generation: str,
        runtime_session_id: str,
        event_sequence: int,
    ) -> ObserverOutboxRecord | None: ...

    async def append_observer_outbox(
        self, **values: object
    ) -> ObserverOutboxRecord: ...

    async def pending_observer_outbox(
        self,
        *,
        limit: int,
        after_sequence: int | None = None,
    ) -> tuple[ObserverOutboxRecord, ...]: ...

    async def ack_observer_outbox(self, ack: StreamAck) -> ObserverOutboxRecord: ...

    async def nack_observer_outbox(
        self,
        nack: StreamNack,
    ) -> ObserverOutboxRecord: ...


class ObserverOutboundLane:
    """Durably stage Observer facts and settle them only from stream ACK/NACK."""

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

    async def stage_snapshot(
        self,
        snapshot: SessionSnapshot,
        *,
        connector_sequence: int,
        force_new_attempt: bool = False,
        transport_epoch_id: str | None = None,
    ) -> ObserverOutboxRecord:
        message_type = _observer_message_type(
            "session.snapshot", snapshot.observer_contract
        )
        return await self._stage(
            message_type=message_type,
            profile=snapshot.profile,
            session_key=snapshot.session_key,
            runtime_generation=snapshot.runtime_generation,
            runtime_session_id=snapshot.runtime_session_id,
            event_sequence=snapshot.event_sequence,
            payload=self._codec.encode_session_snapshot(snapshot),
            payload_mapping=self._codec.session_snapshot_payload(snapshot),
            connector_sequence=connector_sequence,
            force_new_attempt=force_new_attempt,
            transport_epoch_id=transport_epoch_id,
        )

    async def stage_event(
        self,
        event: SessionEvent,
        *,
        connector_sequence: int,
        transport_epoch_id: str | None = None,
    ) -> ObserverOutboxRecord:
        message_type = _observer_message_type("session.event", event.observer_contract)
        return await self._stage(
            message_type=message_type,
            profile=event.profile,
            session_key=event.session_key,
            runtime_generation=event.runtime_generation,
            runtime_session_id=event.session_id,
            event_sequence=event.event_sequence,
            payload=self._codec.encode_session_event(event),
            payload_mapping=self._codec.session_event_payload(event),
            connector_sequence=connector_sequence,
            force_new_attempt=False,
            transport_epoch_id=transport_epoch_id,
        )

    async def pending(
        self,
        *,
        limit: int,
        after_sequence: int | None = None,
        include_settled: bool = False,
    ) -> tuple[ObserverOutboxRecord, ...]:
        return await self._storage.pending_observer_outbox(
            limit=limit,
            after_sequence=after_sequence,
            include_settled=include_settled,
        )

    async def acknowledge(self, ack: StreamAck) -> ObserverOutboxRecord:
        return await self._storage.ack_observer_outbox(ack)

    async def reject(self, nack: StreamNack) -> ObserverOutboxRecord:
        return await self._storage.nack_observer_outbox(nack)

    async def transport_sent(self, _record: ObserverOutboxRecord) -> None:
        """Transport acceptance deliberately has no business-settlement effect."""

    async def _stage(
        self,
        *,
        message_type: str,
        profile: str,
        session_key: str,
        runtime_generation: str,
        runtime_session_id: str,
        event_sequence: int,
        payload: bytes,
        payload_mapping: Mapping[str, object],
        connector_sequence: int,
        force_new_attempt: bool,
        transport_epoch_id: str | None,
    ) -> ObserverOutboxRecord:
        existing = await self._storage.get_observer_fact(
            transport_epoch_id=transport_epoch_id,
            message_type=message_type,
            profile=profile,
            session_key=session_key,
            runtime_generation=runtime_generation,
            runtime_session_id=runtime_session_id,
            event_sequence=event_sequence,
        )
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
        return await self._storage.append_observer_outbox(
            message_id=str(message_id),
            connector_sequence=connector_sequence,
            transport_epoch_id=transport_epoch_id,
            message_type=message_type,
            profile=profile,
            session_key=session_key,
            runtime_generation=runtime_generation,
            runtime_session_id=runtime_session_id,
            event_sequence=event_sequence,
            payload=payload,
            frame=frame,
        )


def _observer_message_type(base: str, observer_contract: int) -> str:
    if observer_contract == 1:
        return base
    if observer_contract == 2:
        return f"{base}.v2"
    raise ValueError("Observer contract is unsupported")


__all__ = ["ObserverOutboundLane"]
