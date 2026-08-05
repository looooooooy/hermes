from __future__ import annotations

import hashlib
from datetime import datetime

from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session

from hermes_connector.adapters.persistence.sqlite.models.observer_outbox import (
    ObserverOutboxRow,
)
from hermes_connector.domain.observer import StreamAck, StreamNack
from hermes_connector.domain.storage import (
    IdempotencyConflict,
    ObserverOutboxRecord,
    StorageOverloaded,
)


def put(
    session: Session,
    *,
    message_id: str,
    connector_sequence: int,
    transport_epoch_id: str | None,
    message_type: str,
    profile: str,
    session_key: str,
    runtime_generation: str,
    runtime_session_id: str,
    event_sequence: int,
    payload: bytes,
    frame: bytes,
    max_pending: int,
    now: str,
) -> ObserverOutboxRecord:
    digest = hashlib.sha256(payload).hexdigest()
    identity = (
        message_type,
        profile,
        session_key,
        runtime_generation,
        runtime_session_id,
        event_sequence,
    )
    existing = session.get(ObserverOutboxRow, message_id)
    if existing is not None:
        existing_identity = (
            existing.message_type,
            existing.profile,
            existing.session_key,
            existing.runtime_generation,
            existing.runtime_session_id,
            existing.event_sequence,
        )
        if (
            existing.message_id != message_id
            or existing.connector_sequence != connector_sequence
            or existing.transport_epoch_id != transport_epoch_id
            or existing_identity != identity
            or existing.payload_digest != digest
            or bytes(existing.payload) != payload
            or bytes(existing.frame) != frame
        ):
            raise IdempotencyConflict()
        return _record(existing)

    occupied_sequence = session.scalar(
        select(ObserverOutboxRow).where(
            ObserverOutboxRow.transport_epoch_id == transport_epoch_id,
            ObserverOutboxRow.connector_sequence == connector_sequence,
        )
    )
    if occupied_sequence is not None:
        raise IdempotencyConflict()
    total_count = int(
        session.scalar(select(func.count()).select_from(ObserverOutboxRow)) or 0
    )
    if total_count >= max_pending:
        required = total_count - max_pending + 1
        if transport_epoch_id is None:
            old_epoch = ObserverOutboxRow.transport_epoch_id.is_not(None)
        else:
            old_epoch = or_(
                ObserverOutboxRow.transport_epoch_id.is_(None),
                ObserverOutboxRow.transport_epoch_id != transport_epoch_id,
            )
        removable = session.scalars(
            select(ObserverOutboxRow)
            .where(ObserverOutboxRow.state.in_(("acked", "rejected", "retired")))
            .order_by(
                case((old_epoch, 0), else_=1),
                ObserverOutboxRow.settled_at,
                ObserverOutboxRow.created_at,
                ObserverOutboxRow.message_id,
            )
            .limit(required)
        ).all()
        if len(removable) != required:
            raise StorageOverloaded()
        for candidate in removable:
            session.delete(candidate)
        session.flush()
    row = ObserverOutboxRow(
        message_id=message_id,
        payload_digest=digest,
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
        state="pending",
        created_at=now,
        settled_at=None,
    )
    session.add(row)
    session.flush()
    return _record(row)


def get(session: Session, message_id: str) -> ObserverOutboxRecord | None:
    row = session.get(ObserverOutboxRow, message_id)
    return _record(row) if row is not None else None


def get_fact(
    session: Session,
    *,
    transport_epoch_id: str | None,
    message_type: str,
    profile: str,
    session_key: str,
    runtime_generation: str,
    runtime_session_id: str,
    event_sequence: int,
) -> ObserverOutboxRecord | None:
    row = session.scalar(
        select(ObserverOutboxRow)
        .where(
            ObserverOutboxRow.transport_epoch_id == transport_epoch_id,
            ObserverOutboxRow.message_type == message_type,
            ObserverOutboxRow.profile == profile,
            ObserverOutboxRow.session_key == session_key,
            ObserverOutboxRow.runtime_generation == runtime_generation,
            ObserverOutboxRow.runtime_session_id == runtime_session_id,
            ObserverOutboxRow.event_sequence == event_sequence,
        )
        .order_by(ObserverOutboxRow.connector_sequence.desc())
        .limit(1)
    )
    return _record(row) if row is not None else None


def pending(
    session: Session,
    *,
    limit: int,
    after_sequence: int | None,
    include_settled: bool = False,
) -> tuple[ObserverOutboxRecord, ...]:
    statement = select(ObserverOutboxRow)
    if not include_settled:
        statement = statement.where(ObserverOutboxRow.state == "pending")
    if after_sequence is not None:
        statement = statement.where(
            ObserverOutboxRow.connector_sequence > after_sequence
        )
    rows = session.scalars(
        statement.order_by(ObserverOutboxRow.connector_sequence).limit(limit)
    ).all()
    return tuple(_record(row) for row in rows)


def acknowledge(
    session: Session,
    *,
    ack: StreamAck,
) -> ObserverOutboxRecord:
    row = _matching_row(session, ack)
    if row.state == "rejected":
        raise IdempotencyConflict()
    if row.state == "pending":
        row.state = "acked"
        row.settled_at = _utc_text(ack.committed_at)
        session.flush()
    return _record(row)


def reject(
    session: Session,
    *,
    nack: StreamNack,
) -> ObserverOutboxRecord:
    row = _matching_row(session, nack)
    if row.state == "acked":
        raise IdempotencyConflict()
    if row.state == "pending":
        row.state = "rejected"
        row.settled_at = _utc_text(nack.rejected_at)
        session.flush()
    return _record(row)


def validate_transport_target(
    session: Session,
    *,
    message_id: str,
    epoch_id: str,
    sequence: int,
    message_type: str,
    event_sequence: int,
) -> bool:
    row = session.get(ObserverOutboxRow, message_id)
    return bool(
        row is not None
        and row.transport_epoch_id == epoch_id
        and row.connector_sequence == sequence
        and row.message_type == message_type
        and row.event_sequence == event_sequence
    )


def _matching_row(
    session: Session,
    receipt: StreamAck | StreamNack,
) -> ObserverOutboxRow:
    row = session.get(ObserverOutboxRow, str(receipt.observer_message_id))
    if row is None:
        raise IdempotencyConflict()
    expected = (
        receipt.payload_digest,
        receipt.connector_sequence,
        receipt.observer_message_type,
        receipt.profile,
        receipt.session_key,
        receipt.runtime_generation,
        receipt.runtime_session_id,
        receipt.event_sequence,
    )
    actual = (
        row.payload_digest,
        row.connector_sequence,
        row.message_type,
        row.profile,
        row.session_key,
        row.runtime_generation,
        row.runtime_session_id,
        row.event_sequence,
    )
    if actual != expected:
        raise IdempotencyConflict()
    return row


def _record(row: ObserverOutboxRow) -> ObserverOutboxRecord:
    return ObserverOutboxRecord(
        message_id=row.message_id,
        payload_digest=row.payload_digest,
        connector_sequence=row.connector_sequence,
        message_type=row.message_type,
        profile=row.profile,
        session_key=row.session_key,
        runtime_generation=row.runtime_generation,
        runtime_session_id=row.runtime_session_id,
        event_sequence=row.event_sequence,
        payload=bytes(row.payload),
        frame=bytes(row.frame),
        state=row.state,
        transport_epoch_id=row.transport_epoch_id,
    )


def _utc_text(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


__all__ = [
    "acknowledge",
    "get",
    "get_fact",
    "pending",
    "put",
    "reject",
    "validate_transport_target",
]
