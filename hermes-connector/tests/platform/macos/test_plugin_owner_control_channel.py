from __future__ import annotations

import asyncio
import json
import os
import socket
import tempfile
import threading
from contextlib import asynccontextmanager, contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from typing import Any
from uuid import UUID

import pytest

from hermes_connector.adapters.platform.macos import plugin_control_relay
from hermes_connector.adapters.platform.macos.process_identity import (
    current_process_identity,
)
from hermes_connector.application.owner_control_lane import OwnerControlScope
from hermes_connector.domain.cloud_protocol import CommandDelivery
from hermes_connector.domain.control_command import (
    LocalControlFailure,
    LocalControlOutcomeUnknown,
)
from hermes_connector.domain.owner_control import (
    OwnerControlCallFailed,
    OwnerControlOutcomeUnknown,
)


@dataclass(frozen=True)
class _ProcessIdentity:
    start_time_ns: int = 1_000
    executable_path: Path = Path("/private/fixture/hermes-python")
    executable_device: int = 41
    executable_inode: int = 73
    bundle_id: str = "com.nousresearch.hermes"


_PROCESS_EVIDENCE = current_process_identity(os.getpid())
assert _PROCESS_EVIDENCE is not None


async def _authority():
    return SimpleNamespace(
        profile="default",
        runtime_generation="runtime-generation-1",
        instance_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        host_bundle_id="com.nousresearch.hermes",
        process_identity=_PROCESS_EVIDENCE,
        required_capabilities=("session.control",),
        optional_capabilities=(),
    )


@dataclass
class _CountingScandir:
    inner: Any
    yielded: int = 0

    def __enter__(self):
        self.inner.__enter__()
        return self

    def __exit__(self, *args: object) -> None:
        self.inner.__exit__(*args)

    def __iter__(self):
        return self

    def __next__(self):
        entry = next(self.inner)
        self.yielded += 1
        return entry


@contextmanager
def _published_control_descriptor(instance_id: object):
    temporary_root = Path("/tmp").resolve(strict=True)
    with tempfile.TemporaryDirectory(
        prefix="hcp-descriptor-",
        dir=temporary_root,
    ) as raw_root:
        root = Path(raw_root)
        registry = root / "registry"
        sockets = root / "sockets"
        registry.mkdir(mode=0o700)
        sockets.mkdir(mode=0o700)
        socket_path = sockets / "control.sock"
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(socket_path))
        os.chmod(socket_path, 0o600)
        descriptor = registry / "gateway.json"
        descriptor.write_text(
            json.dumps(
                {
                    "version": 2,
                    "pid": os.getpid(),
                    "profile": "default",
                    "runtime_generation": "runtime-generation-1",
                    "socket_path": str(socket_path),
                    "instance_id": instance_id,
                    "process_start_time_ns": _PROCESS_EVIDENCE.start_time_ns,
                    "process_executable": str(_PROCESS_EVIDENCE.executable_path),
                    "process_executable_device": (_PROCESS_EVIDENCE.executable_device),
                    "process_executable_inode": _PROCESS_EVIDENCE.executable_inode,
                    "host_bundle_id": "com.nousresearch.hermes",
                }
            ),
            encoding="utf-8",
        )
        descriptor.chmod(0o600)
        relay = plugin_control_relay.MacOSPluginControlRelay(
            registry_directory=registry,
            socket_directory=sockets,
            profile="default",
            user_id="user-1",
            provider="hermes-cloud",
            authority=_authority,
        )
        try:
            yield relay, socket_path
        finally:
            listener.close()


def test_control_discovery_accepts_real_plugin_canonical_instance_uuid() -> None:
    with _published_control_descriptor("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa") as (
        relay,
        socket_path,
    ):
        endpoints = relay._discover()

        assert len(endpoints) == 1
        assert endpoints[0].pid == os.getpid()
        assert endpoints[0].socket_path == socket_path


def test_control_v1_descriptor_is_rejected_without_identity_guessing() -> None:
    with _published_control_descriptor("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa") as (
        relay,
        _socket_path,
    ):
        descriptor = next(relay._registry_directory.glob("*.json"))
        value = json.loads(descriptor.read_text(encoding="utf-8"))
        for field in (
            "runtime_generation",
            "process_start_time_ns",
            "process_executable",
            "process_executable_device",
            "process_executable_inode",
            "host_bundle_id",
        ):
            value.pop(field)
        value["version"] = 1
        descriptor.write_text(json.dumps(value), encoding="utf-8")
        descriptor.chmod(0o600)
        assert relay._discover() == ()


def test_control_same_numeric_pid_reuse_during_discovery_fails_closed() -> None:
    with _published_control_descriptor("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa") as (
        relay,
        _socket_path,
    ):
        descriptor = next(relay._registry_directory.glob("*.json"))
        value = json.loads(descriptor.read_text(encoding="utf-8"))
        expected = _ProcessIdentity()
        value.update(
            {
                "version": 2,
                "runtime_generation": "runtime-generation-1",
                "process_start_time_ns": expected.start_time_ns,
                "process_executable": str(expected.executable_path),
                "process_executable_device": expected.executable_device,
                "process_executable_inode": expected.executable_inode,
                "host_bundle_id": expected.bundle_id,
            }
        )
        descriptor.write_text(json.dumps(value), encoding="utf-8")
        descriptor.chmod(0o600)
        observed = iter((expected, _ProcessIdentity(start_time_ns=2_000)))
        relay._process_identity_provider = lambda _: next(observed)

        assert relay._discover() == ()


@pytest.mark.asyncio
async def test_control_same_uid_replacement_socket_fails_before_attach(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _published_control_descriptor("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa") as (
        relay,
        socket_path,
    ):
        endpoints = relay._discover()
        assert len(endpoints) == 1
        socket_path.unlink()
        replacement = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        replacement.bind(str(socket_path))
        socket_path.chmod(0o600)
        connected = False

        @asynccontextmanager
        async def connect(**_kwargs: object):
            nonlocal connected
            connected = True
            yield _WebSocket()

        monkeypatch.setattr(relay, "_discover", lambda: endpoints)
        monkeypatch.setattr(plugin_control_relay, "unix_connect", connect)
        try:
            with pytest.raises(LocalControlFailure) as captured:
                await relay.execute(_command())
            assert captured.value.code == "owner_adapter_unavailable"
            assert connected is False
        finally:
            replacement.close()


@pytest.mark.parametrize(
    "instance_id",
    (
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA",
        "{aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa}",
        True,
        1,
        None,
    ),
)
def test_control_discovery_rejects_noncanonical_instance_uuid(
    instance_id: object,
) -> None:
    with _published_control_descriptor(instance_id) as (relay, _socket_path):
        assert relay._discover() == ()


def test_control_discovery_stops_at_overflow_and_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _published_control_descriptor("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa") as (
        relay,
        _socket_path,
    ):
        for index in range(plugin_control_relay._MAX_CANDIDATES * 4):
            extra = relay._registry_directory / f"extra-{index:03d}.json"
            extra.write_text("{}", encoding="utf-8")
            extra.chmod(0o600)

        real_scandir = os.scandir
        scans: list[_CountingScandir] = []

        def counting_scandir(path: object) -> _CountingScandir:
            scan = _CountingScandir(real_scandir(path))
            scans.append(scan)
            return scan

        monkeypatch.setattr(plugin_control_relay.os, "scandir", counting_scandir)

        assert relay._discover() == ()
        assert len(scans) == 1
        assert scans[0].yielded == plugin_control_relay._MAX_CANDIDATES + 1


def test_control_discovery_bounds_all_directory_entries_not_only_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _published_control_descriptor("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa") as (
        relay,
        _socket_path,
    ):
        for index in range(plugin_control_relay._MAX_CANDIDATES * 4):
            extra = relay._registry_directory / f"ignored-{index:03d}.tmp"
            extra.write_text("not-a-descriptor", encoding="utf-8")
            extra.chmod(0o600)

        real_scandir = os.scandir
        scans: list[_CountingScandir] = []

        def counting_scandir(path: object) -> _CountingScandir:
            scan = _CountingScandir(real_scandir(path))
            scans.append(scan)
            return scan

        monkeypatch.setattr(plugin_control_relay.os, "scandir", counting_scandir)

        assert relay._discover() == ()
        assert len(scans) == 1
        assert scans[0].yielded == plugin_control_relay._MAX_CANDIDATES * 2 + 1


def test_control_discovery_rejects_descriptor_inode_replaced_before_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _published_control_descriptor("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa") as (
        relay,
        _socket_path,
    ):
        descriptor = relay._registry_directory / "gateway.json"
        replacement = relay._registry_directory / ".replacement"
        replacement.write_bytes(descriptor.read_bytes())
        replacement.chmod(0o600)
        original_inode = descriptor.stat().st_ino
        real_open = os.open
        swapped = False

        def replacing_open(
            path: object,
            flags: int,
            mode: int = 0o777,
            *,
            dir_fd: int | None = None,
        ) -> int:
            nonlocal swapped
            if path == "gateway.json" and dir_fd is not None and not swapped:
                os.replace(replacement, descriptor)
                swapped = True
            return real_open(path, flags, mode, dir_fd=dir_fd)

        monkeypatch.setattr(plugin_control_relay.os, "open", replacing_open)

        assert relay._discover() == ()
        assert swapped is True
        assert descriptor.stat().st_ino != original_inode


def test_control_discovery_rejects_descriptor_changed_during_fd_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _published_control_descriptor("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa") as (
        relay,
        _socket_path,
    ):
        descriptor = relay._registry_directory / "gateway.json"
        real_read = os.read
        changed = False

        def changing_read(file_descriptor: int, count: int) -> bytes:
            nonlocal changed
            value = real_read(file_descriptor, count)
            if value and not changed:
                with descriptor.open("ab") as stream:
                    stream.write(b" ")
                    stream.flush()
                    os.fsync(stream.fileno())
                changed = True
            return value

        monkeypatch.setattr(plugin_control_relay.os, "read", changing_read)

        assert relay._discover() == ()
        assert changed is True


@pytest.mark.parametrize("unsafe_kind", ("symlink", "directory", "wide", "oversized"))
def test_control_discovery_rejects_unsafe_descriptor_metadata(
    unsafe_kind: str,
) -> None:
    with _published_control_descriptor("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa") as (
        relay,
        _socket_path,
    ):
        descriptor = relay._registry_directory / "gateway.json"
        if unsafe_kind == "symlink":
            outside = relay._registry_directory.parent / "outside.json"
            outside.write_bytes(descriptor.read_bytes())
            outside.chmod(0o600)
            descriptor.unlink()
            descriptor.symlink_to(outside)
        elif unsafe_kind == "directory":
            descriptor.unlink()
            descriptor.mkdir(mode=0o600)
        elif unsafe_kind == "wide":
            descriptor.chmod(0o640)
        else:
            descriptor.write_bytes(b"{" + b"x" * 16_384 + b"}")
            descriptor.chmod(0o600)

        assert relay._discover() == ()


def test_control_discovery_rejects_descriptor_owned_by_another_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _published_control_descriptor("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa") as (
        relay,
        _socket_path,
    ):
        real_stat = os.stat

        def wrong_owner_stat(path: object, *args: object, **kwargs: object):
            metadata = real_stat(path, *args, **kwargs)
            if path != "gateway.json" or kwargs.get("dir_fd") is None:
                return metadata
            fields = list(metadata)
            fields[4] = os.geteuid() + 1
            return os.stat_result(fields)

        monkeypatch.setattr(plugin_control_relay.os, "stat", wrong_owner_stat)

        assert relay._discover() == ()


def test_plugin_owner_control_channel_surface_exists() -> None:
    assert hasattr(
        plugin_control_relay,
        "MacOSPluginOwnerControlChannelFactory",
    )
    assert hasattr(plugin_control_relay, "MacOSPluginOwnerControlChannel")


class _PeerCredentialSocket:
    def __init__(self, peer_pid: int) -> None:
        self._peer_pid = peer_pid

    def getsockopt(self, level: int, option: int) -> int:
        assert (level, option) == (0, 2)
        return self._peer_pid


class _PeerTransport:
    def __init__(self, peer_pid: int) -> None:
        self._socket = _PeerCredentialSocket(peer_pid)

    def get_extra_info(self, name: str):
        return self._socket if name == "socket" else None


class _WebSocket:
    def __init__(self, *, peer_pid: int | None = None) -> None:
        self.sent: list[dict[str, object]] = []
        self.closed = 0
        self.receive_failure: BaseException | None = None
        self.transport = _PeerTransport(os.getpid() if peer_pid is None else peer_pid)

    async def send(self, frame: str) -> None:
        self.sent.append(json.loads(frame))

    async def recv(self) -> str:
        if self.receive_failure is not None:
            raise self.receive_failure
        request = self.sent[-1]
        method = request["method"]
        if method == "relay.control.attach":
            result = {"attached": True, "connection_role": "control"}
        elif method in {"session.control.acquire", "session.control.renew"}:
            result = {
                "lease_id": "fixture-lease-secret-not-real-0001",
                "expires_at_epoch_ms": 1785463232000,
                "control_revision": 3,
                "controller_kind": "mobile",
                "controller_label": "Hermes Mobile",
                "pending_input": None,
            }
        elif method == "session.control.release":
            result = {"released": True, "control_revision": 4}
        elif method == "session.command.status":
            assert request["params"]["method"] == "approval.respond"
            result = {
                "status": "accepted",
                "client_request_id": "request-status",
            }
        elif method == "prompt.submit":
            result = {
                "status": "queued",
                "client_request_id": "request-prompt",
                "client_turn_id": "turn-prompt",
                "server_turn_id": "server-turn-prompt",
            }
        elif method in {"session.interrupt", "session.steer"}:
            result = {
                "status": "accepted",
                "client_request_id": f"request-{method.rsplit('.', 1)[1]}",
            }
        elif method in {"approval.respond", "clarify.respond"}:
            kind = method.split(".", 1)[0]
            result = {
                "status": "accepted",
                "kind": kind,
                "request_id": f"pending-{kind}",
                "client_request_id": f"request-{kind}",
                "control_revision": 7,
            }
        else:
            result = {
                "controller_kind": "desktop",
                "controller_label": "Hermes Desktop",
                "control_revision": 3,
                "lease_expires_at_epoch_ms": 0,
                "pending_input": None,
            }
        return json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result})

    async def close(self) -> None:
        self.closed += 1


class _LegacyLocalStatusWebSocket(_WebSocket):
    async def recv(self) -> str:
        if self.sent[-1]["method"] != "session.control.status":
            return await super().recv()
        request = self.sent[-1]
        return json.dumps(
            {
                "jsonrpc": "2.0",
                "id": request["id"],
                "result": {
                    "controller_kind": "local",
                    "controller_label": "Hermes Desktop",
                    "control_revision": 3,
                    "lease_expires_at_epoch_ms": 0,
                    "pending_input": None,
                },
            }
        )


class _EffectUnknownWebSocket(_WebSocket):
    async def recv(self) -> str:
        if self.sent[-1]["method"] != "session.interrupt":
            return await super().recv()
        request = self.sent[-1]
        return json.dumps(
            {
                "jsonrpc": "2.0",
                "id": request["id"],
                "result": {
                    "status": "unknown",
                    "client_request_id": "request-effect-unknown",
                },
            }
        )


class _HangingPhaseWebSocket(_WebSocket):
    def __init__(self, method: str) -> None:
        super().__init__()
        self._method = method
        self.recv_entered = asyncio.Event()

    async def recv(self) -> str:
        if self.sent[-1]["method"] == self._method:
            self.recv_entered.set()
            await asyncio.Event().wait()
        return await super().recv()


class _FailingPhaseWebSocket(_WebSocket):
    def __init__(self, method: str, failure: BaseException) -> None:
        super().__init__()
        self._method = method
        self._failure = failure

    async def recv(self) -> str:
        if self.sent[-1]["method"] == self._method:
            raise self._failure
        return await super().recv()


class _FailingSendPhaseWebSocket(_WebSocket):
    def __init__(self, method: str, failure: BaseException) -> None:
        super().__init__()
        self._method = method
        self._failure = failure

    async def send(self, frame: str) -> None:
        request = json.loads(frame)
        self.sent.append(request)
        if request["method"] == self._method:
            raise self._failure


class _MutationResponseWebSocket(_WebSocket):
    def __init__(self, response: dict[str, object]) -> None:
        super().__init__()
        self._response = response

    async def recv(self) -> str:
        request = self.sent[-1]
        if request["method"] == "session.interrupt":
            return json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": request["id"],
                    **self._response,
                }
            )
        return await super().recv()


def _scope() -> OwnerControlScope:
    return OwnerControlScope(
        control_transport_id=UUID("11111111-1111-4111-8111-111111111111"),
        principal_id="principal-1",
        client_instance_id=UUID("22222222-2222-4222-8222-222222222222"),
        session_key="durable-root-1",
        profile="default",
    )


def _command() -> CommandDelivery:
    issued_at = datetime(2026, 8, 1, tzinfo=UTC)
    return CommandDelivery(
        command_id=UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
        connector_instance_id=UUID("11111111-1111-4111-8111-111111111111"),
        client_instance_id=UUID("22222222-2222-4222-8222-222222222222"),
        session_key="durable-root-1",
        profile="default",
        client_request_id="client-request-1",
        method="session.interrupt",
        params=MappingProxyType(
            {
                "runtime_session_id": "runtime-7",
                "runtime_generation": "generation-7",
            }
        ),
        issued_at=issued_at,
        expires_at=issued_at + timedelta(minutes=1),
        revision=1,
    )


def _control_endpoint(
    *,
    instance_id: str = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
) -> plugin_control_relay.ControlEndpoint:
    return plugin_control_relay.ControlEndpoint(
        pid=os.getpid(),
        profile="default",
        socket_path=Path("/private/fixture-sockets/control.sock"),
        instance_id=instance_id,
        runtime_generation="runtime-generation-1",
        host_bundle_id="com.nousresearch.hermes",
        process_identity=_PROCESS_EVIDENCE,
        socket_device=51,
        socket_inode=79,
    )


def _factory(monkeypatch: pytest.MonkeyPatch, websocket: _WebSocket):
    factory_class = plugin_control_relay.MacOSPluginOwnerControlChannelFactory
    factory = factory_class(
        registry_directory=Path("/private/fixture-registry"),
        socket_directory=Path("/private/fixture-sockets"),
        profile="default",
        provider="hermes-cloud",
        authority=_authority,
        process_identity_provider=lambda _: _PROCESS_EVIDENCE,
    )
    monkeypatch.setattr(
        factory,
        "_discover",
        lambda: (_control_endpoint(),),
    )
    monkeypatch.setattr(factory, "_require_endpoint_evidence", lambda _: None)
    connections: list[tuple[str, str, int]] = []

    async def connect(*, path: str, uri: str, max_size: int):
        connections.append((path, uri, max_size))
        return websocket

    monkeypatch.setattr(plugin_control_relay, "unix_connect", connect)
    return factory, connections


def _relay_with_websocket(
    monkeypatch: pytest.MonkeyPatch,
    websocket: _WebSocket,
    *,
    timeout_seconds: float = 0.01,
) -> plugin_control_relay.MacOSPluginControlRelay:
    relay = plugin_control_relay.MacOSPluginControlRelay(
        registry_directory=Path("/private/fixture-registry"),
        socket_directory=Path("/private/fixture-sockets"),
        profile="default",
        user_id="user-1",
        provider="hermes-cloud",
        authority=_authority,
        timeout_seconds=timeout_seconds,
        process_identity_provider=lambda _: _PROCESS_EVIDENCE,
    )
    monkeypatch.setattr(
        relay,
        "_discover",
        lambda: (_control_endpoint(),),
    )
    monkeypatch.setattr(relay, "_require_endpoint_evidence", lambda _: None)

    @asynccontextmanager
    async def connect(**_kwargs: object):
        yield websocket

    monkeypatch.setattr(plugin_control_relay, "unix_connect", connect)
    return relay


@pytest.mark.asyncio
async def test_control_endpoint_from_different_local_runtime_fails_before_attach(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    websocket = _WebSocket()

    async def authority():
        return SimpleNamespace(
            profile="default",
            runtime_generation="runtime-generation-1",
            instance_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            host_bundle_id="com.nousresearch.hermes",
            process_identity=_PROCESS_EVIDENCE,
            required_capabilities=("session.control",),
            optional_capabilities=(),
        )

    relay = plugin_control_relay.MacOSPluginControlRelay(
        registry_directory=Path("/private/fixture-registry"),
        socket_directory=Path("/private/fixture-sockets"),
        profile="default",
        user_id="user-1",
        provider="hermes-cloud",
        authority=authority,
        process_identity_provider=lambda _: _PROCESS_EVIDENCE,
    )
    monkeypatch.setattr(
        relay,
        "_discover",
        lambda: (
            plugin_control_relay.ControlEndpoint(
                pid=os.getpid(),
                profile="default",
                socket_path=Path("/private/fixture-sockets/control.sock"),
                instance_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                runtime_generation="runtime-generation-1",
                host_bundle_id="com.nousresearch.hermes",
                process_identity=_PROCESS_EVIDENCE,
                socket_device=51,
                socket_inode=79,
            ),
        ),
    )
    connected = False

    @asynccontextmanager
    async def connect(**_kwargs: object):
        nonlocal connected
        connected = True
        yield websocket

    monkeypatch.setattr(plugin_control_relay, "unix_connect", connect)

    with pytest.raises(LocalControlFailure) as raised:
        await relay.execute(_command())

    assert raised.value.code == "owner_adapter_unavailable"
    assert connected is False
    assert websocket.sent == []


@pytest.mark.asyncio
async def test_control_relay_discovery_is_inside_total_deadline_without_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _published_control_descriptor("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa") as (
        relay,
        _socket_path,
    ):
        relay._timeout_seconds = 0.01
        loop = asyncio.get_running_loop()
        entered = asyncio.Event()
        release = threading.Event()
        network_calls: list[str] = []

        def blocking_discovery() -> tuple[Path, ...]:
            loop.call_soon_threadsafe(entered.set)
            release.wait(timeout=1)
            return ()

        async def unexpected_connect(**_kwargs: object):
            network_calls.append("connect")
            raise AssertionError("network must not start after discovery deadline")

        monkeypatch.setattr(relay, "_discover", blocking_discovery)
        monkeypatch.setattr(plugin_control_relay, "unix_connect", unexpected_connect)
        operation = asyncio.create_task(relay.execute(_command()))
        await entered.wait()
        try:
            with pytest.raises(LocalControlFailure) as captured:
                await asyncio.wait_for(asyncio.shield(operation), timeout=0.2)
            assert captured.value.code == "owner_adapter_unavailable"
            assert network_calls == []
        finally:
            release.set()
            with suppress(LocalControlFailure, AssertionError):
                await operation


@pytest.mark.asyncio
async def test_control_relay_peer_pid_must_match_descriptor_publisher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    websocket = _WebSocket(peer_pid=os.getpid() + 1)
    relay = _relay_with_websocket(monkeypatch, websocket)

    with pytest.raises(LocalControlFailure) as captured:
        await relay.execute(_command())

    assert captured.value.code == "owner_adapter_unavailable"
    assert captured.value.retryable is True
    assert websocket.sent == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method",
    ("relay.control.attach", "session.control.acquire"),
)
async def test_control_relay_pre_effect_rpc_timeout_remains_retryable_failure(
    monkeypatch: pytest.MonkeyPatch,
    method: str,
) -> None:
    websocket = _HangingPhaseWebSocket(method)
    relay = _relay_with_websocket(monkeypatch, websocket)

    with pytest.raises(LocalControlFailure) as captured:
        await relay.execute(_command())

    assert captured.value.code == "owner_adapter_unavailable"
    assert captured.value.retryable is True
    assert [frame["method"] for frame in websocket.sent][-1] == method


@pytest.mark.asyncio
async def test_control_relay_mutation_timeout_after_send_is_outcome_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    websocket = _HangingPhaseWebSocket("session.interrupt")
    relay = _relay_with_websocket(monkeypatch, websocket)

    with pytest.raises(LocalControlOutcomeUnknown):
        await relay.execute(_command())

    assert websocket.sent[-1]["method"] == "session.interrupt"


@pytest.mark.asyncio
async def test_control_relay_mutation_connection_loss_after_send_is_outcome_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    websocket = _FailingPhaseWebSocket(
        "session.interrupt",
        OSError("connection lost"),
    )
    relay = _relay_with_websocket(monkeypatch, websocket)

    with pytest.raises(LocalControlOutcomeUnknown):
        await relay.execute(_command())


@pytest.mark.asyncio
async def test_control_relay_mutation_send_connection_loss_is_outcome_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    websocket = _FailingSendPhaseWebSocket(
        "session.interrupt",
        OSError("connection lost during send"),
    )
    relay = _relay_with_websocket(monkeypatch, websocket)

    with pytest.raises(LocalControlOutcomeUnknown):
        await relay.execute(_command())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method",
    ("relay.control.attach", "session.control.acquire"),
)
async def test_control_relay_pre_effect_send_failure_remains_retryable(
    monkeypatch: pytest.MonkeyPatch,
    method: str,
) -> None:
    websocket = _FailingSendPhaseWebSocket(
        method,
        OSError("connection lost during send"),
    )
    relay = _relay_with_websocket(monkeypatch, websocket)

    with pytest.raises(LocalControlFailure) as captured:
        await relay.execute(_command())

    assert captured.value.code == "owner_adapter_unavailable"
    assert captured.value.retryable is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    (
        {
            "error": {"code": 4202, "message": "runtime unavailable"},
            "unexpected": True,
        },
        {"error": {"code": 4202}},
        {"error": {"code": "4202", "message": "runtime unavailable"}},
        {"error": {"code": 4999, "message": "unknown error"}},
        {"result": [], "unexpected": True},
        {"result": []},
        {"id": "different-request", "result": {}},
    ),
)
async def test_control_relay_malformed_mutation_error_is_outcome_unknown(
    monkeypatch: pytest.MonkeyPatch,
    response: dict[str, object],
) -> None:
    relay = _relay_with_websocket(
        monkeypatch,
        _MutationResponseWebSocket(response),
    )

    with pytest.raises(LocalControlOutcomeUnknown):
        await relay.execute(_command())


@pytest.mark.asyncio
async def test_control_relay_valid_mutation_error_remains_deterministic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    relay = _relay_with_websocket(
        monkeypatch,
        _MutationResponseWebSocket(
            {"error": {"code": 4209, "message": "method not allowed"}}
        ),
    )

    with pytest.raises(LocalControlFailure) as captured:
        await relay.execute(_command())

    assert captured.value.code == "method_not_allowed"
    assert captured.value.retryable is False


@pytest.mark.asyncio
async def test_control_relay_external_cancellation_during_mutation_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    websocket = _HangingPhaseWebSocket("session.interrupt")
    relay = _relay_with_websocket(
        monkeypatch,
        websocket,
        timeout_seconds=10,
    )
    operation = asyncio.create_task(relay.execute(_command()))
    await websocket.recv_entered.wait()

    operation.cancel()

    with pytest.raises(asyncio.CancelledError):
        await operation


@pytest.mark.asyncio
async def test_owner_control_factory_discovery_is_inside_total_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    websocket = _WebSocket()
    factory, network_calls = _factory(monkeypatch, websocket)
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    release = threading.Event()

    def blocking_discovery() -> tuple[Path, ...]:
        loop.call_soon_threadsafe(entered.set)
        release.wait(timeout=1)
        return ()

    monkeypatch.setattr(factory, "_discover", blocking_discovery)
    operation = asyncio.create_task(
        factory.open(
            scope=_scope(),
            request_id=UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
            timeout_seconds=0.01,
        )
    )
    await entered.wait()
    try:
        with pytest.raises(OwnerControlCallFailed) as captured:
            await asyncio.wait_for(asyncio.shield(operation), timeout=0.2)
        assert captured.value.code == 4306
        assert captured.value.reason == "deadline_exceeded_before_effect"
        assert network_calls == []
    finally:
        release.set()
        with suppress(OwnerControlCallFailed):
            await operation


@pytest.mark.asyncio
async def test_owner_control_factory_peer_pid_must_match_descriptor_publisher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    websocket = _WebSocket(peer_pid=os.getpid() + 1)
    factory, _network_calls = _factory(monkeypatch, websocket)

    with pytest.raises(OwnerControlCallFailed) as captured:
        await factory.open(
            scope=_scope(),
            request_id=UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
            timeout_seconds=0.1,
        )

    assert captured.value.code == 4214
    assert captured.value.reason == "owner_adapter_unavailable"
    assert websocket.sent == []
    assert websocket.closed == 1


@pytest.mark.asyncio
async def test_owner_control_missing_socket_before_deadline_is_adapter_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def authority():
        return SimpleNamespace(
            profile="default",
            runtime_generation="runtime-generation-1",
            instance_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            host_bundle_id="com.nousresearch.hermes",
            process_identity=_PROCESS_EVIDENCE,
            required_capabilities=("session.control",),
            optional_capabilities=(),
        )

    factory = plugin_control_relay.MacOSPluginOwnerControlChannelFactory(
        registry_directory=Path("/private/fixture-registry"),
        socket_directory=Path("/private/fixture-sockets"),
        profile="default",
        provider="hermes-cloud",
        authority=authority,
        process_identity_provider=lambda _: _PROCESS_EVIDENCE,
    )
    endpoint = plugin_control_relay.ControlEndpoint(
        pid=os.getpid(),
        profile="default",
        socket_path=Path("/private/fixture-sockets/control.sock"),
        instance_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        runtime_generation="runtime-generation-1",
        host_bundle_id="com.nousresearch.hermes",
        process_identity=_PROCESS_EVIDENCE,
        socket_device=51,
        socket_inode=79,
    )
    monkeypatch.setattr(factory, "_discover", lambda: (endpoint,))
    monkeypatch.setattr(factory, "_require_endpoint_evidence", lambda _: None)

    async def missing_socket(**_kwargs: object):
        raise FileNotFoundError("must-never-appear")

    monkeypatch.setattr(plugin_control_relay, "unix_connect", missing_socket)

    with pytest.raises(OwnerControlCallFailed) as captured:
        await factory.open(
            scope=_scope(),
            request_id=UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
            timeout_seconds=0.1,
        )

    assert captured.value.code == 4214
    assert captured.value.reason == "owner_adapter_unavailable"
    assert "must-never-appear" not in str(captured.value)


@pytest.mark.asyncio
async def test_factory_attaches_once_and_reuses_one_socket_for_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    websocket = _WebSocket()
    factory, connections = _factory(monkeypatch, websocket)
    scope = _scope()

    channel = await factory.open(
        scope=scope,
        request_id=UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
        timeout_seconds=3,
    )
    acquired = await channel.execute(
        operation="session.control.acquire",
        request_id=UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
        body=MappingProxyType(
            {
                "runtime_session_id": "runtime-7",
            }
        ),
        timeout_seconds=2,
    )
    await channel.execute(
        operation="session.control.renew",
        request_id=UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd"),
        body=MappingProxyType({"lease_id": "fixture-lease-secret-not-real-0001"}),
        timeout_seconds=2,
    )
    await channel.execute(
        operation="session.control.release",
        request_id=UUID("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"),
        body=MappingProxyType({"lease_id": "fixture-lease-secret-not-real-0001"}),
        timeout_seconds=2,
    )
    status = await channel.execute(
        operation="session.control.status",
        request_id=UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc"),
        body=MappingProxyType({}),
        timeout_seconds=2,
    )

    assert len(connections) == 1
    assert [frame["method"] for frame in websocket.sent] == [
        "relay.control.attach",
        "session.control.acquire",
        "session.control.renew",
        "session.control.release",
        "session.control.status",
    ]
    assert websocket.sent[0]["params"] == {
        "claims": {
            "user_id": "principal-1",
            "provider": "hermes-cloud",
            "connection_role": "control",
            "client_instance_id": "22222222-2222-4222-8222-222222222222",
            "session_key": "durable-root-1",
            "profile": "default",
        }
    }
    assert websocket.sent[1]["params"] == {
        "session_key": "durable-root-1",
        "profile": "default",
        "runtime_session_id": "runtime-7",
        "runtime_generation": "runtime-generation-1",
    }
    for request in websocket.sent[2:4]:
        assert request["params"] == {
            "session_key": "durable-root-1",
            "profile": "default",
            "lease_id": "fixture-lease-secret-not-real-0001",
            "runtime_session_id": "runtime-7",
            "runtime_generation": "runtime-generation-1",
        }
    assert acquired["lease_id"] == "fixture-lease-secret-not-real-0001"
    assert status["controller_kind"] == "desktop"


@pytest.mark.asyncio
async def test_owner_channel_normalizes_legacy_local_status_at_plugin_ingress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    websocket = _LegacyLocalStatusWebSocket()
    factory, _connections = _factory(monkeypatch, websocket)
    channel = await factory.open(
        scope=_scope(),
        request_id=UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
        timeout_seconds=3,
    )

    status = await channel.execute(
        operation="session.control.status",
        request_id=UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc"),
        body=MappingProxyType({}),
        timeout_seconds=2,
    )

    assert status["controller_kind"] == "desktop"
    assert status["controller_label"] == "Hermes Desktop"


@pytest.mark.asyncio
async def test_owner_channel_maps_explicit_unknown_result_to_effect_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    websocket = _EffectUnknownWebSocket()
    factory, _connections = _factory(monkeypatch, websocket)
    channel = await factory.open(
        scope=_scope(),
        request_id=UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
        timeout_seconds=3,
    )

    with pytest.raises(OwnerControlOutcomeUnknown):
        await channel.execute(
            operation="session.interrupt",
            request_id=UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc"),
            body=MappingProxyType(
                {
                    "lease_id": "opaque-lease",
                    "client_request_id": "request-effect-unknown",
                }
            ),
            timeout_seconds=2,
        )


@pytest.mark.asyncio
async def test_owner_channel_forwards_safe_mobile_actions_with_authoritative_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    websocket = _WebSocket()
    factory, _connections = _factory(monkeypatch, websocket)
    channel = await factory.open(
        scope=_scope(),
        request_id=UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
        timeout_seconds=3,
    )
    cases = (
        (
            "session.command.status",
            {
                "method": "approval.respond",
                "client_request_id": "request-status",
            },
        ),
        (
            "prompt.submit",
            {
                "lease_id": "lease",
                "client_request_id": "request-prompt",
                "client_turn_id": "turn-prompt",
                "text": "Queue this turn",
            },
        ),
        (
            "session.interrupt",
            {"lease_id": "lease", "client_request_id": "request-interrupt"},
        ),
        (
            "session.steer",
            {
                "lease_id": "lease",
                "client_request_id": "request-steer",
                "text": "Focus on the first failure",
            },
        ),
        (
            "approval.respond",
            {
                "lease_id": "lease",
                "client_request_id": "request-approval",
                "request_id": "pending-approval",
                "choice": "allow_once",
            },
        ),
        (
            "clarify.respond",
            {
                "lease_id": "lease",
                "client_request_id": "request-clarify",
                "request_id": "pending-clarify",
                "choice_id": "choice-1",
            },
        ),
    )

    for index, (operation, body) in enumerate(cases, start=1):
        await channel.execute(
            operation=operation,
            request_id=UUID(f"00000000-0000-4000-8000-{index:012d}"),
            body=MappingProxyType(body),
            timeout_seconds=2,
        )

    for frame, (operation, body) in zip(websocket.sent[1:], cases, strict=True):
        assert frame["method"] == operation
        assert frame["params"] == {
            "session_key": "durable-root-1",
            "profile": "default",
            "runtime_generation": "runtime-generation-1",
            **body,
        }


@pytest.mark.asyncio
async def test_channel_maps_post_send_disconnect_to_unknown_without_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    websocket = _WebSocket()
    factory, _connections = _factory(monkeypatch, websocket)
    channel = await factory.open(
        scope=_scope(),
        request_id=UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
        timeout_seconds=3,
    )
    websocket.receive_failure = TimeoutError()

    with pytest.raises(OwnerControlOutcomeUnknown):
        await channel.execute(
            operation="session.control.renew",
            request_id=UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
            body=MappingProxyType({"lease_id": "fixture-lease-secret-not-real-0001"}),
            timeout_seconds=2,
        )

    assert [frame["method"] for frame in websocket.sent].count(
        "session.control.renew"
    ) == 1


@pytest.mark.asyncio
async def test_channel_maps_exact_plugin_error_and_closes_idempotently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    websocket = _WebSocket()
    factory, _connections = _factory(monkeypatch, websocket)
    channel = await factory.open(
        scope=_scope(),
        request_id=UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
        timeout_seconds=3,
    )

    async def receive_error() -> str:
        request = websocket.sent[-1]
        return json.dumps(
            {
                "jsonrpc": "2.0",
                "id": request["id"],
                "error": {"code": 4203, "message": "controller_conflict"},
            }
        )

    monkeypatch.setattr(websocket, "recv", receive_error)
    with pytest.raises(OwnerControlCallFailed) as captured:
        await channel.execute(
            operation="session.control.acquire",
            request_id=UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
            body=MappingProxyType({}),
            timeout_seconds=2,
        )

    assert captured.value.code == 4203
    assert captured.value.reason == "controller_conflict"
    await channel.close()
    await channel.close()
    assert websocket.closed == 1
