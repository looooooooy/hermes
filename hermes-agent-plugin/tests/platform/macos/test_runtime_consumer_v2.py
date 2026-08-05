"""Exact v2 discovery and pre-sensitive-frame consumer trust checks."""

from __future__ import annotations

import json
import os
import tempfile
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path

import pytest
from tests.test_support.runtime_descriptor_v2 import runtime_authority_v2

from hermes_agent_plugin.adapters.platform.macos import control_relay, observer_relay
from hermes_agent_plugin.adapters.platform.macos.local_gateway_paths import (
    MacOSLocalGatewayPaths,
)


def _paths(root: Path) -> MacOSLocalGatewayPaths:
    return MacOSLocalGatewayPaths(
        local_gateway_registry_directory=root / "local-registry",
        local_gateway_socket_directory=root / "local-sockets",
        control_registry_directory=root / "control-registry",
        control_socket_directory=root / "control-sockets",
        observer_registry_directory=root / "observer-registry",
        observer_socket_directory=root / "observer-sockets",
    )


def _start_role(role: str, paths: MacOSLocalGatewayPaths, stack: ExitStack):
    authority = runtime_authority_v2()
    if role == "control":
        registration = control_relay.start_control_endpoint(
            authority=authority,
            dispatcher=lambda _request, _transport: None,
            paths=paths,
        )
    else:
        registration = observer_relay.start_observer_endpoint(
            authority=authority,
            dispatch=lambda _request, _transport: None,
            remove_observer_subscriptions=lambda _transport: None,
            paths=paths,
        )
    stack.callback(registration.close)
    return authority


def _descriptor(directory: Path) -> Path:
    entries = tuple(directory.glob("gateway-*.json"))
    assert len(entries) == 1
    return entries[0]


@pytest.mark.parametrize("role", ("control", "observer"))
@pytest.mark.parametrize("mutation", ("missing", "extra", "v1", "malformed"))
def test_production_consumers_reject_nonexact_descriptor_v2(
    role: str,
    mutation: str,
) -> None:
    with tempfile.TemporaryDirectory(prefix="hap-consumer-v2-", dir="/tmp") as raw:
        paths = _paths(Path(raw).resolve())
        with ExitStack() as stack:
            _start_role(role, paths, stack)
            registry = (
                paths.control_registry_directory
                if role == "control"
                else paths.observer_registry_directory
            )
            descriptor_path = _descriptor(registry)
            value = json.loads(descriptor_path.read_text(encoding="utf-8"))
            if mutation == "missing":
                value.pop("host_bundle_id")
            elif mutation == "extra":
                value["unexpected"] = "value"
            elif mutation == "v1":
                value["version"] = 1
            else:
                value["process_executable_inode"] = "not-an-integer"
            descriptor_path.write_text(json.dumps(value), encoding="utf-8")
            descriptor_path.chmod(0o600)

            endpoints = (
                control_relay.list_control_endpoints(paths=paths)
                if role == "control"
                else observer_relay.list_observer_endpoints(paths=paths)
            )
            assert endpoints == []


@pytest.mark.parametrize("role", ("control", "observer"))
def test_discovery_rejects_mixed_process_identity_snapshot(role: str) -> None:
    with tempfile.TemporaryDirectory(prefix="hap-consumer-pid-", dir="/tmp") as raw:
        paths = _paths(Path(raw).resolve())
        with ExitStack() as stack:
            authority = _start_role(role, paths, stack)
            calls = 0

            def mixed_identity(_pid):
                nonlocal calls
                calls += 1
                if calls == 1:
                    return authority.process_identity
                return replace(
                    authority.process_identity,
                    start_time_ns=authority.process_identity.start_time_ns + 1_000,
                )

            endpoints = (
                control_relay.list_control_endpoints(
                    paths=paths,
                    process_identity_provider=mixed_identity,
                )
                if role == "control"
                else observer_relay.list_observer_endpoints(
                    paths=paths,
                    process_identity_provider=mixed_identity,
                )
            )
            assert endpoints == []


class _RecordingWebSocket:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.closed = False

    def send(self, value: str) -> None:
        self.sent.append(value)

    def recv(self, **_kwargs):
        raise AssertionError("consumer must reject before receiving application frames")

    def close(self) -> None:
        self.closed = True


@pytest.mark.parametrize("role", ("control", "observer"))
@pytest.mark.parametrize("failure", ("pid-reuse", "socket-swap", "wrong-peer"))
def test_connect_proofs_fail_before_claims_session_key_or_subscribe(
    role: str,
    failure: str,
) -> None:
    with tempfile.TemporaryDirectory(prefix="hap-connect-proof-", dir="/tmp") as raw:
        paths = _paths(Path(raw).resolve())
        with ExitStack() as stack:
            authority = _start_role(role, paths, stack)
            websocket = _RecordingWebSocket()
            process_calls = 0
            socket_calls = 0

            def process_identity(_pid):
                nonlocal process_calls
                process_calls += 1
                if failure == "pid-reuse" and process_calls >= 4:
                    return replace(
                        authority.process_identity,
                        start_time_ns=authority.process_identity.start_time_ns + 1_000,
                    )
                return authority.process_identity

            def socket_identity(_endpoint):
                nonlocal socket_calls
                socket_calls += 1
                return not (failure == "socket-swap" and socket_calls >= 2)

            peer_pid = authority.pid + 1 if failure == "wrong-peer" else authority.pid
            if role == "control":
                hub = control_relay.ControlRelayHub(
                    current_pid=1,
                    paths=paths,
                    connect=lambda *_args, **_kwargs: websocket,
                    peer_pid_provider=lambda _websocket: peer_pid,
                    process_identity_provider=process_identity,
                    socket_identity_provider=socket_identity,
                )
                result = hub.call(
                    {
                        "jsonrpc": "2.0",
                        "id": "request-1",
                        "method": "session.interrupt",
                        "params": {},
                    },
                    transport=object(),
                    auth_claims={
                        "user_id": "user-1",
                        "provider": "hermes-cloud",
                        "connection_role": "control",
                        "client_instance_id": ("22222222-2222-4222-8222-222222222222"),
                        "session_key": "sensitive-session-key",
                        "profile": "default",
                    },
                    profile="default",
                )
            else:
                hub = observer_relay.ObserverRelayHub(
                    current_pid=1,
                    paths=paths,
                    connect=lambda *_args, **_kwargs: websocket,
                    peer_pid_provider=lambda _websocket: peer_pid,
                    process_identity_provider=process_identity,
                    socket_identity_provider=socket_identity,
                )
                result = hub.subscribe(
                    "sensitive-session-key",
                    "default",
                    object(),
                    runtime_generation="runtime-generation-1",
                )

            assert result is None
            assert websocket.sent == []
            assert websocket.closed is True
            assert "sensitive-session-key" not in "".join(websocket.sent)
            assert os.getpid() == authority.pid
