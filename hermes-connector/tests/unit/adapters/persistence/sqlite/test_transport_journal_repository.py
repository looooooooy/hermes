from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from hermes_connector.adapters.persistence.sqlite.models.transport_journal import (
    TransportFrameJournalRow,
)
from hermes_connector.adapters.sqlite_storage import SQLiteStorageComponent
from hermes_connector.bootstrap.config import ConnectorConfig
from hermes_connector.domain.observer import StreamAck
from hermes_connector.domain.storage import (
    IdempotencyConflict,
    StorageOverloaded,
    StorageSequenceConflict,
)

EPOCH_1 = "85000000-0000-4000-8000-000000000001"
EPOCH_2 = "85000000-0000-4000-8000-000000000002"
MESSAGE_1 = "86000000-0000-4000-8000-000000000001"
MESSAGE_2 = "86000000-0000-4000-8000-000000000002"


async def _start(
    path: Path,
    config: ConnectorConfig | None = None,
) -> tuple[SQLiteStorageComponent, asyncio.Task[None]]:
    storage = SQLiteStorageComponent(path, config or ConnectorConfig())
    await storage.start()
    runner = asyncio.create_task(storage.run())
    assert await storage.ready()
    return storage, runner


async def _stop(
    storage: SQLiteStorageComponent,
    runner: asyncio.Task[None],
) -> None:
    await storage.drain()
    await storage.stop()
    await runner


@pytest.mark.asyncio
async def test_epoch_transition_and_exact_frame_replay_are_durable(
    tmp_path: Path,
) -> None:
    path = tmp_path / "connector.sqlite3"
    storage, runner = await _start(path)
    initial = await storage.get_cloud_session()
    assert initial.transport_epoch_id is None
    assert initial.fresh_epoch_required is True

    checkpoint = await storage.begin_transport_epoch(
        epoch_id=EPOCH_1,
        runtime_generation="runtime-1",
        previous_connection_id="87000000-0000-4000-8000-000000000001",
        next_outbound_sequence=0,
        next_inbound_sequence=0,
    )
    assert checkpoint.transport_epoch_id == EPOCH_1
    assert checkpoint.fresh_epoch_required is False

    first = await storage.stage_transport_frame(
        epoch_id=EPOCH_1,
        sequence=0,
        message_id=MESSAGE_1,
        message_type="command.receipt",
        business_kind="command.receipt",
        business_key=MESSAGE_2,
        business_revision=1,
        runtime_generation="runtime-1",
        frame=b'{"message_id":"fixed","sequence":0}',
    )
    replay = await storage.stage_transport_frame(
        epoch_id=EPOCH_1,
        sequence=0,
        message_id=MESSAGE_1,
        message_type="command.receipt",
        business_kind="command.receipt",
        business_key=MESSAGE_2,
        business_revision=1,
        runtime_generation="runtime-1",
        frame=b'{"message_id":"fixed","sequence":0}',
    )
    assert replay == first

    with pytest.raises(IdempotencyConflict):
        await storage.stage_transport_frame(
            epoch_id=EPOCH_1,
            sequence=0,
            message_id=MESSAGE_2,
            message_type="connector.heartbeat",
            business_kind="heartbeat",
            business_key="heartbeat-0",
            business_revision=0,
            runtime_generation="runtime-1",
            frame=b"different",
        )

    sent = await storage.mark_transport_sent(epoch_id=EPOCH_1, sequence=0)
    assert sent.state == "sent"
    assert (await storage.get_cloud_session()).next_outbound_sequence == 1
    await _stop(storage, runner)

    reopened, reopened_runner = await _start(path)
    pending = await reopened.pending_transport_frames(epoch_id=EPOCH_1, limit=10)
    assert pending == (sent,)
    assert pending[0].frame == b'{"message_id":"fixed","sequence":0}'
    await _stop(reopened, reopened_runner)


@pytest.mark.asyncio
async def test_fresh_epoch_retires_old_transport_and_resets_checkpoint_atomically(
    tmp_path: Path,
) -> None:
    storage, runner = await _start(tmp_path / "connector.sqlite3")
    await storage.begin_transport_epoch(
        epoch_id=EPOCH_1,
        runtime_generation="runtime-1",
        previous_connection_id="87000000-0000-4000-8000-000000000001",
        next_outbound_sequence=4,
        next_inbound_sequence=6,
    )
    old = await storage.stage_transport_frame(
        epoch_id=EPOCH_1,
        sequence=4,
        message_id=MESSAGE_1,
        message_type="connector.heartbeat",
        business_kind="heartbeat",
        business_key="heartbeat-4",
        business_revision=4,
        runtime_generation="runtime-1",
        frame=b"old-frame",
    )
    assert old.state == "staged"

    fresh = await storage.begin_transport_epoch(
        epoch_id=EPOCH_2,
        runtime_generation="runtime-2",
        previous_connection_id="87000000-0000-4000-8000-000000000002",
        next_outbound_sequence=0,
        next_inbound_sequence=0,
    )

    assert fresh.transport_epoch_id == EPOCH_2
    assert fresh.next_outbound_sequence == 0
    assert fresh.next_inbound_sequence == 0
    assert await storage.pending_transport_frames(epoch_id=EPOCH_1, limit=10) == ()
    retired = await storage.transport_frame(MESSAGE_1)
    assert retired is not None
    assert retired.state == "retired"
    replacement = await storage.stage_transport_frame(
        epoch_id=EPOCH_2,
        sequence=0,
        message_id=MESSAGE_2,
        message_type="command.receipt",
        business_kind="command.receipt",
        business_key=MESSAGE_2,
        business_revision=1,
        runtime_generation="runtime-2",
        frame=b"new-frame",
    )
    assert replacement.sequence == 0
    await _stop(storage, runner)


@pytest.mark.asyncio
async def test_fresh_handshake_commit_is_durable_idempotent_and_not_a_journal_gap(
    tmp_path: Path,
) -> None:
    path = tmp_path / "connector.sqlite3"
    storage, runner = await _start(path)
    await storage.begin_transport_epoch(
        epoch_id=EPOCH_1,
        runtime_generation="runtime-1",
        previous_connection_id=None,
        next_outbound_sequence=0,
        next_inbound_sequence=0,
    )
    await _stop(storage, runner)

    reopened, reopened_runner = await _start(path)
    committed = await reopened.commit_transport_handshake(
        epoch_id=EPOCH_1,
        previous_connection_id="87000000-0000-4000-8000-000000000001",
        next_outbound_sequence=1,
        next_inbound_sequence=1,
    )
    repeated = await reopened.commit_transport_handshake(
        epoch_id=EPOCH_1,
        previous_connection_id="87000000-0000-4000-8000-000000000001",
        next_outbound_sequence=1,
        next_inbound_sequence=1,
    )

    assert repeated == committed
    assert committed.next_outbound_sequence == 1
    assert committed.next_inbound_sequence == 1
    assert await reopened.pending_transport_frames(epoch_id=EPOCH_1, limit=10) == ()
    with pytest.raises((IdempotencyConflict, StorageSequenceConflict)):
        await reopened.commit_transport_handshake(
            epoch_id=EPOCH_1,
            previous_connection_id="87000000-0000-4000-8000-000000000001",
            next_outbound_sequence=0,
            next_inbound_sequence=0,
        )
    await _stop(reopened, reopened_runner)


@pytest.mark.asyncio
async def test_fresh_handshake_preserve_binds_connection_without_advancing(
    tmp_path: Path,
) -> None:
    storage, runner = await _start(tmp_path / "connector.sqlite3")
    await storage.begin_transport_epoch(
        epoch_id=EPOCH_1,
        runtime_generation="runtime-1",
        previous_connection_id=None,
        next_outbound_sequence=0,
        next_inbound_sequence=0,
    )

    committed = await storage.commit_transport_handshake(
        epoch_id=EPOCH_1,
        previous_connection_id="87000000-0000-4000-8000-000000000001",
        next_outbound_sequence=0,
        next_inbound_sequence=0,
    )

    assert committed.previous_connection_id == ("87000000-0000-4000-8000-000000000001")
    assert committed.next_outbound_sequence == 0
    assert committed.next_inbound_sequence == 0
    await _stop(storage, runner)


@pytest.mark.asyncio
async def test_resumed_handshake_strictly_advances_both_cursors_without_journal_row(
    tmp_path: Path,
) -> None:
    storage, runner = await _start(tmp_path / "connector.sqlite3")
    old_connection = "87000000-0000-4000-8000-000000000001"
    new_connection = "87000000-0000-4000-8000-000000000002"
    await storage.begin_transport_epoch(
        epoch_id=EPOCH_1,
        runtime_generation="runtime-1",
        previous_connection_id=old_connection,
        next_outbound_sequence=7,
        next_inbound_sequence=11,
    )

    committed = await storage.commit_transport_handshake(
        epoch_id=EPOCH_1,
        previous_connection_id=new_connection,
        next_outbound_sequence=8,
        next_inbound_sequence=12,
    )

    assert committed.previous_connection_id == new_connection
    assert committed.next_outbound_sequence == 8
    assert committed.next_inbound_sequence == 12
    assert committed.transport_recovery_floor == 8
    assert await storage.pending_transport_frames(epoch_id=EPOCH_1, limit=10) == ()
    await _stop(storage, runner)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("connection_id", "next_outbound", "next_inbound"),
    (
        ("87000000-0000-4000-8000-000000000002", 9, 13),
        ("87000000-0000-4000-8000-000000000002", 8, 11),
        ("87000000-0000-4000-8000-000000000001", 8, 12),
    ),
)
async def test_resumed_handshake_rejects_non_cas_or_reused_connection(
    tmp_path: Path,
    connection_id: str,
    next_outbound: int,
    next_inbound: int,
) -> None:
    storage, runner = await _start(tmp_path / "connector.sqlite3")
    await storage.begin_transport_epoch(
        epoch_id=EPOCH_1,
        runtime_generation="runtime-1",
        previous_connection_id="87000000-0000-4000-8000-000000000001",
        next_outbound_sequence=7,
        next_inbound_sequence=11,
    )

    with pytest.raises((IdempotencyConflict, StorageSequenceConflict)):
        await storage.commit_transport_handshake(
            epoch_id=EPOCH_1,
            previous_connection_id=connection_id,
            next_outbound_sequence=next_outbound,
            next_inbound_sequence=next_inbound,
        )

    checkpoint = await storage.get_cloud_session()
    assert checkpoint.next_outbound_sequence == 7
    assert checkpoint.next_inbound_sequence == 11
    await _stop(storage, runner)


@pytest.mark.asyncio
async def test_journal_never_evicts_active_attempts_to_make_capacity(
    tmp_path: Path,
) -> None:
    storage, runner = await _start(
        tmp_path / "connector.sqlite3",
        ConnectorConfig(transport_journal_entries=1),
    )
    await storage.begin_transport_epoch(
        epoch_id=EPOCH_1,
        runtime_generation="runtime-1",
        previous_connection_id="87000000-0000-4000-8000-000000000001",
        next_outbound_sequence=0,
        next_inbound_sequence=0,
    )
    await storage.stage_transport_frame(
        epoch_id=EPOCH_1,
        sequence=0,
        message_id=MESSAGE_1,
        message_type="connector.heartbeat",
        business_kind="heartbeat",
        business_key="heartbeat-0",
        business_revision=0,
        runtime_generation="runtime-1",
        frame=b"first",
    )
    await storage.mark_transport_sent(epoch_id=EPOCH_1, sequence=0)
    with pytest.raises(StorageOverloaded):
        await storage.stage_transport_frame(
            epoch_id=EPOCH_1,
            sequence=1,
            message_id=MESSAGE_2,
            message_type="connector.heartbeat",
            business_kind="heartbeat",
            business_key="heartbeat-1",
            business_revision=1,
            runtime_generation="runtime-1",
            frame=b"second",
        )
    retained = await storage.transport_frame(MESSAGE_1)
    assert retained is not None
    assert retained.state == "sent"
    await _stop(storage, runner)


@pytest.mark.asyncio
async def test_authoritative_recovery_floor_allows_current_epoch_settled_eviction(
    tmp_path: Path,
) -> None:
    path = tmp_path / "connector.sqlite3"
    storage, runner = await _start(
        path,
        ConnectorConfig(transport_journal_entries=1),
    )
    await storage.begin_transport_epoch(
        epoch_id=EPOCH_1,
        runtime_generation="runtime-1",
        previous_connection_id=None,
        next_outbound_sequence=0,
        next_inbound_sequence=0,
    )
    await storage.stage_transport_frame(
        epoch_id=EPOCH_1,
        sequence=0,
        message_id=MESSAGE_1,
        message_type="connector.heartbeat",
        business_kind="heartbeat",
        business_key="heartbeat-0",
        business_revision=0,
        runtime_generation="runtime-1",
        frame=b"frame-0",
    )
    await storage.mark_transport_sent(epoch_id=EPOCH_1, sequence=0)
    await storage.settle_transport_cursor(epoch_id=EPOCH_1, next_sequence=1)
    checkpoint = await storage.get_cloud_session()
    assert checkpoint.transport_recovery_floor == 1

    replacement = await storage.stage_transport_frame(
        epoch_id=EPOCH_1,
        sequence=1,
        message_id=MESSAGE_2,
        message_type="connector.heartbeat",
        business_kind="heartbeat",
        business_key="heartbeat-1",
        business_revision=1,
        runtime_generation="runtime-1",
        frame=b"frame-1",
    )

    assert replacement.sequence == 1
    assert await storage.transport_frame(MESSAGE_1) is None
    await _stop(storage, runner)

    reopened, reopened_runner = await _start(path)
    assert (await reopened.get_cloud_session()).transport_recovery_floor == 1
    await _stop(reopened, reopened_runner)


@pytest.mark.asyncio
async def test_unsettled_current_epoch_attempt_is_never_evicted_at_capacity(
    tmp_path: Path,
) -> None:
    storage, runner = await _start(
        tmp_path / "connector.sqlite3",
        ConnectorConfig(transport_journal_entries=1),
    )
    await storage.begin_transport_epoch(
        epoch_id=EPOCH_1,
        runtime_generation="runtime-1",
        previous_connection_id=None,
        next_outbound_sequence=0,
        next_inbound_sequence=0,
    )
    await storage.stage_transport_frame(
        epoch_id=EPOCH_1,
        sequence=0,
        message_id=MESSAGE_1,
        message_type="connector.heartbeat",
        business_kind="heartbeat",
        business_key="heartbeat-0",
        business_revision=0,
        runtime_generation="runtime-1",
        frame=b"frame-0",
    )
    await storage.mark_transport_sent(epoch_id=EPOCH_1, sequence=0)

    with pytest.raises(StorageOverloaded):
        await storage.stage_transport_frame(
            epoch_id=EPOCH_1,
            sequence=1,
            message_id=MESSAGE_2,
            message_type="connector.heartbeat",
            business_kind="heartbeat",
            business_key="heartbeat-1",
            business_revision=1,
            runtime_generation="runtime-1",
            frame=b"frame-1",
        )
    await _stop(storage, runner)


@pytest.mark.asyncio
async def test_same_epoch_rewind_accepts_floor_and_rejects_below_floor(
    tmp_path: Path,
) -> None:
    storage, runner = await _start(tmp_path / "connector.sqlite3")
    await storage.begin_transport_epoch(
        epoch_id=EPOCH_1,
        runtime_generation="runtime-1",
        previous_connection_id=None,
        next_outbound_sequence=1,
        next_inbound_sequence=0,
    )
    assert (await storage.get_cloud_session()).transport_recovery_floor == 1
    await storage.stage_transport_frame(
        epoch_id=EPOCH_1,
        sequence=1,
        message_id=MESSAGE_1,
        message_type="connector.heartbeat",
        business_kind="heartbeat",
        business_key="heartbeat-1",
        business_revision=1,
        runtime_generation="runtime-1",
        frame=b"frame-1",
    )
    await storage.mark_transport_sent(epoch_id=EPOCH_1, sequence=1)

    at_floor = await storage.reconcile_transport_epoch(
        epoch_id=EPOCH_1,
        previous_connection_id="87000000-0000-4000-8000-000000000001",
        next_outbound_sequence=1,
        next_inbound_sequence=0,
    )
    assert at_floor.next_outbound_sequence == 1
    with pytest.raises(StorageSequenceConflict):
        await storage.reconcile_transport_epoch(
            epoch_id=EPOCH_1,
            previous_connection_id="87000000-0000-4000-8000-000000000001",
            next_outbound_sequence=0,
            next_inbound_sequence=0,
        )
    await _stop(storage, runner)


@pytest.mark.asyncio
async def test_authoritative_heartbeat_settlement_sustains_more_than_2048_frames(
    tmp_path: Path,
) -> None:
    storage, runner = await _start(
        tmp_path / "connector.sqlite3",
        ConnectorConfig(transport_journal_entries=32),
    )
    await storage.begin_transport_epoch(
        epoch_id=EPOCH_1,
        runtime_generation="runtime-1",
        previous_connection_id=None,
        next_outbound_sequence=0,
        next_inbound_sequence=0,
    )
    for sequence in range(2_050):
        await storage.stage_transport_frame(
            epoch_id=EPOCH_1,
            sequence=sequence,
            message_id=f"86000000-0000-4000-8000-{sequence + 100:012d}",
            message_type="connector.heartbeat",
            business_kind="heartbeat",
            business_key=f"heartbeat-{sequence}",
            business_revision=sequence,
            runtime_generation="runtime-1",
            frame=f"frame-{sequence}".encode(),
        )
        await storage.mark_transport_sent(epoch_id=EPOCH_1, sequence=sequence)
        await storage.settle_transport_cursor(
            epoch_id=EPOCH_1,
            next_sequence=sequence + 1,
        )

    checkpoint = await storage.get_cloud_session()
    assert checkpoint.next_outbound_sequence == 2_050
    assert checkpoint.transport_recovery_floor == 2_050
    await _stop(storage, runner)


@pytest.mark.asyncio
async def test_recovery_floor_does_not_advance_across_missing_journal_sequence(
    tmp_path: Path,
) -> None:
    path = tmp_path / "connector.sqlite3"
    storage, runner = await _start(path)
    await storage.begin_transport_epoch(
        epoch_id=EPOCH_1,
        runtime_generation="runtime-1",
        previous_connection_id=None,
        next_outbound_sequence=0,
        next_inbound_sequence=0,
    )
    for sequence, message_id in enumerate((MESSAGE_1, MESSAGE_2)):
        await storage.stage_transport_frame(
            epoch_id=EPOCH_1,
            sequence=sequence,
            message_id=message_id,
            message_type="connector.heartbeat",
            business_kind="heartbeat",
            business_key=f"heartbeat-{sequence}",
            business_revision=sequence,
            runtime_generation="runtime-1",
            frame=f"frame-{sequence}".encode(),
        )
        await storage.mark_transport_sent(epoch_id=EPOCH_1, sequence=sequence)
    await _stop(storage, runner)

    engine = create_engine(f"sqlite+pysqlite:///{path}")
    try:
        with Session(engine) as session, session.begin():
            row = session.get(TransportFrameJournalRow, MESSAGE_1)
            assert row is not None
            session.delete(row)
    finally:
        engine.dispose()

    reopened, reopened_runner = await _start(path)
    with pytest.raises(StorageSequenceConflict):
        await reopened.settle_transport_cursor(epoch_id=EPOCH_1, next_sequence=2)
    assert (await reopened.get_cloud_session()).transport_recovery_floor == 0
    assert (await reopened.transport_frame(MESSAGE_2)).state == "sent"
    await _stop(reopened, reopened_runner)


@pytest.mark.asyncio
async def test_business_ack_without_cloud_floor_keeps_settled_rewind_frame(
    tmp_path: Path,
) -> None:
    storage, runner = await _start(
        tmp_path / "connector.sqlite3",
        ConnectorConfig(transport_journal_entries=1),
    )
    await storage.begin_transport_epoch(
        epoch_id=EPOCH_1,
        runtime_generation="runtime-1",
        previous_connection_id=None,
        next_outbound_sequence=0,
        next_inbound_sequence=0,
    )
    observer = await storage.append_observer_outbox(
        message_id=MESSAGE_1,
        connector_sequence=0,
        transport_epoch_id=EPOCH_1,
        message_type="session.event",
        profile="default",
        session_key="session-1",
        runtime_generation="runtime-1",
        runtime_session_id="runtime-session-1",
        event_sequence=4,
        payload=b"observer",
        frame=b"observer-frame",
    )
    await storage.mark_transport_sent(epoch_id=EPOCH_1, sequence=0)
    await storage.ack_observer_outbox(
        StreamAck(
            observer_message_id=UUID(observer.message_id),
            payload_digest=observer.payload_digest,
            connector_sequence=observer.connector_sequence,
            observer_message_type=observer.message_type,
            profile=observer.profile,
            session_key=observer.session_key,
            runtime_generation=observer.runtime_generation,
            runtime_session_id=observer.runtime_session_id,
            event_sequence=observer.event_sequence,
            committed_at=datetime(2026, 8, 1, tzinfo=UTC),
        )
    )
    assert (await storage.get_cloud_session()).transport_recovery_floor == 0

    with pytest.raises(StorageOverloaded):
        await storage.stage_transport_frame(
            epoch_id=EPOCH_1,
            sequence=1,
            message_id=MESSAGE_2,
            message_type="connector.heartbeat",
            business_kind="heartbeat",
            business_key="heartbeat-1",
            business_revision=1,
            runtime_generation="runtime-1",
            frame=b"next",
        )

    assert (await storage.transport_frame(MESSAGE_1)).state == "settled"
    await _stop(storage, runner)


@pytest.mark.asyncio
async def test_cloud_floor_continuity_accepts_already_settled_observer_frame(
    tmp_path: Path,
) -> None:
    storage, runner = await _start(tmp_path / "connector.sqlite3")
    await storage.begin_transport_epoch(
        epoch_id=EPOCH_1,
        runtime_generation="runtime-1",
        previous_connection_id=None,
        next_outbound_sequence=0,
        next_inbound_sequence=0,
    )
    observer = await storage.append_observer_outbox(
        message_id=MESSAGE_1,
        connector_sequence=0,
        transport_epoch_id=EPOCH_1,
        message_type="session.event",
        profile="default",
        session_key="session-1",
        runtime_generation="runtime-1",
        runtime_session_id="runtime-session-1",
        event_sequence=4,
        payload=b"observer",
        frame=b"observer-frame",
    )
    await storage.mark_transport_sent(epoch_id=EPOCH_1, sequence=0)
    await storage.ack_observer_outbox(
        StreamAck(
            observer_message_id=UUID(observer.message_id),
            payload_digest=observer.payload_digest,
            connector_sequence=observer.connector_sequence,
            observer_message_type=observer.message_type,
            profile=observer.profile,
            session_key=observer.session_key,
            runtime_generation=observer.runtime_generation,
            runtime_session_id=observer.runtime_session_id,
            event_sequence=observer.event_sequence,
            committed_at=datetime(2026, 8, 1, tzinfo=UTC),
        )
    )
    await storage.stage_transport_frame(
        epoch_id=EPOCH_1,
        sequence=1,
        message_id=MESSAGE_2,
        message_type="connector.heartbeat",
        business_kind="heartbeat",
        business_key="heartbeat-1",
        business_revision=1,
        runtime_generation="runtime-1",
        frame=b"next",
    )
    await storage.mark_transport_sent(epoch_id=EPOCH_1, sequence=1)

    await storage.settle_transport_cursor(epoch_id=EPOCH_1, next_sequence=2)

    assert (await storage.get_cloud_session()).transport_recovery_floor == 2
    assert (await storage.get_observer_outbox(MESSAGE_1)).state == "acked"
    await _stop(storage, runner)


@pytest.mark.asyncio
async def test_owner_executing_recovers_as_effect_unknown_without_reclaim(
    tmp_path: Path,
) -> None:
    path = tmp_path / "connector.sqlite3"
    storage, runner = await _start(path)
    inserted = await storage.put_owner_control(
        request_id="88000000-0000-4000-8000-000000000001",
        request_digest="a" * 64,
        control_transport_id="89000000-0000-4000-8000-000000000001",
        operation="session.control.status",
        request_payload=b'{"operation":"session.control.status"}',
        scope_payload=b'{"profile":"default","session_key":"session-1"}',
    )
    assert inserted.inserted is True
    assert await storage.claim_owner_control(inserted.record.request_id) is True
    await _stop(storage, runner)

    reopened, reopened_runner = await _start(path)
    recovered = await reopened.get_owner_control(inserted.record.request_id)
    assert recovered is not None
    assert recovered.state == "effect_unknown"
    assert recovered.response_payload is not None
    assert await reopened.claim_owner_control(inserted.record.request_id) is False
    await _stop(reopened, reopened_runner)


@pytest.mark.asyncio
async def test_live_owner_cancellation_durably_completes_effect_unknown(
    tmp_path: Path,
) -> None:
    storage, runner = await _start(tmp_path / "connector.sqlite3")
    request_id = "88000000-0000-4000-8000-000000000021"
    await storage.put_owner_control(
        request_id=request_id,
        request_digest="c" * 64,
        control_transport_id="89000000-0000-4000-8000-000000000021",
        operation="session.control.status",
        request_payload=b"{}",
        scope_payload=b"{}",
    )
    assert await storage.claim_owner_control(request_id) is True

    unknown = await storage.mark_owner_control_effect_unknown(request_id)

    assert unknown.state == "effect_unknown"
    assert unknown.response_payload is not None
    assert b"effect_unknown" in unknown.response_payload
    assert await storage.mark_owner_control_effect_unknown(request_id) == unknown
    assert await storage.claim_owner_control(request_id) is False
    await _stop(storage, runner)


@pytest.mark.asyncio
async def test_owner_received_records_are_bounded_for_post_cursor_recovery(
    tmp_path: Path,
) -> None:
    storage, runner = await _start(tmp_path / "connector.sqlite3")
    request_ids = (
        "88000000-0000-4000-8000-000000000031",
        "88000000-0000-4000-8000-000000000032",
    )
    for request_id in request_ids:
        await storage.put_owner_control(
            request_id=request_id,
            request_digest="d" * 64,
            control_transport_id="89000000-0000-4000-8000-000000000031",
            operation="session.control.status",
            request_payload=b"{}",
            scope_payload=b"{}",
        )

    records = await storage.owner_control_records(state="received", limit=1)

    assert tuple(record.request_id for record in records) == request_ids[:1]
    await _stop(storage, runner)


@pytest.mark.asyncio
async def test_owner_put_and_inbound_advance_commit_or_rollback_in_one_transaction(
    tmp_path: Path,
) -> None:
    storage, runner = await _start(tmp_path / "connector.sqlite3")
    await storage.begin_transport_epoch(
        epoch_id=EPOCH_1,
        runtime_generation="runtime-1",
        previous_connection_id=None,
        next_outbound_sequence=0,
        next_inbound_sequence=0,
    )
    committed_id = "88000000-0000-4000-8000-000000000041"
    committed = await storage.put_owner_control_and_advance_inbound(
        expected_sequence=0,
        request_id=committed_id,
        request_digest="e" * 64,
        control_transport_id="89000000-0000-4000-8000-000000000041",
        operation="session.control.status",
        request_payload=b"{}",
        scope_payload=b"{}",
    )

    assert committed.inserted is True
    assert (await storage.get_cloud_session()).next_inbound_sequence == 1
    rollback_id = "88000000-0000-4000-8000-000000000042"
    with pytest.raises(StorageSequenceConflict):
        await storage.put_owner_control_and_advance_inbound(
            expected_sequence=0,
            request_id=rollback_id,
            request_digest="f" * 64,
            control_transport_id="89000000-0000-4000-8000-000000000042",
            operation="session.control.status",
            request_payload=b"{}",
            scope_payload=b"{}",
        )

    assert await storage.get_owner_control(rollback_id) is None
    assert (await storage.get_cloud_session()).next_inbound_sequence == 1
    await _stop(storage, runner)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_epoch",
    [
        1,
        True,
        "85000000000040008000000000000001",
        "abcdefab-cdef-4abc-8abc-abcdefabcdef".upper(),
        "00000000-0000-0000-0000-000000000000",
    ],
)
async def test_transport_epoch_rejects_noncanonical_or_nonstring_uuid(
    tmp_path: Path,
    invalid_epoch: object,
) -> None:
    storage, runner = await _start(tmp_path / "connector.sqlite3")
    with pytest.raises((TypeError, ValueError)):
        await storage.begin_transport_epoch(
            epoch_id=invalid_epoch,  # type: ignore[arg-type]
            runtime_generation="runtime-1",
            previous_connection_id=None,
            next_outbound_sequence=0,
            next_inbound_sequence=0,
        )
    await _stop(storage, runner)


@pytest.mark.asyncio
async def test_v6_storage_rejects_invalid_ids_enums_digest_and_text_bounds(
    tmp_path: Path,
) -> None:
    storage, runner = await _start(tmp_path / "connector.sqlite3")
    await storage.begin_transport_epoch(
        epoch_id=EPOCH_1,
        runtime_generation="runtime-1",
        previous_connection_id=None,
        next_outbound_sequence=0,
        next_inbound_sequence=0,
    )
    base = {
        "epoch_id": EPOCH_1,
        "sequence": 0,
        "message_id": MESSAGE_1,
        "message_type": "connector.heartbeat",
        "business_kind": "heartbeat",
        "business_key": "heartbeat-0",
        "business_revision": 0,
        "runtime_generation": "runtime-1",
        "frame": b"frame",
    }
    for change in (
        {"message_id": "not-a-uuid"},
        {"message_type": "unknown.message"},
        {"business_kind": "unknown"},
        {"business_key": ""},
        {"runtime_generation": "x" * 129},
    ):
        with pytest.raises((TypeError, ValueError)):
            await storage.stage_transport_frame(**(base | change))
    with pytest.raises(ValueError):
        await storage.put_owner_control(
            request_id="88000000-0000-4000-8000-000000000031",
            request_digest="G" * 64,
            control_transport_id="89000000-0000-4000-8000-000000000031",
            operation="session.control.status",
            request_payload=b"{}",
            scope_payload=b"{}",
        )
    with pytest.raises(ValueError):
        await storage.put_owner_control(
            request_id="88000000-0000-4000-8000-000000000031",
            request_digest="d" * 64,
            control_transport_id="89000000-0000-4000-8000-000000000031",
            operation="session.cancel",
            request_payload=b"{}",
            scope_payload=b"{}",
        )
    await _stop(storage, runner)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message_type", "business_kind", "business_key", "business_revision"),
    (
        ("session.event", "command.result", MESSAGE_2, 1),
        ("command.receipt", "command.result", MESSAGE_2, 1),
        ("session.snapshot", "observer", "not-a-uuid", 1),
        ("control.response", "control.response", "not-a-uuid", 1),
        ("connector.heartbeat", "heartbeat", "heartbeat-wrong", 0),
    ),
)
async def test_transport_business_identity_rejects_corrupt_pair_or_key(
    tmp_path: Path,
    message_type: str,
    business_kind: str,
    business_key: str,
    business_revision: int,
) -> None:
    storage, runner = await _start(tmp_path / "connector.sqlite3")
    await storage.begin_transport_epoch(
        epoch_id=EPOCH_1,
        runtime_generation="runtime-1",
        previous_connection_id=None,
        next_outbound_sequence=0,
        next_inbound_sequence=0,
    )

    with pytest.raises(ValueError):
        await storage.stage_transport_frame(
            epoch_id=EPOCH_1,
            sequence=0,
            message_id=MESSAGE_1,
            message_type=message_type,
            business_kind=business_kind,
            business_key=business_key,
            business_revision=business_revision,
            runtime_generation="runtime-1",
            frame=b"corrupt",
        )
    await _stop(storage, runner)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message_type", "business_kind", "business_key", "business_revision"),
    (
        ("command.receipt", "command.receipt", MESSAGE_2, 1),
        ("control.response", "control.response", MESSAGE_2, 1),
        ("session.event", "observer", MESSAGE_2, 1),
    ),
)
async def test_transport_settlement_missing_business_target_rolls_back_cursor(
    tmp_path: Path,
    message_type: str,
    business_kind: str,
    business_key: str,
    business_revision: int,
) -> None:
    storage, runner = await _start(tmp_path / "connector.sqlite3")
    await storage.begin_transport_epoch(
        epoch_id=EPOCH_1,
        runtime_generation="runtime-1",
        previous_connection_id=None,
        next_outbound_sequence=0,
        next_inbound_sequence=0,
    )
    await storage.stage_transport_frame(
        epoch_id=EPOCH_1,
        sequence=0,
        message_id=MESSAGE_1,
        message_type=message_type,
        business_kind=business_kind,
        business_key=business_key,
        business_revision=business_revision,
        runtime_generation="runtime-1",
        frame=b"missing-target",
    )
    await storage.mark_transport_sent(epoch_id=EPOCH_1, sequence=0)

    with pytest.raises(StorageSequenceConflict):
        await storage.settle_transport_cursor(epoch_id=EPOCH_1, next_sequence=1)

    checkpoint = await storage.get_cloud_session()
    assert checkpoint.transport_recovery_floor == 0
    assert (await storage.transport_frame(MESSAGE_1)).state == "sent"
    await _stop(storage, runner)


@pytest.mark.asyncio
async def test_command_transport_settlement_revision_mismatch_rolls_back_cursor(
    tmp_path: Path,
) -> None:
    storage, runner = await _start(tmp_path / "connector.sqlite3")
    await storage.begin_transport_epoch(
        epoch_id=EPOCH_1,
        runtime_generation="runtime-1",
        previous_connection_id=None,
        next_outbound_sequence=0,
        next_inbound_sequence=0,
    )
    await storage.put_command(
        command_id=MESSAGE_2,
        message_id="8a000000-0000-4000-8000-000000000099",
        digest="sha256:" + ("b" * 64),
        delivery_payload=b"{}",
        receipt_payload=b"{}",
        expires_at="2026-08-01T12:00:00Z",
        revision=1,
    )
    await storage.stage_transport_frame(
        epoch_id=EPOCH_1,
        sequence=0,
        message_id=MESSAGE_1,
        message_type="command.receipt",
        business_kind="command.receipt",
        business_key=MESSAGE_2,
        business_revision=2,
        runtime_generation="runtime-1",
        frame=b"wrong-revision",
    )
    await storage.mark_transport_sent(epoch_id=EPOCH_1, sequence=0)

    with pytest.raises(StorageSequenceConflict):
        await storage.settle_transport_cursor(epoch_id=EPOCH_1, next_sequence=1)

    assert (await storage.get_cloud_session()).transport_recovery_floor == 0
    assert (await storage.transport_frame(MESSAGE_1)).state == "sent"
    await _stop(storage, runner)


@pytest.mark.asyncio
async def test_cloud_cursor_settles_transport_without_claiming_observer_business_ack(
    tmp_path: Path,
) -> None:
    storage, runner = await _start(tmp_path / "connector.sqlite3")
    await storage.begin_transport_epoch(
        epoch_id=EPOCH_1,
        runtime_generation="runtime-1",
        previous_connection_id="87000000-0000-4000-8000-000000000001",
        next_outbound_sequence=0,
        next_inbound_sequence=0,
    )
    await storage.put_command(
        command_id=MESSAGE_2,
        message_id="8a000000-0000-4000-8000-000000000001",
        digest="sha256:" + ("b" * 64),
        delivery_payload=b"{}",
        receipt_payload=b'{"command_id":"command-1"}',
        expires_at="2026-08-01T12:00:00Z",
        revision=1,
    )
    command_frame = await storage.stage_transport_frame(
        epoch_id=EPOCH_1,
        sequence=0,
        message_id=MESSAGE_1,
        message_type="command.receipt",
        business_kind="command.receipt",
        business_key=MESSAGE_2,
        business_revision=1,
        runtime_generation="runtime-1",
        frame=b"command-frame",
    )
    await storage.mark_transport_sent(epoch_id=EPOCH_1, sequence=0)
    settled = await storage.settle_transport_cursor(epoch_id=EPOCH_1, next_sequence=1)
    assert settled[0].message_id == command_frame.message_id
    command = await storage.get_command(MESSAGE_2)
    assert command is not None
    assert command.state == "delivered"
    assert command.receipt_acknowledged is True

    await storage.append_observer_outbox(
        message_id="8b000000-0000-4000-8000-000000000001",
        connector_sequence=1,
        transport_epoch_id=EPOCH_1,
        message_type="session.event",
        profile="default",
        session_key="session-1",
        runtime_generation="runtime-1",
        runtime_session_id="runtime-session-1",
        event_sequence=1,
        payload=b"{}",
        frame=b"observer-frame",
    )
    await storage.stage_transport_frame(
        epoch_id=EPOCH_1,
        sequence=1,
        message_id="8b000000-0000-4000-8000-000000000001",
        message_type="session.event",
        business_kind="observer",
        business_key="8b000000-0000-4000-8000-000000000001",
        business_revision=1,
        runtime_generation="runtime-1",
        frame=b"observer-frame",
    )
    await storage.mark_transport_sent(epoch_id=EPOCH_1, sequence=1)
    await storage.settle_transport_cursor(epoch_id=EPOCH_1, next_sequence=2)
    observer = await storage.get_observer_outbox("8b000000-0000-4000-8000-000000000001")
    assert observer is not None
    assert observer.state == "pending"
    await _stop(storage, runner)


@pytest.mark.asyncio
async def test_same_epoch_reconcile_settles_post_send_precommit_or_replays_exact(
    tmp_path: Path,
) -> None:
    storage, runner = await _start(tmp_path / "connector.sqlite3")
    await storage.begin_transport_epoch(
        epoch_id=EPOCH_1,
        runtime_generation="runtime-1",
        previous_connection_id=None,
        next_outbound_sequence=0,
        next_inbound_sequence=0,
    )
    staged = await storage.stage_transport_frame(
        epoch_id=EPOCH_1,
        sequence=0,
        message_id=MESSAGE_1,
        message_type="connector.heartbeat",
        business_kind="heartbeat",
        business_key="heartbeat-0",
        business_revision=0,
        runtime_generation="runtime-1",
        frame=b"exact-zero",
    )

    reconciled = await storage.reconcile_transport_epoch(
        epoch_id=EPOCH_1,
        previous_connection_id="87000000-0000-4000-8000-000000000001",
        next_outbound_sequence=1,
        next_inbound_sequence=2,
    )
    assert reconciled.next_outbound_sequence == 1
    assert (await storage.transport_frame(staged.message_id)).state == "settled"

    one = await storage.stage_transport_frame(
        epoch_id=EPOCH_1,
        sequence=1,
        message_id=MESSAGE_2,
        message_type="connector.heartbeat",
        business_kind="heartbeat",
        business_key="heartbeat-1",
        business_revision=1,
        runtime_generation="runtime-1",
        frame=b"exact-one",
    )
    await storage.mark_transport_sent(epoch_id=EPOCH_1, sequence=1)
    rewound = await storage.reconcile_transport_epoch(
        epoch_id=EPOCH_1,
        previous_connection_id="87000000-0000-4000-8000-000000000001",
        next_outbound_sequence=1,
        next_inbound_sequence=2,
    )
    assert rewound.next_outbound_sequence == 1
    replay = await storage.pending_transport_frames(epoch_id=EPOCH_1, limit=10)
    assert len(replay) == 1
    assert replay[0].message_id == one.message_id
    assert replay[0].state == "staged"
    assert replay[0].frame == b"exact-one"
    await _stop(storage, runner)


@pytest.mark.asyncio
async def test_observer_attempt_and_transport_journal_stage_atomically(
    tmp_path: Path,
) -> None:
    storage, runner = await _start(
        tmp_path / "connector.sqlite3",
        ConnectorConfig(transport_journal_entries=1),
    )
    await storage.begin_transport_epoch(
        epoch_id=EPOCH_1,
        runtime_generation="runtime-1",
        previous_connection_id=None,
        next_outbound_sequence=0,
        next_inbound_sequence=0,
    )
    await storage.stage_transport_frame(
        epoch_id=EPOCH_1,
        sequence=0,
        message_id=MESSAGE_1,
        message_type="connector.heartbeat",
        business_kind="heartbeat",
        business_key="heartbeat-0",
        business_revision=0,
        runtime_generation="runtime-1",
        frame=b"active",
    )
    await storage.mark_transport_sent(epoch_id=EPOCH_1, sequence=0)

    observer_id = "8b000000-0000-4000-8000-000000000002"
    with pytest.raises(StorageOverloaded):
        await storage.append_observer_outbox(
            message_id=observer_id,
            connector_sequence=1,
            transport_epoch_id=EPOCH_1,
            message_type="session.event",
            profile="default",
            session_key="session-1",
            runtime_generation="runtime-1",
            runtime_session_id="runtime-session-1",
            event_sequence=1,
            payload=b"{}",
            frame=b"observer-frame",
        )
    assert await storage.get_observer_outbox(observer_id) is None
    assert await storage.transport_frame(observer_id) is None
    await _stop(storage, runner)


@pytest.mark.asyncio
async def test_observer_sequence_and_fact_lookup_are_epoch_scoped(
    tmp_path: Path,
) -> None:
    storage, runner = await _start(tmp_path / "connector.sqlite3")
    await storage.begin_transport_epoch(
        epoch_id=EPOCH_1,
        runtime_generation="runtime-1",
        previous_connection_id=None,
        next_outbound_sequence=0,
        next_inbound_sequence=0,
    )
    first_id = "8b000000-0000-4000-8000-000000000011"
    await storage.append_observer_outbox(
        message_id=first_id,
        connector_sequence=0,
        transport_epoch_id=EPOCH_1,
        message_type="session.event",
        profile="default",
        session_key="session-1",
        runtime_generation="runtime-1",
        runtime_session_id="runtime-session-1",
        event_sequence=1,
        payload=b"first",
        frame=b"first-frame",
    )
    await storage.begin_transport_epoch(
        epoch_id=EPOCH_2,
        runtime_generation="runtime-1",
        previous_connection_id=None,
        next_outbound_sequence=0,
        next_inbound_sequence=0,
    )
    second_id = "8b000000-0000-4000-8000-000000000012"
    second = await storage.append_observer_outbox(
        message_id=second_id,
        connector_sequence=0,
        transport_epoch_id=EPOCH_2,
        message_type="session.event",
        profile="default",
        session_key="session-1",
        runtime_generation="runtime-1",
        runtime_session_id="runtime-session-1",
        event_sequence=1,
        payload=b"second",
        frame=b"second-frame",
    )
    found = await storage.get_observer_fact(
        transport_epoch_id=EPOCH_2,
        message_type="session.event",
        profile="default",
        session_key="session-1",
        runtime_generation="runtime-1",
        runtime_session_id="runtime-session-1",
        event_sequence=1,
    )
    assert found == second
    assert found.message_id == second_id
    await _stop(storage, runner)


@pytest.mark.asyncio
async def test_begin_epoch_same_id_is_exact_idempotency_only(tmp_path: Path) -> None:
    storage, runner = await _start(tmp_path / "connector.sqlite3")
    first = await storage.begin_transport_epoch(
        epoch_id=EPOCH_1,
        runtime_generation="runtime-1",
        previous_connection_id=None,
        next_outbound_sequence=0,
        next_inbound_sequence=0,
    )
    repeated = await storage.begin_transport_epoch(
        epoch_id=EPOCH_1,
        runtime_generation="runtime-1",
        previous_connection_id=None,
        next_outbound_sequence=0,
        next_inbound_sequence=0,
    )
    assert repeated == first
    with pytest.raises(IdempotencyConflict):
        await storage.begin_transport_epoch(
            epoch_id=EPOCH_1,
            runtime_generation="runtime-2",
            previous_connection_id=None,
            next_outbound_sequence=0,
            next_inbound_sequence=0,
        )
    with pytest.raises(IdempotencyConflict):
        await storage.begin_transport_epoch(
            epoch_id=EPOCH_1,
            runtime_generation="runtime-1",
            previous_connection_id=None,
            next_outbound_sequence=1,
            next_inbound_sequence=0,
        )
    await _stop(storage, runner)


@pytest.mark.asyncio
async def test_confirmed_floor_compacts_current_epoch_settled_rewind_window(
    tmp_path: Path,
) -> None:
    storage, runner = await _start(
        tmp_path / "connector.sqlite3",
        ConnectorConfig(transport_journal_entries=1),
    )
    await storage.begin_transport_epoch(
        epoch_id=EPOCH_1,
        runtime_generation="runtime-1",
        previous_connection_id=None,
        next_outbound_sequence=0,
        next_inbound_sequence=0,
    )
    await storage.stage_transport_frame(
        epoch_id=EPOCH_1,
        sequence=0,
        message_id=MESSAGE_1,
        message_type="connector.heartbeat",
        business_kind="heartbeat",
        business_key="heartbeat-0",
        business_revision=0,
        runtime_generation="runtime-1",
        frame=b"rewind-window",
    )
    await storage.mark_transport_sent(epoch_id=EPOCH_1, sequence=0)
    await storage.settle_transport_cursor(epoch_id=EPOCH_1, next_sequence=1)
    await storage.stage_transport_frame(
        epoch_id=EPOCH_1,
        sequence=1,
        message_id=MESSAGE_2,
        message_type="connector.heartbeat",
        business_kind="heartbeat",
        business_key="heartbeat-1",
        business_revision=1,
        runtime_generation="runtime-1",
        frame=b"next",
    )
    assert await storage.transport_frame(MESSAGE_1) is None
    await _stop(storage, runner)
