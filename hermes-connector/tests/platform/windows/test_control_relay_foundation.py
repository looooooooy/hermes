from __future__ import annotations

import json
import os
import struct
import time
from uuid import UUID

import pytest
from hermes_agent_plugin.adapters.platform.windows.control_relay import (
    list_control_endpoints,
    start_control_endpoint,
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


def _authority():
    return capture_windows_host_authority(
        profile="default",
        host_bundle_id="com.hermes.windows-control-test",
    ).bind_runtime("generation-windows-control-1")


def _send_frame(connection, frame: dict) -> None:
    encoded = json.dumps(frame, sort_keys=True, separators=(",", ":")).encode()
    write_all(connection.handle, struct.pack(">I", len(encoded)) + encoded)


def _recv_frame(connection) -> dict:
    deadline = time.monotonic() + 3.0
    prefix = read_exact(connection.handle, 4, deadline=deadline)
    size = struct.unpack(">I", prefix)[0]
    assert 0 < size <= 1024 * 1024
    return json.loads(read_exact(connection.handle, size, deadline=deadline).decode())


def _claims(authority) -> dict[str, str]:
    return {
        "user_id": "device-windows-control-test",
        "provider": "hermes-cloud",
        "connection_role": "control",
        "client_instance_id": str(UUID("11111111-1111-4111-8111-111111111111")),
        "session_key": "session-a",
        "profile": authority.profile,
    }


def test_control_attach_and_allowed_rpc_reach_dispatcher_with_local_only() -> None:
    authority = _authority()
    observed: list[tuple[dict, object]] = []

    def dispatcher(request: dict, transport: object) -> dict:
        observed.append((request, transport))
        return {
            "jsonrpc": "2.0",
            "id": request.get("id"),
            "result": {
                "method": request.get("method"),
                "relay_local_only": request.get("params", {}).get("relay_local_only"),
            },
        }

    registration = start_control_endpoint(
        authority=authority,
        dispatcher=dispatcher,
    )
    connection = connect_same_user_pipe(
        profile_pipe_name("control", "default"),
        timeout_seconds=2.0,
    )
    try:
        endpoints = list_control_endpoints()
        assert len(endpoints) == 1
        assert endpoints[0].pid == authority.pid
        assert endpoints[0].profile == authority.profile
        assert endpoints[0].instance_id == authority.instance_id

        _send_frame(
            connection,
            {
                "jsonrpc": "2.0",
                "id": "attach-1",
                "method": "relay.control.attach",
                "params": {"claims": _claims(authority)},
            },
        )
        attach = _recv_frame(connection)
        assert attach == {
            "jsonrpc": "2.0",
            "id": "attach-1",
            "result": {"attached": True, "connection_role": "control"},
        }

        _send_frame(
            connection,
            {
                "jsonrpc": "2.0",
                "id": "control-1",
                "method": "session.control.status",
                "params": {"durable_session_key": "session-a"},
            },
        )
        response = _recv_frame(connection)
        assert response["id"] == "control-1"
        assert response["result"] == {
            "method": "session.control.status",
            "relay_local_only": True,
        }
        assert len(observed) == 1
        relayed_request, transport = observed[0]
        assert relayed_request["params"]["relay_local_only"] is True
        assert transport.auth_claims == _claims(authority)
        assert transport.connection_role == "control"
    finally:
        connection.close()
        registration.close()
    assert list_control_endpoints() == []


def test_control_rejects_attach_without_bound_claims_before_dispatch() -> None:
    authority = _authority()
    called = False

    def dispatcher(_request: dict, _transport: object) -> dict:
        nonlocal called
        called = True
        return {"jsonrpc": "2.0", "id": "unexpected", "result": {}}

    registration = start_control_endpoint(authority=authority, dispatcher=dispatcher)
    connection = connect_same_user_pipe(
        profile_pipe_name("control", "default"),
        timeout_seconds=2.0,
    )
    try:
        _send_frame(
            connection,
            {
                "jsonrpc": "2.0",
                "id": "attach-bad",
                "method": "relay.control.attach",
                "params": {"claims": {"profile": "default"}},
            },
        )
        response = _recv_frame(connection)
        assert response["id"] == "attach-bad"
        assert response["error"]["code"] == 4200
        assert called is False
    finally:
        connection.close()
        registration.close()
