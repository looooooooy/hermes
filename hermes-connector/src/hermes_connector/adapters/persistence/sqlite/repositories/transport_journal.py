from __future__ import annotations

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.orm import Session

from hermes_connector.adapters.persistence.sqlite.models.cloud_session import (
    CloudSessionCheckpointRow,
)
from hermes_connector.adapters.persistence.sqlite.models.observer_outbox import (
    ObserverOutboxRow,
)
from hermes_connector.adapters.persistence.sqlite.models.session_catalog_ack_receipt import (
    SessionCatalogAckReceiptRow,
)
from hermes_connector.adapters.persistence.sqlite.models.session_catalog_outbox import (
    SessionCatalogOutboxRow,
)
from hermes_connector.adapters.persistence.sqlite.models.transport_journal import (
    TransportFrameJournalRow,
)
from hermes_connector.adapters.sqlite_models import OutboxMessage
from hermes_connector.domain.identifiers import canonical_uuid
from hermes_connector.domain.storage import (
    CloudSessionCheckpoint,
    IdempotencyConflict,
    StorageOverloaded,
    StorageSequenceConflict,
    TransportFrameRecord,
)

_CHECKPOINT_ID = 1
_ACTIVE_STATES = frozenset({"staged", "sent"})
_TERMINAL_STATES = frozenset({"settled", "retired"})
_BUSINESS_PAIRS = {
    "connector.heartbeat": "heartbeat",
    "command.receipt": "command.receipt",
    "command.result": "command.result",
    "control.response": "control.response",
    "session.snapshot": "observer",
    "session.event": "observer",
    "session.snapshot.v2": "observer",
    "session.event.v2": "observer",
    "session.catalog.snapshot.page": "session_catalog",
    "session.catalog.event": "session_catalog",
}


def begin_epoch(
    session: Session,
    *,
    epoch_id: str,
    runtime_generation: str,
    previous_connection_id: str | None,
    next_outbound_sequence: int,
    next_inbound_sequence: int,
    now: str,
) -> CloudSessionCheckpoint:
    checkpoint = _checkpoint(session, now)
    if checkpoint.transport_epoch_id == epoch_id:
        exact = (
            checkpoint.runtime_generation == runtime_generation
            and checkpoint.previous_connection_id == previous_connection_id
            and checkpoint.next_outbound_sequence == next_outbound_sequence
            and checkpoint.next_inbound_sequence == next_inbound_sequence
            and not checkpoint.fresh_epoch_required
        )
        if not exact:
            raise IdempotencyConflict()
        return _checkpoint_record(checkpoint)
    if checkpoint.transport_epoch_id != epoch_id:
        stale_receipts = session.scalars(
            select(SessionCatalogAckReceiptRow).where(
                SessionCatalogAckReceiptRow.runtime_generation
                != runtime_generation
            )
        ).all()
        for receipt in stale_receipts:
            session.delete(receipt)
        active = session.scalars(
            select(TransportFrameJournalRow).where(
                TransportFrameJournalRow.state.in_(_ACTIVE_STATES)
            )
        ).all()
        for row in active:
            row.state = "retired"
            row.updated_at = now
            row.settled_at = now
        observer_attempts = session.scalars(
            select(ObserverOutboxRow).where(ObserverOutboxRow.state == "pending")
        ).all()
        for row in observer_attempts:
            row.state = "retired"
            row.settled_at = now
        catalog_attempts = session.scalars(
            select(SessionCatalogOutboxRow).where(
                SessionCatalogOutboxRow.state == "pending"
            )
        ).all()
        for row in catalog_attempts:
            row.state = "retired"
            row.settled_at = now
        legacy = session.scalars(
            select(OutboxMessage).where(OutboxMessage.state == "pending")
        ).all()
        for row in legacy:
            row.state = "retired"
            row.acked_at = now
    checkpoint.previous_connection_id = previous_connection_id
    checkpoint.transport_epoch_id = epoch_id
    checkpoint.runtime_generation = runtime_generation
    checkpoint.next_outbound_sequence = next_outbound_sequence
    checkpoint.next_inbound_sequence = next_inbound_sequence
    checkpoint.transport_recovery_floor = next_outbound_sequence
    checkpoint.reconciliation_required = False
    checkpoint.fresh_epoch_required = False
    checkpoint.updated_at = now
    session.flush()
    return _checkpoint_record(checkpoint)


def commit_handshake(
    session: Session,
    *,
    epoch_id: str,
    previous_connection_id: str,
    next_outbound_sequence: int,
    next_inbound_sequence: int,
    now: str,
) -> CloudSessionCheckpoint:
    checkpoint = session.get(CloudSessionCheckpointRow, _CHECKPOINT_ID)
    if (
        checkpoint is None
        or checkpoint.fresh_epoch_required
        or checkpoint.transport_epoch_id != epoch_id
    ):
        raise StorageSequenceConflict()
    exact = (
        checkpoint.previous_connection_id == previous_connection_id
        and checkpoint.next_outbound_sequence == next_outbound_sequence
        and checkpoint.next_inbound_sequence == next_inbound_sequence
    )
    if exact:
        if checkpoint.transport_recovery_floor < next_outbound_sequence:
            checkpoint.transport_recovery_floor = next_outbound_sequence
            checkpoint.updated_at = now
            session.flush()
        return _checkpoint_record(checkpoint)
    current_outbound = checkpoint.next_outbound_sequence
    current_inbound = checkpoint.next_inbound_sequence
    fresh_preserve = (
        checkpoint.previous_connection_id is None
        and current_outbound == 0
        and current_inbound == 0
        and next_outbound_sequence == current_outbound
        and next_inbound_sequence == current_inbound
    )
    strict_advance = (
        previous_connection_id != checkpoint.previous_connection_id
        and next_outbound_sequence == current_outbound + 1
        and next_inbound_sequence == current_inbound + 1
    )
    occupied_handshake_sequence = session.scalar(
        select(TransportFrameJournalRow.message_id).where(
            TransportFrameJournalRow.epoch_id == epoch_id,
            TransportFrameJournalRow.sequence == current_outbound,
        )
    )
    if not fresh_preserve and (
        not strict_advance or occupied_handshake_sequence is not None
    ):
        raise IdempotencyConflict()
    checkpoint.previous_connection_id = previous_connection_id
    checkpoint.next_outbound_sequence = next_outbound_sequence
    checkpoint.next_inbound_sequence = next_inbound_sequence
    checkpoint.transport_recovery_floor = next_outbound_sequence
    checkpoint.reconciliation_required = False
    checkpoint.updated_at = now
    session.flush()
    return _checkpoint_record(checkpoint)


def stage(
    session: Session,
    *,
    epoch_id: str,
    sequence: int,
    message_id: str,
    message_type: str,
    business_kind: str,
    business_key: str,
    business_revision: int,
    runtime_generation: str | None,
    frame: bytes,
    max_entries: int,
    now: str,
) -> TransportFrameRecord:
    _validate_business_identity(
        sequence=sequence,
        message_type=message_type,
        business_kind=business_kind,
        business_key=business_key,
        business_revision=business_revision,
    )
    existing = session.get(TransportFrameJournalRow, message_id)
    if existing is not None:
        actual = (
            existing.epoch_id,
            existing.sequence,
            existing.message_id,
            existing.message_type,
            existing.business_kind,
            existing.business_key,
            existing.business_revision,
            existing.runtime_generation,
            bytes(existing.frame),
        )
        expected = (
            epoch_id,
            sequence,
            message_id,
            message_type,
            business_kind,
            business_key,
            business_revision,
            runtime_generation,
            frame,
        )
        if actual != expected:
            raise IdempotencyConflict()
        return _record(existing)
    existing_business = session.scalar(
        select(TransportFrameJournalRow).where(
            TransportFrameJournalRow.epoch_id == epoch_id,
            TransportFrameJournalRow.business_kind == business_kind,
            TransportFrameJournalRow.business_key == business_key,
            TransportFrameJournalRow.business_revision == business_revision,
        )
    )
    if existing_business is not None:
        return _record(existing_business)

    checkpoint = session.get(CloudSessionCheckpointRow, _CHECKPOINT_ID)
    if (
        checkpoint is None
        or checkpoint.fresh_epoch_required
        or checkpoint.transport_epoch_id != epoch_id
        or checkpoint.next_outbound_sequence != sequence
    ):
        raise StorageSequenceConflict()
    occupied = session.scalar(
        select(TransportFrameJournalRow).where(
            TransportFrameJournalRow.epoch_id == epoch_id,
            TransportFrameJournalRow.sequence == sequence,
        )
    )
    if occupied is not None:
        raise IdempotencyConflict()
    _make_capacity(session, maximum=max_entries, current_epoch_id=epoch_id)
    row = TransportFrameJournalRow(
        message_id=message_id,
        epoch_id=epoch_id,
        sequence=sequence,
        message_type=message_type,
        business_kind=business_kind,
        business_key=business_key,
        business_revision=business_revision,
        runtime_generation=runtime_generation,
        frame=frame,
        state="staged",
        created_at=now,
        updated_at=now,
        settled_at=None,
    )
    session.add(row)
    session.flush()
    return _record(row)


def mark_sent(
    session: Session,
    *,
    epoch_id: str,
    sequence: int,
    now: str,
) -> TransportFrameRecord:
    row = session.scalar(
        select(TransportFrameJournalRow).where(
            TransportFrameJournalRow.epoch_id == epoch_id,
            TransportFrameJournalRow.sequence == sequence,
        )
    )
    checkpoint = session.get(CloudSessionCheckpointRow, _CHECKPOINT_ID)
    if row is None or checkpoint is None or checkpoint.transport_epoch_id != epoch_id:
        raise StorageSequenceConflict()
    if row.state == "sent" and checkpoint.next_outbound_sequence == sequence + 1:
        return _record(row)
    if row.state != "staged" or checkpoint.next_outbound_sequence != sequence:
        raise StorageSequenceConflict()
    row.state = "sent"
    row.updated_at = now
    checkpoint.next_outbound_sequence += 1
    checkpoint.updated_at = now
    session.flush()
    return _record(row)


def get(session: Session, message_id: str) -> TransportFrameRecord | None:
    row = session.get(TransportFrameJournalRow, message_id)
    return _record(row) if row is not None else None


def pending(
    session: Session,
    *,
    epoch_id: str,
    limit: int,
    after_sequence: int | None,
) -> tuple[TransportFrameRecord, ...]:
    statement = select(TransportFrameJournalRow).where(
        TransportFrameJournalRow.epoch_id == epoch_id,
        TransportFrameJournalRow.state.in_(_ACTIVE_STATES),
    )
    if after_sequence is not None:
        statement = statement.where(TransportFrameJournalRow.sequence > after_sequence)
    rows = session.scalars(
        statement.order_by(TransportFrameJournalRow.sequence).limit(limit)
    ).all()
    return tuple(_record(row) for row in rows)


def settle_cursor(
    session: Session,
    *,
    epoch_id: str,
    next_sequence: int,
    now: str,
) -> tuple[TransportFrameRecord, ...]:
    checkpoint = session.get(CloudSessionCheckpointRow, _CHECKPOINT_ID)
    if (
        checkpoint is None
        or checkpoint.transport_epoch_id != epoch_id
        or next_sequence > checkpoint.next_outbound_sequence
        or next_sequence < checkpoint.transport_recovery_floor
    ):
        raise StorageSequenceConflict()
    floor = checkpoint.transport_recovery_floor
    interval = session.scalars(
        select(TransportFrameJournalRow)
        .where(
            TransportFrameJournalRow.epoch_id == epoch_id,
            TransportFrameJournalRow.sequence >= floor,
            TransportFrameJournalRow.sequence < next_sequence,
        )
        .order_by(TransportFrameJournalRow.sequence)
    ).all()
    if tuple(row.sequence for row in interval) != tuple(range(floor, next_sequence)):
        raise StorageSequenceConflict()
    if any(row.state == "retired" for row in interval):
        raise StorageSequenceConflict()
    rows = tuple(row for row in interval if row.state in _ACTIVE_STATES)
    for row in rows:
        row.state = "settled"
        row.updated_at = now
        row.settled_at = now
    checkpoint.transport_recovery_floor = max(
        checkpoint.transport_recovery_floor,
        next_sequence,
    )
    checkpoint.updated_at = now
    session.flush()
    return tuple(_record(row) for row in rows)


def reconcile(
    session: Session,
    *,
    epoch_id: str,
    previous_connection_id: str,
    next_outbound_sequence: int,
    next_inbound_sequence: int,
    now: str,
) -> tuple[CloudSessionCheckpoint, tuple[TransportFrameRecord, ...]]:
    checkpoint = session.get(CloudSessionCheckpointRow, _CHECKPOINT_ID)
    if (
        checkpoint is None
        or checkpoint.fresh_epoch_required
        or checkpoint.transport_epoch_id != epoch_id
    ):
        raise StorageSequenceConflict()
    if next_outbound_sequence < checkpoint.transport_recovery_floor:
        raise StorageSequenceConflict()
    old_next = checkpoint.next_outbound_sequence
    if next_outbound_sequence != old_next:
        lower = min(old_next, next_outbound_sequence)
        upper = max(old_next, next_outbound_sequence)
        rows = session.scalars(
            select(TransportFrameJournalRow)
            .where(
                TransportFrameJournalRow.epoch_id == epoch_id,
                TransportFrameJournalRow.sequence >= lower,
                TransportFrameJournalRow.sequence < upper,
            )
            .order_by(TransportFrameJournalRow.sequence)
        ).all()
        if tuple(row.sequence for row in rows) != tuple(range(lower, upper)):
            raise StorageSequenceConflict()
        if next_outbound_sequence < old_next:
            for row in rows:
                if row.state == "retired":
                    raise StorageSequenceConflict()
                row.state = "staged"
                row.updated_at = now
                row.settled_at = None
    checkpoint.previous_connection_id = previous_connection_id
    checkpoint.next_outbound_sequence = next_outbound_sequence
    checkpoint.next_inbound_sequence = next_inbound_sequence
    checkpoint.reconciliation_required = False
    checkpoint.updated_at = now
    settled = settle_cursor(
        session,
        epoch_id=epoch_id,
        next_sequence=next_outbound_sequence,
        now=now,
    )
    session.flush()
    return _checkpoint_record(checkpoint), settled


def settle_message(
    session: Session,
    *,
    message_id: str,
    now: str,
) -> TransportFrameRecord | None:
    row = session.get(TransportFrameJournalRow, message_id)
    if row is None:
        return None
    if row.state in _ACTIVE_STATES:
        row.state = "settled"
        row.updated_at = now
        row.settled_at = now
        session.flush()
    return _record(row)


def retire_business_kind(
    session: Session,
    *,
    business_kind: str,
    now: str,
) -> None:
    rows = session.scalars(
        select(TransportFrameJournalRow).where(
            TransportFrameJournalRow.business_kind == business_kind,
            TransportFrameJournalRow.state.in_(_ACTIVE_STATES),
        )
    ).all()
    for row in rows:
        row.state = "retired"
        row.updated_at = now
        row.settled_at = now
    session.flush()


def _make_capacity(
    session: Session,
    *,
    maximum: int,
    current_epoch_id: str,
) -> None:
    count = int(
        session.scalar(select(func.count()).select_from(TransportFrameJournalRow)) or 0
    )
    if count < maximum:
        return
    required = count - maximum + 1
    checkpoint = session.get(CloudSessionCheckpointRow, _CHECKPOINT_ID)
    if checkpoint is None or checkpoint.transport_epoch_id != current_epoch_id:
        raise StorageSequenceConflict()
    removable = session.scalars(
        select(TransportFrameJournalRow)
        .where(
            or_(
                and_(
                    TransportFrameJournalRow.state.in_(_TERMINAL_STATES),
                    TransportFrameJournalRow.epoch_id != current_epoch_id,
                ),
                and_(
                    TransportFrameJournalRow.state == "settled",
                    TransportFrameJournalRow.epoch_id == current_epoch_id,
                    TransportFrameJournalRow.sequence
                    < checkpoint.transport_recovery_floor,
                ),
            )
        )
        .order_by(
            case(
                (TransportFrameJournalRow.epoch_id != current_epoch_id, 0),
                else_=1,
            ),
            TransportFrameJournalRow.updated_at,
            TransportFrameJournalRow.message_id,
        )
        .limit(required)
    ).all()
    if len(removable) != required:
        raise StorageOverloaded()
    for row in removable:
        session.delete(row)
    session.flush()


def _checkpoint(session: Session, now: str) -> CloudSessionCheckpointRow:
    row = session.get(CloudSessionCheckpointRow, _CHECKPOINT_ID)
    if row is None:
        row = CloudSessionCheckpointRow(
            id=_CHECKPOINT_ID,
            previous_connection_id=None,
            next_outbound_sequence=0,
            next_inbound_sequence=0,
            reconciliation_required=False,
            transport_epoch_id=None,
            runtime_generation=None,
            fresh_epoch_required=True,
            transport_recovery_floor=0,
            updated_at=now,
        )
        session.add(row)
        session.flush()
    return row


def _checkpoint_record(row: CloudSessionCheckpointRow) -> CloudSessionCheckpoint:
    return CloudSessionCheckpoint(
        previous_connection_id=row.previous_connection_id,
        next_outbound_sequence=row.next_outbound_sequence,
        next_inbound_sequence=row.next_inbound_sequence,
        reconciliation_required=row.reconciliation_required,
        transport_epoch_id=row.transport_epoch_id,
        runtime_generation=row.runtime_generation,
        fresh_epoch_required=row.fresh_epoch_required,
        transport_recovery_floor=row.transport_recovery_floor,
    )


def _record(row: TransportFrameJournalRow) -> TransportFrameRecord:
    return TransportFrameRecord(
        message_id=row.message_id,
        epoch_id=row.epoch_id,
        sequence=row.sequence,
        message_type=row.message_type,
        business_kind=row.business_kind,
        business_key=row.business_key,
        business_revision=row.business_revision,
        runtime_generation=row.runtime_generation,
        frame=bytes(row.frame),
        state=row.state,
        created_at=row.created_at,
        updated_at=row.updated_at,
        settled_at=row.settled_at,
    )


def _validate_business_identity(
    *,
    sequence: int,
    message_type: str,
    business_kind: str,
    business_key: str,
    business_revision: int,
) -> None:
    if _BUSINESS_PAIRS.get(message_type) != business_kind:
        raise ValueError("transport message and business kind do not match")
    if business_kind == "heartbeat":
        if (
            business_revision != sequence
            or business_key != f"heartbeat-{business_revision}"
        ):
            raise ValueError("heartbeat business identity is invalid")
        return
    canonical_uuid(business_key)


__all__ = [
    "begin_epoch",
    "commit_handshake",
    "get",
    "mark_sent",
    "pending",
    "reconcile",
    "retire_business_kind",
    "settle_cursor",
    "settle_message",
    "stage",
]
