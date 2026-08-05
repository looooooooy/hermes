from __future__ import annotations

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from hermes_connector.adapters.persistence.sqlite.models.control_command import (
    ControlCommandRow,
)
from hermes_connector.domain.storage import (
    CommandOutboxRecord,
    CommandPutResult,
    CommandRecord,
    IdempotencyConflict,
    StorageOverloaded,
)

_TERMINAL_STATES = frozenset({"succeeded", "failed", "unknown"})


def put(
    session: Session,
    *,
    command_id: str,
    message_id: str,
    digest: str,
    delivery_payload: bytes,
    receipt_payload: bytes,
    expires_at: str,
    revision: int,
    max_entries: int,
    now: str,
) -> CommandPutResult:
    row = session.get(ControlCommandRow, command_id)
    if row is not None:
        if row.message_id != message_id or row.digest != digest:
            raise IdempotencyConflict()
        return CommandPutResult(_record(row), inserted=False)
    occupied_message = session.scalar(
        select(ControlCommandRow).where(ControlCommandRow.message_id == message_id)
    )
    if occupied_message is not None:
        raise IdempotencyConflict()
    count = int(
        session.scalar(select(func.count()).select_from(ControlCommandRow)) or 0
    )
    if count >= max_entries:
        required = count - max_entries + 1
        removable = session.scalars(
            select(ControlCommandRow)
            .where(
                ControlCommandRow.state.in_(_TERMINAL_STATES),
                ControlCommandRow.receipt_acknowledged.is_(True),
                ControlCommandRow.result_acknowledged.is_(True),
            )
            .order_by(
                ControlCommandRow.completed_at,
                ControlCommandRow.command_id,
            )
            .limit(required)
        ).all()
        if len(removable) != required:
            raise StorageOverloaded()
        for candidate in removable:
            session.delete(candidate)
        session.flush()
    row = ControlCommandRow(
        command_id=command_id,
        message_id=message_id,
        digest=digest,
        state="delivered",
        delivery_payload=delivery_payload,
        receipt_payload=receipt_payload,
        result_payload=None,
        expires_at=expires_at,
        receipt_revision=revision,
        revision=revision,
        receipt_acknowledged=False,
        result_acknowledged=False,
        created_at=now,
        updated_at=now,
        completed_at=None,
    )
    session.add(row)
    session.flush()
    return CommandPutResult(_record(row), inserted=True)


def get(session: Session, command_id: str) -> CommandRecord | None:
    row = session.get(ControlCommandRow, command_id)
    return _record(row) if row is not None else None


def claim(session: Session, *, command_id: str, now: str) -> bool:
    row = session.get(ControlCommandRow, command_id)
    if row is None or row.state != "delivered":
        return False
    row.state = "executing"
    row.updated_at = now
    session.flush()
    return True


def complete(
    session: Session,
    *,
    command_id: str,
    state: str,
    result_payload: bytes,
    revision: int,
    now: str,
) -> CommandRecord:
    if state not in _TERMINAL_STATES:
        raise ValueError("command completion state must be terminal")
    row = session.get(ControlCommandRow, command_id)
    if row is None:
        raise ValueError("command does not exist")
    if row.state in _TERMINAL_STATES:
        if (
            row.state != state
            or row.result_payload != result_payload
            or row.revision != revision
        ):
            raise IdempotencyConflict()
        return _record(row)
    if row.state != "executing":
        raise ValueError("command must be executing before completion")
    row.state = state
    row.result_payload = result_payload
    row.revision = revision
    row.result_acknowledged = False
    row.updated_at = now
    row.completed_at = now
    session.flush()
    return _record(row)


def records(
    session: Session,
    *,
    state: str | None,
    limit: int,
) -> tuple[CommandRecord, ...]:
    statement = select(ControlCommandRow)
    if state is not None:
        statement = statement.where(ControlCommandRow.state == state)
    rows = session.scalars(
        statement.order_by(
            ControlCommandRow.created_at,
            ControlCommandRow.command_id,
        ).limit(limit)
    ).all()
    return tuple(_record(row) for row in rows)


def pending_messages(
    session: Session,
    *,
    limit: int,
    after_created_at: str | None = None,
    after_command_id: str | None = None,
    after_message_type: str | None = None,
) -> tuple[CommandOutboxRecord, ...]:
    statement = select(ControlCommandRow).where(
        (ControlCommandRow.receipt_acknowledged.is_(False))
        | (
            (ControlCommandRow.result_payload.is_not(None))
            & (ControlCommandRow.result_acknowledged.is_(False))
        )
    )
    if (
        after_created_at is not None
        and after_command_id is not None
        and after_message_type is not None
    ):
        after_row = or_(
            ControlCommandRow.created_at > after_created_at,
            and_(
                ControlCommandRow.created_at == after_created_at,
                ControlCommandRow.command_id > after_command_id,
            ),
        )
        if after_message_type == "command.receipt":
            after_row = or_(
                after_row,
                and_(
                    ControlCommandRow.created_at == after_created_at,
                    ControlCommandRow.command_id == after_command_id,
                    ControlCommandRow.result_payload.is_not(None),
                    ControlCommandRow.result_acknowledged.is_(False),
                ),
            )
        statement = statement.where(after_row)
    rows = session.scalars(
        statement.order_by(
            ControlCommandRow.created_at, ControlCommandRow.command_id
        ).limit(limit)
    ).all()
    messages: list[CommandOutboxRecord] = []
    for row in rows:
        if not row.receipt_acknowledged:
            messages.append(
                CommandOutboxRecord(
                    command_id=row.command_id,
                    message_type="command.receipt",
                    payload=bytes(row.receipt_payload),
                    revision=row.receipt_revision,
                    created_at=row.created_at,
                )
            )
        if row.result_payload is not None and not row.result_acknowledged:
            messages.append(
                CommandOutboxRecord(
                    command_id=row.command_id,
                    message_type="command.result",
                    payload=bytes(row.result_payload),
                    revision=row.revision,
                    created_at=row.created_at,
                )
            )
        if len(messages) >= limit:
            return tuple(messages[:limit])
    return tuple(messages)


def acknowledge(
    session: Session,
    *,
    command_id: str,
    message_type: str,
    now: str,
) -> bool:
    row = session.get(ControlCommandRow, command_id)
    if row is None:
        return False
    if message_type == "command.receipt":
        row.receipt_acknowledged = True
    elif message_type == "command.result":
        if row.result_payload is None:
            return False
        row.result_acknowledged = True
    else:
        raise ValueError("message_type is not a command acknowledgement target")
    row.updated_at = now
    session.flush()
    return True


def acknowledge_transport(
    session: Session,
    *,
    command_id: str,
    message_type: str,
    revision: int,
    now: str,
) -> bool:
    row = session.get(ControlCommandRow, command_id)
    if row is None:
        return False
    if message_type == "command.receipt":
        if row.receipt_revision != revision:
            return False
    elif message_type == "command.result":
        if row.result_payload is None or row.revision != revision:
            return False
    else:
        return False
    return acknowledge(
        session,
        command_id=command_id,
        message_type=message_type,
        now=now,
    )


def prune(
    session: Session,
    *,
    completed_before: str,
    limit: int,
) -> int:
    rows = session.scalars(
        select(ControlCommandRow)
        .where(
            ControlCommandRow.state.in_(_TERMINAL_STATES),
            ControlCommandRow.receipt_acknowledged.is_(True),
            ControlCommandRow.result_acknowledged.is_(True),
            ControlCommandRow.completed_at.is_not(None),
            ControlCommandRow.completed_at < completed_before,
        )
        .order_by(ControlCommandRow.completed_at, ControlCommandRow.command_id)
        .limit(limit)
    ).all()
    for row in rows:
        session.delete(row)
    session.flush()
    return len(rows)


def _record(row: ControlCommandRow) -> CommandRecord:
    return CommandRecord(
        command_id=row.command_id,
        message_id=row.message_id,
        digest=row.digest,
        state=row.state,
        delivery_payload=bytes(row.delivery_payload),
        receipt_payload=bytes(row.receipt_payload),
        result_payload=(
            bytes(row.result_payload) if row.result_payload is not None else None
        ),
        expires_at=row.expires_at,
        receipt_revision=row.receipt_revision,
        revision=row.revision,
        receipt_acknowledged=row.receipt_acknowledged,
        result_acknowledged=row.result_acknowledged,
        created_at=row.created_at,
        updated_at=row.updated_at,
        completed_at=row.completed_at,
    )
