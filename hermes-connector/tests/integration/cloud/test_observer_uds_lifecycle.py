from __future__ import annotations

import asyncio
import json
import os
import stat
import tempfile
import threading
from pathlib import Path

import pytest
from websockets.asyncio.server import unix_serve

from hermes_connector.adapters.platform.macos.observer_client import MacOSObserverClient
from hermes_connector.adapters.platform.macos.observer_discovery import (
    MacOSObserverEndpointDiscovery,
)
from hermes_connector.adapters.platform.macos.process_identity import (
    current_process_identity,
)
from hermes_connector.domain.local_gateway import LocalRuntimeAuthority

_LIFECYCLE_REPETITIONS = 100
_PROCESS_IDENTITY = current_process_identity(os.getpid())
assert _PROCESS_IDENTITY is not None


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


async def _authority() -> LocalRuntimeAuthority:
    return LocalRuntimeAuthority(
        profile="default",
        runtime_generation="runtime-generation-1",
        instance_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        host_bundle_id="com.nousresearch.hermes",
        process_identity=_PROCESS_IDENTITY,
        required_capabilities=("session.observe",),
        optional_capabilities=(),
    )


async def _authority_v2() -> LocalRuntimeAuthority:
    authority = await _authority()
    return LocalRuntimeAuthority(
        profile=authority.profile,
        runtime_generation="runtime-20260801-01",
        instance_id=authority.instance_id,
        host_bundle_id=authority.host_bundle_id,
        process_identity=authority.process_identity,
        required_capabilities=authority.required_capabilities,
        optional_capabilities=("session.observe.output-parity.v1",),
    )


def _discovery(
    root: Path,
    socket_path: Path,
    *,
    runtime_generation: str = "runtime-generation-1",
) -> MacOSObserverEndpointDiscovery:
    registry = root / "r"
    registry.mkdir(mode=0o700)
    registry.chmod(0o700)
    descriptor = registry / "gateway-observer.json"
    descriptor.write_text(
        json.dumps(
            {
                "version": 2,
                "pid": os.getpid(),
                "profile": "default",
                "runtime_generation": runtime_generation,
                "socket_path": str(socket_path),
                "instance_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
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
    descriptor.chmod(0o600)
    return MacOSObserverEndpointDiscovery(registry, socket_path.parent)


@pytest.mark.asyncio
async def test_real_websocket_over_uds_is_snapshot_first_then_live() -> None:
    short_directory = tempfile.TemporaryDirectory(prefix="hc-obs-", dir="/tmp")
    root = Path(short_directory.name)
    socket_directory = root / "s"
    socket_directory.mkdir(mode=0o700)
    socket_directory.chmod(0o700)
    socket_path = socket_directory / "observer.sock"
    received: list[dict[str, object]] = []

    async def handler(websocket: object) -> None:
        await websocket.send(  # type: ignore[attr-defined]
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "method": "event",
                    "params": {
                        "type": "gateway.ready",
                        "payload": {
                            "observer_contract": 1,
                            "local_gateway_protocol": 1,
                            "connection_role": "observer",
                            "profile": "default",
                            "runtime_generation": "runtime-generation-1",
                            "instance_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                        },
                    },
                }
            )
        )
        request = json.loads(await websocket.recv())  # type: ignore[attr-defined]
        received.append(request)
        await websocket.send(  # type: ignore[attr-defined]
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": request["id"],
                    "result": {
                        "subscription_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                        "profile": "default",
                        "runtime_generation": "runtime-generation-1",
                        "session_key": "session-root-1",
                        "runtime_session_id": "runtime-session-1",
                        "running": True,
                        "status": "running",
                        "event_sequence": 0,
                        "snapshot_event_sequence": 0,
                        "messages": [],
                        "inflight": {
                            "user": None,
                            "assistant": None,
                            "streaming": False,
                            "error": None,
                        },
                        "replay_events": [],
                    },
                }
            )
        )
        await websocket.send(  # type: ignore[attr-defined]
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "method": "event",
                    "params": {
                        "type": "message.delta",
                        "profile": "default",
                        "runtime_generation": "runtime-generation-1",
                        "session_id": "runtime-session-1",
                        "session_key": "session-root-1",
                        "event_sequence": 1,
                        "payload": {"text": "live"},
                    },
                }
            )
        )
        await websocket.recv()  # type: ignore[attr-defined]

    server = await unix_serve(handler, str(socket_path))
    socket_path.chmod(0o600)
    client = MacOSObserverClient(
        discovery=_discovery(root, socket_path),
        authority=_authority,
    )
    try:
        subscription = await client.subscribe(
            profile="default",
            session_key="session-root-1",
        )
        event = await anext(subscription.events())
        await subscription.close()

        assert subscription.snapshot.event_sequence == 0
        assert event.event_sequence == 1
        assert received[0]["params"] == {
            "profile": "default",
            "relay_local_only": True,
            "runtime_generation": "runtime-generation-1",
            "session_key": "session-root-1",
        }
    finally:
        await client.aclose()
        server.close()
        await server.wait_closed()
        short_directory.cleanup()


@pytest.mark.asyncio
async def test_v2_real_websocket_over_uds_uses_generated_projection() -> None:
    short_directory = tempfile.TemporaryDirectory(prefix="hc-obs2-", dir="/tmp")
    root = Path(short_directory.name)
    socket_directory = root / "s"
    socket_directory.mkdir(mode=0o700)
    socket_directory.chmod(0o700)
    socket_path = socket_directory / "observer.sock"
    received: list[dict[str, object]] = []
    fixture_root = Path(__file__).parents[4] / "contracts" / "fixtures" / "valid"
    snapshot = json.loads(
        (fixture_root / "session-snapshot-v2-payload.json").read_text(
            encoding="utf-8"
        )
    )
    event = {
        "observer_contract": 2,
        "profile": "default",
        "runtime_generation": "runtime-20260801-01",
        "session_key": "session-root-1",
        "session_id": "runtime-session-1",
        "type": "tool.update",
        "event_sequence": 5,
        "payload": {
            "turn_id": "turn-1",
            "tool_call_id": "tool-1",
            "revision": 2,
            "first_event_sequence": 3,
            "operation": "upsert",
            "status": "completed",
            "name": "Contract tests",
        },
    }

    async def handler(websocket: object) -> None:
        await websocket.send(  # type: ignore[attr-defined]
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "method": "event",
                    "params": {
                        "type": "gateway.ready",
                        "payload": {
                            "observer_contract": 2,
                            "connection_role": "observer",
                        },
                    },
                }
            )
        )
        request = json.loads(await websocket.recv())  # type: ignore[attr-defined]
        received.append(request)
        await websocket.send(  # type: ignore[attr-defined]
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": request["id"],
                    "result": {
                        "subscription_id": (
                            "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
                        ),
                        **snapshot,
                    },
                }
            )
        )
        await websocket.send(  # type: ignore[attr-defined]
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "method": "event",
                    "params": event,
                }
            )
        )
        received.append(  # type: ignore[arg-type]
            json.loads(await websocket.recv())  # type: ignore[attr-defined]
        )

    server = await unix_serve(handler, str(socket_path))
    socket_path.chmod(0o600)
    discovery = _discovery(
        root,
        socket_path,
        runtime_generation="runtime-20260801-01",
    )
    client = MacOSObserverClient(discovery=discovery, authority=_authority_v2)
    try:
        subscription = await client.subscribe(
            profile="default",
            session_key="session-root-1",
        )
        observed = await anext(subscription.events())
        await subscription.close()

        assert subscription.snapshot.observer_contract == 2
        assert observed.observer_contract == 2
        assert observed.event_sequence == 5
        assert received == [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "session.observe.subscribe",
                "params": {
                    "observer_contract": 2,
                    "profile": "default",
                    "session_key": "session-root-1",
                },
            },
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "session.observe.unsubscribe",
                "params": {
                    "observer_contract": 2,
                    "subscription_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                },
            },
        ]
    finally:
        await client.aclose()
        server.close()
        await server.wait_closed()
        short_directory.cleanup()


@pytest.mark.asyncio
async def test_observer_uds_repeats_one_hundred_lifecycles_without_live_leak() -> None:
    short_directory = tempfile.TemporaryDirectory(prefix="hc-obs-", dir="/tmp")
    root = Path(short_directory.name)
    socket_directory = root / "s"
    socket_directory.mkdir(mode=0o700)
    socket_directory.chmod(0o700)
    socket_path = socket_directory / "observer.sock"
    active = 0
    completed = 0

    async def handler(websocket: object) -> None:
        nonlocal active, completed
        active += 1
        try:
            await websocket.send(  # type: ignore[attr-defined]
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "method": "event",
                        "params": {
                            "type": "gateway.ready",
                            "payload": {
                                "observer_contract": 1,
                                "local_gateway_protocol": 1,
                                "connection_role": "observer",
                                "profile": "default",
                                "runtime_generation": "runtime-generation-1",
                                "instance_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                            },
                        },
                    }
                )
            )
            request = json.loads(await websocket.recv())  # type: ignore[attr-defined]
            await websocket.send(  # type: ignore[attr-defined]
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": request["id"],
                        "result": {
                            "subscription_id": (
                                f"bbbbbbbb-bbbb-4bbb-8bbb-{completed:012x}"
                            ),
                            "profile": "default",
                            "runtime_generation": "runtime-generation-1",
                            "session_key": "session-root-1",
                            "runtime_session_id": "runtime-session-1",
                            "running": False,
                            "status": "idle",
                            "event_sequence": 0,
                            "snapshot_event_sequence": 0,
                            "messages": [],
                            "inflight": {
                                "user": None,
                                "assistant": None,
                                "streaming": False,
                                "error": None,
                            },
                            "replay_events": [],
                        },
                    }
                )
            )
            await websocket.recv()  # type: ignore[attr-defined]
            completed += 1
        finally:
            active -= 1

    before_server_descriptors = _descriptor_counts()
    before_client_threads = _live_thread_ids()
    server = await unix_serve(handler, str(socket_path))
    socket_path.chmod(0o600)
    client = MacOSObserverClient(
        discovery=_discovery(root, socket_path),
        authority=_authority,
    )
    try:
        warmup = await client.subscribe(
            profile="default",
            session_key="session-root-1",
        )
        await warmup.close()
        for _ in range(100):
            if active == 0:
                break
            await asyncio.sleep(0)
        baseline_tasks = _live_tasks()
        baseline_threads = _live_thread_ids()
        baseline_descriptors = _descriptor_counts()

        for _ in range(_LIFECYCLE_REPETITIONS):
            subscription = await client.subscribe(
                profile="default",
                session_key="session-root-1",
            )
            await subscription.close()
            for _ in range(100):
                if active == 0:
                    break
                await asyncio.sleep(0)
            assert active == 0
            assert _live_tasks() == baseline_tasks
            assert _live_thread_ids() == baseline_threads
            assert _descriptor_counts() == baseline_descriptors

        assert completed == _LIFECYCLE_REPETITIONS + 1
        assert active == 0
    finally:
        await client.aclose()
        server.close()
        await server.wait_closed()
        short_directory.cleanup()
    assert _live_thread_ids() == before_client_threads
    assert _descriptor_counts() == before_server_descriptors
