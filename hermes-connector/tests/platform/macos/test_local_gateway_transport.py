from __future__ import annotations

import asyncio
import os
import socket
import struct
import tempfile
import threading
import unittest
from contextlib import suppress
from pathlib import Path
from uuid import UUID

from hermes_connector.adapters.contract_codec import (
    FrameTooLarge,
    InvalidEnvelope,
)
from hermes_connector.adapters.platform.macos.local_gateway_transport import (
    MAX_LOCAL_BODY_BYTES,
    MacOSLocalGatewayTransport,
)
from hermes_connector.adapters.platform.macos.process_identity import (
    current_process_identity,
)
from hermes_connector.application.local_gateway_client import (
    LocalDeadlineExceeded,
    LocalGatewayClient,
)
from hermes_connector.bootstrap.config import ConnectorConfig
from hermes_connector.domain.local_gateway import (
    AgentEndpoint,
    ProcessIdentityEvidence,
)

INSTANCE_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
REQUEST = b'{"contract_version":1,"message_type":"local.hello"}'
RESPONSE = b'{"contract_version":1,"message_type":"local.welcome"}'


def endpoint(socket_path: Path, *, pid: int | None = None) -> AgentEndpoint:
    process_identity = current_process_identity(os.getpid())
    assert process_identity is not None
    socket_metadata = socket_path.lstat()
    return AgentEndpoint(
        pid=os.getpid() if pid is None else pid,
        profile="default",
        socket_path=socket_path,
        instance_id=INSTANCE_ID,
        runtime_generation="runtime-generation-1",
        host_bundle_id="com.nousresearch.hermes",
        process_identity=process_identity,
        socket_device=socket_metadata.st_dev,
        socket_inode=socket_metadata.st_ino,
        registry_path=socket_path.with_suffix(".json"),
    )


class StaticDiscovery:
    def __init__(self, value: AgentEndpoint) -> None:
        self.value = value

    async def discover(self, profile: str) -> tuple[AgentEndpoint, ...]:
        return (self.value,) if profile == self.value.profile else ()

    async def aclose(self) -> None:
        return None


class NoopSessionState:
    async def invalidate_runtime(
        self,
        previous_generation: str,
        current_generation: str,
    ) -> None:
        raise AssertionError("unexpected generation change")


async def read_frame(reader: asyncio.StreamReader) -> bytes:
    prefix = await reader.readexactly(4)
    length = struct.unpack(">I", prefix)[0]
    return await reader.readexactly(length)


class MacOSLocalGatewayTransportTest(unittest.TestCase):
    def test_same_uid_replacement_socket_with_same_process_pid_is_rejected(
        self,
    ) -> None:
        async def scenario(root: Path) -> None:
            socket_path = root / "gateway.sock"
            original = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            original.bind(str(socket_path))
            os.chmod(socket_path, 0o600)
            gateway_endpoint = endpoint(socket_path)
            socket_path.unlink()
            replacement = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            replacement.bind(str(socket_path))
            os.chmod(socket_path, 0o600)
            try:
                with self.assertRaises(InvalidEnvelope):
                    await MacOSLocalGatewayTransport().connect(gateway_endpoint)
            finally:
                replacement.close()
                original.close()

        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(scenario(Path(directory)))

    def test_preflight_peer_proof_connects_and_closes_without_sending_a_frame(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            socket_path = Path(directory) / "gateway.sock"
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(str(socket_path))
            listener.listen(1)
            os.chmod(socket_path, 0o600)
            received: list[bytes] = []

            def serve() -> None:
                connection, _ = listener.accept()
                with connection:
                    received.append(connection.recv(1))

            server = threading.Thread(target=serve)
            server.start()
            try:
                MacOSLocalGatewayTransport().probe_peer(
                    endpoint(socket_path),
                    timeout_seconds=0.1,
                )
                server.join(timeout=1.0)
                self.assertFalse(server.is_alive())
                self.assertEqual(received, [b""])
            finally:
                listener.close()

    def test_same_numeric_peer_pid_with_reused_process_identity_is_rejected(
        self,
    ) -> None:
        async def scenario(root: Path) -> None:
            socket_path = root / "gateway.sock"

            async def handle(
                reader: asyncio.StreamReader,
                writer: asyncio.StreamWriter,
            ) -> None:
                await reader.read()
                writer.close()
                await writer.wait_closed()

            server = await asyncio.start_unix_server(handle, path=socket_path)
            os.chmod(socket_path, 0o600)
            gateway_endpoint = endpoint(socket_path)
            replaced = ProcessIdentityEvidence(
                start_time_ns=gateway_endpoint.process_identity.start_time_ns + 1,
                executable_path=gateway_endpoint.process_identity.executable_path,
                executable_device=gateway_endpoint.process_identity.executable_device,
                executable_inode=gateway_endpoint.process_identity.executable_inode,
            )
            observed = iter((gateway_endpoint.process_identity, replaced))
            try:
                with self.assertRaises(InvalidEnvelope):
                    await MacOSLocalGatewayTransport(
                        process_identity_provider=lambda _: next(observed)
                    ).connect(gateway_endpoint)
            finally:
                server.close()
                await server.wait_closed()

        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(scenario(Path(directory)))

    def test_connected_peer_pid_must_match_descriptor_publisher(self) -> None:
        async def scenario(root: Path) -> None:
            socket_path = root / "gateway.sock"

            async def handle(
                reader: asyncio.StreamReader,
                writer: asyncio.StreamWriter,
            ) -> None:
                await reader.read()
                writer.close()
                await writer.wait_closed()

            server = await asyncio.start_unix_server(handle, path=socket_path)
            os.chmod(socket_path, 0o600)
            try:
                with self.assertRaises(InvalidEnvelope) as raised:
                    gateway_endpoint = endpoint(socket_path, pid=os.getpid() + 1)
                    await MacOSLocalGatewayTransport(
                        process_identity_provider=lambda _: (
                            gateway_endpoint.process_identity
                        )
                    ).connect(gateway_endpoint)
                self.assertEqual(raised.exception.code, 4301)
            finally:
                server.close()
                await server.wait_closed()

        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(scenario(Path(directory)))

    def test_real_unix_socket_uses_frozen_length_prefixed_single_exchange(
        self,
    ) -> None:
        async def scenario(root: Path) -> None:
            socket_path = root / "gateway.sock"
            received: list[bytes] = []

            async def handle(
                reader: asyncio.StreamReader,
                writer: asyncio.StreamWriter,
            ) -> None:
                received.append(await read_frame(reader))
                writer.write(struct.pack(">I", len(RESPONSE)) + RESPONSE)
                await writer.drain()
                writer.close()
                await writer.wait_closed()

            server = await asyncio.start_unix_server(handle, path=socket_path)
            os.chmod(socket_path, 0o600)
            try:
                connection = await MacOSLocalGatewayTransport().connect(
                    endpoint(socket_path)
                )
                self.assertEqual(await connection.exchange(REQUEST), RESPONSE)
                await connection.close()
                await connection.close()

                self.assertEqual(received, [REQUEST])
                with self.assertRaises(InvalidEnvelope) as raised:
                    await connection.exchange(REQUEST)
                self.assertEqual(raised.exception.code, 4301)
            finally:
                server.close()
                await server.wait_closed()

        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(scenario(Path(directory)))

    def test_rejects_zero_oversized_and_truncated_response_frames(self) -> None:
        async def scenario(root: Path) -> None:
            cases = (
                (struct.pack(">I", 0), InvalidEnvelope, 4301),
                (
                    struct.pack(">I", MAX_LOCAL_BODY_BYTES + 1),
                    FrameTooLarge,
                    4302,
                ),
                (struct.pack(">I", 8) + b"{}", InvalidEnvelope, 4301),
            )
            for index, (response, error_type, error_code) in enumerate(cases):
                socket_path = root / f"gateway-{index}.sock"

                async def handle(
                    reader: asyncio.StreamReader,
                    writer: asyncio.StreamWriter,
                    response: bytes = response,
                ) -> None:
                    await read_frame(reader)
                    writer.write(response)
                    await writer.drain()
                    writer.close()
                    await writer.wait_closed()

                server = await asyncio.start_unix_server(handle, path=socket_path)
                os.chmod(socket_path, 0o600)
                try:
                    connection = await MacOSLocalGatewayTransport().connect(
                        endpoint(socket_path)
                    )
                    with self.assertRaises(error_type) as raised:
                        await connection.exchange(REQUEST)
                    self.assertEqual(raised.exception.code, error_code)
                    await connection.close()
                finally:
                    server.close()
                    await server.wait_closed()

        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(scenario(Path(directory)))

    def test_cancelled_read_closes_real_unix_connection(self) -> None:
        async def scenario(root: Path) -> None:
            socket_path = root / "gateway.sock"
            peer_closed = asyncio.Event()

            async def handle(
                reader: asyncio.StreamReader,
                writer: asyncio.StreamWriter,
            ) -> None:
                await read_frame(reader)
                await reader.read()
                peer_closed.set()
                writer.close()
                await writer.wait_closed()

            server = await asyncio.start_unix_server(handle, path=socket_path)
            os.chmod(socket_path, 0o600)
            try:
                connection = await MacOSLocalGatewayTransport().connect(
                    endpoint(socket_path)
                )
                exchange = asyncio.create_task(connection.exchange(REQUEST))
                await asyncio.sleep(0)
                exchange.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await exchange
                await asyncio.wait_for(peer_closed.wait(), timeout=1.0)
            finally:
                server.close()
                await server.wait_closed()

        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(scenario(Path(directory)))

    def test_real_unix_stalled_response_obeys_client_deadline_and_closes(
        self,
    ) -> None:
        async def scenario(root: Path) -> None:
            socket_path = root / "gateway.sock"
            peer_closed = asyncio.Event()

            async def handle(
                reader: asyncio.StreamReader,
                writer: asyncio.StreamWriter,
            ) -> None:
                await read_frame(reader)
                await reader.read()
                peer_closed.set()
                writer.close()
                await writer.wait_closed()

            server = await asyncio.start_unix_server(handle, path=socket_path)
            os.chmod(socket_path, 0o600)
            gateway_endpoint = endpoint(socket_path)
            gateway = LocalGatewayClient(
                profile="default",
                client_instance_id=UUID("11111111-1111-4111-8111-111111111111"),
                required_capabilities=("session.observe",),
                optional_capabilities=(),
                discovery=StaticDiscovery(gateway_endpoint),
                transport=MacOSLocalGatewayTransport(),
                session_state=NoopSessionState(),
                config=ConnectorConfig(
                    local_connect_timeout_seconds=0.02,
                    local_rpc_deadline_seconds=0.02,
                    local_max_reconnect_attempts=1,
                ),
            )
            try:
                await gateway.start()
                runner = asyncio.create_task(gateway.run())
                self.assertFalse(await gateway.ready())
                with self.assertRaises(LocalDeadlineExceeded) as raised:
                    await runner
                self.assertEqual(raised.exception.code, 4306)
                await asyncio.wait_for(peer_closed.wait(), timeout=1.0)
            finally:
                await gateway.stop()
                server.close()
                await server.wait_closed()

        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(scenario(Path(directory)))

    def test_rejects_empty_and_oversized_request_before_writing(self) -> None:
        async def scenario(root: Path) -> None:
            socket_path = root / "gateway.sock"

            async def handle(
                reader: asyncio.StreamReader,
                writer: asyncio.StreamWriter,
            ) -> None:
                with suppress(asyncio.IncompleteReadError):
                    await reader.readexactly(1)
                writer.close()
                await writer.wait_closed()

            cases = (
                (b"", InvalidEnvelope, 4301),
                (
                    b"x" * (MAX_LOCAL_BODY_BYTES + 1),
                    FrameTooLarge,
                    4302,
                ),
            )
            for request, error_type, error_code in cases:
                server = await asyncio.start_unix_server(handle, path=socket_path)
                os.chmod(socket_path, 0o600)
                try:
                    connection = await MacOSLocalGatewayTransport().connect(
                        endpoint(socket_path)
                    )
                    with self.assertRaises(error_type) as raised:
                        await connection.exchange(request)
                    self.assertEqual(raised.exception.code, error_code)
                    await connection.close()
                finally:
                    server.close()
                    await server.wait_closed()
                    socket_path.unlink(missing_ok=True)

        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(scenario(Path(directory)))


if __name__ == "__main__":
    unittest.main()
