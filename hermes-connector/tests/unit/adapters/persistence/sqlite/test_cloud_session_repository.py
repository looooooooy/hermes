from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from hermes_connector.adapters.sqlite_storage import SQLiteStorageComponent
from hermes_connector.bootstrap.config import ConnectorConfig
from hermes_connector.domain.storage import StorageSequenceConflict


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


@pytest.mark.asyncio
async def test_cloud_checkpoint_cas_reset_and_restart_are_durable(
    tmp_path: Path,
) -> None:
    path = tmp_path / "connector.sqlite3"
    storage, runner = await _start(path)
    initial = await storage.get_cloud_session()
    assert initial.previous_connection_id is None
    assert initial.next_outbound_sequence == 0
    assert initial.next_inbound_sequence == 0
    assert initial.reconciliation_required is False

    assert await storage.advance_cloud_outbound(0) == 1
    assert await storage.advance_cloud_inbound(0) == 1
    with pytest.raises(StorageSequenceConflict):
        await storage.advance_cloud_outbound(0)

    reset = await storage.begin_cloud_reconciliation(
        previous_connection_id="22222222-2222-4222-8222-222222222222",
        next_outbound_sequence=7,
        next_inbound_sequence=9,
    )
    assert reset.reconciliation_required is True
    assert reset.next_outbound_sequence == 7
    assert reset.next_inbound_sequence == 9
    completed = await storage.complete_cloud_reconciliation(
        previous_connection_id="33333333-3333-4333-8333-333333333333"
    )
    assert completed.reconciliation_required is False
    await _stop(storage, runner)

    reopened, reopened_runner = await _start(path)
    durable = await reopened.get_cloud_session()
    assert durable.previous_connection_id == "33333333-3333-4333-8333-333333333333"
    assert durable.next_outbound_sequence == 7
    assert durable.next_inbound_sequence == 9
    assert durable.reconciliation_required is False
    await _stop(reopened, reopened_runner)
