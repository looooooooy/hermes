from __future__ import annotations

import asyncio
import hashlib
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest

from hermes_connector.adapters.sqlite_storage import SQLiteStorageComponent
from hermes_connector.bootstrap.config import ConnectorConfig
from hermes_connector.domain.observer import StreamAck, StreamNack
from hermes_connector.domain.storage import IdempotencyConflict, StorageOverloaded

MESSAGE_ID = "83000000-0000-4000-8000-000000000001"
PAYLOAD = b'{"event_sequence":5}'
FRAME = b'{"message_type":"session.event","sequence":41}'
DIGEST = hashlib.sha256(PAYLOAD).hexdigest()


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


async def _append(
    storage: SQLiteStorageComponent,
    *,
    message_id: str = MESSAGE_ID,
    connector_sequence: int = 41,
    frame: bytes = FRAME,
) -> object:
    return await storage.append_observer_outbox(
        message_id=message_id,
        connector_sequence=connector_sequence,
        message_type="session.event",
        profile="default",
        session_key="session-root-1",
        runtime_generation="runtime-generation-1",
        runtime_session_id="runtime-session-1",
        event_sequence=5,
        payload=PAYLOAD,
        frame=frame,
    )


@pytest.mark.asyncio
async def test_observer_fact_is_durable_and_replays_exact_identity_after_restart(
    tmp_path: Path,
) -> None:
    path = tmp_path / "connector.sqlite3"
    storage, runner = await _start(path)
    record = await _append(storage)
    assert record.payload_digest == DIGEST
    assert record.state == "pending"
    await _stop(storage, runner)

    reopened, reopened_runner = await _start(path)
    pending = await reopened.pending_observer_outbox(limit=10)
    assert len(pending) == 1
    assert pending[0].message_id == MESSAGE_ID
    assert pending[0].connector_sequence == 41
    assert pending[0].frame == FRAME
    assert pending[0].payload_digest == DIGEST
    await _stop(reopened, reopened_runner)


@pytest.mark.asyncio
async def test_observer_identity_is_idempotent_and_digest_conflicts_fail_closed(
    tmp_path: Path,
) -> None:
    storage, runner = await _start(tmp_path / "connector.sqlite3")
    first = await _append(storage)
    second = await _append(storage)
    assert second == first

    with pytest.raises(IdempotencyConflict):
        await storage.append_observer_outbox(
            message_id=MESSAGE_ID,
            connector_sequence=41,
            message_type="session.event",
            profile="default",
            session_key="session-root-1",
            runtime_generation="runtime-generation-1",
            runtime_session_id="runtime-session-1",
            event_sequence=5,
            payload=b'{"event_sequence":5,"changed":true}',
            frame=FRAME,
        )
    await _stop(storage, runner)


@pytest.mark.asyncio
async def test_only_exact_stream_ack_marks_business_fact_acknowledged(
    tmp_path: Path,
) -> None:
    storage, runner = await _start(tmp_path / "connector.sqlite3")
    await _append(storage)
    ack = StreamAck(
        observer_message_id=UUID(MESSAGE_ID),
        payload_digest=DIGEST,
        connector_sequence=41,
        observer_message_type="session.event",
        profile="default",
        session_key="session-root-1",
        runtime_generation="runtime-generation-1",
        runtime_session_id="runtime-session-1",
        event_sequence=5,
        committed_at=datetime(2026, 7, 31, 9, 0, 1, tzinfo=UTC),
    )

    mismatch = replace(ack, payload_digest="b" * 64)
    with pytest.raises(IdempotencyConflict):
        await storage.ack_observer_outbox(mismatch)
    assert len(await storage.pending_observer_outbox(limit=10)) == 1

    committed = await storage.ack_observer_outbox(ack)
    assert committed.state == "acked"
    assert await storage.pending_observer_outbox(limit=10) == ()
    await _stop(storage, runner)


@pytest.mark.asyncio
async def test_stream_nack_retains_rejected_fact_for_audit_and_stops_replay(
    tmp_path: Path,
) -> None:
    storage, runner = await _start(tmp_path / "connector.sqlite3")
    await _append(storage)
    nack = StreamNack(
        observer_message_id=UUID(MESSAGE_ID),
        payload_digest=DIGEST,
        connector_sequence=41,
        observer_message_type="session.event",
        profile="default",
        session_key="session-root-1",
        runtime_generation="runtime-generation-1",
        runtime_session_id="runtime-session-1",
        event_sequence=5,
        reason="event_gap",
        expected_event_sequence=4,
        recovery="send_snapshot",
        rejected_at=datetime(2026, 7, 31, 9, 0, 2, tzinfo=UTC),
    )

    rejected = await storage.nack_observer_outbox(nack)

    assert rejected.state == "rejected"
    assert await storage.pending_observer_outbox(limit=10) == ()
    retained = await storage.get_observer_outbox(MESSAGE_ID)
    assert retained is not None
    assert retained.state == "rejected"
    await _stop(storage, runner)


@pytest.mark.asyncio
async def test_rejected_fact_allows_new_attempt_and_latest_lookup_keeps_audit(
    tmp_path: Path,
) -> None:
    storage, runner = await _start(tmp_path / "connector.sqlite3")
    await _append(storage)
    nack = StreamNack(
        observer_message_id=UUID(MESSAGE_ID),
        payload_digest=DIGEST,
        connector_sequence=41,
        observer_message_type="session.event",
        profile="default",
        session_key="session-root-1",
        runtime_generation="runtime-generation-1",
        runtime_session_id="runtime-session-1",
        event_sequence=5,
        reason="event_gap",
        expected_event_sequence=4,
        recovery="send_snapshot",
        rejected_at=datetime(2026, 7, 31, 9, 0, 2, tzinfo=UTC),
    )
    await storage.nack_observer_outbox(nack)

    replacement_id = "83000000-0000-4000-8000-000000000002"
    replacement = await _append(
        storage,
        message_id=replacement_id,
        connector_sequence=42,
        frame=b'{"message_type":"session.event","sequence":42}',
    )

    assert replacement.message_id == replacement_id
    assert replacement.connector_sequence == 42
    assert replacement.state == "pending"
    latest = await storage.get_observer_fact(
        message_type="session.event",
        profile="default",
        session_key="session-root-1",
        runtime_generation="runtime-generation-1",
        runtime_session_id="runtime-session-1",
        event_sequence=5,
    )
    assert latest == replacement
    rejected = await storage.get_observer_outbox(MESSAGE_ID)
    assert rejected is not None
    assert rejected.state == "rejected"
    await _stop(storage, runner)


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_state", ("acked", "rejected"))
async def test_total_retention_evicts_oldest_safe_terminal_payload(
    tmp_path: Path,
    terminal_state: str,
) -> None:
    storage, runner = await _start(
        tmp_path / "connector.sqlite3",
        ConnectorConfig(bounded_queue_items=1),
    )
    first = await _append(storage)
    receipt_values = {
        "observer_message_id": UUID(first.message_id),
        "payload_digest": first.payload_digest,
        "connector_sequence": first.connector_sequence,
        "observer_message_type": first.message_type,
        "profile": first.profile,
        "session_key": first.session_key,
        "runtime_generation": first.runtime_generation,
        "runtime_session_id": first.runtime_session_id,
        "event_sequence": first.event_sequence,
    }
    if terminal_state == "acked":
        await storage.ack_observer_outbox(
            StreamAck(
                **receipt_values,
                committed_at=datetime(2026, 7, 31, 9, 0, 1, tzinfo=UTC),
            )
        )
    else:
        await storage.nack_observer_outbox(
            StreamNack(
                **receipt_values,
                reason="event_gap",
                expected_event_sequence=4,
                recovery="send_snapshot",
                rejected_at=datetime(2026, 7, 31, 9, 0, 2, tzinfo=UTC),
            )
        )

    replacement_id = "83000000-0000-4000-8000-000000000002"
    await _append(
        storage,
        message_id=replacement_id,
        connector_sequence=42,
        frame=b"replacement",
    )

    assert await storage.get_observer_outbox(first.message_id) is None
    assert await storage.get_observer_outbox(replacement_id) is not None
    await _stop(storage, runner)


@pytest.mark.asyncio
async def test_total_retention_evicts_retired_old_epoch_after_fresh_rotation(
    tmp_path: Path,
) -> None:
    storage, runner = await _start(
        tmp_path / "connector.sqlite3",
        ConnectorConfig(bounded_queue_items=1),
    )
    epoch_one = "85000000-0000-4000-8000-000000000001"
    epoch_two = "85000000-0000-4000-8000-000000000002"
    await storage.begin_transport_epoch(
        epoch_id=epoch_one,
        runtime_generation="runtime-1",
        previous_connection_id=None,
        next_outbound_sequence=0,
        next_inbound_sequence=0,
    )
    old = await storage.append_observer_outbox(
        message_id=MESSAGE_ID,
        connector_sequence=0,
        transport_epoch_id=epoch_one,
        message_type="session.event",
        profile="default",
        session_key="session-root-1",
        runtime_generation="runtime-generation-1",
        runtime_session_id="runtime-session-1",
        event_sequence=5,
        payload=PAYLOAD,
        frame=FRAME,
    )
    await storage.begin_transport_epoch(
        epoch_id=epoch_two,
        runtime_generation="runtime-1",
        previous_connection_id=None,
        next_outbound_sequence=0,
        next_inbound_sequence=0,
    )

    current_id = "83000000-0000-4000-8000-000000000002"
    await storage.append_observer_outbox(
        message_id=current_id,
        connector_sequence=0,
        transport_epoch_id=epoch_two,
        message_type="session.event",
        profile="default",
        session_key="session-root-1",
        runtime_generation="runtime-generation-1",
        runtime_session_id="runtime-session-2",
        event_sequence=6,
        payload=b"current",
        frame=b"current-frame",
    )

    assert await storage.get_observer_outbox(old.message_id) is None
    assert await storage.get_observer_outbox(current_id) is not None
    await _stop(storage, runner)


@pytest.mark.asyncio
async def test_total_retention_never_evicts_pending_rows(
    tmp_path: Path,
) -> None:
    storage, runner = await _start(
        tmp_path / "connector.sqlite3",
        ConnectorConfig(bounded_queue_items=1),
    )
    first = await _append(storage)

    with pytest.raises(StorageOverloaded):
        await _append(
            storage,
            message_id="83000000-0000-4000-8000-000000000002",
            connector_sequence=42,
            frame=b"second",
        )

    retained = await storage.get_observer_outbox(first.message_id)
    assert retained is not None
    assert retained.payload == PAYLOAD
    await _stop(storage, runner)


@pytest.mark.asyncio
async def test_invalid_observer_text_is_rejected_before_queue_and_writer_survives(
    tmp_path: Path,
) -> None:
    storage, runner = await _start(tmp_path / "connector.sqlite3")

    with pytest.raises(TypeError):
        await storage.append_observer_outbox(
            message_id=MESSAGE_ID,
            connector_sequence=41,
            message_type="session.event",
            profile=1,  # type: ignore[arg-type]
            session_key="session-root-1",
            runtime_generation="runtime-generation-1",
            runtime_session_id="runtime-session-1",
            event_sequence=5,
            payload=PAYLOAD,
            frame=FRAME,
        )

    committed = await _append(storage)
    assert committed.message_id == MESSAGE_ID
    assert not runner.done()
    await _stop(storage, runner)
