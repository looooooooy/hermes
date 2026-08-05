"""Lifecycle and cleanup behavior of the macOS Local Gateway transport."""

from __future__ import annotations

import socket
import struct
import threading
import time
from pathlib import Path

import pytest
from local_gateway_support import (
    _descriptor,
    _exchange,
    _hello,
    _settings,
    _started_bootstrap,
)


def test_stop_cancels_a_blocked_handshake_and_leaves_no_transport_thread(
    short_root: Path,
) -> None:
    baseline_threads = {(thread.name, thread.ident) for thread in threading.enumerate()}
    bootstrap = _started_bootstrap(
        short_root,
        first_frame_timeout_s=10.0,
        handshake_timeout_s=10.0,
    )
    _, descriptor = _descriptor(short_root / "registry")
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.settimeout(1.0)
    connection.connect(descriptor["socket_path"])

    started = time.monotonic()
    bootstrap.stop(timeout_s=0.5)
    elapsed = time.monotonic() - started
    connection.close()

    assert elapsed < 0.5
    assert {
        (thread.name, thread.ident) for thread in threading.enumerate()
    } == baseline_threads
    assert list((short_root / "registry").iterdir()) == []
    assert list((short_root / "sockets").iterdir()) == []


def test_stop_deadline_can_be_retried_from_stopping_to_finish_cleanup(
    short_root: Path,
) -> None:
    from hermes_agent_plugin.adapters.local_protocol.handshake_v1 import (
        LocalContractV1Adapter,
    )
    from hermes_agent_plugin.adapters.platform.macos.local_gateway_transport import (
        LocalTransportState,
        MacOSLocalGatewayResource,
    )
    from hermes_agent_plugin.domain.lifecycle import (
        LifecycleDeadlineExceeded,
    )

    handler_started = threading.Event()
    release_handler = threading.Event()
    adapter = LocalContractV1Adapter(runtime_generation="runtime-1")

    def blocking_handler(body: bytes) -> str:
        handler_started.set()
        release_handler.wait(timeout=1.0)
        return adapter.handle_hello(body)

    resource = MacOSLocalGatewayResource(
        settings=_settings(short_root),
        hello_handler=blocking_handler,
    )
    resource.start(time.monotonic() + 1.0)
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.settimeout(1.0)
    connection.connect(str(resource.socket_path))
    body = _hello()
    connection.sendall(struct.pack("!I", len(body)) + body)
    assert handler_started.wait(timeout=0.5)

    with pytest.raises(
        LifecycleDeadlineExceeded,
        match="lifecycle_deadline_exceeded",
    ):
        resource.stop(time.monotonic() + 0.02)

    assert resource.state is LocalTransportState.STOPPING
    assert resource.descriptor_path.exists() is False
    release_handler.set()
    connection.close()
    resource.stop(time.monotonic() + 1.0)

    assert resource.state is LocalTransportState.STOPPED
    assert resource.socket_path.exists() is False
    assert list((_settings(short_root).registry_directory).iterdir()) == []


def test_restart_replaces_stale_entries_and_changes_runtime_generation(
    short_root: Path,
) -> None:
    bootstrap = _started_bootstrap(
        short_root,
        generations=("runtime-1", "runtime-2"),
    )
    first_descriptor_path, first_descriptor = _descriptor(short_root / "registry")
    first_socket_path = Path(first_descriptor["socket_path"])
    assert _exchange(first_socket_path, _hello())["runtime_generation"] == ("runtime-1")

    bootstrap.stop()
    bootstrap.start()
    second_descriptor_path, second_descriptor = _descriptor(short_root / "registry")
    second_socket_path = Path(second_descriptor["socket_path"])

    assert second_descriptor_path == first_descriptor_path
    assert second_socket_path == first_socket_path
    assert second_descriptor["instance_id"] != first_descriptor["instance_id"]
    assert _exchange(second_socket_path, _hello())["runtime_generation"] == (
        "runtime-2"
    )

    bootstrap.stop()


def test_repeated_resource_start_and_stop_are_idempotent(
    short_root: Path,
) -> None:
    from hermes_agent_plugin.adapters.platform.macos.local_gateway_transport import (
        LocalTransportState,
        MacOSLocalGatewayResource,
    )

    resource = MacOSLocalGatewayResource(
        settings=_settings(short_root),
        hello_handler=lambda _body: "{}",
    )
    deadline = time.monotonic() + 1.0

    resource.start(deadline)
    first_instance = resource.instance_id
    resource.start(deadline)
    assert resource.instance_id == first_instance
    assert resource.state is LocalTransportState.READY

    resource.stop(deadline)
    resource.stop(deadline)
    assert resource.state is LocalTransportState.STOPPED


def test_untrusted_stale_paths_fail_closed_without_unlinking(
    short_root: Path,
) -> None:
    from hermes_agent_plugin.adapters.platform.macos.local_gateway_transport import (
        MacOSLocalGatewayResource,
    )

    settings = _settings(short_root)
    settings.registry_directory.mkdir(mode=0o700)
    settings.socket_directory.mkdir(mode=0o700)
    resource = MacOSLocalGatewayResource(
        settings=settings,
        hello_handler=lambda _body: "{}",
    )
    resource.socket_path.write_text("do-not-delete", encoding="utf-8")

    with pytest.raises(RuntimeError, match="untrusted stale local gateway"):
        resource.start(time.monotonic() + 1.0)

    assert resource.socket_path.read_text(encoding="utf-8") == "do-not-delete"


def test_descriptor_write_failure_removes_only_attempt_owned_resources(
    short_root: Path,
    monkeypatch,
) -> None:
    from hermes_agent_plugin.adapters.platform.macos import local_gateway_transport
    from hermes_agent_plugin.adapters.platform.macos.local_gateway_transport import (
        LocalTransportState,
        MacOSLocalGatewayResource,
    )

    resource = MacOSLocalGatewayResource(
        settings=_settings(short_root),
        hello_handler=lambda _body: "{}",
    )

    def fail_write(_descriptor: int, _body: bytes) -> int:
        raise OSError("synthetic descriptor write failure")

    monkeypatch.setattr(local_gateway_transport.os, "write", fail_write)
    with pytest.raises(
        OSError,
        match="synthetic descriptor write failure",
    ):
        resource.start(time.monotonic() + 1.0)

    assert resource.state is LocalTransportState.STOPPED
    assert list((short_root / "registry").iterdir()) == []
    assert list((short_root / "sockets").iterdir()) == []


def test_internal_handshake_failure_never_logs_exception_body(
    short_root: Path,
    caplog,
) -> None:
    from hermes_agent_plugin.adapters.platform.macos.local_gateway_transport import (
        MacOSLocalGatewayResource,
    )

    def fail_handshake(_body: bytes) -> str:
        raise RuntimeError("must-not-leak")

    resource = MacOSLocalGatewayResource(
        settings=_settings(short_root),
        hello_handler=fail_handshake,
    )
    resource.start(time.monotonic() + 1.0)

    assert _exchange(resource.socket_path, _hello()) == {
        "error": {"code": 4301, "reason": "invalid_envelope"}
    }
    resource.stop(time.monotonic() + 1.0)

    assert "must-not-leak" not in caplog.text


def test_native_uds_path_limit_is_rejected_before_filesystem_effect(
    short_root: Path,
) -> None:
    from hermes_agent_plugin.adapters.platform.macos.local_gateway_transport import (
        MacOSLocalGatewayResource,
    )

    long_socket_directory = short_root / ("x" * 100)
    settings = _settings(
        short_root,
        socket_directory=long_socket_directory,
    )

    with pytest.raises(
        ValueError,
        match="unix socket path exceeds native limit",
    ):
        MacOSLocalGatewayResource(
            settings=settings,
            hello_handler=lambda _body: "{}",
        )

    assert long_socket_directory.exists() is False


@pytest.mark.parametrize("pid", (0, -1, 2_147_483_648, True))
def test_settings_enforce_discovery_descriptor_pid_schema(
    short_root: Path,
    pid: object,
) -> None:
    from hermes_agent_plugin.adapters.platform.macos.local_gateway_transport import (
        _MacOSLocalGatewaySettings,
    )

    with pytest.raises(ValueError, match="pid must be a valid POSIX pid"):
        _MacOSLocalGatewaySettings(
            profile="default",
            registry_directory=short_root / "registry",
            socket_directory=short_root / "sockets",
            pid=pid,
        )
