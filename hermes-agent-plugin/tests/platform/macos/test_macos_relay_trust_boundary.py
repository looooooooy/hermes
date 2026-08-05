"""macOS filesystem trust boundaries owned by canonical relay adapters."""

from __future__ import annotations

import json
import os
import socket
import threading
import time
from pathlib import Path

import pytest
from local_trust_support import (
    _bind_unix_socket,
)
from tests.test_support.runtime_descriptor_v2 import runtime_authority_v2

from hermes_agent_plugin.adapters.platform.macos.runtime_descriptor_v2 import (
    encode_runtime_descriptor_v2,
)


def _relay_module(relay_name: str):
    if relay_name == "observer":
        from hermes_agent_plugin.adapters.platform.macos import observer_relay

        return observer_relay
    from hermes_agent_plugin.adapters.platform.macos import control_relay

    return control_relay


def _set_relay_directories(
    monkeypatch,
    relay_name: str,
    *,
    registry: Path,
    sockets: Path,
) -> None:
    prefix = relay_name.upper()
    monkeypatch.setenv(f"HERMES_{prefix}_REGISTRY_DIR", str(registry))
    monkeypatch.setenv(f"HERMES_{prefix}_SOCKET_DIR", str(sockets))


@pytest.mark.parametrize("relay_name", ["observer", "control"])
@pytest.mark.parametrize("profile", ["", "bad/profile"])
def test_relay_registration_rejects_invalid_profile_before_starting_server(
    relay_name: str,
    profile: str,
    monkeypatch,
) -> None:
    module = _relay_module(relay_name)
    server_started = False

    def fail_if_started(*args, **kwargs):
        nonlocal server_started
        server_started = True
        raise AssertionError("server must not start")

    monkeypatch.setattr(module, "unix_serve", fail_if_started)

    with pytest.raises(ValueError, match="profile is invalid"):
        runtime_authority_v2(profile=profile)

    assert server_started is False


class _FakeServer:
    def serve_forever(self) -> None:
        return None

    def shutdown(self) -> None:
        return None


class _BoundUnixServer:
    def __init__(self, path: Path, *, fail_shutdown: bool = False) -> None:
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socket.bind(str(path))
        self.shutdown_calls = 0
        self.fail_shutdown = fail_shutdown

    def serve_forever(self) -> None:
        return None

    def shutdown(self) -> None:
        self.shutdown_calls += 1
        self.socket.close()
        if self.fail_shutdown:
            raise RuntimeError("server shutdown failed")


class _IdleThread:
    def is_alive(self) -> bool:
        return False


class _RecordingDispatcher:
    def __init__(self) -> None:
        self.shutdown_calls: list[tuple[bool, bool]] = []

    def submit(self, fn, /, *args, **kwargs):
        raise AssertionError("submit must not be called")

    def shutdown(self, *, wait: bool, cancel_futures: bool) -> None:
        self.shutdown_calls.append((wait, cancel_futures))


class _RetryShutdownServer:
    def __init__(self) -> None:
        self.release = threading.Event()
        self.shutdown_calls = 0

    def serve_forever(self) -> None:
        self.release.wait()

    def shutdown(self) -> None:
        self.shutdown_calls += 1
        if self.shutdown_calls == 1:
            raise RuntimeError("server shutdown failed")
        self.release.set()


class _ReleaseOnCloseSocket:
    def __init__(self, server_socket: socket.socket, release: threading.Event) -> None:
        self._server_socket = server_socket
        self._release = release

    def close(self) -> None:
        self._server_socket.close()
        self._release.set()


class _FailingShutdownBoundServer:
    def __init__(self, path: Path) -> None:
        self.release = threading.Event()
        server_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server_socket.bind(str(path))
        self.socket = _ReleaseOnCloseSocket(server_socket, self.release)
        self.shutdown_calls = 0

    def serve_forever(self) -> None:
        self.release.wait()

    def shutdown(self) -> None:
        self.shutdown_calls += 1
        raise RuntimeError("injected shutdown failure")


@pytest.mark.parametrize("relay_name", ["observer", "control"])
def test_relay_registration_refuses_non_socket_created_by_server(
    relay_name: str,
    tmp_path: Path,
    monkeypatch,
) -> None:
    module = _relay_module(relay_name)
    registry = tmp_path / f"{relay_name}-registry"
    sockets = Path("/tmp").resolve(strict=True) / (
        f"hap-{os.getpid()}-{relay_name}-{tmp_path.stat().st_ino}"
    )
    _set_relay_directories(
        monkeypatch,
        relay_name,
        registry=registry,
        sockets=sockets,
    )

    def fake_unix_serve(*args, **kwargs):
        Path(kwargs["path"]).touch()
        return _FakeServer()

    monkeypatch.setattr(module, "unix_serve", fake_unix_serve)

    with pytest.raises(RuntimeError, match="untrusted local relay socket"):
        if relay_name == "observer":
            module.start_observer_endpoint(
                authority=runtime_authority_v2(),
                dispatch=lambda request, transport: None,
                remove_observer_subscriptions=lambda transport: None,
            )
        else:
            module.start_control_endpoint(
                authority=runtime_authority_v2(),
                dispatcher=lambda request, transport: None,
            )

    assert list(registry.glob("gateway-*.json")) == []


def test_control_start_chmod_failure_unwinds_server_dispatcher_and_socket(
    tmp_path: Path,
    monkeypatch,
    short_root: Path,
) -> None:
    from hermes_agent_plugin.adapters.platform.macos import control_relay

    registry = tmp_path / "control-registry"
    sockets = short_root / "control-sockets"
    _set_relay_directories(
        monkeypatch,
        "control",
        registry=registry,
        sockets=sockets,
    )
    servers: list[_BoundUnixServer] = []

    def bind_server(*_args, **kwargs):
        server = _BoundUnixServer(Path(kwargs["path"]))
        servers.append(server)
        return server

    real_chmod = os.chmod

    def fail_socket_chmod(path, mode):
        if str(path).endswith(".sock"):
            raise OSError("injected socket chmod failure")
        real_chmod(path, mode)

    baseline_workers = {
        thread.ident
        for thread in threading.enumerate()
        if thread.name.startswith("control-owner-action-")
    }
    monkeypatch.setattr(control_relay, "unix_serve", bind_server)
    monkeypatch.setattr(control_relay.os, "chmod", fail_socket_chmod)

    with pytest.raises(OSError, match="injected socket chmod failure"):
        control_relay.start_control_endpoint(
            authority=runtime_authority_v2(),
            dispatcher=lambda _request, _transport: None,
        )

    assert len(servers) == 1
    assert servers[0].shutdown_calls == 1
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        current_workers = {
            thread.ident
            for thread in threading.enumerate()
            if thread.name.startswith("control-owner-action-")
        }
        if not current_workers - baseline_workers:
            break
        time.sleep(0.01)
    assert not current_workers - baseline_workers
    assert list(sockets.iterdir()) == []


def test_control_registration_close_cleans_every_owned_resource_after_server_error(
    tmp_path: Path,
    short_root: Path,
) -> None:
    from hermes_agent_plugin.adapters.platform.macos.control_relay import (
        ControlEndpointRegistration,
    )

    registry = tmp_path / "registry"
    sockets = short_root / "sockets"
    registry.mkdir(mode=0o700)
    sockets.mkdir(mode=0o700)
    instance_id = "instance-1"
    registration_path = registry / "gateway-1-instance-1.json"
    registration_path.write_text(
        json.dumps({"instance_id": instance_id}),
        encoding="utf-8",
    )
    registration_path.chmod(0o600)
    socket_path = sockets / "control.sock"
    server = _BoundUnixServer(socket_path, fail_shutdown=True)
    socket_path.chmod(0o600)
    dispatcher = _RecordingDispatcher()
    registration = ControlEndpointRegistration(
        registration_path,
        socket_path,
        instance_id,
        server,
        _IdleThread(),
        dispatcher,
    )

    with pytest.raises(RuntimeError, match="server shutdown failed"):
        registration.close()

    assert dispatcher.shutdown_calls == [(False, True)]
    assert registration_path.exists() is False
    assert socket_path.exists() is False


@pytest.mark.parametrize("relay_name", ("control", "observer"))
def test_relay_registry_write_failure_unlinks_temp_registry_file(
    relay_name: str,
    tmp_path: Path,
    monkeypatch,
    short_root: Path,
) -> None:
    module = _relay_module(relay_name)
    registry = tmp_path / f"{relay_name}-registry"
    sockets = short_root / f"{relay_name}-sockets"
    _set_relay_directories(
        monkeypatch,
        relay_name,
        registry=registry,
        sockets=sockets,
    )

    def fail_publication(**_kwargs):
        raise OSError("injected registry write failure")

    monkeypatch.setattr(module, "publish_runtime_descriptor_v2", fail_publication)

    with pytest.raises(OSError, match="injected registry write failure"):
        if relay_name == "observer":
            module.start_observer_endpoint(
                authority=runtime_authority_v2(),
                dispatch=lambda _request, _transport: None,
                remove_observer_subscriptions=lambda _transport: None,
            )
        else:
            module.start_control_endpoint(
                authority=runtime_authority_v2(),
                dispatcher=lambda _request, _transport: None,
            )

    assert list(registry.iterdir()) == []
    assert list(sockets.iterdir()) == []


def test_control_registration_close_can_retry_until_relay_thread_stops(
    tmp_path: Path,
    short_root: Path,
) -> None:
    from hermes_agent_plugin.adapters.platform.macos.control_relay import (
        ControlEndpointRegistration,
    )

    registry = tmp_path / "retry-registry"
    sockets = short_root / "retry-sockets"
    registry.mkdir(mode=0o700)
    sockets.mkdir(mode=0o700)
    instance_id = "retry-instance"
    registration_path = registry / "gateway-1-retry.json"
    registration_path.write_text(
        json.dumps({"instance_id": instance_id}),
        encoding="utf-8",
    )
    registration_path.chmod(0o600)
    socket_path = sockets / "retry.sock"
    bound_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    bound_socket.bind(str(socket_path))
    bound_socket.close()
    socket_path.chmod(0o600)
    server = _RetryShutdownServer()
    relay_thread = threading.Thread(
        target=server.serve_forever,
        name="retry-control-relay",
        daemon=True,
    )
    relay_thread.start()
    registration = ControlEndpointRegistration(
        registration_path,
        socket_path,
        instance_id,
        server,
        relay_thread,
    )

    with pytest.raises(RuntimeError, match="server shutdown failed"):
        registration.close()

    assert relay_thread.is_alive()
    assert registration_path.exists()
    registration.close()
    relay_thread.join(timeout=1.0)
    assert relay_thread.is_alive() is False
    assert registration_path.exists() is False
    assert socket_path.exists() is False


@pytest.mark.parametrize("relay_name", ("control", "observer"))
def test_relay_start_rollback_force_stops_server_when_shutdown_raises(
    relay_name: str,
    tmp_path: Path,
    monkeypatch,
    short_root: Path,
) -> None:
    module = _relay_module(relay_name)
    registry = tmp_path / f"{relay_name}-rollback-registry"
    sockets = short_root / f"{relay_name}-rollback-sockets"
    _set_relay_directories(
        monkeypatch,
        relay_name,
        registry=registry,
        sockets=sockets,
    )
    servers: list[_FailingShutdownBoundServer] = []

    def bind_server(*_args, **kwargs):
        server = _FailingShutdownBoundServer(Path(kwargs["path"]))
        servers.append(server)
        return server

    monkeypatch.setattr(module, "unix_serve", bind_server)

    def fail_publication(**_kwargs):
        raise OSError("injected registry write failure")

    monkeypatch.setattr(module, "publish_runtime_descriptor_v2", fail_publication)

    with pytest.raises(OSError, match="injected registry write failure"):
        if relay_name == "observer":
            module.start_observer_endpoint(
                authority=runtime_authority_v2(),
                dispatch=lambda _request, _transport: None,
                remove_observer_subscriptions=lambda _transport: None,
            )
        else:
            module.start_control_endpoint(
                authority=runtime_authority_v2(),
                dispatcher=lambda _request, _transport: None,
            )

    assert len(servers) == 1
    assert servers[0].shutdown_calls == 1
    assert list(registry.iterdir()) == []
    assert list(sockets.iterdir()) == []
    assert not any(
        thread.is_alive() and thread.name.startswith(f"{relay_name}-socket-")
        for thread in threading.enumerate()
    )


def _endpoint_registry(
    *,
    relay_name: str,
    registry: Path,
    socket_path: Path,
    profile: str = "default",
) -> Path:
    path = registry / "gateway-123-instance.json"
    payload = encode_runtime_descriptor_v2(
        runtime_authority_v2(),
        socket_path=socket_path,
    )
    payload["profile"] = profile
    path.write_text(
        json.dumps(payload),
        encoding="utf-8",
    )
    path.chmod(0o600)
    return path


@pytest.mark.parametrize("relay_name", ["observer", "control"])
def test_relay_enumeration_accepts_only_valid_profile_and_socket(
    relay_name: str,
    short_private_directory: Path,
    monkeypatch,
) -> None:
    module = _relay_module(relay_name)
    root = short_private_directory.resolve(strict=False)
    registry = root / f"{relay_name}-registry"
    sockets = root / f"{relay_name}-sockets"
    registry.mkdir(mode=0o700)
    sockets.mkdir(mode=0o700)
    socket_path = sockets / "owner.sock"
    server = _bind_unix_socket(socket_path)
    _set_relay_directories(
        monkeypatch,
        relay_name,
        registry=registry,
        sockets=sockets,
    )
    registration_path = _endpoint_registry(
        relay_name=relay_name,
        registry=registry,
        socket_path=socket_path,
    )
    try:
        endpoints = (
            module.list_observer_endpoints()
            if relay_name == "observer"
            else module.list_control_endpoints()
        )
        assert len(endpoints) == 1

        payload = json.loads(registration_path.read_text(encoding="utf-8"))
        payload["profile"] = "bad/profile"
        registration_path.write_text(json.dumps(payload), encoding="utf-8")
        registration_path.chmod(0o600)
        assert (
            module.list_observer_endpoints()
            if relay_name == "observer"
            else module.list_control_endpoints()
        ) == []

        payload["profile"] = "default"
        payload["instance_id"] = "11111111111141118111111111111111"
        registration_path.write_text(json.dumps(payload), encoding="utf-8")
        registration_path.chmod(0o600)
        assert (
            module.list_observer_endpoints()
            if relay_name == "observer"
            else module.list_control_endpoints()
        ) == []
    finally:
        server.close()


@pytest.mark.parametrize("relay_name", ["observer", "control"])
def test_relay_enumeration_rejects_non_private_registry_file(
    relay_name: str,
    short_private_directory: Path,
    monkeypatch,
) -> None:
    module = _relay_module(relay_name)
    registry = short_private_directory / f"{relay_name}-registry"
    sockets = short_private_directory / f"{relay_name}-sockets"
    registry.mkdir(mode=0o700)
    sockets.mkdir(mode=0o700)
    socket_path = sockets / "owner.sock"
    server = _bind_unix_socket(socket_path)
    _set_relay_directories(
        monkeypatch,
        relay_name,
        registry=registry,
        sockets=sockets,
    )
    path = _endpoint_registry(
        relay_name=relay_name,
        registry=registry,
        socket_path=socket_path,
    )
    path.chmod(0o644)
    try:
        assert (
            module.list_observer_endpoints()
            if relay_name == "observer"
            else module.list_control_endpoints()
        ) == []
    finally:
        server.close()


@pytest.mark.parametrize("relay_name", ["observer", "control"])
@pytest.mark.parametrize("socket_kind", ["regular", "fifo", "symlink"])
def test_relay_enumeration_rejects_non_socket_endpoint(
    relay_name: str,
    socket_kind: str,
    short_private_directory: Path,
    monkeypatch,
) -> None:
    module = _relay_module(relay_name)
    registry = short_private_directory / f"{relay_name}-registry"
    sockets = short_private_directory / f"{relay_name}-sockets"
    registry.mkdir(mode=0o700)
    sockets.mkdir(mode=0o700)
    socket_path = sockets / "owner.sock"
    server = None
    if socket_kind == "regular":
        socket_path.touch(mode=0o600)
    elif socket_kind == "fifo":
        os.mkfifo(socket_path, mode=0o600)
    else:
        target = sockets / "target.sock"
        server = _bind_unix_socket(target)
        socket_path.symlink_to(target)
    _set_relay_directories(
        monkeypatch,
        relay_name,
        registry=registry,
        sockets=sockets,
    )
    _endpoint_registry(
        relay_name=relay_name,
        registry=registry,
        socket_path=socket_path,
    )
    try:
        assert (
            module.list_observer_endpoints()
            if relay_name == "observer"
            else module.list_control_endpoints()
        ) == []
    finally:
        if server is not None:
            server.close()


@pytest.mark.parametrize("relay_name", ["observer", "control"])
def test_dead_endpoint_cleanup_never_deletes_regular_socket_path(
    relay_name: str,
    short_private_directory: Path,
    monkeypatch,
) -> None:
    module = _relay_module(relay_name)
    registry = short_private_directory / f"{relay_name}-registry"
    sockets = short_private_directory / f"{relay_name}-sockets"
    registry.mkdir(mode=0o700)
    sockets.mkdir(mode=0o700)
    victim = sockets / "victim.sock"
    victim.write_text("must survive", encoding="utf-8")
    victim.chmod(0o600)
    _set_relay_directories(
        monkeypatch,
        relay_name,
        registry=registry,
        sockets=sockets,
    )
    _endpoint_registry(
        relay_name=relay_name,
        registry=registry,
        socket_path=victim,
    )
    monkeypatch.setattr(module, "_pid_is_alive", lambda pid: False)

    assert (
        module.list_observer_endpoints()
        if relay_name == "observer"
        else module.list_control_endpoints()
    ) == []
    assert victim.read_text(encoding="utf-8") == "must survive"


@pytest.mark.parametrize("relay_name", ["observer", "control"])
def test_relay_enumeration_does_not_read_or_remove_registry_symlink(
    relay_name: str,
    short_private_directory: Path,
    monkeypatch,
) -> None:
    module = _relay_module(relay_name)
    registry = short_private_directory / f"{relay_name}-registry"
    sockets = short_private_directory / f"{relay_name}-sockets"
    registry.mkdir(mode=0o700)
    sockets.mkdir(mode=0o700)
    target = short_private_directory / "outside.json"
    target.write_text(
        '{"version":1,"ws_url":"must-not-be-read"}',
        encoding="utf-8",
    )
    target.chmod(0o600)
    path = registry / "gateway-123-malicious.json"
    path.symlink_to(target)
    _set_relay_directories(
        monkeypatch,
        relay_name,
        registry=registry,
        sockets=sockets,
    )

    assert (
        module.list_observer_endpoints()
        if relay_name == "observer"
        else module.list_control_endpoints()
    ) == []
    assert path.is_symlink()
    assert target.exists()
