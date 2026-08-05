from __future__ import annotations

from sqlalchemy.orm import Session

from hermes_connector.adapters.persistence.sqlite.models.cloud_session import (
    CloudSessionCheckpointRow,
)
from hermes_connector.domain.storage import (
    CloudSessionCheckpoint,
    StorageSequenceConflict,
)

_CHECKPOINT_ID = 1


def load(session: Session) -> CloudSessionCheckpoint:
    row = session.get(CloudSessionCheckpointRow, _CHECKPOINT_ID)
    if row is None:
        return CloudSessionCheckpoint(None, 0, 0, False, None, None, True, 0)
    return _record(row)


def advance_outbound(
    session: Session,
    *,
    expected_sequence: int,
    updated_at: str,
) -> int:
    row = _get_or_create(session, updated_at)
    if row.next_outbound_sequence != expected_sequence:
        raise StorageSequenceConflict()
    row.next_outbound_sequence += 1
    row.updated_at = updated_at
    session.flush()
    return row.next_outbound_sequence


def advance_inbound(
    session: Session,
    *,
    expected_sequence: int,
    updated_at: str,
) -> int:
    row = _get_or_create(session, updated_at)
    if row.next_inbound_sequence != expected_sequence:
        raise StorageSequenceConflict()
    row.next_inbound_sequence += 1
    row.updated_at = updated_at
    session.flush()
    return row.next_inbound_sequence


def begin_reconciliation(
    session: Session,
    *,
    previous_connection_id: str,
    next_outbound_sequence: int,
    next_inbound_sequence: int,
    updated_at: str,
) -> CloudSessionCheckpoint:
    row = _get_or_create(session, updated_at)
    row.previous_connection_id = previous_connection_id
    row.next_outbound_sequence = next_outbound_sequence
    row.next_inbound_sequence = next_inbound_sequence
    row.reconciliation_required = True
    row.updated_at = updated_at
    session.flush()
    return _record(row)


def complete_reconciliation(
    session: Session,
    *,
    previous_connection_id: str,
    updated_at: str,
) -> CloudSessionCheckpoint:
    row = _get_or_create(session, updated_at)
    row.previous_connection_id = previous_connection_id
    row.reconciliation_required = False
    row.updated_at = updated_at
    session.flush()
    return _record(row)


def _get_or_create(session: Session, updated_at: str) -> CloudSessionCheckpointRow:
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
            updated_at=updated_at,
        )
        session.add(row)
        session.flush()
    return row


def _record(row: CloudSessionCheckpointRow) -> CloudSessionCheckpoint:
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
