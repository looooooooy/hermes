"""Published entry point transition gate for a future Host SPI v1 runtime."""

from __future__ import annotations

import json
import socket
import struct
import sys
import tempfile
from importlib.metadata import entry_points
from pathlib import Path
from types import SimpleNamespace

import pytest
from tests.test_support.host_spi_v1 import TEST_HOST_SPI_FACTORIES

from hermes_agent_plugin.adapters.local_protocol.handshake_v1 import (
    LocalHello,
    decode_local_welcome,
    encode_local_hello,
)


class _Registration:
    def __init__(self, label: str, events: list[str]) -> None:
        self._label = label
        self._events = events
        self.closed = False

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self._events.append(f"close:{self._label}")


class _PreparedObserver:
    snapshot = SimpleNamespace(event_sequence=0)
    activation_deadline_monotonic = 100.0

    def __init__(self, events: list[str]) -> None:
        self._events = events

    def activate(self) -> _Registration:
        self._events.append("observer:activate")
        return _Registration("observer-subscription", self._events)

    def close(self) -> None:
        self._events.append("observer:prepared-close")


class _FutureHostV1:
    host_api_version = 1

    def __init__(self) -> None:
        self.events: list[str] = []
        self.endpoints: dict[str, object] = {}

    def runtime_descriptor(self) -> object:
        self.events.append("runtime:descriptor")
        return SimpleNamespace(
            profile="default",
            runtime_generation="generation-1",
            state="ready",
            capabilities=frozenset(
                {
                    "approval.respond",
                    "clarify.respond",
                    "prompt.submit",
                    "session.control",
                    "session.interrupt",
                    "session.observe",
                    "session.steer",
                }
            ),
        )

    def add_runtime_listener(self, listener: object) -> _Registration:
        self.events.append("runtime:listener")
        self.listener = listener
        return _Registration("runtime-listener", self.events)

    def register_local_endpoint(self, endpoint: object) -> _Registration:
        role = endpoint.connection_role
        self.events.append(f"endpoint:{role}")
        self.endpoints[role] = endpoint
        return _Registration(role, self.events)

    def prepare_observer(self, request: object, sink: object) -> _PreparedObserver:
        self.events.append("observer:prepare")
        self.observer_request = request
        self.observer_sink = sink
        return _PreparedObserver(self.events)

    def control_snapshot(self, scope: object) -> object:
        self.events.append("control:snapshot")
        self.control_scope = scope
        return SimpleNamespace(control_revision=0)

    def invoke_owner_action(self, request: object) -> object:
        raise AssertionError("owner actions are outside this installation gate")

    def audit(self, event: object) -> None:
        self.events.append(f"audit:{event.name}")


class _FutureContextV1:
    gateway_extension_spi_version = 1
    gateway_extension_capabilities = frozenset(
        {
            "audit.safe.v1",
            "extension.lifecycle.v1",
            "runtime.descriptor.v1",
            "session.observe.v1",
            "session.owner-actions.v1",
        }
    )

    def __init__(self, host: _FutureHostV1) -> None:
        self._host = host
        self.registration: _Registration | None = None

    def register_gateway_extension(
        self, extension: object, *, spi_version: int
    ) -> None:
        assert spi_version == 1
        self.registration = extension.install(self._host)


def _receive_exact(connection: socket.socket, size: int) -> bytes:
    body = bytearray()
    while len(body) < size:
        chunk = connection.recv(size - len(body))
        if not chunk:
            raise EOFError("local gateway closed before a complete frame")
        body.extend(chunk)
    return bytes(body)


def test_published_entrypoint_installs_runs_and_closes_on_future_host_spi_v1(
    monkeypatch,
) -> None:
    matches = [
        entry_point
        for entry_point in entry_points().select(group="hermes_agent.plugins")
        if entry_point.name == "hermes-agent-plugin"
    ]
    assert len(matches) == 1
    plugin = matches[0].load()
    from hermes_agent_plugin.bootstrap import registration as registration_module

    monkeypatch.setattr(
        registration_module,
        "load_public_host_spi_factories",
        lambda: TEST_HOST_SPI_FACTORIES,
    )
    host = _FutureHostV1()
    context = _FutureContextV1(host)

    plugin.register(context)

    assert context.registration is not None
    assert set(host.endpoints) == {"local-gateway", "control", "observer"}
    prepared = host.endpoints["observer"].prepare_observer(
        {
            "profile": "default",
            "session_key": "durable-1",
            "runtime_generation": "generation-1",
        },
        object(),
    )
    subscription = prepared.activate()
    snapshot = host.endpoints["control"].read_control_snapshot(
        {
            "profile": "default",
            "session_key": "durable-1",
            "runtime_generation": "generation-1",
        }
    )
    assert snapshot.control_revision == 0

    subscription.close()
    context.registration.close()

    assert host.events == [
        "runtime:descriptor",
        "runtime:listener",
        "endpoint:local-gateway",
        "endpoint:observer",
        "endpoint:control",
        "audit:runtime.lifecycle",
        "observer:prepare",
        "observer:activate",
        "control:snapshot",
        "close:observer-subscription",
        "close:control",
        "close:observer",
        "close:local-gateway",
        "close:runtime-listener",
        "audit:runtime.lifecycle",
    ]


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS production UDS")
def test_published_entrypoint_opens_real_generic_gateway_descriptor_and_uds(
    monkeypatch,
) -> None:
    matches = [
        entry_point
        for entry_point in entry_points().select(group="hermes_agent.plugins")
        if entry_point.name == "hermes-agent-plugin"
    ]
    assert len(matches) == 1
    plugin = matches[0].load()
    from hermes_agent_plugin.bootstrap import registration as registration_module

    monkeypatch.setattr(
        registration_module,
        "load_public_host_spi_factories",
        lambda: TEST_HOST_SPI_FACTORIES,
    )

    class ProductionOpeningHost(_FutureHostV1):
        def runtime_descriptor(self) -> object:
            return SimpleNamespace(
                profile="default",
                runtime_generation="generation-production-1",
                host_bundle_id="com.nousresearch.hermes",
                state="ready",
                capabilities=frozenset(
                    {
                        "approval.respond",
                        "clarify.respond",
                        "prompt.submit",
                        "session.control",
                        "session.interrupt",
                        "session.observe",
                        "session.steer",
                    }
                ),
            )

        def register_local_endpoint(self, endpoint: object) -> object:
            role = endpoint.connection_role
            registration = endpoint.open_local_endpoint(self.runtime_descriptor())
            self.endpoints[role] = endpoint
            return registration

    with tempfile.TemporaryDirectory(
        prefix="hap-entrypoint-",
        dir="/tmp",
    ) as raw_root:
        root = Path(raw_root).resolve()
        path_values = {
            "HERMES_LOCAL_GATEWAY_REGISTRY_DIR": root / "local-registry",
            "HERMES_LOCAL_GATEWAY_SOCKET_DIR": root / "local-sockets",
            "HERMES_CONTROL_REGISTRY_DIR": root / "control-registry",
            "HERMES_CONTROL_SOCKET_DIR": root / "control-sockets",
            "HERMES_OBSERVER_REGISTRY_DIR": root / "observer-registry",
            "HERMES_OBSERVER_SOCKET_DIR": root / "observer-sockets",
        }
        for name, path in path_values.items():
            monkeypatch.setenv(name, str(path))

        host = ProductionOpeningHost()
        context = _FutureContextV1(host)
        plugin.register(context)
        assert context.registration is not None
        descriptors = list(
            path_values["HERMES_LOCAL_GATEWAY_REGISTRY_DIR"].glob("gateway-*.json")
        )
        assert len(descriptors) == 1
        descriptor = json.loads(descriptors[0].read_text(encoding="utf-8"))
        socket_path = Path(descriptor["socket_path"])
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
            connection.connect(str(socket_path))
            connection.sendall(struct.pack("!I", len(hello)) + hello)
            response_size = struct.unpack("!I", _receive_exact(connection, 4))[0]
            welcome = decode_local_welcome(_receive_exact(connection, response_size))
        assert welcome.runtime_generation == "generation-production-1"
        assert welcome.accepted_capabilities == (
            "session.control",
            "session.observe",
        )

        context.registration.close()
        context.registration.close()

        assert (
            list(
                path_values["HERMES_LOCAL_GATEWAY_REGISTRY_DIR"].glob("gateway-*.json")
            )
            == []
        )
        assert socket_path.exists() is False
