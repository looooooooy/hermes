from __future__ import annotations

import asyncio
import json
import os
import stat
import struct
import sys
import tempfile
import threading
from pathlib import Path
from uuid import UUID

import pytest

from hermes_connector.adapters.contract_codec import encode_local_welcome
from hermes_connector.adapters.platform.macos.agent_discovery import (
    MacOSAgentDiscovery,
)
from hermes_connector.adapters.platform.macos.instance_lock import MacOSInstanceLock
from hermes_connector.adapters.platform.macos.local_gateway_transport import (
    MacOSLocalGatewayTransport,
)
from hermes_connector.adapters.platform.macos.process_identity import (
    current_process_identity,
)
from hermes_connector.adapters.sqlite_storage import SQLiteStorageComponent
from hermes_connector.application.local_gateway_client import LocalGatewayClient
from hermes_connector.bootstrap.config import ConnectorConfig
from hermes_connector.bootstrap.runtime import build_service_runner
from hermes_connector.bootstrap.safe_logging import SafeStructuredLogger
from hermes_connector.domain.contract_messages import LocalWelcome
from hermes_connector.domain.local_gateway import LocalGatewayState

_CLIENT_INSTANCE_ID = UUID("11111111-1111-4111-8111-111111111111")
_PLUGIN_INSTANCE_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
_LIFECYCLE_REPETITIONS = 100
_PROCESS_IDENTITY = current_process_identity(os.getpid())
assert _PROCESS_IDENTITY is not None


class _NoProjectionState:
    async def invalidate_runtime(
        self,
        previous_generation: str,
        current_generation: str,
    ) -> None:
        raise AssertionError("the lifecycle gate uses one runtime generation")


def _descriptor_counts() -> tuple[int, int]:
    descriptor_count = 0
    socket_count = 0
    for name in os.listdir("/dev/fd"):
        try:
            metadata = os.fstat(int(name))
        except (OSError, ValueError):
            continue
        descriptor_count += 1
        socket_count += stat.S_ISSOCK(metadata.st_mode)
    return descriptor_count, socket_count


def _live_thread_ids() -> frozenset[int | None]:
    return frozenset(
        thread.ident for thread in threading.enumerate() if thread.is_alive()
    )


def _live_tasks() -> frozenset[asyncio.Task[object]]:
    current = asyncio.current_task()
    return frozenset(
        task for task in asyncio.all_tasks() if task is not current and not task.done()
    )


async def _read_frame(reader: asyncio.StreamReader) -> bytes:
    prefix = await reader.readexactly(4)
    body_length = struct.unpack(">I", prefix)[0]
    return await reader.readexactly(body_length)


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS lifecycle gate")
@pytest.mark.asyncio
async def test_public_runtime_lifecycle_repeats_without_resource_residue(
    tmp_path: Path,
) -> None:
    registry_directory = tmp_path / "registry"
    short_socket_directory = tempfile.TemporaryDirectory(
        prefix="hc-life-",
        dir="/tmp",
    )
    socket_directory = Path(short_socket_directory.name)
    state_directory = tmp_path / "state"
    for directory in (registry_directory, state_directory):
        directory.mkdir(mode=0o700)
        directory.chmod(0o700)
    socket_directory.chmod(0o700)

    socket_path = socket_directory / "gateway.sock"
    active_peers = 0
    accepted_peers = 0
    peers_drained = asyncio.Event()
    peers_drained.set()
    welcome = encode_local_welcome(
        LocalWelcome(
            contract_version=1,
            message_type="local.welcome",
            runtime_generation="runtime-1",
            profile="default",
            accepted_capabilities=("session.observe",),
            unavailable_optional_capabilities=(),
        )
    )

    async def handle_gateway(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        nonlocal active_peers, accepted_peers
        active_peers += 1
        accepted_peers += 1
        peers_drained.clear()
        try:
            await _read_frame(reader)
            writer.write(struct.pack(">I", len(welcome)) + welcome)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            active_peers -= 1
            if active_peers == 0:
                peers_drained.set()

    server = await asyncio.start_unix_server(handle_gateway, path=socket_path)
    socket_path.chmod(0o600)
    descriptor_path = registry_directory / "gateway.json"
    descriptor_path.write_text(
        json.dumps(
            {
                "version": 2,
                "pid": os.getpid(),
                "profile": "default",
                "runtime_generation": "runtime-1",
                "socket_path": str(socket_path),
                "instance_id": _PLUGIN_INSTANCE_ID,
                "process_start_time_ns": _PROCESS_IDENTITY.start_time_ns,
                "process_executable": str(_PROCESS_IDENTITY.executable_path),
                "process_executable_device": _PROCESS_IDENTITY.executable_device,
                "process_executable_inode": _PROCESS_IDENTITY.executable_inode,
                "host_bundle_id": "com.nousresearch.hermes",
            },
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    descriptor_path.chmod(0o600)

    baseline_threads = _live_thread_ids()
    baseline_tasks = _live_tasks()
    baseline_descriptors = _descriptor_counts()
    lock_path = state_directory / "connector.lock"
    config = ConnectorConfig(
        local_discovery_poll_interval_seconds=60.0,
        start_deadline_seconds=2.0,
        stop_deadline_seconds=2.0,
    )

    try:
        for repetition in range(_LIFECYCLE_REPETITIONS):
            storage = SQLiteStorageComponent(
                state_directory / f"connector-{repetition}.sqlite3",
                config,
            )
            local_gateway = LocalGatewayClient(
                profile="default",
                client_instance_id=_CLIENT_INSTANCE_ID,
                required_capabilities=("session.observe",),
                optional_capabilities=(),
                discovery=MacOSAgentDiscovery(
                    registry_directory,
                    socket_directory,
                ),
                transport=MacOSLocalGatewayTransport(),
                session_state=_NoProjectionState(),
                config=config,
            )
            runner = build_service_runner(
                lock_path=lock_path,
                components=(storage, local_gateway),
                config=config,
                logger=SafeStructuredLogger(lambda _: None),
                platform_name="darwin",
            )

            await runner.start()
            await runner.stop()
            await asyncio.wait_for(peers_drained.wait(), timeout=1.0)

            assert runner.state.value == "stopped"
            assert local_gateway.state is LocalGatewayState.DISCONNECTED
            assert storage._engine is None
            assert storage._executor is None
            assert _live_tasks() == baseline_tasks
            assert _descriptor_counts() == baseline_descriptors

            lock_probe = MacOSInstanceLock(lock_path)
            lock_probe.acquire()
            lock_probe.close()
            assert not lock_probe.is_held

        assert accepted_peers == _LIFECYCLE_REPETITIONS
        assert active_peers == 0
        assert _live_thread_ids() == baseline_threads
    finally:
        server.close()
        await server.wait_closed()
        socket_path.unlink(missing_ok=True)
        short_socket_directory.cleanup()
