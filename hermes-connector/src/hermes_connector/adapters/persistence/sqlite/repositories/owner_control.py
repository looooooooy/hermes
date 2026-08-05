from __future__ import annotations

import json

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from hermes_connector.adapters.persistence.sqlite.models.owner_control import (
    OwnerControlResultRow,
)
from hermes_connector.domain.storage import (
    IdempotencyConflict,
    OwnerControlPutResult,
    OwnerControlRecord,
    StorageOverloaded,
)

_TERMINAL_STATES = frozenset({"completed", "effect_unknown"})


def put(
    session: Session,
    *,
    request_id: str,
    request_digest: str,
    control_transport_id: str,
    operation: str,
    request_payload: bytes,
    scope_payload: bytes,
    max_entries: int,
    now: str,
) -> OwnerControlPutResult:
    row = session.get(OwnerControlResultRow, request_id)
    if row is not None:
        if (
            row.request_digest != request_digest
            or row.control_transport_id != control_transport_id
            or row.operation != operation
            or bytes(row.request_payload) != request_payload
            or bytes(row.scope_payload) != scope_payload
        ):
            raise IdempotencyConflict()
        return OwnerControlPutResult(_record(row), inserted=False)
    count = int(
        session.scalar(select(func.count()).select_from(OwnerControlResultRow)) or 0
    )
    if count >= max_entries:
        required = count - max_entries + 1
        removable = session.scalars(
            select(OwnerControlResultRow)
            .where(
                OwnerControlResultRow.state.in_(_TERMINAL_STATES),
                OwnerControlResultRow.transport_received.is_(True),
            )
            .order_by(
                OwnerControlResultRow.updated_at, OwnerControlResultRow.request_id
            )
            .limit(required)
        ).all()
        if len(removable) != required:
            raise StorageOverloaded()
        for candidate in removable:
            session.delete(candidate)
        session.flush()
    row = OwnerControlResultRow(
        request_id=request_id,
        request_digest=request_digest,
        control_transport_id=control_transport_id,
        operation=operation,
        request_payload=request_payload,
        scope_payload=scope_payload,
        response_payload=None,
        state="received",
        response_revision=1,
        transport_received=False,
        created_at=now,
        updated_at=now,
        completed_at=None,
    )
    session.add(row)
    session.flush()
    return OwnerControlPutResult(_record(row), inserted=True)


def get(session: Session, request_id: str) -> OwnerControlRecord | None:
    row = session.get(OwnerControlResultRow, request_id)
    return _record(row) if row is not None else None


def records(
    session: Session,
    *,
    state: str,
    limit: int,
) -> tuple[OwnerControlRecord, ...]:
    rows = session.scalars(
        select(OwnerControlResultRow)
        .where(OwnerControlResultRow.state == state)
        .order_by(OwnerControlResultRow.created_at, OwnerControlResultRow.request_id)
        .limit(limit)
    ).all()
    return tuple(_record(row) for row in rows)


def claim(session: Session, *, request_id: str, now: str) -> bool:
    row = session.get(OwnerControlResultRow, request_id)
    if row is None or row.state != "received":
        return False
    row.state = "executing"
    row.updated_at = now
    session.flush()
    return True


def complete(
    session: Session,
    *,
    request_id: str,
    response_payload: bytes,
    response_revision: int,
    now: str,
) -> OwnerControlRecord:
    row = session.get(OwnerControlResultRow, request_id)
    if row is None:
        raise ValueError("owner control request does not exist")
    if row.state in _TERMINAL_STATES:
        if (
            row.state != "completed"
            or bytes(row.response_payload or b"") != response_payload
            or row.response_revision != response_revision
        ):
            raise IdempotencyConflict()
        return _record(row)
    if row.state != "executing":
        raise ValueError("owner control request must be executing before completion")
    row.state = "completed"
    row.response_payload = response_payload
    row.response_revision = response_revision
    row.transport_received = False
    row.updated_at = now
    row.completed_at = now
    session.flush()
    return _record(row)


def recover_executing(session: Session, *, now: str) -> int:
    rows = session.scalars(
        select(OwnerControlResultRow)
        .where(OwnerControlResultRow.state == "executing")
        .order_by(OwnerControlResultRow.created_at, OwnerControlResultRow.request_id)
    ).all()
    for row in rows:
        row.state = "effect_unknown"
        row.response_payload = _effect_unknown_payload(row, now)
        row.transport_received = False
        row.updated_at = now
        row.completed_at = now
    session.flush()
    return len(rows)


def mark_effect_unknown(
    session: Session,
    *,
    request_id: str,
    now: str,
) -> OwnerControlRecord:
    row = session.get(OwnerControlResultRow, request_id)
    if row is None:
        raise ValueError("owner control request does not exist")
    if row.state in _TERMINAL_STATES:
        return _record(row)
    if row.state != "executing":
        raise ValueError("owner control request was not executing")
    row.state = "effect_unknown"
    row.response_payload = _effect_unknown_payload(row, now)
    row.transport_received = False
    row.updated_at = now
    row.completed_at = now
    session.flush()
    return _record(row)


def mark_transport_received(
    session: Session,
    *,
    request_id: str,
    response_revision: int,
    now: str,
) -> bool:
    row = session.get(OwnerControlResultRow, request_id)
    if (
        row is None
        or row.state not in _TERMINAL_STATES
        or row.response_revision != response_revision
    ):
        return False
    row.transport_received = True
    row.updated_at = now
    session.flush()
    return True


def pending(
    session: Session,
    *,
    limit: int,
    after_created_at: str | None = None,
    after_request_id: str | None = None,
) -> tuple[OwnerControlRecord, ...]:
    statement = select(OwnerControlResultRow).where(
        OwnerControlResultRow.state.in_(_TERMINAL_STATES),
        OwnerControlResultRow.transport_received.is_(False),
        OwnerControlResultRow.response_payload.is_not(None),
    )
    if after_created_at is not None and after_request_id is not None:
        statement = statement.where(
            or_(
                OwnerControlResultRow.created_at > after_created_at,
                and_(
                    OwnerControlResultRow.created_at == after_created_at,
                    OwnerControlResultRow.request_id > after_request_id,
                ),
            )
        )
    rows = session.scalars(
        statement.order_by(
            OwnerControlResultRow.created_at, OwnerControlResultRow.request_id
        ).limit(limit)
    ).all()
    return tuple(_record(row) for row in rows)


def _effect_unknown_payload(row: OwnerControlResultRow, now: str) -> bytes:
    return json.dumps(
        {
            "completed_at": now,
            "control_transport_id": row.control_transport_id,
            "error": {"code": 4307, "reason": "effect_unknown"},
            "operation": row.operation,
            "request_id": row.request_id,
            "state": "unknown",
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _record(row: OwnerControlResultRow) -> OwnerControlRecord:
    return OwnerControlRecord(
        request_id=row.request_id,
        request_digest=row.request_digest,
        control_transport_id=row.control_transport_id,
        operation=row.operation,
        request_payload=bytes(row.request_payload),
        scope_payload=bytes(row.scope_payload),
        response_payload=(
            bytes(row.response_payload) if row.response_payload is not None else None
        ),
        state=row.state,
        response_revision=row.response_revision,
        transport_received=row.transport_received,
        created_at=row.created_at,
        updated_at=row.updated_at,
        completed_at=row.completed_at,
    )


__all__ = [
    "claim",
    "complete",
    "get",
    "mark_effect_unknown",
    "mark_transport_received",
    "pending",
    "put",
    "records",
    "recover_executing",
]
