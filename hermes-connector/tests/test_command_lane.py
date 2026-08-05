from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from uuid import UUID

from hermes_connector.adapters.cloud.codec import ConnectorProtocolCodec
from hermes_connector.adapters.sqlite_storage import SQLiteStorageComponent
from hermes_connector.application.command_lane import (
    CommandLane,
    CommandRejected,
    CommandScope,
)
from hermes_connector.bootstrap.config import ConnectorConfig
from hermes_connector.domain.cloud_protocol import CommandDelivery
from hermes_connector.domain.contract_messages import CloudEnvelope
from hermes_connector.domain.control_command import LocalControlFailure
from hermes_connector.domain.storage import StorageOverloaded

ROOT = Path(__file__).resolve().parents[2]
DELIVERY_FIXTURE = ROOT / "contracts/fixtures/valid/command-deliver-payload.json"
NOW = datetime(2026, 7, 30, 8, 0, 1, tzinfo=UTC)


class _Relay:
    def __init__(self) -> None:
        self.calls: list[CommandDelivery] = []

    async def execute(self, command: CommandDelivery) -> MappingProxyType:
        self.calls.append(command)
        return MappingProxyType(
            {
                "status": "accepted",
                "client_request_id": command.client_request_id,
                "client_turn_id": command.params.get("client_turn_id", ""),
                "server_turn_id": "turn-server-09",
            }
        )


class _FailingRelay:
    async def execute(self, _command: CommandDelivery) -> MappingProxyType:
        raise LocalControlFailure("lease_expired")


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


def _envelope(codec: ConnectorProtocolCodec) -> CloudEnvelope:
    payload = json.loads(DELIVERY_FIXTURE.read_text(encoding="utf-8"))
    return CloudEnvelope(
        contract_version=1,
        message_id=UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd"),
        message_type="command.deliver",
        tenant_id="tenant-1",
        device_id="device-1",
        sequence=7,
        sent_at=NOW,
        payload=MappingProxyType(payload),
    )


def _envelope_with_ids(
    codec: ConnectorProtocolCodec,
    *,
    command_id: str,
    message_id: str,
    client_request_id: str,
) -> CloudEnvelope:
    envelope = _envelope(codec)
    return replace(
        envelope,
        message_id=UUID(message_id),
        payload=MappingProxyType(
            {
                **envelope.payload,
                "command_id": command_id,
                "client_request_id": client_request_id,
            }
        ),
    )


def _scope() -> CommandScope:
    return CommandScope(
        tenant_id="tenant-1",
        device_id="device-1",
        connector_instance_id=UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
        profile="default",
        allowed_session_keys=frozenset({"durable-root-1"}),
    )


class CommandLaneTest(unittest.TestCase):
    def test_valid_command_executes_once_and_duplicate_returns_prior_result(
        self,
    ) -> None:
        async def scenario(path: Path) -> None:
            codec = ConnectorProtocolCodec()
            storage, runner = await _start(path)
            relay = _Relay()
            lane = CommandLane(
                storage=storage,
                relay=relay,
                scope=_scope(),
                codec=codec,
                clock=lambda: NOW,
            )

            first = await lane.process(_envelope(codec))
            replay = await lane.process(_envelope(codec))

            self.assertEqual(first.state, "succeeded")
            self.assertEqual(replay, first)
            self.assertEqual(len(relay.calls), 1)
            pending = await lane.pending_cloud_messages(limit=8)
            self.assertEqual(
                [message.message_type for message in pending],
                ["command.receipt", "command.result"],
            )
            result = codec.decode_command_result(pending[1].payload)
            self.assertEqual(result.result["server_turn_id"], "turn-server-09")
            await _stop(storage, runner)

        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(scenario(Path(directory) / "connector.sqlite3"))

    def test_scope_ttl_and_future_issuance_fail_before_persistence(self) -> None:
        async def scenario(path: Path) -> None:
            codec = ConnectorProtocolCodec()
            storage, runner = await _start(path)
            relay = _Relay()
            lane = CommandLane(
                storage=storage,
                relay=relay,
                scope=_scope(),
                codec=codec,
                clock=lambda: NOW,
            )
            baseline = _envelope(codec)
            delivery = codec.decode_command_delivery_payload(baseline.payload)
            mutations = (
                replace(baseline, tenant_id="other"),
                replace(baseline, device_id="other"),
                replace(
                    baseline,
                    payload=MappingProxyType(
                        {
                            **baseline.payload,
                            "connector_instance_id": (
                                "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
                            ),
                        }
                    ),
                ),
                replace(
                    baseline,
                    payload=MappingProxyType({**baseline.payload, "profile": "other"}),
                ),
                replace(
                    baseline,
                    payload=MappingProxyType(
                        {**baseline.payload, "session_key": "unauthorized"}
                    ),
                ),
                replace(
                    baseline,
                    payload=MappingProxyType(
                        {
                            **baseline.payload,
                            "expires_at": "2026-07-30T08:00:01Z",
                        }
                    ),
                ),
                replace(
                    baseline,
                    payload=MappingProxyType(
                        {
                            **baseline.payload,
                            "issued_at": "2026-07-30T08:00:02Z",
                            "expires_at": "2026-07-30T08:05:02Z",
                        }
                    ),
                ),
            )
            self.assertEqual(delivery.session_key, "durable-root-1")
            for mutation in mutations:
                with (
                    self.subTest(mutation=mutation),
                    self.assertRaises(CommandRejected),
                ):
                    await lane.process(mutation)
            self.assertEqual(relay.calls, [])
            self.assertIsNone(
                await storage.get_command("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
            )
            await _stop(storage, runner)

        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(scenario(Path(directory) / "connector.sqlite3"))

    def test_recovery_marks_executing_unknown_without_reexecution(self) -> None:
        async def scenario(path: Path) -> None:
            codec = ConnectorProtocolCodec()
            storage, runner = await _start(path)
            envelope = _envelope(codec)
            delivery = codec.decode_command_delivery_payload(envelope.payload)
            receipt = codec.decode_command_receipt(
                (
                    ROOT / "contracts/fixtures/valid/command-receipt-payload.json"
                ).read_bytes()
            )
            await storage.put_command(
                command_id=str(delivery.command_id),
                message_id=str(envelope.message_id),
                digest="sha256:" + ("a" * 64),
                delivery_payload=b'{"projection":"safe"}',
                receipt_payload=codec.encode_command_receipt(receipt),
                expires_at="2026-07-30T08:05:00Z",
                revision=1,
            )
            await storage.claim_command(str(delivery.command_id))
            relay = _Relay()
            lane = CommandLane(
                storage=storage,
                relay=relay,
                scope=_scope(),
                codec=codec,
                clock=lambda: NOW,
            )

            recovered = await lane.recover_inflight(limit=8)

            self.assertEqual(recovered, 1)
            record = await storage.get_command(str(delivery.command_id))
            self.assertIsNotNone(record)
            assert record is not None and record.result_payload is not None
            self.assertEqual(record.state, "unknown")
            result = codec.decode_command_result(record.result_payload)
            self.assertEqual(result.error["code"], "command_unknown")
            self.assertEqual(relay.calls, [])
            await _stop(storage, runner)

        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(scenario(Path(directory) / "connector.sqlite3"))

    def test_local_numeric_error_is_returned_as_safe_named_error(self) -> None:
        async def scenario(path: Path) -> None:
            codec = ConnectorProtocolCodec()
            storage, runner = await _start(path)
            lane = CommandLane(
                storage=storage,
                relay=_FailingRelay(),
                scope=_scope(),
                codec=codec,
                clock=lambda: NOW,
            )

            record = await lane.process(_envelope(codec))

            assert record.result_payload is not None
            result = codec.decode_command_result(record.result_payload)
            self.assertEqual(result.state, "failed")
            self.assertEqual(result.error["code"], "lease_expired")
            self.assertNotIn("4205", record.result_payload.decode())
            await _stop(storage, runner)

        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(scenario(Path(directory) / "connector.sqlite3"))

    def test_full_unacknowledged_ledger_fails_closed_before_relay(self) -> None:
        async def scenario(path: Path) -> None:
            codec = ConnectorProtocolCodec()
            storage, runner = await _start(
                path,
                ConnectorConfig(command_retention_entries=1),
            )
            relay = _Relay()
            lane = CommandLane(
                storage=storage,
                relay=relay,
                scope=_scope(),
                codec=codec,
                clock=lambda: NOW,
            )
            first = _envelope(codec)
            second = _envelope_with_ids(
                codec,
                command_id="11111111-1111-4111-8111-111111111111",
                message_id="22222222-2222-4222-8222-222222222222",
                client_request_id="req-client-02",
            )

            await lane.process(first)
            with self.assertRaises(StorageOverloaded):
                await lane.process(second)

            self.assertEqual(len(relay.calls), 1)
            self.assertIsNotNone(
                await storage.get_command("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
            )
            self.assertIsNone(
                await storage.get_command("11111111-1111-4111-8111-111111111111")
            )
            await _stop(storage, runner)

        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(scenario(Path(directory) / "connector.sqlite3"))

    def test_full_ledger_prunes_only_acknowledged_terminal_record(self) -> None:
        async def scenario(path: Path) -> None:
            codec = ConnectorProtocolCodec()
            storage, runner = await _start(
                path,
                ConnectorConfig(command_retention_entries=1),
            )
            relay = _Relay()
            lane = CommandLane(
                storage=storage,
                relay=relay,
                scope=_scope(),
                codec=codec,
                clock=lambda: NOW,
            )
            first = await lane.process(_envelope(codec))
            self.assertEqual(first.state, "succeeded")
            self.assertTrue(
                await lane.acknowledge_cloud_message(
                    command_id=first.command_id,
                    message_type="command.receipt",
                )
            )
            self.assertTrue(
                await lane.acknowledge_cloud_message(
                    command_id=first.command_id,
                    message_type="command.result",
                )
            )
            second = _envelope_with_ids(
                codec,
                command_id="11111111-1111-4111-8111-111111111111",
                message_id="22222222-2222-4222-8222-222222222222",
                client_request_id="req-client-02",
            )

            result = await lane.process(second)

            self.assertEqual(result.state, "succeeded")
            self.assertEqual(len(relay.calls), 2)
            self.assertIsNone(
                await storage.get_command("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
            )
            records = await storage.command_records(state=None, limit=8)
            self.assertEqual(
                [record.command_id for record in records],
                ["11111111-1111-4111-8111-111111111111"],
            )
            await _stop(storage, runner)

        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(scenario(Path(directory) / "connector.sqlite3"))

    def test_deferred_session_binding_is_validated_by_local_owner_relay(self) -> None:
        async def scenario(path: Path) -> None:
            codec = ConnectorProtocolCodec()
            storage, runner = await _start(path)
            relay = _Relay()
            scope = replace(_scope(), allowed_session_keys=None)
            lane = CommandLane(
                storage=storage,
                relay=relay,
                scope=scope,
                codec=codec,
                clock=lambda: NOW,
            )

            record = await lane.process(_envelope(codec))

            self.assertEqual(record.state, "succeeded")
            self.assertEqual(len(relay.calls), 1)
            await _stop(storage, runner)

        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(scenario(Path(directory) / "connector.sqlite3"))
