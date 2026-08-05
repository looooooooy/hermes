"""Shared fixtures for verified macOS Local Gateway tests."""

from __future__ import annotations

import json
import socket
import struct
from pathlib import Path

from tests.test_support.local_gateway_runtime import LocalGatewayTestRuntime
from tests.test_support.runtime_descriptor_v2 import runtime_authority_v2

from hermes_agent_plugin.adapters.platform.macos.local_gateway_paths import (
    MacOSLocalGatewayPaths,
)
from hermes_agent_plugin.adapters.platform.macos.local_gateway_transport import (
    _MacOSLocalGatewaySettings,
)


def _hello(**overrides: object) -> bytes:
    payload: dict[str, object] = {
        "contract_version": 1,
        "message_type": "local.hello",
        "client_instance_id": "11111111-1111-4111-8111-111111111111",
        "profile": "default",
        "required_capabilities": ["session.observe"],
        "optional_capabilities": ["session.control"],
    }
    payload.update(overrides)
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def _recv_exact(connection: socket.socket, size: int) -> bytes:
    body = bytearray()
    while len(body) < size:
        chunk = connection.recv(size - len(body))
        if not chunk:
            raise EOFError("local gateway closed before a complete frame")
        body.extend(chunk)
    return bytes(body)


def _exchange(socket_path: Path, body: bytes) -> dict:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(1.0)
        connection.connect(str(socket_path))
        connection.sendall(struct.pack("!I", len(body)) + body)
        response_size = struct.unpack("!I", _recv_exact(connection, 4))[0]
        response = _recv_exact(connection, response_size)
        assert connection.recv(1) == b""
    return json.loads(response.decode("utf-8", errors="strict"))


def _settings(
    tmp_path: Path,
    **overrides: object,
):
    values: dict[str, object] = {
        "profile": "default",
        "registry_directory": tmp_path / "registry",
        "socket_directory": tmp_path / "sockets",
        "authority": runtime_authority_v2(runtime_generation="runtime-1"),
    }
    values.update(overrides)
    return _MacOSLocalGatewaySettings(**values)


def _paths(root: Path) -> MacOSLocalGatewayPaths:
    return MacOSLocalGatewayPaths(
        local_gateway_registry_directory=root / "registry",
        local_gateway_socket_directory=root / "sockets",
        control_registry_directory=root / "control-registry",
        control_socket_directory=root / "control-sockets",
        observer_registry_directory=root / "observer-registry",
        observer_socket_directory=root / "observer-sockets",
    )


def _started_bootstrap(
    tmp_path: Path,
    *,
    generations: tuple[str, ...] = ("runtime-1",),
    **settings_overrides: object,
) -> LocalGatewayTestRuntime:
    generation_values = iter(generations)
    bootstrap = LocalGatewayTestRuntime(
        generation_factory=lambda: next(generation_values),
        macos_local_gateway_paths=_paths(tmp_path),
        macos_local_gateway_options=settings_overrides,
    )
    bootstrap.install()
    bootstrap.start()
    return bootstrap


def _descriptor(registry_directory: Path) -> tuple[Path, dict]:
    descriptors = list(registry_directory.glob("gateway-*.json"))
    assert len(descriptors) == 1
    path = descriptors[0]
    return path, json.loads(path.read_text(encoding="utf-8"))
