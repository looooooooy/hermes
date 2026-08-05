from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from hermes_connector.adapters.persistence.sqlite.repositories import control_command
from hermes_connector.adapters.sqlite_storage import SQLiteStorageComponent
from hermes_connector.bootstrap.config import ConnectorConfig
from hermes_connector.domain.storage import IdempotencyConflict, StorageOverloaded

DIGEST_FIRST = "sha256:" + ("a" * 64)
DIGEST_SECOND = "sha256:" + ("b" * 64)
DIGEST_DIFFERENT = "sha256:" + ("c" * 64)


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


class CommandStorageTest(unittest.TestCase):
    def test_invalid_command_boundary_does_not_poison_following_write(self) -> None:
        async def scenario(path: Path) -> None:
            storage, runner = await _start(path)
            with self.assertRaises(ValueError):
                await storage.put_command(
                    command_id="not-a-uuid",
                    message_id="dddddddd-dddd-4ddd-8ddd-dddddddddddd",
                    digest="sha256:not-hex",
                    delivery_payload=b"{}",
                    receipt_payload=b"{}",
                    expires_at="not-an-instant",
                    revision=1,
                )
            committed = await storage.put_command(
                command_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                message_id="dddddddd-dddd-4ddd-8ddd-dddddddddddd",
                digest="sha256:" + ("a" * 64),
                delivery_payload=b"{}",
                receipt_payload=b"{}",
                expires_at="2026-07-30T08:05:00Z",
                revision=1,
            )
            self.assertTrue(committed.inserted)
            self.assertFalse(runner.done())
            await _stop(storage, runner)

        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(scenario(Path(directory) / "connector.sqlite3"))

    def test_pending_command_query_is_sql_bounded(self) -> None:
        class Scalars:
            def all(self) -> list[object]:
                return []

        class InspectingSession:
            def scalars(self, statement: object) -> Scalars:
                self.assert_has_limit = statement._limit_clause is not None
                return Scalars()

        session = InspectingSession()
        self.assertEqual(control_command.pending_messages(session, limit=3), ())
        self.assertTrue(session.assert_has_limit)

    def test_command_is_idempotent_and_claimed_once(self) -> None:
        async def scenario(path: Path) -> None:
            storage, runner = await _start(path)

            first = await storage.put_command(
                command_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                message_id="dddddddd-dddd-4ddd-8ddd-dddddddddddd",
                digest=DIGEST_FIRST,
                delivery_payload=b'{"method":"prompt.submit"}',
                receipt_payload=b'{"state":"delivered"}',
                expires_at="2026-07-30T08:05:00Z",
                revision=1,
            )
            replay = await storage.put_command(
                command_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                message_id="dddddddd-dddd-4ddd-8ddd-dddddddddddd",
                digest=DIGEST_FIRST,
                delivery_payload=b'{"must":"not-replace-original"}',
                receipt_payload=b'{"must":"not-replace-original"}',
                expires_at="2026-07-30T08:05:00Z",
                revision=1,
            )

            self.assertTrue(first.inserted)
            self.assertFalse(replay.inserted)
            self.assertEqual(
                replay.record.delivery_payload, first.record.delivery_payload
            )
            self.assertTrue(await storage.claim_command(first.record.command_id))
            self.assertFalse(await storage.claim_command(first.record.command_id))
            with self.assertRaises(IdempotencyConflict):
                await storage.put_command(
                    command_id=first.record.command_id,
                    message_id=first.record.message_id,
                    digest=DIGEST_DIFFERENT,
                    delivery_payload=b"{}",
                    receipt_payload=b"{}",
                    expires_at="2026-07-30T08:05:00Z",
                    revision=1,
                )
            await _stop(storage, runner)

        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(scenario(Path(directory) / "connector.sqlite3"))

    def test_pending_receipt_and_result_survive_restart_and_prune_after_ack(
        self,
    ) -> None:
        async def scenario(path: Path) -> None:
            storage, runner = await _start(path)
            command_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
            await storage.put_command(
                command_id=command_id,
                message_id="dddddddd-dddd-4ddd-8ddd-dddddddddddd",
                digest=DIGEST_FIRST,
                delivery_payload=b'{"method":"session.interrupt"}',
                receipt_payload=b'{"state":"delivered"}',
                expires_at="2026-07-30T08:00:10Z",
                revision=1,
            )
            self.assertTrue(await storage.claim_command(command_id))
            completed = await storage.complete_command(
                command_id=command_id,
                state="succeeded",
                result_payload=b'{"state":"succeeded"}',
                revision=2,
            )
            self.assertEqual(completed.state, "succeeded")
            await _stop(storage, runner)

            reopened, reopened_runner = await _start(path)
            pending = await reopened.pending_command_messages(limit=8)
            self.assertEqual(
                [(item.message_type, item.payload) for item in pending],
                [
                    ("command.receipt", b'{"state":"delivered"}'),
                    ("command.result", b'{"state":"succeeded"}'),
                ],
            )
            self.assertTrue(
                await reopened.ack_command_message(
                    command_id=command_id,
                    message_type="command.receipt",
                )
            )
            self.assertTrue(
                await reopened.ack_command_message(
                    command_id=command_id,
                    message_type="command.result",
                )
            )
            self.assertEqual(
                await reopened.prune_commands(
                    completed_before="9999-12-31T23:59:59Z",
                    limit=1,
                ),
                1,
            )
            self.assertIsNone(await reopened.get_command(command_id))
            await _stop(reopened, reopened_runner)

        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(scenario(Path(directory) / "connector.sqlite3"))

    def test_pending_command_messages_use_stable_keyset_across_more_than_two_pages(
        self,
    ) -> None:
        async def scenario(path: Path) -> None:
            storage, runner = await _start(path)
            for index in range(1, 4):
                command_id = f"aaaaaaaa-aaaa-4aaa-8aaa-{index:012d}"
                await storage.put_command(
                    command_id=command_id,
                    message_id=f"dddddddd-dddd-4ddd-8ddd-{index:012d}",
                    digest="sha256:" + (f"{index:x}" * 64)[:64],
                    delivery_payload=b"{}",
                    receipt_payload=f'{{"receipt":{index}}}'.encode(),
                    expires_at="2026-07-30T08:05:00Z",
                    revision=1,
                )
                await storage.claim_command(command_id)
                await storage.complete_command(
                    command_id=command_id,
                    state="succeeded",
                    result_payload=f'{{"result":{index}}}'.encode(),
                    revision=2,
                )

            messages = []
            cursor: tuple[str, str, str] | None = None
            for _ in range(4):
                page = await storage.pending_command_messages(
                    limit=2,
                    after_created_at=cursor[0] if cursor else None,
                    after_command_id=cursor[1] if cursor else None,
                    after_message_type=cursor[2] if cursor else None,
                )
                messages.extend(page)
                if not page:
                    break
                last = page[-1]
                cursor = (last.created_at, last.command_id, last.message_type)

            self.assertEqual(len(messages), 6)
            self.assertEqual(
                [(message.command_id, message.message_type) for message in messages],
                [
                    (f"aaaaaaaa-aaaa-4aaa-8aaa-{index:012d}", message_type)
                    for index in range(1, 4)
                    for message_type in ("command.receipt", "command.result")
                ],
            )
            await _stop(storage, runner)

        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(scenario(Path(directory) / "connector.sqlite3"))

    def test_executing_commands_are_queryable_for_unknown_recovery(self) -> None:
        async def scenario(path: Path) -> None:
            storage, runner = await _start(path)
            command_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
            await storage.put_command(
                command_id=command_id,
                message_id="dddddddd-dddd-4ddd-8ddd-dddddddddddd",
                digest=DIGEST_FIRST,
                delivery_payload=b"{}",
                receipt_payload=b"{}",
                expires_at="2026-07-30T08:05:00Z",
                revision=1,
            )
            await storage.claim_command(command_id)

            executing = await storage.command_records(state="executing", limit=8)

            self.assertEqual([record.command_id for record in executing], [command_id])
            await _stop(storage, runner)

        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(scenario(Path(directory) / "connector.sqlite3"))

    def test_command_retention_is_bounded_without_dropping_pending_messages(
        self,
    ) -> None:
        async def scenario(path: Path) -> None:
            storage = SQLiteStorageComponent(
                path,
                ConnectorConfig(
                    bounded_queue_items=1,
                    command_retention_entries=1,
                ),
            )
            await storage.start()
            runner = asyncio.create_task(storage.run())
            assert await storage.ready()
            first_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
            await storage.put_command(
                command_id=first_id,
                message_id="dddddddd-dddd-4ddd-8ddd-dddddddddddd",
                digest=DIGEST_FIRST,
                delivery_payload=b"{}",
                receipt_payload=b"{}",
                expires_at="2026-07-30T08:05:00Z",
                revision=1,
            )
            with self.assertRaises(StorageOverloaded):
                await storage.put_command(
                    command_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                    message_id="eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
                    digest=DIGEST_SECOND,
                    delivery_payload=b"{}",
                    receipt_payload=b"{}",
                    expires_at="2026-07-30T08:05:00Z",
                    revision=1,
                )

            await storage.claim_command(first_id)
            await storage.complete_command(
                command_id=first_id,
                state="succeeded",
                result_payload=b"{}",
                revision=2,
            )
            pending = await storage.pending_command_messages(limit=2)
            self.assertEqual(
                [message.message_type for message in pending],
                ["command.receipt", "command.result"],
            )
            await storage.ack_command_message(
                command_id=first_id,
                message_type="command.receipt",
            )
            await storage.ack_command_message(
                command_id=first_id,
                message_type="command.result",
            )
            second_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
            await storage.put_command(
                command_id=second_id,
                message_id="eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
                digest=DIGEST_SECOND,
                delivery_payload=b"{}",
                receipt_payload=b"{}",
                expires_at="2026-07-30T08:05:00Z",
                revision=1,
            )

            self.assertIsNone(await storage.get_command(first_id))
            self.assertIsNotNone(await storage.get_command(second_id))
            await _stop(storage, runner)

        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(scenario(Path(directory) / "connector.sqlite3"))
