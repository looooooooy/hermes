"""Shared Control E2E harness must not depend on Plugin shadow Core DTOs."""

from __future__ import annotations

import json
import socket
import struct
import sys
import tempfile
from pathlib import Path

import pytest
from hermes_agent_plugin.adapters.host import spi_v1
from hermes_agent_plugin.adapters.host.extension import _HostOwnerActionAdapter
from hermes_agent_plugin.adapters.local_protocol.handshake_v1 import (
    LocalHello,
    decode_local_welcome,
    encode_local_hello,
)
from hermes_agent_plugin.adapters.platform.macos.local_gateway_paths import (
    MacOSLocalGatewayPaths,
)

from tests.e2e.control_pipeline.harness import GatewayExtensionV1ControlTestHost
from tests.test_support.host_spi_v1 import TestOwnerActionResult


def test_harness_owner_result_is_opaque_test_support() -> None:
    result = TestOwnerActionResult(
        status="accepted",
        payload={"server_turn_id": "server-turn-1"},
    )

    assert _HostOwnerActionAdapter._result(result) == (
        "accepted",
        {"server_turn_id": "server-turn-1"},
    )
    assert (
        GatewayExtensionV1ControlTestHost.invoke_owner_action.__annotations__["return"]
        == "TestOwnerActionResult"
    )
    assert not hasattr(spi_v1, "OwnerActionResult")


def _receive_exact(connection: socket.socket, size: int) -> bytes:
    body = bytearray()
    while len(body) < size:
        chunk = connection.recv(size - len(body))
        if not chunk:
            raise EOFError("local gateway closed before a complete frame")
        body.extend(chunk)
    return bytes(body)


@pytest.mark.skipif(sys.platform != "darwin", reason="requires macOS UDS backend")
def test_control_host_opens_real_local_gateway_and_closes_endpoints_in_reverse() -> (
    None
):
    with tempfile.TemporaryDirectory(prefix="hcp-", dir="/tmp") as raw_root:
        root = Path(raw_root).resolve(strict=True)
        root.chmod(0o700)
        paths = MacOSLocalGatewayPaths(
            local_gateway_registry_directory=root / "local-registry",
            local_gateway_socket_directory=root / "local-sockets",
            control_registry_directory=root / "control-registry",
            control_socket_directory=root / "control-sockets",
            observer_registry_directory=root / "observer-registry",
            observer_socket_directory=root / "observer-sockets",
        )
        host = GatewayExtensionV1ControlTestHost(
            paths,
            profile="default",
            runtime_generation="runtime-generation-1",
        )

        host.start()
        assert host.active_endpoint_roles == (
            "control",
            "local-gateway",
            "observer",
        )
        descriptors = list(
            paths.local_gateway_registry_directory.glob("gateway-*.json")
        )
        assert len(descriptors) == 1
        descriptor = json.loads(descriptors[0].read_text(encoding="utf-8"))
        hello = encode_local_hello(
            LocalHello(
                contract_version=1,
                message_type="local.hello",
                client_instance_id="11111111-1111-4111-8111-111111111111",
                profile="default",
                required_capabilities=("session.observe",),
                optional_capabilities=("session.control",),
                extensions={},
            )
        ).encode("utf-8", errors="strict")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(1.0)
            connection.connect(descriptor["socket_path"])
            connection.sendall(struct.pack("!I", len(hello)) + hello)
            response_size = struct.unpack("!I", _receive_exact(connection, 4))[0]
            welcome = decode_local_welcome(_receive_exact(connection, response_size))

        assert welcome.runtime_generation == "runtime-generation-1"
        assert welcome.accepted_capabilities == (
            "session.control",
            "session.observe",
        )

        host.close()
        host.close()

        assert host.active_endpoint_roles == ()
        assert host.endpoint_events == [
            "open:local-gateway",
            "open:observer",
            "open:control",
            "close:control",
            "close:observer",
            "close:local-gateway",
        ]
        assert list(paths.local_gateway_registry_directory.glob("gateway-*.json")) == []
        assert Path(descriptor["socket_path"]).exists() is False
