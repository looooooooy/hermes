from __future__ import annotations

import asyncio
import tempfile
import unittest
from contextlib import suppress
from pathlib import Path

from sqlalchemy import URL, create_engine, func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from hermes_connector.adapters.sqlite_models import InboxMessage
from hermes_connector.adapters.sqlite_storage import SQLiteStorageComponent
from hermes_connector.application.supervisor import Supervisor
from hermes_connector.bootstrap.config import ConnectorConfig
from hermes_connector.bootstrap.safe_logging import SafeStructuredLogger
from hermes_connector.domain.storage import (
    IdempotencyConflict,
    StorageCorrupt,
    StorageDeadlineExceeded,
    StorageFrameTooLarge,
    StorageFull,
    StorageOverloaded,
    StorageReadOnly,
)


def config(**overrides: object) -> ConnectorConfig:
    values: dict[str, object] = {
        "start_deadline_seconds": 0.2,
        "stop_deadline_seconds": 0.2,
        "storage_write_deadline_seconds": 0.2,
        "storage_busy_timeout_ms": 100,
        "bounded_queue_items": 8,
    }
    values.update(overrides)
    return ConnectorConfig(**values)


async def start_running(
    path: Path,
    *,
    storage_config: ConnectorConfig | None = None,
    write_fault: object = None,
) -> tuple[SQLiteStorageComponent, asyncio.Task[None]]:
    storage = SQLiteStorageComponent(
        path,
        storage_config or config(),
        write_fault=write_fault,
    )
    await storage.start()
    runner = asyncio.create_task(storage.run(), name="test:sqlite-writer")
    self_ready = await storage.ready()
    if not self_ready:
        raise AssertionError("storage did not become ready")
    return storage, runner


async def stop_running(
    storage: SQLiteStorageComponent,
    runner: asyncio.Task[None],
) -> None:
    await storage.drain()
    await storage.stop()
    await runner


def orm_inbox_rows(path: Path) -> list[tuple[str, str]]:
    engine = create_engine(URL.create("sqlite+pysqlite", database=str(path)))
    try:
        with Session(engine) as session:
            records = session.scalars(
                select(InboxMessage).order_by(InboxMessage.message_id)
            ).all()
            return [(record.message_id, record.digest) for record in records]
    finally:
        engine.dispose()


def orm_inbox_count(path: Path) -> int:
    engine = create_engine(URL.create("sqlite+pysqlite", database=str(path)))
    try:
        with Session(engine) as session:
            count = session.scalar(select(func.count()).select_from(InboxMessage))
            return int(count or 0)
    finally:
        engine.dispose()


class SQLiteStorageBehaviorTest(unittest.TestCase):
    def test_inbox_idempotency_conflict_and_commit_before_return(self) -> None:
        async def scenario(path: Path) -> None:
            storage, runner = await start_running(path)

            first = await storage.put_inbox(
                message_id="message-1",
                digest="digest-a",
                payload=b'{"command":"observe"}',
            )
            repeated = await storage.put_inbox(
                message_id="message-1",
                digest="digest-a",
                payload=b'{"ignored":"same-digest"}',
            )

            self.assertTrue(first.inserted)
            self.assertFalse(repeated.inserted)
            self.assertEqual(repeated.record.state, first.record.state)
            with self.assertRaises(IdempotencyConflict) as raised:
                await storage.put_inbox(
                    message_id="message-1",
                    digest="digest-b",
                    payload=b"{}",
                )
            self.assertEqual(raised.exception.code, 4308)
            self.assertEqual(raised.exception.error_name, "idempotency_conflict")

            await storage.put_inbox(
                message_id="message-2",
                digest="digest-c",
                payload=b"{}",
            )
            await stop_running(storage, runner)

            self.assertEqual(
                orm_inbox_rows(path),
                [
                    ("message-1", "digest-a"),
                    ("message-2", "digest-c"),
                ],
            )

        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(scenario(Path(directory) / "connector.sqlite3"))

    def test_outbox_pending_ack_and_cursor_are_ordered_and_idempotent(self) -> None:
        async def scenario(path: Path) -> None:
            storage, runner = await start_running(path)
            await storage.append_outbox(
                message_id="out-2",
                stream="connector-up",
                sequence=2,
                payload=b'{"sequence":2}',
            )
            await storage.append_outbox(
                message_id="out-1",
                stream="connector-up",
                sequence=1,
                payload=b'{"sequence":1}',
            )
            await storage.append_outbox(
                message_id="out-3",
                stream="connector-up",
                sequence=3,
                payload=b'{"sequence":3}',
            )

            pending = await storage.pending_outbox(limit=2)
            self.assertEqual(
                [(record.sequence, record.message_id) for record in pending],
                [(1, "out-1"), (2, "out-2")],
            )
            self.assertTrue(await storage.ack_outbox("out-1"))
            self.assertTrue(await storage.ack_outbox("out-1"))
            self.assertFalse(await storage.ack_outbox("missing"))
            self.assertEqual(
                [record.sequence for record in await storage.pending_outbox(limit=8)],
                [2, 3],
            )

            self.assertEqual(await storage.advance_cursor("connector-up", 5), 5)
            self.assertEqual(await storage.advance_cursor("connector-up", 3), 5)
            self.assertEqual(await storage.advance_cursor("connector-up", 8), 8)
            self.assertEqual(await storage.get_cursor("connector-up"), 8)
            await stop_running(storage, runner)

        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(scenario(Path(directory) / "connector.sqlite3"))

    def test_queue_bound_and_write_deadline_are_explicit(self) -> None:
        async def scenario(path: Path) -> None:
            storage = SQLiteStorageComponent(
                path,
                config(
                    bounded_queue_items=1,
                    storage_write_deadline_seconds=0.01,
                ),
            )
            await storage.start()
            first = asyncio.create_task(
                storage.put_inbox(
                    message_id="queued",
                    digest="digest",
                    payload=b"{}",
                )
            )
            await asyncio.sleep(0)
            self.assertEqual(storage.queued_write_count, 1)

            with self.assertRaises(StorageOverloaded) as raised:
                await storage.put_inbox(
                    message_id="overflow",
                    digest="digest",
                    payload=b"{}",
                )
            self.assertEqual(raised.exception.code, 4305)
            with self.assertRaises(StorageDeadlineExceeded) as deadline:
                await first
            self.assertEqual(deadline.exception.code, 4306)

            runner = asyncio.create_task(storage.run())
            await storage.drain()
            await storage.stop()
            await runner
            self.assertEqual(orm_inbox_count(path), 0)

        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(scenario(Path(directory) / "connector.sqlite3"))

    def test_oversized_payload_is_rejected_before_queueing(self) -> None:
        async def scenario(path: Path) -> None:
            storage = SQLiteStorageComponent(path, config())
            await storage.start()
            oversized = b"x" * 262_145

            for write in (
                storage.put_inbox(
                    message_id="inbox",
                    digest="digest",
                    payload=oversized,
                ),
                storage.append_outbox(
                    message_id="outbox",
                    stream="up",
                    sequence=1,
                    payload=oversized,
                ),
            ):
                with self.assertRaises(StorageFrameTooLarge) as raised:
                    await write
                self.assertEqual(raised.exception.code, 4302)
            self.assertEqual(storage.queued_write_count, 0)
            await storage.stop()

        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(scenario(Path(directory) / "connector.sqlite3"))

    def test_drain_waits_for_queued_writes_and_accepts_exact_payload_limit(
        self,
    ) -> None:
        async def scenario(path: Path) -> None:
            storage = SQLiteStorageComponent(path, config())
            await storage.start()
            writes = [
                asyncio.create_task(
                    storage.put_inbox(
                        message_id=f"queued-{index}",
                        digest=f"digest-{index}",
                        payload=b"x" * 262_144 if index == 0 else b"{}",
                    )
                )
                for index in range(3)
            ]
            await asyncio.sleep(0)
            runner = asyncio.create_task(storage.run())

            await storage.drain()
            results = await asyncio.gather(*writes)
            self.assertTrue(all(result.inserted for result in results))
            self.assertEqual(storage.queued_write_count, 0)
            record = await storage.get_inbox("queued-0")
            self.assertIsNotNone(record)
            self.assertEqual(len(record.payload), 262_144)
            await storage.stop()
            await runner

        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(scenario(Path(directory) / "connector.sqlite3"))

    def test_supervisor_owns_writer_and_drain_persists_all_writes(self) -> None:
        async def scenario(path: Path) -> None:
            storage = SQLiteStorageComponent(path, config())
            supervisor = Supervisor(
                [storage],
                config(),
                SafeStructuredLogger(lambda _: None),
            )
            await supervisor.start()
            writes = [
                asyncio.create_task(
                    storage.put_inbox(
                        message_id=f"message-{index}",
                        digest=f"digest-{index}",
                        payload=b"{}",
                    )
                )
                for index in range(6)
            ]
            await asyncio.gather(*writes)
            await supervisor.stop()

            self.assertEqual(orm_inbox_count(path), 6)

        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(scenario(Path(directory) / "connector.sqlite3"))

    def test_committed_data_survives_writer_crash_and_reopen(self) -> None:
        async def scenario(path: Path) -> None:
            storage, runner = await start_running(path)
            await storage.put_inbox(
                message_id="durable-in",
                digest="digest",
                payload=b"{}",
            )
            await storage.append_outbox(
                message_id="durable-out",
                stream="up",
                sequence=7,
                payload=b"{}",
            )
            await storage.advance_cursor("up", 7)

            runner.cancel()
            with suppress(asyncio.CancelledError):
                await runner

            reopened, reopened_runner = await start_running(path)
            self.assertEqual(
                (await reopened.get_inbox("durable-in")).digest,
                "digest",
            )
            self.assertEqual(
                [
                    record.message_id
                    for record in await reopened.pending_outbox(limit=8)
                ],
                ["durable-out"],
            )
            self.assertEqual(await reopened.get_cursor("up"), 7)
            await stop_running(reopened, reopened_runner)

        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(scenario(Path(directory) / "connector.sqlite3"))


class SQLiteStorageFailureTest(unittest.TestCase):
    def test_full_and_read_only_fail_without_false_ack_and_stop_writes(self) -> None:
        async def scenario(
            path: Path,
            expected_error: type[Exception],
            fault: object,
        ) -> None:
            storage, runner = await start_running(
                path,
                write_fault=fault,
            )
            with self.assertRaises(expected_error):
                await storage.put_inbox(
                    message_id="must-not-ack",
                    digest="digest",
                    payload=b"{}",
                )
            with self.assertRaises(expected_error):
                await storage.put_inbox(
                    message_id="rejected-after-fatal",
                    digest="digest",
                    payload=b"{}",
                )
            with self.assertRaises(expected_error):
                await runner

        failures = (
            ("database or disk is full", StorageFull),
            ("attempt to write a readonly database", StorageReadOnly),
        )
        for message, expected_error in failures:
            with (
                self.subTest(error=expected_error.__name__),
                tempfile.TemporaryDirectory() as directory,
            ):
                path = Path(directory) / "connector.sqlite3"

                def fault(_: str, error_message: str = message) -> None:
                    raise OperationalError(
                        statement=None,
                        params=None,
                        orig=RuntimeError(error_message),
                    )

                asyncio.run(scenario(path, expected_error, fault))
                self.assertEqual(orm_inbox_count(path), 0)

    def test_corrupt_database_maps_stably_at_start(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "connector.sqlite3"
            path.write_bytes(b"not a sqlite database")
            storage = SQLiteStorageComponent(path, config())

            with self.assertRaises(StorageCorrupt) as raised:
                asyncio.run(storage.start())

            self.assertEqual(raised.exception.error_name, "storage_corrupt")


if __name__ == "__main__":
    unittest.main()
