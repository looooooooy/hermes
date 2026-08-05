"""Frozen protocol behavior of the macOS Local Gateway transport."""

from __future__ import annotations

import json
import os
import socket
import stat
import struct
import time
from pathlib import Path
from uuid import UUID

import pytest
from local_gateway_support import (
    _descriptor,
    _exchange,
    _hello,
    _recv_exact,
    _settings,
    _started_bootstrap,
)

from hermes_agent_plugin.domain.lifecycle import GatewayState


def test_transport_transition_table_matches_ascii_state_machine() -> None:
    from hermes_agent_plugin.adapters.platform.macos.local_gateway_transport import (
        LOCAL_TRANSPORT_TRANSITIONS,
        LocalTransportState,
        MacOSLocalGatewayResource,
    )

    assert LOCAL_TRANSPORT_TRANSITIONS == {
        LocalTransportState.NEW: frozenset({LocalTransportState.STARTING}),
        LocalTransportState.STARTING: frozenset(
            {
                LocalTransportState.READY,
                LocalTransportState.STOPPING,
            }
        ),
        LocalTransportState.READY: frozenset(
            {
                LocalTransportState.DRAINING,
                LocalTransportState.STOPPING,
            }
        ),
        LocalTransportState.DRAINING: frozenset({LocalTransportState.STOPPING}),
        LocalTransportState.STOPPING: frozenset({LocalTransportState.STOPPED}),
        LocalTransportState.STOPPED: frozenset({LocalTransportState.STARTING}),
    }
    assert "NEW -> STARTING -> READY -> DRAINING" in (
        MacOSLocalGatewayResource.__doc__ or ""
    )


def test_bootstrap_publishes_private_descriptor_and_serves_local_welcome(
    short_root: Path,
) -> None:
    bootstrap = _started_bootstrap(short_root)
    registry = short_root / "registry"
    descriptor_path, descriptor = _descriptor(registry)
    socket_path = Path(descriptor["socket_path"])

    assert descriptor["version"] == 2
    assert descriptor["pid"] == os.getpid()
    assert descriptor["profile"] == "default"
    assert descriptor["runtime_generation"] == "runtime-1"
    assert descriptor["socket_path"] == str(socket_path)
    assert descriptor["host_bundle_id"] == "com.nousresearch.hermes"
    assert set(descriptor) == {
        "version",
        "pid",
        "profile",
        "runtime_generation",
        "socket_path",
        "instance_id",
        "process_start_time_ns",
        "process_executable",
        "process_executable_device",
        "process_executable_inode",
        "host_bundle_id",
    }
    assert str(UUID(descriptor["instance_id"])) == descriptor["instance_id"]
    assert stat.S_IMODE(registry.stat().st_mode) == 0o700
    assert stat.S_IMODE((short_root / "sockets").stat().st_mode) == 0o700
    assert stat.S_IMODE(descriptor_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(socket_path.stat().st_mode) == 0o600

    assert _exchange(socket_path, _hello()) == {
        "contract_version": 1,
        "message_type": "local.welcome",
        "runtime_generation": "runtime-1",
        "profile": "default",
        "accepted_capabilities": ["session.control", "session.observe"],
        "unavailable_optional_capabilities": [],
    }

    bootstrap.stop()
    assert bootstrap.state is GatewayState.STOPPED
    assert list(registry.iterdir()) == []
    assert list((short_root / "sockets").iterdir()) == []


@pytest.mark.parametrize(
    ("body", "code", "reason"),
    [
        (_hello(contract_version=2), 4300, "contract_unsupported"),
        (b'{"not":"a hello"}', 4301, "invalid_envelope"),
        (b'{"profile":"\xff"}', 4303, "invalid_utf8"),
        (
            _hello(
                required_capabilities=["session.observe", "view.card"],
            ),
            4304,
            "capability_not_available",
        ),
    ],
)
def test_transport_maps_untrusted_handshake_failures_to_safe_error_frames(
    short_root: Path,
    body: bytes,
    code: int,
    reason: str,
) -> None:
    bootstrap = _started_bootstrap(short_root)
    _, descriptor = _descriptor(short_root / "registry")

    assert _exchange(Path(descriptor["socket_path"]), body) == {
        "error": {"code": code, "reason": reason}
    }

    bootstrap.stop()


def test_oversized_length_is_rejected_before_body_read(
    short_root: Path,
) -> None:
    bootstrap = _started_bootstrap(short_root)
    _, descriptor = _descriptor(short_root / "registry")
    socket_path = Path(descriptor["socket_path"])

    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(1.0)
        connection.connect(str(socket_path))
        connection.sendall(struct.pack("!I", 262_145))
        response_size = struct.unpack("!I", _recv_exact(connection, 4))[0]
        response = json.loads(_recv_exact(connection, response_size).decode("utf-8"))

    assert response == {"error": {"code": 4302, "reason": "frame_too_large"}}
    bootstrap.stop()


@pytest.mark.parametrize(
    "body",
    (
        b"",
        b'{"contract_version":1,"contract_version":1}',
        _hello(profile="another-profile"),
    ),
    ids=("zero-length", "duplicate-key", "profile-mismatch"),
)
def test_transport_rejects_frozen_protocol_edge_cases(
    short_root: Path,
    body: bytes,
) -> None:
    bootstrap = _started_bootstrap(short_root)
    _, descriptor = _descriptor(short_root / "registry")

    assert _exchange(Path(descriptor["socket_path"]), body) == {
        "error": {"code": 4301, "reason": "invalid_envelope"}
    }
    bootstrap.stop()


@pytest.mark.parametrize(
    "wire",
    (
        b"\x00\x00",
        struct.pack("!I", 8) + b"{",
    ),
    ids=("truncated-prefix", "truncated-body"),
)
def test_truncated_frame_closes_without_invoking_business_effect(
    short_root: Path,
    wire: bytes,
) -> None:
    calls: list[bytes] = []
    from hermes_agent_plugin.adapters.platform.macos.local_gateway_transport import (
        MacOSLocalGatewayResource,
    )

    resource = MacOSLocalGatewayResource(
        settings=_settings(short_root),
        hello_handler=lambda body: calls.append(body) or "{}",
    )
    resource.start(time.monotonic() + 1.0)

    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(1.0)
        connection.connect(str(resource.socket_path))
        connection.sendall(wire)
        connection.shutdown(socket.SHUT_WR)
        response_size = struct.unpack("!I", _recv_exact(connection, 4))[0]
        response = json.loads(_recv_exact(connection, response_size).decode("utf-8"))

    assert response == {"error": {"code": 4301, "reason": "invalid_envelope"}}
    assert calls == []
    resource.stop(time.monotonic() + 1.0)


def test_body_read_deadline_returns_4306_before_handler(
    short_root: Path,
) -> None:
    calls: list[bytes] = []
    from hermes_agent_plugin.adapters.platform.macos.local_gateway_transport import (
        MacOSLocalGatewayResource,
    )

    resource = MacOSLocalGatewayResource(
        settings=_settings(
            short_root,
            read_timeout_s=0.03,
            handshake_timeout_s=0.2,
        ),
        hello_handler=lambda body: calls.append(body) or "{}",
    )
    resource.start(time.monotonic() + 1.0)

    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(1.0)
        connection.connect(str(resource.socket_path))
        connection.sendall(struct.pack("!I", 8) + b"{")
        response_size = struct.unpack("!I", _recv_exact(connection, 4))[0]
        response = json.loads(_recv_exact(connection, response_size).decode("utf-8"))

    assert response == {
        "error": {
            "code": 4306,
            "reason": "deadline_exceeded_before_effect",
        }
    }
    assert calls == []
    resource.stop(time.monotonic() + 1.0)


def test_second_request_is_never_processed_on_one_connection(
    short_root: Path,
) -> None:
    from hermes_agent_plugin.adapters.local_protocol.handshake_v1 import (
        LocalContractV1Adapter,
    )
    from hermes_agent_plugin.adapters.platform.macos.local_gateway_transport import (
        MacOSLocalGatewayResource,
    )

    calls: list[bytes] = []
    adapter = LocalContractV1Adapter(runtime_generation="runtime-1")

    def handle(body: bytes) -> str:
        calls.append(body)
        return adapter.handle_hello(body)

    resource = MacOSLocalGatewayResource(
        settings=_settings(short_root),
        hello_handler=handle,
    )
    resource.start(time.monotonic() + 1.0)
    body = _hello()
    wire = struct.pack("!I", len(body)) + body

    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(1.0)
        connection.connect(str(resource.socket_path))
        connection.sendall(wire + wire)
        response_size = struct.unpack("!I", _recv_exact(connection, 4))[0]
        response = json.loads(_recv_exact(connection, response_size).decode("utf-8"))
        assert connection.recv(1) == b""

    assert response["message_type"] == "local.welcome"
    assert len(calls) == 1
    resource.stop(time.monotonic() + 1.0)


def test_first_frame_deadline_returns_4306_without_waiting_forever(
    short_root: Path,
) -> None:
    bootstrap = _started_bootstrap(
        short_root,
        first_frame_timeout_s=0.03,
        handshake_timeout_s=0.2,
    )
    _, descriptor = _descriptor(short_root / "registry")

    started = time.monotonic()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(1.0)
        connection.connect(descriptor["socket_path"])
        response_size = struct.unpack("!I", _recv_exact(connection, 4))[0]
        response = json.loads(_recv_exact(connection, response_size).decode("utf-8"))
    elapsed = time.monotonic() - started

    assert response == {
        "error": {
            "code": 4306,
            "reason": "deadline_exceeded_before_effect",
        }
    }
    assert elapsed < 0.5
    bootstrap.stop()
