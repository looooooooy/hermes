from __future__ import annotations

import asyncio
import json
import shutil
import ssl
import subprocess
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from uuid import UUID

import pytest
from websockets.asyncio.server import serve

from hermes_connector.adapters.cloud.codec import ConnectorProtocolCodec
from hermes_connector.adapters.cloud.websocket_transport import (
    WebSocketsCloudTransport,
)
from hermes_connector.adapters.sqlite_storage import SQLiteStorageComponent
from hermes_connector.application.cloud_wss_client import CloudClientConfig
from hermes_connector.bootstrap.cloud import build_cloud_wss_client
from hermes_connector.bootstrap.config import ConnectorConfig
from hermes_connector.domain.cloud_protocol import ConnectorWelcome
from hermes_connector.domain.cloud_session import CloudSessionState
from hermes_connector.domain.contract_messages import CloudEnvelope
from hermes_connector.domain.local_gateway import (
    LocalRuntimeAuthority,
    ProcessIdentityEvidence,
)
from hermes_connector.domain.storage import (
    CloudSessionCheckpoint,
    StorageEffectUnknown,
    TransportFrameRecord,
)

NOW = datetime(2026, 7, 30, 12, 0, 0, tzinfo=UTC)


class _TokenProvider:
    async def access_token(self) -> str:
        return "loopback-token"

    async def clear_access_token(self) -> None:
        return None


class _RuntimeAuthority:
    async def current_runtime_authority(self) -> LocalRuntimeAuthority:
        return LocalRuntimeAuthority(
            profile="default",
            runtime_generation="runtime-loopback",
            instance_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            host_bundle_id="com.nousresearch.hermes",
            process_identity=ProcessIdentityEvidence(
                start_time_ns=1_000,
                executable_path=Path("/private/fixture/hermes-python"),
                executable_device=41,
                executable_inode=73,
            ),
            required_capabilities=("session.observe",),
            optional_capabilities=(),
        )


class _LifecycleTokenProvider(_TokenProvider):
    def __init__(self) -> None:
        self.lifecycle_signals: list[str] = []
        self.cleared = False

    async def clear_access_token(self) -> None:
        self.cleared = True

    async def apply_lifecycle_signal(self, signal: str) -> None:
        self.lifecycle_signals.append(signal)
        await self.clear_access_token()


class _Storage:
    def __init__(self) -> None:
        self.cursors = {
            "cloud.connector.outbound": 0,
            "cloud.connector.inbound": 0,
        }
        self.previous_connection_id: str | None = None
        self.transport_epoch_id: str | None = None
        self.runtime_generation: str | None = None
        self.fresh_epoch_required = True
        self.transport_records: list[TransportFrameRecord] = []

    async def get_cursor(self, stream: str) -> int | None:
        return self.cursors.get(stream)

    async def pending_outbox(
        self,
        *,
        limit: int,
        after_sequence: int | None = None,
        stream: str | None = None,
        include_settled: bool = False,
    ) -> tuple:
        return ()

    async def get_cloud_session(self) -> CloudSessionCheckpoint:
        return CloudSessionCheckpoint(
            previous_connection_id=self.previous_connection_id,
            next_outbound_sequence=self.cursors["cloud.connector.outbound"],
            next_inbound_sequence=self.cursors["cloud.connector.inbound"],
            reconciliation_required=False,
            transport_epoch_id=self.transport_epoch_id,
            runtime_generation=self.runtime_generation,
            fresh_epoch_required=self.fresh_epoch_required,
        )

    async def begin_transport_epoch(
        self,
        *,
        epoch_id: str,
        runtime_generation: str,
        previous_connection_id: str | None,
        next_outbound_sequence: int,
        next_inbound_sequence: int,
    ) -> CloudSessionCheckpoint:
        if epoch_id != self.transport_epoch_id:
            self.transport_records = [
                replace(record, state="retired") for record in self.transport_records
            ]
        self.transport_epoch_id = epoch_id
        self.runtime_generation = runtime_generation
        self.previous_connection_id = previous_connection_id
        self.fresh_epoch_required = False
        self.cursors["cloud.connector.outbound"] = next_outbound_sequence
        self.cursors["cloud.connector.inbound"] = next_inbound_sequence
        return await self.get_cloud_session()

    async def commit_transport_handshake(
        self,
        *,
        epoch_id: str,
        previous_connection_id: str,
        next_outbound_sequence: int,
        next_inbound_sequence: int,
    ) -> CloudSessionCheckpoint:
        assert epoch_id == self.transport_epoch_id
        self.previous_connection_id = previous_connection_id
        self.cursors["cloud.connector.outbound"] = next_outbound_sequence
        self.cursors["cloud.connector.inbound"] = next_inbound_sequence
        return await self.get_cloud_session()

    async def reconcile_transport_epoch(
        self,
        *,
        epoch_id: str,
        previous_connection_id: str,
        next_outbound_sequence: int,
        next_inbound_sequence: int,
    ) -> CloudSessionCheckpoint:
        assert epoch_id == self.transport_epoch_id
        self.previous_connection_id = previous_connection_id
        self.cursors["cloud.connector.outbound"] = next_outbound_sequence
        self.cursors["cloud.connector.inbound"] = next_inbound_sequence
        return await self.get_cloud_session()

    async def stage_transport_frame(self, **values: object) -> TransportFrameRecord:
        record = TransportFrameRecord(
            message_id=str(values["message_id"]),
            epoch_id=str(values["epoch_id"]),
            sequence=int(values["sequence"]),
            message_type=str(values["message_type"]),
            business_kind=str(values["business_kind"]),
            business_key=str(values["business_key"]),
            business_revision=int(values["business_revision"]),
            runtime_generation=str(values["runtime_generation"]),
            frame=bytes(values["frame"]),
            state="staged",
            created_at="now",
            updated_at="now",
            settled_at=None,
        )
        self.transport_records.append(record)
        return record

    async def mark_transport_sent(
        self,
        *,
        epoch_id: str,
        sequence: int,
    ) -> TransportFrameRecord:
        for index, record in enumerate(self.transport_records):
            if record.epoch_id == epoch_id and record.sequence == sequence:
                sent = replace(record, state="sent")
                self.transport_records[index] = sent
                self.cursors["cloud.connector.outbound"] += 1
                return sent
        raise AssertionError("staged transport frame is missing")

    async def pending_transport_frames(
        self,
        *,
        epoch_id: str,
        limit: int,
        after_sequence: int | None = None,
    ) -> tuple[TransportFrameRecord, ...]:
        return tuple(
            record
            for record in self.transport_records
            if record.epoch_id == epoch_id
            and record.state in {"staged", "sent"}
            and (after_sequence is None or record.sequence > after_sequence)
        )[:limit]

    async def settle_transport_cursor(
        self,
        *,
        epoch_id: str,
        next_sequence: int,
    ) -> tuple[TransportFrameRecord, ...]:
        settled: list[TransportFrameRecord] = []
        for index, record in enumerate(self.transport_records):
            if record.epoch_id == epoch_id and record.sequence < next_sequence:
                changed = replace(record, state="settled")
                self.transport_records[index] = changed
                settled.append(changed)
        return tuple(settled)

    async def advance_cloud_outbound(self, expected_sequence: int) -> int:
        assert self.cursors["cloud.connector.outbound"] == expected_sequence
        self.cursors["cloud.connector.outbound"] += 1
        return self.cursors["cloud.connector.outbound"]

    async def advance_cloud_inbound(self, expected_sequence: int) -> int:
        assert self.cursors["cloud.connector.inbound"] == expected_sequence
        self.cursors["cloud.connector.inbound"] += 1
        return self.cursors["cloud.connector.inbound"]

    async def complete_cloud_reconciliation(
        self,
        *,
        previous_connection_id: str,
    ) -> CloudSessionCheckpoint:
        return await self.get_cloud_session()


@pytest.mark.asyncio
async def test_real_websocket_loopback_negotiates_and_sends_heartbeat() -> None:
    codec = ConnectorProtocolCodec()
    result: asyncio.Future[tuple[str, str]] = asyncio.get_running_loop().create_future()

    async def handler(connection) -> None:
        try:
            assert connection.request.headers["Authorization"] == (
                "Bearer loopback-token"
            )
            hello_frame = await connection.recv()
            assert isinstance(hello_frame, str)
            hello = codec.decode_envelope(hello_frame.encode("utf-8", errors="strict"))
            assert hello.message_type == "connector.hello"
            decoded_hello = codec.decode_hello_payload(hello.payload)

            welcome = ConnectorWelcome(
                connection_id=UUID("22222222-2222-4222-8222-222222222222"),
                server_generation="cloud-loopback",
                server_time=NOW,
                accepted_capabilities=("session.observe",),
                unavailable_optional_capabilities=(),
                resume_decision="fresh",
                next_connector_sequence=0,
                next_cloud_sequence=0,
                heartbeat_interval_ms=20_000,
                max_in_flight=8,
            )
            await connection.send(
                codec.encode_envelope(
                    CloudEnvelope(
                        contract_version=1,
                        message_id=UUID("33333333-3333-4333-8333-333333333333"),
                        message_type="connector.welcome",
                        tenant_id=hello.tenant_id,
                        device_id=hello.device_id,
                        sequence=0,
                        sent_at=NOW,
                        payload=MappingProxyType(
                            json.loads(codec.encode_welcome(welcome))
                        ),
                    )
                ).decode("utf-8", errors="strict")
            )

            heartbeat_frame = await connection.recv()
            assert isinstance(heartbeat_frame, str)
            heartbeat_envelope = codec.decode_envelope(
                heartbeat_frame.encode("utf-8", errors="strict")
            )
            heartbeat = codec.decode_heartbeat_payload(heartbeat_envelope.payload)
            result.set_result(
                (
                    decoded_hello.connector_version,
                    heartbeat.session_state,
                )
            )
        except (AssertionError, ConnectionError, TypeError, ValueError) as exc:
            if not result.done():
                result.set_exception(exc)

    async with serve(
        handler,
        "127.0.0.1",
        0,
        subprotocols=("hermes.connector.v1",),
    ) as server:
        port = server.sockets[0].getsockname()[1]
        client = build_cloud_wss_client(
            config=CloudClientConfig(
                endpoint=f"ws://127.0.0.1:{port}/connector",
                tenant_id="tenant-loopback",
                device_id="device-loopback",
                connector_instance_id=UUID("11111111-1111-4111-8111-111111111111"),
                connector_version="1.0.0",
            ),
            token_provider=_TokenProvider(),
            storage=_Storage(),
            runtime_authority=_RuntimeAuthority(),
            utc_now=lambda: NOW,
            message_id_factory=lambda: UUID("44444444-4444-4444-8444-444444444444"),
        )

        await client.start()
        assert client.state is CloudSessionState.ACTIVE
        await client.send_heartbeat()

        assert await asyncio.wait_for(result, timeout=2) == ("1.0.0", "active")
        await client.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reason", "expected_signal", "terminal"),
    (
        ("device_authorization_revoked", "revoked", True),
        ("device_authorization_suspended", "suspended", True),
        ("device_authorization_unknown", None, False),
    ),
)
async def test_run_handles_real_websocket_policy_close_and_reconnect_policy(
    reason: str,
    expected_signal: str | None,
    terminal: bool,
) -> None:
    codec = ConnectorProtocolCodec()
    connection_count = 0
    reconnected = asyncio.Event()
    server_errors: list[BaseException] = []

    async def handler(connection) -> None:
        nonlocal connection_count
        connection_count += 1
        current_connection = connection_count
        try:
            hello_frame = await connection.recv()
            assert isinstance(hello_frame, str)
            hello_envelope = codec.decode_envelope(
                hello_frame.encode("utf-8", errors="strict")
            )
            hello = codec.decode_hello_payload(hello_envelope.payload)
            welcome = ConnectorWelcome(
                connection_id=UUID("22222222-2222-4222-8222-222222222222"),
                server_generation="cloud-loopback",
                server_time=NOW,
                accepted_capabilities=("session.observe",),
                unavailable_optional_capabilities=(),
                resume_decision="fresh",
                next_connector_sequence=hello.resume.next_outbound_sequence,
                next_cloud_sequence=hello.resume.next_inbound_sequence,
                heartbeat_interval_ms=20_000,
                max_in_flight=8,
            )
            await connection.send(
                codec.encode_envelope(
                    CloudEnvelope(
                        contract_version=1,
                        message_id=UUID("33333333-3333-4333-8333-333333333333"),
                        message_type="connector.welcome",
                        tenant_id=hello_envelope.tenant_id,
                        device_id=hello_envelope.device_id,
                        sequence=hello.resume.next_inbound_sequence,
                        sent_at=NOW,
                        payload=MappingProxyType(
                            json.loads(codec.encode_welcome(welcome))
                        ),
                    )
                ).decode("utf-8", errors="strict")
            )
            if current_connection == 1:
                await connection.close(
                    code=1008,
                    reason=reason,
                )
                return
            reconnected.set()
            await connection.wait_closed()
        except BaseException as error:
            server_errors.append(error)
            raise

    async with serve(
        handler,
        "127.0.0.1",
        0,
        subprotocols=("hermes.connector.v1",),
    ) as server:
        port = server.sockets[0].getsockname()[1]
        token_provider = _LifecycleTokenProvider()
        client = build_cloud_wss_client(
            config=CloudClientConfig(
                endpoint=f"ws://127.0.0.1:{port}/connector",
                tenant_id="tenant-loopback",
                device_id="device-loopback",
                connector_instance_id=UUID("11111111-1111-4111-8111-111111111111"),
                connector_version="1.0.0",
            ),
            token_provider=token_provider,
            storage=_Storage(),
            runtime_authority=_RuntimeAuthority(),
            utc_now=lambda: NOW,
            message_id_factory=lambda: UUID("44444444-4444-4444-8444-444444444444"),
            sleep=lambda _delay: asyncio.sleep(0),
        )

        await client.start()
        run_task = asyncio.create_task(client.run())
        if terminal:
            await asyncio.wait_for(run_task, timeout=2)
            assert client.state is CloudSessionState.DISCONNECTED
            assert client.reconnect_allowed is False
            assert connection_count == 1
            assert token_provider.lifecycle_signals == [expected_signal]
            assert token_provider.cleared is True
        else:
            await asyncio.wait_for(reconnected.wait(), timeout=2)
            for _ in range(200):
                if client.state is CloudSessionState.ACTIVE:
                    break
                await asyncio.sleep(0.01)
            assert run_task.done() is False
            assert client.state is CloudSessionState.ACTIVE
            assert client.reconnect_allowed is True
            assert connection_count == 2
            assert token_provider.lifecycle_signals == []
            assert token_provider.cleared is False
            await client.stop()
            await asyncio.wait_for(run_task, timeout=2)
        assert server_errors == []


@pytest.mark.asyncio
async def test_real_websocket_replays_exact_frame_after_post_send_precommit_crash(
    tmp_path: Path,
) -> None:
    codec = ConnectorProtocolCodec()
    received: list[bytes] = []
    connections = 0

    async def handler(connection) -> None:
        nonlocal connections
        connections += 1
        hello_raw = await connection.recv()
        assert isinstance(hello_raw, str)
        hello_envelope = codec.decode_envelope(hello_raw.encode())
        hello = codec.decode_hello_payload(hello_envelope.payload)
        welcome = ConnectorWelcome(
            connection_id=UUID(f"22222222-2222-4222-8222-{connections:012d}"),
            server_generation="cloud-loopback",
            server_time=NOW,
            accepted_capabilities=("session.observe",),
            unavailable_optional_capabilities=(),
            resume_decision="fresh" if connections == 1 else "reset_required",
            next_connector_sequence=0,
            next_cloud_sequence=0,
            heartbeat_interval_ms=20_000,
            max_in_flight=8,
        )
        await connection.send(
            codec.encode_envelope(
                CloudEnvelope(
                    contract_version=1,
                    message_id=UUID(f"33333333-3333-4333-8333-{connections:012d}"),
                    message_type="connector.welcome",
                    tenant_id=hello_envelope.tenant_id,
                    device_id=hello_envelope.device_id,
                    sequence=hello.resume.next_inbound_sequence,
                    sent_at=NOW,
                    payload=MappingProxyType(json.loads(codec.encode_welcome(welcome))),
                )
            ).decode()
        )
        frame = await connection.recv()
        assert isinstance(frame, str)
        received.append(frame.encode())

    fail_mark_sent = True

    def post_send_fault(operation: str) -> None:
        nonlocal fail_mark_sent
        if operation == "mark_transport_sent" and fail_mark_sent:
            fail_mark_sent = False
            raise RuntimeError("post-send precommit crash")

    path = tmp_path / "connector.sqlite3"
    config = replace(
        ConnectorConfig(),
        storage_write_deadline_seconds=0.1,
    )
    first_storage = SQLiteStorageComponent(
        path,
        config,
        write_fault=post_send_fault,
    )
    await first_storage.start()
    first_runner = asyncio.create_task(first_storage.run())
    assert await first_storage.ready()

    async with serve(
        handler,
        "127.0.0.1",
        0,
        subprotocols=("hermes.connector.v1",),
    ) as server:
        port = server.sockets[0].getsockname()[1]

        def build(storage: SQLiteStorageComponent):
            return build_cloud_wss_client(
                config=CloudClientConfig(
                    endpoint=f"ws://127.0.0.1:{port}/connector",
                    tenant_id="tenant-loopback",
                    device_id="device-loopback",
                    connector_instance_id=UUID("11111111-1111-4111-8111-111111111111"),
                    connector_version="1.0.0",
                ),
                token_provider=_TokenProvider(),
                storage=storage,
                runtime_authority=_RuntimeAuthority(),
                utc_now=lambda: NOW,
                message_id_factory=lambda: UUID("44444444-4444-4444-8444-444444444444"),
            )

        first = build(first_storage)
        await first.start()
        with pytest.raises(StorageEffectUnknown):
            await first.send_heartbeat()
        await first.stop()
        with pytest.raises(RuntimeError, match="post-send precommit crash"):
            await first_runner
        await first_storage.stop()

        second_storage = SQLiteStorageComponent(path, config)
        await second_storage.start()
        second_runner = asyncio.create_task(second_storage.run())
        assert await second_storage.ready()
        second = build(second_storage)
        await second.start()

        assert len(received) == 2
        assert received[1] == received[0]
        replay = codec.decode_envelope(received[1])
        assert replay.message_type == "connector.heartbeat"
        assert replay.sequence == 0

        await second.stop()
        await second_storage.drain()
        await second_storage.stop()
        await second_runner


def _generate_test_certificate(directory: Path) -> tuple[Path, Path]:
    openssl = shutil.which("openssl")
    if openssl is None:
        pytest.skip("OpenSSL is required for the real TLS loopback test")
    certificate = directory / "localhost.crt"
    private_key = directory / "localhost.key"
    subprocess.run(
        (
            openssl,
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(private_key),
            "-out",
            str(certificate),
            "-days",
            "1",
            "-subj",
            "/CN=localhost",
            "-addext",
            "subjectAltName=DNS:localhost,IP:127.0.0.1",
        ),
        check=True,
        capture_output=True,
    )
    return certificate, private_key


@pytest.mark.asyncio
async def test_real_wss_loopback_validates_test_ca_and_subprotocol(
    tmp_path: Path,
) -> None:
    certificate, private_key = _generate_test_certificate(tmp_path)
    server_tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_tls.load_cert_chain(certificate, private_key)
    client_tls = ssl.create_default_context(cafile=str(certificate))
    received_authorization: asyncio.Future[str] = (
        asyncio.get_running_loop().create_future()
    )

    async def handler(connection) -> None:
        received_authorization.set_result(connection.request.headers["Authorization"])
        assert await connection.recv() == "tls-probe"
        await connection.send("tls-ack")

    async with serve(
        handler,
        "127.0.0.1",
        0,
        ssl=server_tls,
        subprotocols=("hermes.connector.v1",),
    ) as server:
        port = server.sockets[0].getsockname()[1]
        transport = WebSocketsCloudTransport(ssl_context=client_tls)
        connection = await transport.connect(
            f"wss://localhost:{port}/connector",
            token="tls-loopback-token",
        )
        await connection.send(b"tls-probe", timeout_seconds=2)
        assert await connection.receive(timeout_seconds=2) == b"tls-ack"
        assert await asyncio.wait_for(received_authorization, timeout=2) == (
            "Bearer tls-loopback-token"
        )
        await connection.close(code=1000, reason="", timeout_seconds=2)
