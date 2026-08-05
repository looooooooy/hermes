"""Durable ORM ACK/NACK delivery for Connector Observer ingress."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from typing import Protocol
from uuid import RFC_4122, UUID, uuid4

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from hermes_cloud.domain.canonical_json import canonical_payload_digest
from hermes_cloud.domain.connector_gateway import (
    ConnectorIdentity,
    ConnectorObserverReceiptDelivery,
)
from hermes_cloud.platform.postgres.models import ConnectorObserverReceiptModel

_MAX_RECEIPT_PAYLOAD_BYTES = 16_384
_DEFAULT_PENDING_CAPACITY = 1_024
_DEFAULT_SETTLED_RETENTION = 1_024
_PRUNE_BATCH = 64


class SessionFactory(Protocol):
    def begin(self) -> AbstractContextManager[Session]: ...


class SqlAlchemyObserverReceiptRouter:
    """Persist and redeliver Observer receipts until Connector confirmation."""

    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        uuid_factory: Callable[[], UUID] = uuid4,
        pending_capacity: int = _DEFAULT_PENDING_CAPACITY,
        settled_retention: int = _DEFAULT_SETTLED_RETENTION,
    ) -> None:
        if type(pending_capacity) is not int or pending_capacity <= 0:
            raise ValueError("Observer receipt pending capacity is invalid")
        if type(settled_retention) is not int or settled_retention < 0:
            raise ValueError("Observer receipt settled retention is invalid")
        self._session_factory = session_factory
        self._now = now
        self._uuid_factory = uuid_factory
        self._pending_capacity = pending_capacity
        self._settled_retention = settled_retention

    async def stage_and_reserve(
        self,
        *,
        identity: ConnectorIdentity,
        connection_id: str,
        observer_message_id: str,
        receipt_type: str,
        payload: Mapping[str, object],
        sequence: int,
    ) -> ConnectorObserverReceiptDelivery:
        return await asyncio.to_thread(
            self._stage_and_reserve,
            identity,
            connection_id,
            observer_message_id,
            receipt_type,
            payload,
            sequence,
        )

    async def next_pending(
        self,
        *,
        identity: ConnectorIdentity,
        connection_id: str,
    ) -> str | None:
        return await asyncio.to_thread(
            self._next_pending,
            identity,
            connection_id,
        )

    async def reserve_redelivery(
        self,
        *,
        identity: ConnectorIdentity,
        connection_id: str,
        observer_message_id: str,
        sequence: int,
    ) -> ConnectorObserverReceiptDelivery:
        return await asyncio.to_thread(
            self._reserve_redelivery,
            identity,
            connection_id,
            observer_message_id,
            sequence,
        )

    async def mark_sent(
        self,
        *,
        identity: ConnectorIdentity,
        connection_id: str,
        observer_message_id: str,
        message_id: str,
        sequence: int,
    ) -> None:
        await asyncio.to_thread(
            self._mark_sent,
            identity,
            connection_id,
            observer_message_id,
            message_id,
            sequence,
        )

    async def confirm_through_cursor(
        self,
        *,
        identity: ConnectorIdentity,
        connection_id: str,
        durable_next_inbound_sequence: int,
    ) -> int:
        return await asyncio.to_thread(
            self._confirm_through_cursor,
            identity,
            connection_id,
            durable_next_inbound_sequence,
        )

    def _stage_and_reserve(
        self,
        identity: ConnectorIdentity,
        connection_id: str,
        observer_message_id: str,
        receipt_type: str,
        payload: Mapping[str, object],
        sequence: int,
    ) -> ConnectorObserverReceiptDelivery:
        tenant_id: UUID = _uuid(identity.tenant_id)
        device_id: UUID = _uuid(identity.device_id)
        connection: UUID = _uuid(connection_id)
        observer_id = _uuid(observer_message_id)
        kind = _receipt_type(receipt_type)
        position = _sequence(sequence)
        record, digest = _payload(payload, observer_message_id)
        now = _utc(self._now())
        with self._session_factory.begin() as session:
            key = (tenant_id, device_id, observer_id)
            row = session.get(
                ConnectorObserverReceiptModel,
                key,
                with_for_update=True,
            )
            if row is not None:
                if row.receipt_type != kind or row.payload_digest != digest:
                    raise ValueError("Observer receipt binding conflicts")
                if row.state != "pending":
                    raise RuntimeError("Observer receipt is already settled")
                if (
                    row.dispatch_connection_id == connection
                    and row.dispatch_sequence == position
                ):
                    return _delivery(row)
                return self._reserve_row(
                    row,
                    connection=connection,
                    sequence=position,
                    now=now,
                )
            pending = session.scalars(
                select(ConnectorObserverReceiptModel.observer_message_id)
                .where(
                    ConnectorObserverReceiptModel.tenant_id == tenant_id,
                    ConnectorObserverReceiptModel.device_id == device_id,
                    ConnectorObserverReceiptModel.state == "pending",
                )
                .order_by(ConnectorObserverReceiptModel.observer_message_id)
                .limit(self._pending_capacity)
            ).all()
            if len(pending) >= self._pending_capacity:
                raise RuntimeError("Observer receipt pending capacity reached")
            message_id = _factory_uuid(self._uuid_factory)
            row = ConnectorObserverReceiptModel(
                tenant_id=tenant_id,
                device_id=device_id,
                observer_message_id=observer_id,
                receipt_type=kind,
                payload=record,
                payload_digest=digest,
                state="pending",
                dispatch_connection_id=connection,
                dispatch_message_id=message_id,
                dispatch_sequence=position,
                dispatch_attempts=1,
                created_at=now,
                updated_at=now,
                sent_at=None,
                settled_at=None,
            )
            session.add(row)
            session.flush()
            self._prune_settled(session, tenant_id=tenant_id, device_id=device_id)
            return _delivery(row)

    def _next_pending(
        self,
        identity: ConnectorIdentity,
        connection_id: str,
    ) -> str | None:
        tenant_id: UUID = _uuid(identity.tenant_id)
        device_id: UUID = _uuid(identity.device_id)
        connection: UUID = _uuid(connection_id)
        with self._session_factory.begin() as session:
            pending_statement = (
                select(ConnectorObserverReceiptModel.observer_message_id)
                .where(
                    ConnectorObserverReceiptModel.tenant_id == tenant_id,
                    ConnectorObserverReceiptModel.device_id == device_id,
                    ConnectorObserverReceiptModel.state == "pending",
                    or_(
                        ConnectorObserverReceiptModel.dispatch_connection_id.is_(None),
                        ConnectorObserverReceiptModel.dispatch_connection_id
                        != connection,
                    ),
                )
                .order_by(
                    ConnectorObserverReceiptModel.updated_at,
                    ConnectorObserverReceiptModel.observer_message_id,
                )
                .limit(1)
            )
            observer_id = session.scalar(pending_statement)
            return str(observer_id) if observer_id is not None else None

    def _reserve_redelivery(
        self,
        identity: ConnectorIdentity,
        connection_id: str,
        observer_message_id: str,
        sequence: int,
    ) -> ConnectorObserverReceiptDelivery:
        tenant_id, device_id = _identity(identity)
        connection = _uuid(connection_id)
        observer_id = _uuid(observer_message_id)
        position = _sequence(sequence)
        now = _utc(self._now())
        with self._session_factory.begin() as session:
            row = session.get(
                ConnectorObserverReceiptModel,
                (tenant_id, device_id, observer_id),
                with_for_update=True,
            )
            if row is None or row.state != "pending":
                raise RuntimeError("Observer receipt pending delivery changed")
            if row.dispatch_connection_id == connection:
                raise RuntimeError("Observer receipt is already reserved")
            return self._reserve_row(
                row,
                connection=connection,
                sequence=position,
                now=now,
            )

    def _reserve_row(
        self,
        row: ConnectorObserverReceiptModel,
        *,
        connection: UUID,
        sequence: int,
        now: datetime,
    ) -> ConnectorObserverReceiptDelivery:
        row.dispatch_connection_id = connection
        row.dispatch_message_id = _factory_uuid(self._uuid_factory)
        row.dispatch_sequence = sequence
        row.dispatch_attempts += 1
        row.updated_at = now
        row.sent_at = None
        return _delivery(row)

    def _mark_sent(
        self,
        identity: ConnectorIdentity,
        connection_id: str,
        observer_message_id: str,
        message_id: str,
        sequence: int,
    ) -> None:
        tenant_id, device_id = _identity(identity)
        connection = _uuid(connection_id)
        observer_id = _uuid(observer_message_id)
        dispatch_message = _uuid(message_id)
        position = _sequence(sequence)
        now = _utc(self._now())
        with self._session_factory.begin() as session:
            row = session.get(
                ConnectorObserverReceiptModel,
                (tenant_id, device_id, observer_id),
                with_for_update=True,
            )
            if (
                row is None
                or row.state != "pending"
                or row.dispatch_connection_id != connection
                or row.dispatch_message_id != dispatch_message
                or row.dispatch_sequence != position
            ):
                raise RuntimeError("Observer receipt dispatch ownership changed")
            row.sent_at = now
            row.updated_at = now

    def _confirm_through_cursor(
        self,
        identity: ConnectorIdentity,
        connection_id: str,
        durable_next_inbound_sequence: int,
    ) -> int:
        tenant_id: UUID = _uuid(identity.tenant_id)
        device_id: UUID = _uuid(identity.device_id)
        connection: UUID = _uuid(connection_id)
        durable_cursor: int = _sequence(durable_next_inbound_sequence)
        now: datetime = _utc(self._now())
        confirmation_limit: int = self._pending_capacity
        with self._session_factory.begin() as session:
            confirmation_statement = (
                select(ConnectorObserverReceiptModel)
                .where(
                    ConnectorObserverReceiptModel.tenant_id == tenant_id,
                    ConnectorObserverReceiptModel.device_id == device_id,
                    ConnectorObserverReceiptModel.state == "pending",
                    ConnectorObserverReceiptModel.dispatch_connection_id == connection,
                    ConnectorObserverReceiptModel.dispatch_sequence.is_not(None),
                    ConnectorObserverReceiptModel.dispatch_sequence < durable_cursor,
                )
                .order_by(
                    ConnectorObserverReceiptModel.dispatch_sequence,
                    ConnectorObserverReceiptModel.observer_message_id,
                )
                .limit(confirmation_limit)
                .with_for_update()
            )
            rows = session.scalars(confirmation_statement).all()
            for row in rows:
                row.state = "settled"
                row.settled_at = now
                row.updated_at = now
            self._prune_settled(session, tenant_id=tenant_id, device_id=device_id)
            return len(rows)

    def _prune_settled(
        self,
        session: Session,
        *,
        tenant_id: UUID,
        device_id: UUID,
    ) -> None:
        pruning_limit: int = self._settled_retention + _PRUNE_BATCH
        pruning_statement = (
            select(ConnectorObserverReceiptModel)
            .where(
                ConnectorObserverReceiptModel.tenant_id == tenant_id,
                ConnectorObserverReceiptModel.device_id == device_id,
                ConnectorObserverReceiptModel.state == "settled",
            )
            .order_by(
                ConnectorObserverReceiptModel.settled_at.desc(),
                ConnectorObserverReceiptModel.observer_message_id.desc(),
            )
            .limit(pruning_limit)
            .with_for_update()
        )
        rows = session.scalars(pruning_statement).all()
        for row in rows[self._settled_retention :]:
            session.delete(row)


def _identity(identity: ConnectorIdentity) -> tuple[UUID, UUID]:
    return _uuid(identity.tenant_id), _uuid(identity.device_id)


def _uuid(value: str) -> UUID:
    if type(value) is not str:
        raise ValueError("Observer receipt identity is invalid")
    parsed = UUID(value)
    if parsed.variant != RFC_4122 or str(parsed) != value:
        raise ValueError("Observer receipt identity is invalid")
    return parsed


def _factory_uuid(factory: Callable[[], UUID]) -> UUID:
    value = factory()
    if type(value) is not UUID or value.variant != RFC_4122:
        raise ValueError("Observer receipt message identity is invalid")
    return value


def _sequence(value: int) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("Observer receipt sequence is invalid")
    return value


def _receipt_type(value: str) -> str:
    if type(value) is not str or value not in {"stream.ack", "stream.nack"}:
        raise ValueError("Observer receipt type is invalid")
    return value


def _payload(
    value: Mapping[str, object],
    observer_message_id: str,
) -> tuple[dict[str, object], str]:
    if not isinstance(value, Mapping):
        raise TypeError("Observer receipt payload is invalid")
    record = dict(value)
    if record.get("observer_message_id") != observer_message_id:
        raise ValueError("Observer receipt payload identity is invalid")
    encoded = json.dumps(
        record,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if len(encoded) > _MAX_RECEIPT_PAYLOAD_BYTES:
        raise ValueError("Observer receipt payload is too large")
    return record, canonical_payload_digest(record)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Observer receipt clock must be timezone-aware")
    return value.astimezone(UTC)


def _timestamp(value: datetime) -> str:
    return (
        value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    )


def _delivery(row: ConnectorObserverReceiptModel) -> ConnectorObserverReceiptDelivery:
    if (
        row.dispatch_message_id is None
        or row.dispatch_sequence is None
        or row.dispatch_connection_id is None
    ):
        raise RuntimeError("Observer receipt dispatch is incomplete")
    return ConnectorObserverReceiptDelivery(
        observer_message_id=str(row.observer_message_id),
        message_id=str(row.dispatch_message_id),
        message_type=row.receipt_type,
        sequence=row.dispatch_sequence,
        sent_at=_timestamp(_database_utc(row.updated_at)),
        payload=dict(row.payload),
    )


def _database_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
