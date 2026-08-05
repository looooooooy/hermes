from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import pytest

from hermes_connector.adapters.sqlite_storage import SQLiteStorageComponent
from hermes_connector.bootstrap.config import ConnectorConfig
from hermes_connector.domain.storage import IdempotencyConflict, StorageOverloaded

MESSAGE_ID = "93000000-0000-4000-8000-000000000001"
SNAPSHOT_ID = "94000000-0000-4000-8000-000000000001"
EPOCH_ONE = "95000000-0000-4000-8000-000000000001"
EPOCH_TWO = "95000000-0000-4000-8000-000000000002"
PAYLOAD = b'{"catalog_sequence":8}'
FRAME = b'{"message_type":"session.catalog.event","sequence":41}'
DIGEST = hashlib.sha256(PAYLOAD).hexdigest()


async def _start(
    path: Path,
) -> tuple[SQLiteStorageComponent, asyncio.Task[None]]:
    storage = SQLiteStorageComponent(path, ConnectorConfig())
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


async def _append_event(
    storage: SQLiteStorageComponent,
    *,
    message_id: str = MESSAGE_ID,
    connector_sequence: int = 41,
    transport_epoch_id: str | None = None,
    runtime_generation: str = "runtime-generation-1",
):
    return await storage.append_session_catalog_outbox(
        message_id=message_id,
        connector_sequence=connector_sequence,
        transport_epoch_id=transport_epoch_id,
        message_type="session.catalog.event",
        profile="default",
        runtime_generation=runtime_generation,
        snapshot_id=None,
        catalog_revision=None,
        page_index=None,
        is_last=None,
        catalog_sequence=8,
        payload=PAYLOAD,
        frame=FRAME,
    )


async def _append_page(
    storage: SQLiteStorageComponent,
    *,
    message_id: str,
    connector_sequence: int,
    page_index: int,
    is_last: bool,
):
    payload = f'{{"page_index":{page_index}}}'.encode()
    return await storage.append_session_catalog_outbox(
        message_id=message_id,
        connector_sequence=connector_sequence,
        transport_epoch_id=None,
        message_type="session.catalog.snapshot.page",
        profile="default",
        runtime_generation="runtime-generation-1",
        snapshot_id=SNAPSHOT_ID,
        catalog_revision=7,
        page_index=page_index,
        is_last=is_last,
        catalog_sequence=None,
        payload=payload,
        frame=payload,
    )


def _ack() -> dict[str, object]:
    return {
        "profile": "default",
        "runtime_generation": "runtime-generation-1",
        "acked_message_id": MESSAGE_ID,
        "acked_payload_digest": DIGEST,
        "acked_connector_sequence": 41,
        "ack_kind": "event_applied",
        "snapshot_id": None,
        "catalog_revision": None,
        "page_index": None,
        "is_last": None,
        "catalog_sequence": 8,
    }


def _nack() -> dict[str, object]:
    return {
        "profile": "default",
        "runtime_generation": "runtime-generation-1",
        "rejected_message_id": MESSAGE_ID,
        "rejected_payload_digest": DIGEST,
        "rejected_connector_sequence": 41,
        "reason": "event_gap",
        "snapshot_id": None,
        "expected_page_index": None,
        "expected_catalog_sequence": 8,
    }


@pytest.mark.asyncio
async def test_catalog_outbox_survives_process_restart_with_exact_frame(
    tmp_path: Path,
) -> None:
    path = tmp_path / "connector.sqlite3"
    storage, runner = await _start(path)
    staged = await _append_event(storage)
    assert staged.state == "pending"
    await _stop(storage, runner)

    reopened, reopened_runner = await _start(path)
    pending = await reopened.pending_session_catalog_outbox(limit=8)
    assert [(record.message_id, record.frame) for record in pending] == [
        (MESSAGE_ID, FRAME)
    ]
    await _stop(reopened, reopened_runner)


@pytest.mark.asyncio
async def test_duplicate_exact_ack_is_idempotent_but_mismatch_fails_closed(
    tmp_path: Path,
) -> None:
    storage, runner = await _start(tmp_path / "connector.sqlite3")
    await _append_event(storage)

    first = await storage.ack_session_catalog_outbox(**_ack())
    duplicate = await storage.ack_session_catalog_outbox(**_ack())
    assert first == duplicate
    assert duplicate.state == "acked"

    with pytest.raises(IdempotencyConflict):
        await storage.ack_session_catalog_outbox(
            **{**_ack(), "acked_payload_digest": "b" * 64}
        )
    await _stop(storage, runner)


@pytest.mark.asyncio
async def test_nack_retains_rejected_attempt_and_allows_new_attempt(
    tmp_path: Path,
) -> None:
    storage, runner = await _start(tmp_path / "connector.sqlite3")
    await _append_event(storage)

    rejected = await storage.nack_session_catalog_outbox(**_nack())
    assert rejected.state == "rejected"
    assert await storage.pending_session_catalog_outbox(limit=8) == ()

    replacement = await _append_event(
        storage,
        message_id="93000000-0000-4000-8000-000000000002",
        connector_sequence=42,
    )
    assert replacement.state == "pending"
    assert replacement.connector_sequence == 42
    await _stop(storage, runner)


@pytest.mark.asyncio
async def test_fresh_writer_epoch_retires_old_generation_catalog_attempts(
    tmp_path: Path,
) -> None:
    storage, runner = await _start(tmp_path / "connector.sqlite3")
    await storage.begin_transport_epoch(
        epoch_id=EPOCH_ONE,
        runtime_generation="runtime-generation-1",
        previous_connection_id=None,
        next_outbound_sequence=0,
        next_inbound_sequence=0,
    )
    old = await _append_event(
        storage,
        connector_sequence=0,
        transport_epoch_id=EPOCH_ONE,
    )

    await storage.begin_transport_epoch(
        epoch_id=EPOCH_TWO,
        runtime_generation="runtime-generation-2",
        previous_connection_id=None,
        next_outbound_sequence=0,
        next_inbound_sequence=0,
    )

    assert await storage.pending_session_catalog_outbox(limit=8) == ()
    retired = await storage.get_session_catalog_outbox(old.message_id)
    assert retired is not None
    assert retired.state == "retired"
    current = await _append_event(
        storage,
        message_id="93000000-0000-4000-8000-000000000002",
        connector_sequence=0,
        transport_epoch_id=EPOCH_TWO,
        runtime_generation="runtime-generation-2",
    )
    assert current.state == "pending"
    await _stop(storage, runner)


@pytest.mark.asyncio
async def test_terminal_snapshot_ack_settles_every_page_in_the_snapshot(
    tmp_path: Path,
) -> None:
    storage, runner = await _start(tmp_path / "connector.sqlite3")
    first = await _append_page(
        storage,
        message_id="96000000-0000-4000-8000-000000000001",
        connector_sequence=1,
        page_index=0,
        is_last=False,
    )
    terminal = await _append_page(
        storage,
        message_id="96000000-0000-4000-8000-000000000002",
        connector_sequence=2,
        page_index=1,
        is_last=True,
    )

    await storage.ack_session_catalog_outbox(
        profile="default",
        runtime_generation="runtime-generation-1",
        acked_message_id=terminal.message_id,
        acked_payload_digest=terminal.payload_digest,
        acked_connector_sequence=2,
        ack_kind="snapshot_committed",
        snapshot_id=SNAPSHOT_ID,
        catalog_revision=7,
        page_index=1,
        is_last=True,
        catalog_sequence=None,
    )

    assert (await storage.get_session_catalog_outbox(first.message_id)).state == "acked"
    assert (await storage.get_session_catalog_outbox(terminal.message_id)).state == "acked"
    assert await storage.pending_session_catalog_outbox(limit=8) == ()
    await _stop(storage, runner)


@pytest.mark.asyncio
async def test_nonterminal_page_cannot_be_replayed_as_snapshot_commit_ack(
    tmp_path: Path,
) -> None:
    storage, runner = await _start(tmp_path / "connector.sqlite3")
    first = await _append_page(
        storage,
        message_id="96000000-0000-4000-8000-000000000011",
        connector_sequence=1,
        page_index=0,
        is_last=False,
    )
    terminal = await _append_page(
        storage,
        message_id="96000000-0000-4000-8000-000000000012",
        connector_sequence=2,
        page_index=1,
        is_last=True,
    )
    await storage.ack_session_catalog_outbox(
        profile="default",
        runtime_generation="runtime-generation-1",
        acked_message_id=terminal.message_id,
        acked_payload_digest=terminal.payload_digest,
        acked_connector_sequence=terminal.connector_sequence,
        ack_kind="snapshot_committed",
        snapshot_id=SNAPSHOT_ID,
        catalog_revision=7,
        page_index=1,
        is_last=True,
        catalog_sequence=None,
    )

    with pytest.raises(IdempotencyConflict):
        await storage.ack_session_catalog_outbox(
            profile="default",
            runtime_generation="runtime-generation-1",
            acked_message_id=first.message_id,
            acked_payload_digest=first.payload_digest,
            acked_connector_sequence=first.connector_sequence,
            ack_kind="snapshot_committed",
            snapshot_id=SNAPSHOT_ID,
            catalog_revision=7,
            page_index=0,
            is_last=False,
            catalog_sequence=None,
        )
    await _stop(storage, runner)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reason",
    ("runtime_mismatch", "stale_writer", "contract_mismatch"),
)
async def test_positionless_root_nack_reasons_reject_catalog_attempt(
    tmp_path: Path,
    reason: str,
) -> None:
    storage, runner = await _start(tmp_path / f"{reason}.sqlite3")
    record = await _append_event(storage)

    rejected = await storage.nack_session_catalog_outbox(
        profile="default",
        runtime_generation="runtime-generation-1",
        rejected_message_id=record.message_id,
        rejected_payload_digest=record.payload_digest,
        rejected_connector_sequence=record.connector_sequence,
        reason=reason,
        snapshot_id=None,
        expected_page_index=None,
        expected_catalog_sequence=None,
    )

    assert rejected.state == "rejected"
    await _stop(storage, runner)


@pytest.mark.asyncio
async def test_revision_conflict_rejects_terminal_and_retires_snapshot_siblings(
    tmp_path: Path,
) -> None:
    storage, runner = await _start(tmp_path / "connector.sqlite3")
    first = await _append_page(
        storage,
        message_id="97000000-0000-4000-8000-000000000001",
        connector_sequence=1,
        page_index=0,
        is_last=False,
    )
    terminal = await _append_page(
        storage,
        message_id="97000000-0000-4000-8000-000000000002",
        connector_sequence=2,
        page_index=1,
        is_last=True,
    )

    rejected = await storage.nack_session_catalog_outbox(
        profile="default",
        runtime_generation="runtime-generation-1",
        rejected_message_id=terminal.message_id,
        rejected_payload_digest=terminal.payload_digest,
        rejected_connector_sequence=terminal.connector_sequence,
        reason="revision_conflict",
        snapshot_id=SNAPSHOT_ID,
        expected_page_index=1,
        expected_catalog_sequence=None,
    )

    assert rejected.state == "rejected"
    assert (await storage.get_session_catalog_outbox(first.message_id)).state == "retired"
    assert await storage.pending_session_catalog_outbox(limit=8) == ()
    await _stop(storage, runner)


@pytest.mark.asyncio
async def test_non_contract_snapshot_mismatch_is_rejected_before_storage_write(
    tmp_path: Path,
) -> None:
    storage, runner = await _start(tmp_path / "connector.sqlite3")
    record = await _append_page(
        storage,
        message_id="98000000-0000-4000-8000-000000000001",
        connector_sequence=1,
        page_index=0,
        is_last=True,
    )

    with pytest.raises(ValueError, match="reason"):
        await storage.nack_session_catalog_outbox(
            profile="default",
            runtime_generation="runtime-generation-1",
            rejected_message_id=record.message_id,
            rejected_payload_digest=record.payload_digest,
            rejected_connector_sequence=record.connector_sequence,
            reason="snapshot_mismatch",
            snapshot_id=SNAPSHOT_ID,
            expected_page_index=0,
            expected_catalog_sequence=None,
        )
    await _stop(storage, runner)


@pytest.mark.asyncio
async def test_duplicate_nack_must_match_the_persisted_reason_tuple(
    tmp_path: Path,
) -> None:
    storage, runner = await _start(tmp_path / "connector.sqlite3")
    record = await _append_event(storage)
    values = {
        "profile": "default",
        "runtime_generation": "runtime-generation-1",
        "rejected_message_id": record.message_id,
        "rejected_payload_digest": record.payload_digest,
        "rejected_connector_sequence": record.connector_sequence,
        "reason": "runtime_mismatch",
        "snapshot_id": None,
        "expected_page_index": None,
        "expected_catalog_sequence": None,
    }

    first = await storage.nack_session_catalog_outbox(**values)
    duplicate = await storage.nack_session_catalog_outbox(**values)
    assert duplicate == first

    with pytest.raises(IdempotencyConflict):
        await storage.nack_session_catalog_outbox(
            **{**values, "reason": "stale_writer"}
        )
    await _stop(storage, runner)


@pytest.mark.asyncio
async def test_epoch_catalog_frame_recovers_from_orm_after_process_restart(
    tmp_path: Path,
) -> None:
    path = tmp_path / "connector.sqlite3"
    storage, runner = await _start(path)
    await storage.begin_transport_epoch(
        epoch_id=EPOCH_ONE,
        runtime_generation="runtime-generation-1",
        previous_connection_id=None,
        next_outbound_sequence=0,
        next_inbound_sequence=0,
    )
    staged = await _append_event(
        storage,
        connector_sequence=0,
        transport_epoch_id=EPOCH_ONE,
    )
    await _stop(storage, runner)

    reopened, reopened_runner = await _start(path)
    catalog = await reopened.pending_session_catalog_outbox(limit=8)
    transport = await reopened.pending_transport_frames(
        epoch_id=EPOCH_ONE,
        limit=8,
    )

    assert [(record.message_id, record.frame) for record in catalog] == [
        (staged.message_id, FRAME)
    ]
    assert [(record.message_id, record.frame) for record in transport] == [
        (staged.message_id, FRAME)
    ]
    await _stop(reopened, reopened_runner)


@pytest.mark.asyncio
async def test_catalog_backpressure_never_evicts_pending_attempts(
    tmp_path: Path,
) -> None:
    storage = SQLiteStorageComponent(
        tmp_path / "connector.sqlite3",
        ConnectorConfig(bounded_queue_items=1),
    )
    await storage.start()
    runner = asyncio.create_task(storage.run())
    await _append_event(storage)

    with pytest.raises(StorageOverloaded):
        await _append_event(
            storage,
            message_id="99000000-0000-4000-8000-000000000002",
            connector_sequence=42,
        )

    assert [
        record.message_id
        for record in await storage.pending_session_catalog_outbox(limit=1)
    ] == [MESSAGE_ID]
    await _stop(storage, runner)


@pytest.mark.asyncio
async def test_explicit_capability_loss_retires_pending_catalog_attempts(
    tmp_path: Path,
) -> None:
    storage, runner = await _start(tmp_path / "connector.sqlite3")
    await storage.begin_transport_epoch(
        epoch_id=EPOCH_ONE,
        runtime_generation="runtime-generation-1",
        previous_connection_id=None,
        next_outbound_sequence=0,
        next_inbound_sequence=0,
    )
    pending = await _append_event(
        storage,
        connector_sequence=0,
        transport_epoch_id=EPOCH_ONE,
    )

    await storage.retire_session_catalog_outbox()

    assert await storage.pending_session_catalog_outbox(limit=8) == ()
    assert await storage.pending_transport_frames(
        epoch_id=EPOCH_ONE,
        limit=8,
    ) == ()
    retired = await storage.get_session_catalog_outbox(pending.message_id)
    assert retired is not None
    assert retired.state == "retired"
    await _stop(storage, runner)


@pytest.mark.asyncio
async def test_terminal_snapshot_ack_remains_idempotent_after_page_retention(
    tmp_path: Path,
) -> None:
    path = tmp_path / "connector.sqlite3"
    storage = SQLiteStorageComponent(
        path,
        ConnectorConfig(bounded_queue_items=2),
    )
    await storage.start()
    runner = asyncio.create_task(storage.run())
    first = await _append_page(
        storage,
        message_id="9a000000-0000-4000-8000-000000000001",
        connector_sequence=1,
        page_index=0,
        is_last=False,
    )
    terminal = await _append_page(
        storage,
        message_id="9a000000-0000-4000-8000-000000000002",
        connector_sequence=2,
        page_index=1,
        is_last=True,
    )
    ack = {
        "profile": "default",
        "runtime_generation": "runtime-generation-1",
        "acked_message_id": terminal.message_id,
        "acked_payload_digest": terminal.payload_digest,
        "acked_connector_sequence": terminal.connector_sequence,
        "ack_kind": "snapshot_committed",
        "snapshot_id": SNAPSHOT_ID,
        "catalog_revision": 7,
        "page_index": 1,
        "is_last": True,
        "catalog_sequence": None,
    }
    await storage.ack_session_catalog_outbox(**ack)
    await _append_event(
        storage,
        message_id="9a000000-0000-4000-8000-000000000003",
        connector_sequence=3,
    )
    await _append_event(
        storage,
        message_id="9a000000-0000-4000-8000-000000000004",
        connector_sequence=4,
    )
    assert await storage.get_session_catalog_outbox(first.message_id) is None
    assert await storage.get_session_catalog_outbox(terminal.message_id) is None
    await _stop(storage, runner)

    reopened, reopened_runner = await _start(path)
    duplicate = await reopened.ack_session_catalog_outbox(**ack)
    assert duplicate.state == "acked"
    await _stop(reopened, reopened_runner)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("acked_message_id", "9c000000-0000-4000-8000-000000000001"),
        ("acked_payload_digest", "b" * 64),
        ("snapshot_id", "9d000000-0000-4000-8000-000000000001"),
        ("page_index", 1),
    ),
)
async def test_retained_terminal_ack_receipt_rejects_identity_conflicts(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    path = tmp_path / f"{field}.sqlite3"
    storage = SQLiteStorageComponent(
        path,
        ConnectorConfig(bounded_queue_items=1),
    )
    await storage.start()
    runner = asyncio.create_task(storage.run())
    terminal = await _append_page(
        storage,
        message_id="9e000000-0000-4000-8000-000000000001",
        connector_sequence=1,
        page_index=0,
        is_last=True,
    )
    ack = {
        "profile": "default",
        "runtime_generation": "runtime-generation-1",
        "acked_message_id": terminal.message_id,
        "acked_payload_digest": terminal.payload_digest,
        "acked_connector_sequence": terminal.connector_sequence,
        "ack_kind": "snapshot_committed",
        "snapshot_id": SNAPSHOT_ID,
        "catalog_revision": 7,
        "page_index": 0,
        "is_last": True,
        "catalog_sequence": None,
    }
    await storage.ack_session_catalog_outbox(**ack)
    await _append_event(
        storage,
        message_id="9e000000-0000-4000-8000-000000000002",
        connector_sequence=2,
    )
    assert await storage.get_session_catalog_outbox(terminal.message_id) is None
    await _stop(storage, runner)

    reopened, reopened_runner = await _start(path)
    with pytest.raises(IdempotencyConflict):
        await reopened.ack_session_catalog_outbox(**{**ack, field: value})
    await _stop(reopened, reopened_runner)


@pytest.mark.asyncio
async def test_terminal_ack_receipt_is_retired_on_runtime_generation_rollover(
    tmp_path: Path,
) -> None:
    storage = SQLiteStorageComponent(
        tmp_path / "connector.sqlite3",
        ConnectorConfig(bounded_queue_items=1),
    )
    await storage.start()
    runner = asyncio.create_task(storage.run())
    terminal = await _append_page(
        storage,
        message_id="9f000000-0000-4000-8000-000000000001",
        connector_sequence=1,
        page_index=0,
        is_last=True,
    )
    ack = {
        "profile": "default",
        "runtime_generation": "runtime-generation-1",
        "acked_message_id": terminal.message_id,
        "acked_payload_digest": terminal.payload_digest,
        "acked_connector_sequence": terminal.connector_sequence,
        "ack_kind": "snapshot_committed",
        "snapshot_id": SNAPSHOT_ID,
        "catalog_revision": 7,
        "page_index": 0,
        "is_last": True,
        "catalog_sequence": None,
    }
    await storage.ack_session_catalog_outbox(**ack)
    await _append_event(
        storage,
        message_id="9f000000-0000-4000-8000-000000000002",
        connector_sequence=2,
    )
    assert await storage.get_session_catalog_outbox(terminal.message_id) is None

    exact = await storage.ack_session_catalog_outbox(**ack)
    assert exact.state == "acked"
    await storage.begin_transport_epoch(
        epoch_id=EPOCH_TWO,
        runtime_generation="runtime-generation-2",
        previous_connection_id=None,
        next_outbound_sequence=0,
        next_inbound_sequence=0,
    )
    with pytest.raises(IdempotencyConflict):
        await storage.ack_session_catalog_outbox(**ack)
    await _stop(storage, runner)
