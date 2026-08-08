from __future__ import annotations

import json
import os
import struct
import time
from collections.abc import Mapping

import pytest
from hermes_agent_plugin.adapters.platform.windows.local_relay import (
    create_local_relay_backend,
)
from hermes_agent_plugin.adapters.platform.windows.runtime_authority import (
    capture_windows_host_authority,
)

from hermes_connector.adapters.platform.windows.named_pipe import (
    connect_same_user_pipe,
    profile_pipe_name,
    read_exact,
    write_all,
)

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows Named Pipes required")
_MAX_TEST_FRAME_BYTES = 1024 * 1024


def _authority(profile: str):
    return capture_windows_host_authority(
        profile=profile,
        host_bundle_id="com.hermes.windows-observer-test",
    ).bind_runtime(f"generation-{profile}-1")


def _send_frame(connection, frame: Mapping[str, object]) -> None:
    encoded = json.dumps(
        dict(frame),
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    assert 0 < len(encoded) <= _MAX_TEST_FRAME_BYTES
    write_all(connection.handle, struct.pack(">I", len(encoded)) + encoded)


def _recv_frame(connection) -> dict:
    deadline = time.monotonic() + 3.0
    prefix = read_exact(connection.handle, 4, deadline=deadline)
    size = struct.unpack(">I", prefix)[0]
    assert 0 < size <= _MAX_TEST_FRAME_BYTES
    value = json.loads(read_exact(connection.handle, size, deadline=deadline).decode())
    assert isinstance(value, dict)
    return value


def _expected_ready(authority, *, observer_contract: int) -> dict:
    if observer_contract == 2:
        payload = {
            "observer_contract": 2,
            "connection_role": "observer",
        }
    else:
        payload = {
            "local_gateway_protocol": 1,
            "observer_contract": 1,
            "connection_role": "observer",
            "profile": authority.profile,
            "runtime_generation": authority.runtime_generation,
            "instance_id": authority.instance_id,
        }
    return {
        "jsonrpc": "2.0",
        "method": "event",
        "params": {"type": "gateway.ready", "payload": payload},
    }


def test_observer_and_catalog_hold_concurrent_connections_and_push_events() -> None:
    authority = _authority("duplex")
    observed: list[tuple[dict, object]] = []
    transports: dict[str, object] = {}
    cleaned: list[object] = []

    def dispatch(request: dict, transport: object) -> dict:
        observed.append((request, transport))
        method = request.get("method")
        transports[str(method)] = transport
        if method == "session.observe.subscribe":
            return {
                "jsonrpc": "2.0",
                "id": request.get("id"),
                "result": {
                    "runtime_generation": authority.runtime_generation,
                    "subscription_id": "11111111-1111-4111-8111-111111111111",
                },
            }
        if method == "session.catalog.subscribe":
            return {
                "jsonrpc": "2.0",
                "id": request.get("id"),
                "result": {
                    "kind": "catalog-snapshot",
                    "runtime_generation": authority.runtime_generation,
                },
            }
        raise AssertionError(f"unexpected method: {method}")

    backend = create_local_relay_backend()
    registration = backend.start_observer_endpoint(
        authority=authority,
        dispatch=dispatch,
        remove_observer_subscriptions=cleaned.append,
        observer_contract=1,
    )
    observer = connect_same_user_pipe(
        profile_pipe_name("observer", authority.profile),
        timeout_seconds=2.0,
    )
    catalog = connect_same_user_pipe(
        profile_pipe_name("observer", authority.profile),
        timeout_seconds=2.0,
    )
    try:
        assert _recv_frame(observer) == _expected_ready(authority, observer_contract=1)
        assert _recv_frame(catalog) == _expected_ready(authority, observer_contract=1)
        endpoints = backend.list_observer_endpoints()
        assert len(endpoints) == 1
        assert endpoints[0].pid == authority.pid
        assert endpoints[0].runtime_generation == authority.runtime_generation
        assert endpoints[0].instance_id == authority.instance_id

        _send_frame(
            observer,
            {
                "jsonrpc": "2.0",
                "id": "observe-1",
                "method": "session.observe.subscribe",
                "params": {
                    "session_key": "session-a",
                    "profile": authority.profile,
                    "runtime_generation": authority.runtime_generation,
                },
            },
        )
        _send_frame(
            catalog,
            {
                "jsonrpc": "2.0",
                "id": "catalog-1",
                "method": "session.catalog.subscribe",
                "params": {
                    "profile": authority.profile,
                    "runtime_generation": authority.runtime_generation,
                    "page_size": 64,
                },
            },
        )
        assert _recv_frame(observer)["id"] == "observe-1"
        assert _recv_frame(catalog)["id"] == "catalog-1"

        observe_request = next(
            request
            for request, _transport in observed
            if request.get("method") == "session.observe.subscribe"
        )
        catalog_request = next(
            request
            for request, _transport in observed
            if request.get("method") == "session.catalog.subscribe"
        )
        assert observe_request["params"]["relay_local_only"] is True
        assert "relay_local_only" not in catalog_request["params"]

        event = {
            "jsonrpc": "2.0",
            "method": "event",
            "params": {
                "type": "session.updated",
                "payload": {"session_key": "session-a"},
            },
        }
        observer_transport = transports["session.observe.subscribe"]
        assert observer_transport.write(event) is True
        assert _recv_frame(observer) == event
    finally:
        observer.close()
        catalog.close()
        registration.close()
    assert backend.list_observer_endpoints() == []
    assert len(cleaned) == 2


def test_observer_v2_enforces_exact_params_and_injects_runtime_generation() -> None:
    authority = _authority("v2")
    observed: list[dict] = []

    def dispatch(request: dict, _transport: object) -> dict:
        observed.append(request)
        return {
            "jsonrpc": "2.0",
            "id": request.get("id"),
            "result": {
                "runtime_generation": request["params"].get("runtime_generation"),
                "subscription_id": "22222222-2222-4222-8222-222222222222",
            },
        }

    backend = create_local_relay_backend()
    registration = backend.start_observer_endpoint(
        authority=authority,
        dispatch=dispatch,
        remove_observer_subscriptions=lambda _transport: None,
        observer_contract=2,
    )
    connection = connect_same_user_pipe(
        profile_pipe_name("observer", authority.profile),
        timeout_seconds=2.0,
    )
    try:
        assert _recv_frame(connection) == _expected_ready(authority, observer_contract=2)
        _send_frame(
            connection,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "session.observe.subscribe",
                "params": {
                    "observer_contract": 2,
                    "session_key": "session-v2",
                    "profile": authority.profile,
                },
            },
        )
        response = _recv_frame(connection)
        assert response["id"] == 1
        assert observed[0]["params"] == {
            "observer_contract": 2,
            "session_key": "session-v2",
            "profile": authority.profile,
            "runtime_generation": authority.runtime_generation,
        }

        _send_frame(
            connection,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "session.observe.subscribe",
                "params": {
                    "observer_contract": 2,
                    "session_key": "session-v2",
                    "profile": authority.profile,
                    "unexpected": True,
                },
            },
        )
        rejection = _recv_frame(connection)
        assert rejection["id"] == 2
        assert rejection["error"] == {"code": -32602, "message": "invalid params"}
        assert len(observed) == 1
    finally:
        connection.close()
        registration.close()


def test_observer_rejects_mutation_before_dispatch() -> None:
    authority = _authority("readonly")
    called = False

    def dispatch(_request: dict, _transport: object) -> dict:
        nonlocal called
        called = True
        return {"jsonrpc": "2.0", "id": "unexpected", "result": {}}

    backend = create_local_relay_backend()
    registration = backend.start_observer_endpoint(
        authority=authority,
        dispatch=dispatch,
        remove_observer_subscriptions=lambda _transport: None,
        observer_contract=1,
    )
    connection = connect_same_user_pipe(
        profile_pipe_name("observer", authority.profile),
        timeout_seconds=2.0,
    )
    try:
        _recv_frame(connection)
        _send_frame(
            connection,
            {
                "jsonrpc": "2.0",
                "id": "mutation-1",
                "method": "prompt.submit",
                "params": {"session_key": "secret-session"},
            },
        )
        rejection = _recv_frame(connection)
        assert rejection["id"] == "mutation-1"
        assert rejection["error"] == {
            "code": 4003,
            "message": "observer connection is read-only",
        }
        assert called is False
    finally:
        connection.close()
        registration.close()
