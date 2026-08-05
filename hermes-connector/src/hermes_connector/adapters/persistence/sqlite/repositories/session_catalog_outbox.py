from __future__ import annotations

import hashlib

from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session

from hermes_connector.adapters.persistence.sqlite.models.session_catalog_ack_receipt import (
    SessionCatalogAckReceiptRow,
)
from hermes_connector.adapters.persistence.sqlite.models.session_catalog_outbox import (
    SessionCatalogOutboxRow,
)
from hermes_connector.domain.session_catalog import (
    SessionCatalogAck,
    SessionCatalogNack,
)
from hermes_connector.domain.storage import (
    IdempotencyConflict,
    SessionCatalogOutboxRecord,
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
    runtime_generation: str,
    snapshot_id: str | None,
    catalog_revision: int | None,
    page_index: int | None,
    is_last: bool | None,
    catalog_sequence: int | None,
    payload: bytes,
    frame: bytes,
    max_pending: int,
    now: str,
) -> SessionCatalogOutboxRecord:
    digest = hashlib.sha256(payload).hexdigest()
    identity = (
        message_type,
        profile,
        runtime_generation,
        snapshot_id,
        catalog_revision,
        page_index,
        is_last,
        catalog_sequence,
    )
    existing = session.get(SessionCatalogOutboxRow, message_id)
    if existing is not None:
        existing_identity = (
            existing.message_type,
            existing.profile,
            existing.runtime_generation,
            existing.snapshot_id,
            existing.catalog_revision,
            existing.page_index,
            existing.is_last,
            existing.catalog_sequence,
        )
        if (
            existing.connector_sequence != connector_sequence
            or existing.transport_epoch_id != transport_epoch_id
            or existing_identity != identity
            or existing.payload_digest != digest
            or bytes(existing.payload) != payload
            or bytes(existing.frame) != frame
        ):
            raise IdempotencyConflict()
        return _record(existing)

    occupied_sequence = session.scalar(
        select(SessionCatalogOutboxRow).where(
            SessionCatalogOutboxRow.transport_epoch_id == transport_epoch_id,
            SessionCatalogOutboxRow.connector_sequence == connector_sequence,
        )
    )
    if occupied_sequence is not None:
        raise IdempotencyConflict()
    _make_capacity(
        session,
        maximum=max_pending,
        current_epoch_id=transport_epoch_id,
    )
    row = SessionCatalogOutboxRow(
        message_id=message_id,
        payload_digest=digest,
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
        state="pending",
        created_at=now,
        settled_at=None,
        rejection_reason=None,
        rejection_snapshot_id=None,
        rejection_expected_page_index=None,
        rejection_expected_catalog_sequence=None,
    )
    session.add(row)
    session.flush()
    return _record(row)


def get(session: Session, message_id: str) -> SessionCatalogOutboxRecord | None:
    row = session.get(SessionCatalogOutboxRow, message_id)
    return _record(row) if row is not None else None


def get_fact(
    session: Session,
    *,
    transport_epoch_id: str | None,
    message_type: str,
    profile: str,
    runtime_generation: str,
    snapshot_id: str | None,
    catalog_revision: int | None,
    page_index: int | None,
    catalog_sequence: int | None,
) -> SessionCatalogOutboxRecord | None:
    row = session.scalar(
        select(SessionCatalogOutboxRow)
        .where(
            SessionCatalogOutboxRow.transport_epoch_id == transport_epoch_id,
            SessionCatalogOutboxRow.message_type == message_type,
            SessionCatalogOutboxRow.profile == profile,
            SessionCatalogOutboxRow.runtime_generation == runtime_generation,
            SessionCatalogOutboxRow.snapshot_id == snapshot_id,
            SessionCatalogOutboxRow.catalog_revision == catalog_revision,
            SessionCatalogOutboxRow.page_index == page_index,
            SessionCatalogOutboxRow.catalog_sequence == catalog_sequence,
        )
        .order_by(SessionCatalogOutboxRow.connector_sequence.desc())
        .limit(1)
    )
    return _record(row) if row is not None else None


def pending(
    session: Session,
    *,
    limit: int,
    after_sequence: int | None,
    include_settled: bool = False,
) -> tuple[SessionCatalogOutboxRecord, ...]:
    statement = select(SessionCatalogOutboxRow)
    if not include_settled:
        statement = statement.where(SessionCatalogOutboxRow.state == "pending")
    if after_sequence is not None:
        statement = statement.where(
            SessionCatalogOutboxRow.connector_sequence > after_sequence
        )
    rows = session.scalars(
        statement.order_by(SessionCatalogOutboxRow.connector_sequence).limit(limit)
    ).all()
    return tuple(_record(row) for row in rows)


def acknowledge(
    session: Session,
    *,
    ack: SessionCatalogAck,
    now: str,
    max_receipts: int,
) -> SessionCatalogOutboxRecord:
    persisted = session.get(
        SessionCatalogOutboxRow,
        str(ack.acked_message_id),
    )
    if persisted is None:
        return _matching_ack_receipt(session, ack)
    row = _matching_ack_row(session, ack)
    if row.state in {"rejected", "retired"}:
        raise IdempotencyConflict()
    is_snapshot = row.message_type == "session.catalog.snapshot.page"
    if is_snapshot and (row.is_last is not True or row.page_index is None):
        raise IdempotencyConflict()
    if row.state == "acked":
        if is_snapshot:
            _ensure_terminal_ack_receipt(
                session,
                ack=ack,
                now=now,
                maximum=max_receipts,
                replace=False,
            )
        return _record(row)
    rows = [row]
    if is_snapshot:
        rows = list(
            session.scalars(
                select(SessionCatalogOutboxRow)
                .where(
                    SessionCatalogOutboxRow.message_type
                    == "session.catalog.snapshot.page",
                    SessionCatalogOutboxRow.profile == row.profile,
                    SessionCatalogOutboxRow.runtime_generation
                    == row.runtime_generation,
                    SessionCatalogOutboxRow.snapshot_id == row.snapshot_id,
                    SessionCatalogOutboxRow.catalog_revision
                    == row.catalog_revision,
                    SessionCatalogOutboxRow.page_index <= row.page_index,
                )
                .order_by(SessionCatalogOutboxRow.page_index)
            )
        )
        if (
            [candidate.page_index for candidate in rows]
            != list(range(row.page_index + 1))
            or any(candidate.state in {"rejected", "retired"} for candidate in rows)
        ):
            raise IdempotencyConflict()
    for candidate in rows:
        if candidate.state == "pending":
            candidate.state = "acked"
            candidate.settled_at = now
    if is_snapshot:
        _ensure_terminal_ack_receipt(
            session,
            ack=ack,
            now=now,
            maximum=max_receipts,
            replace=True,
        )
    session.flush()
    return _record(row)


def reject(
    session: Session,
    *,
    nack: SessionCatalogNack,
    now: str,
) -> SessionCatalogOutboxRecord:
    row = _matching_nack_row(session, nack)
    if row.state in {"acked", "retired"}:
        raise IdempotencyConflict()
    rejection_tuple = (
        nack.reason,
        str(nack.snapshot_id) if nack.snapshot_id is not None else None,
        nack.expected_page_index,
        nack.expected_catalog_sequence,
    )
    if row.state == "rejected":
        persisted_tuple = (
            row.rejection_reason,
            row.rejection_snapshot_id,
            row.rejection_expected_page_index,
            row.rejection_expected_catalog_sequence,
        )
        if persisted_tuple != rejection_tuple:
            raise IdempotencyConflict()
        return _record(row)
    if row.state == "pending":
        row.state = "rejected"
        row.settled_at = now
        row.rejection_reason = nack.reason
        row.rejection_snapshot_id = rejection_tuple[1]
        row.rejection_expected_page_index = nack.expected_page_index
        row.rejection_expected_catalog_sequence = nack.expected_catalog_sequence
        siblings = session.scalars(
            select(SessionCatalogOutboxRow).where(
                SessionCatalogOutboxRow.message_id != row.message_id,
                SessionCatalogOutboxRow.profile == row.profile,
                SessionCatalogOutboxRow.runtime_generation
                == row.runtime_generation,
                SessionCatalogOutboxRow.state == "pending",
            )
        ).all()
        for sibling in siblings:
            sibling.state = "retired"
            sibling.settled_at = now
        session.flush()
    return _record(row)


def retire_pending(
    session: Session,
    *,
    now: str,
) -> None:
    rows = session.scalars(
        select(SessionCatalogOutboxRow).where(
            SessionCatalogOutboxRow.state == "pending"
        )
    ).all()
    for row in rows:
        row.state = "retired"
        row.settled_at = now
    session.flush()


def validate_transport_target(
    session: Session,
    *,
    message_id: str,
    epoch_id: str,
    sequence: int,
    message_type: str,
    catalog_revision: int,
) -> bool:
    row = session.get(SessionCatalogOutboxRow, message_id)
    business_revision = (
        row.page_index if row is not None and row.page_index is not None else row.catalog_sequence
        if row is not None
        else None
    )
    return bool(
        row is not None
        and row.transport_epoch_id == epoch_id
        and row.connector_sequence == sequence
        and row.message_type == message_type
        and business_revision == catalog_revision
    )


def _matching_ack_row(
    session: Session,
    ack: SessionCatalogAck,
) -> SessionCatalogOutboxRow:
    row = session.get(SessionCatalogOutboxRow, str(ack.acked_message_id))
    if row is None:
        raise IdempotencyConflict()
    expected_position = (
        str(ack.snapshot_id) if ack.snapshot_id is not None else None,
        ack.catalog_revision,
        ack.page_index,
        ack.is_last,
        ack.catalog_sequence,
    )
    actual = (
        row.payload_digest,
        row.connector_sequence,
        row.profile,
        row.runtime_generation,
        row.snapshot_id,
        row.catalog_revision,
        row.page_index,
        row.is_last,
        row.catalog_sequence,
    )
    expected = (
        ack.acked_payload_digest,
        ack.acked_connector_sequence,
        ack.profile,
        ack.runtime_generation,
        *expected_position,
    )
    expected_kind = (
        "snapshot_committed"
        if row.message_type == "session.catalog.snapshot.page"
        else "event_applied"
    )
    if actual != expected or ack.ack_kind != expected_kind:
        raise IdempotencyConflict()
    return row


def _matching_ack_receipt(
    session: Session,
    ack: SessionCatalogAck,
) -> SessionCatalogOutboxRecord:
    receipt = session.get(
        SessionCatalogAckReceiptRow,
        {
            "profile": ack.profile,
            "runtime_generation": ack.runtime_generation,
        },
    )
    if receipt is None or not _receipt_matches(receipt, ack):
        raise IdempotencyConflict()
    return _receipt_record(receipt)


def _ensure_terminal_ack_receipt(
    session: Session,
    *,
    ack: SessionCatalogAck,
    now: str,
    maximum: int,
    replace: bool,
) -> None:
    if (
        ack.ack_kind != "snapshot_committed"
        or ack.snapshot_id is None
        or ack.catalog_revision is None
        or ack.page_index is None
        or ack.is_last is not True
        or ack.catalog_sequence is not None
    ):
        raise IdempotencyConflict()
    key = {
        "profile": ack.profile,
        "runtime_generation": ack.runtime_generation,
    }
    receipt = session.get(SessionCatalogAckReceiptRow, key)
    if receipt is not None and _receipt_matches(receipt, ack):
        return
    if receipt is not None and not replace:
        return
    if receipt is not None:
        if ack.acked_connector_sequence <= receipt.acked_connector_sequence:
            raise IdempotencyConflict()
        _assign_receipt(receipt, ack=ack, now=now)
        return
    _make_receipt_capacity(session, maximum=maximum)
    receipt = SessionCatalogAckReceiptRow(
        profile=ack.profile,
        runtime_generation=ack.runtime_generation,
        acked_message_id=str(ack.acked_message_id),
        acked_payload_digest=ack.acked_payload_digest,
        acked_connector_sequence=ack.acked_connector_sequence,
        snapshot_id=str(ack.snapshot_id),
        catalog_revision=ack.catalog_revision,
        page_index=ack.page_index,
        is_last=True,
        acked_at=now,
    )
    session.add(receipt)


def _assign_receipt(
    receipt: SessionCatalogAckReceiptRow,
    *,
    ack: SessionCatalogAck,
    now: str,
) -> None:
    assert ack.snapshot_id is not None
    assert ack.catalog_revision is not None
    assert ack.page_index is not None
    receipt.acked_message_id = str(ack.acked_message_id)
    receipt.acked_payload_digest = ack.acked_payload_digest
    receipt.acked_connector_sequence = ack.acked_connector_sequence
    receipt.snapshot_id = str(ack.snapshot_id)
    receipt.catalog_revision = ack.catalog_revision
    receipt.page_index = ack.page_index
    receipt.is_last = True
    receipt.acked_at = now


def _receipt_matches(
    receipt: SessionCatalogAckReceiptRow,
    ack: SessionCatalogAck,
) -> bool:
    return (
        ack.ack_kind == "snapshot_committed"
        and ack.catalog_sequence is None
        and ack.is_last is True
        and ack.snapshot_id is not None
        and receipt.acked_message_id == str(ack.acked_message_id)
        and receipt.acked_payload_digest == ack.acked_payload_digest
        and receipt.acked_connector_sequence == ack.acked_connector_sequence
        and receipt.snapshot_id == str(ack.snapshot_id)
        and receipt.catalog_revision == ack.catalog_revision
        and receipt.page_index == ack.page_index
        and receipt.is_last is True
    )


def _make_receipt_capacity(session: Session, *, maximum: int) -> None:
    count = int(
        session.scalar(
            select(func.count()).select_from(SessionCatalogAckReceiptRow)
        )
        or 0
    )
    if count < maximum:
        return
    required = count - maximum + 1
    receipts = session.scalars(
        select(SessionCatalogAckReceiptRow)
        .order_by(
            SessionCatalogAckReceiptRow.acked_at,
            SessionCatalogAckReceiptRow.profile,
            SessionCatalogAckReceiptRow.runtime_generation,
        )
        .limit(required)
    ).all()
    if len(receipts) != required:
        raise StorageOverloaded()
    for receipt in receipts:
        session.delete(receipt)
    session.flush()


def _matching_nack_row(
    session: Session,
    nack: SessionCatalogNack,
) -> SessionCatalogOutboxRow:
    row = session.get(SessionCatalogOutboxRow, str(nack.rejected_message_id))
    if row is None:
        raise IdempotencyConflict()
    actual = (
        row.payload_digest,
        row.connector_sequence,
        row.profile,
        row.runtime_generation,
    )
    expected = (
        nack.rejected_payload_digest,
        nack.rejected_connector_sequence,
        nack.profile,
        nack.runtime_generation,
    )
    if actual != expected:
        raise IdempotencyConflict()
    if nack.reason in {"page_gap", "revision_conflict"}:
        if (
            row.message_type != "session.catalog.snapshot.page"
            or str(nack.snapshot_id) != row.snapshot_id
            or nack.expected_page_index is None
            or nack.expected_catalog_sequence is not None
        ):
            raise IdempotencyConflict()
    elif nack.reason == "event_gap":
        if (
            row.message_type != "session.catalog.event"
            or nack.snapshot_id is not None
            or nack.expected_page_index is not None
            or nack.expected_catalog_sequence is None
        ):
            raise IdempotencyConflict()
    elif nack.reason in {
        "runtime_mismatch",
        "stale_writer",
        "contract_mismatch",
    }:
        if any(
            value is not None
            for value in (
                nack.snapshot_id,
                nack.expected_page_index,
                nack.expected_catalog_sequence,
            )
        ):
            raise IdempotencyConflict()
    else:
        raise IdempotencyConflict()
    return row


def _make_capacity(
    session: Session,
    *,
    maximum: int,
    current_epoch_id: str | None,
) -> None:
    total = int(
        session.scalar(select(func.count()).select_from(SessionCatalogOutboxRow)) or 0
    )
    if total < maximum:
        return
    required = total - maximum + 1
    if current_epoch_id is None:
        old_epoch = SessionCatalogOutboxRow.transport_epoch_id.is_not(None)
    else:
        old_epoch = or_(
            SessionCatalogOutboxRow.transport_epoch_id.is_(None),
            SessionCatalogOutboxRow.transport_epoch_id != current_epoch_id,
        )
    removable = session.scalars(
        select(SessionCatalogOutboxRow)
        .where(SessionCatalogOutboxRow.state.in_(("acked", "rejected", "retired")))
        .order_by(
            case((old_epoch, 0), else_=1),
            SessionCatalogOutboxRow.settled_at,
            SessionCatalogOutboxRow.created_at,
            SessionCatalogOutboxRow.message_id,
        )
        .limit(required)
    ).all()
    if len(removable) != required:
        raise StorageOverloaded()
    for row in removable:
        session.delete(row)
    session.flush()


def _record(row: SessionCatalogOutboxRow) -> SessionCatalogOutboxRecord:
    return SessionCatalogOutboxRecord(
        message_id=row.message_id,
        payload_digest=row.payload_digest,
        connector_sequence=row.connector_sequence,
        message_type=row.message_type,
        profile=row.profile,
        runtime_generation=row.runtime_generation,
        snapshot_id=row.snapshot_id,
        catalog_revision=row.catalog_revision,
        page_index=row.page_index,
        is_last=row.is_last,
        catalog_sequence=row.catalog_sequence,
        payload=bytes(row.payload),
        frame=bytes(row.frame),
        state=row.state,
        transport_epoch_id=row.transport_epoch_id,
        rejection_reason=row.rejection_reason,
        rejection_snapshot_id=row.rejection_snapshot_id,
        rejection_expected_page_index=row.rejection_expected_page_index,
        rejection_expected_catalog_sequence=row.rejection_expected_catalog_sequence,
    )


def _receipt_record(
    receipt: SessionCatalogAckReceiptRow,
) -> SessionCatalogOutboxRecord:
    return SessionCatalogOutboxRecord(
        message_id=receipt.acked_message_id,
        payload_digest=receipt.acked_payload_digest,
        connector_sequence=receipt.acked_connector_sequence,
        message_type="session.catalog.snapshot.page",
        profile=receipt.profile,
        runtime_generation=receipt.runtime_generation,
        snapshot_id=receipt.snapshot_id,
        catalog_revision=receipt.catalog_revision,
        page_index=receipt.page_index,
        is_last=True,
        catalog_sequence=None,
        payload=b"",
        frame=b"",
        state="acked",
        transport_epoch_id=None,
    )


__all__ = [
    "acknowledge",
    "get",
    "get_fact",
    "pending",
    "put",
    "reject",
    "retire_pending",
    "validate_transport_target",
]
