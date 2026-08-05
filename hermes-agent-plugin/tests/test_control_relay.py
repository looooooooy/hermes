from __future__ import annotations

import json
import logging
import os
import queue
import stat
import threading
from pathlib import Path
from uuid import UUID

import pytest

from hermes_agent_plugin.adapters.local_protocol.control_relay import (
    _CLAIM_KEYS,
)
from hermes_agent_plugin.adapters.platform.macos import control_relay
from tests.test_support.runtime_descriptor_v2 import runtime_authority_v2

_ENDPOINT_ONE = "11111111-1111-4111-8111-111111111111"
_ENDPOINT_TWO = "22222222-2222-4222-8222-222222222222"


class _DownstreamTransport:
    def __init__(self) -> None:
        self.frames: list[dict] = []
        self.disconnected = threading.Event()

    def write(self, frame: dict) -> bool:
        self.frames.append(frame)
        return True

    def disconnect(self) -> None:
        self.disconnected.set()


class _FailingDownstreamTransport(_DownstreamTransport):
    def write(self, frame: dict) -> bool:
        self.frames.append(frame)
        return False


class _FakeUpstreamWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.closed = False
        self.incoming: queue.Queue[str | BaseException] = queue.Queue()

    def send(self, value: str) -> None:
        frame = json.loads(value)
        self.sent.append(frame)
        if frame["method"] == "relay.control.attach":
            self.incoming.put(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": frame["id"],
                        "result": {"attached": True, "connection_role": "control"},
                    }
                )
            )
        else:
            self.incoming.put(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": frame["id"],
                        "result": {"controller_kind": "desktop", "control_revision": 0},
                    }
                )
            )

    def recv(self, timeout=None):
        item = self.incoming.get(timeout=timeout)
        if isinstance(item, BaseException):
            raise item
        return item

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.incoming.put(RuntimeError("closed"))


class _RoutingUpstreamWebSocket(_FakeUpstreamWebSocket):
    def __init__(self, response: dict) -> None:
        super().__init__()
        self.response = response

    def send(self, value: str) -> None:
        frame = json.loads(value)
        self.sent.append(frame)
        if frame["method"] == "relay.control.attach":
            response = {
                "jsonrpc": "2.0",
                "id": frame["id"],
                "result": {"attached": True, "connection_role": "control"},
            }
        else:
            response = {"jsonrpc": "2.0", "id": frame["id"], **self.response}
        self.incoming.put(json.dumps(response))


def _claims() -> dict:
    return {
        "user_id": "user-1",
        "provider": "basic",
        "connection_role": "control",
        "client_instance_id": "11111111-1111-4111-8111-111111111111",
        "session_key": "durable-root-1",
        "profile": "default",
        "minted_at": 1,
        "unexpected": "must-not-cross-relay",
    }


def test_control_callback_exception_log_never_contains_sensitive_exception_text(
    caplog,
) -> None:
    sentinel = "token=secret approval_payload=private tool_output=classified"

    class WebSocket:
        def __init__(self) -> None:
            self.sent: list[str] = []

        def __iter__(self):
            return iter(
                (
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": "attach",
                            "method": "relay.control.attach",
                            "params": {"claims": _claims()},
                        }
                    ),
                )
            )

        def send(self, value: str) -> None:
            self.sent.append(value)

        def close(self) -> None:
            return None

    class Dispatcher:
        def submit(self, *_args, **_kwargs):
            return object()

    caplog.set_level(logging.DEBUG)
    control_relay._handle_control_connection(
        WebSocket(),
        dispatcher=lambda _request, _transport: None,
        owner_action_dispatcher=Dispatcher(),
        transport_cleanup=lambda _transport: (_ for _ in ()).throw(
            RuntimeError(sentinel)
        ),
    )

    assert sentinel not in caplog.text


@pytest.mark.parametrize("claim_name", ["session_key", "profile"])
def test_control_relay_claim_sanitizer_rejects_blank_target_claims(claim_name) -> None:
    claims = _claims()
    claims[claim_name] = " "

    assert control_relay._sanitize_claims(claims) is None


@pytest.mark.parametrize(
    "client_instance_id",
    (
        "not-a-uuid",
        "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA",
        "{11111111-1111-4111-8111-111111111111}",
    ),
)
def test_control_relay_claim_sanitizer_rejects_noncanonical_client_id(
    client_instance_id,
) -> None:
    claims = _claims()
    claims["client_instance_id"] = client_instance_id

    assert control_relay._sanitize_claims(claims) is None


def test_control_endpoint_registration_is_private_credential_free_and_removed(
    tmp_path, monkeypatch
) -> None:
    registry = tmp_path / "registry"
    socket_dir = Path("/tmp").resolve(strict=True) / f"hctl-reg-{os.getpid()}"
    monkeypatch.setenv("HERMES_CONTROL_REGISTRY_DIR", str(registry))
    monkeypatch.setenv("HERMES_CONTROL_SOCKET_DIR", str(socket_dir))

    registration = control_relay.start_control_endpoint(
        authority=runtime_authority_v2(),
        dispatcher=lambda _request, _transport: None,
    )
    try:
        files = list(registry.glob("*.json"))
        assert len(files) == 1
        assert stat.S_IMODE(registry.stat().st_mode) == 0o700
        assert stat.S_IMODE(files[0].stat().st_mode) == 0o600
        registry_text = files[0].read_text(encoding="utf-8")
        assert "token" not in registry_text
        assert "credential" not in registry_text
        assert "ws_url" not in registry_text

        endpoint = control_relay.list_control_endpoints()[0]
        assert endpoint.profile == "default"
        assert endpoint.instance_id == str(UUID(endpoint.instance_id))
        assert endpoint.socket_path.exists()
        assert stat.S_IMODE(socket_dir.stat().st_mode) == 0o700
        assert stat.S_IMODE(endpoint.socket_path.stat().st_mode) == 0o600
    finally:
        registration.close()

    assert list(registry.glob("*.json")) == []
    assert list(socket_dir.glob("*.sock")) == []


def test_private_control_socket_binds_sanitized_claims_and_allows_only_control_contract_methods(
    tmp_path,
    monkeypatch,
) -> None:
    registry = tmp_path / "registry"
    socket_dir = Path("/tmp").resolve(strict=True) / f"hctl-rpc-{os.getpid()}"
    monkeypatch.setenv("HERMES_CONTROL_REGISTRY_DIR", str(registry))
    monkeypatch.setenv("HERMES_CONTROL_SOCKET_DIR", str(socket_dir))
    dispatched = []

    def dispatch(request: dict, transport) -> dict:
        dispatched.append((request, transport))
        return {"jsonrpc": "2.0", "id": request["id"], "result": {"ok": True}}

    registration = control_relay.start_control_endpoint(
        authority=runtime_authority_v2(), dispatcher=dispatch
    )
    endpoint = control_relay.list_control_endpoints()[0]
    websocket = control_relay.unix_connect(
        str(endpoint.socket_path), uri="ws://localhost/control"
    )
    try:
        websocket.send(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": "attach",
                    "method": "relay.control.attach",
                    "params": {"claims": _claims()},
                }
            )
        )
        attached = json.loads(websocket.recv())
        assert attached["result"] == {"attached": True, "connection_role": "control"}

        websocket.send(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": "status",
                    "method": "session.control.status",
                    "params": {"session_key": "durable-root-1"},
                }
            )
        )
        assert json.loads(websocket.recv())["result"] == {"ok": True}
        request, transport = dispatched[0]
        assert request["method"] == "session.control.status"
        assert request["params"]["relay_local_only"] is True
        assert transport.connection_role == "control"
        assert dict(transport.auth_claims) == {
            "user_id": "user-1",
            "provider": "basic",
            "connection_role": "control",
            "client_instance_id": "11111111-1111-4111-8111-111111111111",
            "session_key": "durable-root-1",
            "profile": "default",
        }

        websocket.send(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": "prompt",
                    "method": "prompt.submit",
                    "params": {"text": "run-on-owner"},
                }
            )
        )
        assert json.loads(websocket.recv())["result"] == {"ok": True}
        assert dispatched[1][0]["method"] == "prompt.submit"
        assert dispatched[1][0]["params"]["relay_local_only"] is True

        websocket.send(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": "steer",
                    "method": "session.steer",
                    "params": {"text": "inspect the authorization path"},
                }
            )
        )
        assert json.loads(websocket.recv())["result"] == {"ok": True}
        assert dispatched[2][0]["method"] == "session.steer"
        assert dispatched[2][0]["params"]["relay_local_only"] is True

        for request_id, method in (
            ("approval", "approval.respond"),
            ("clarify", "clarify.respond"),
        ):
            websocket.send(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "method": method,
                        "params": {"request_id": f"pending-{request_id}"},
                    }
                )
            )
            assert json.loads(websocket.recv())["result"] == {"ok": True}

        assert [item[0]["method"] for item in dispatched[3:]] == [
            "approval.respond",
            "clarify.respond",
        ]
        assert all(
            item[0]["params"]["relay_local_only"] is True for item in dispatched[3:]
        )

        websocket.send(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": "forbidden",
                    "method": "session.resume",
                    "params": {"session_id": "must-not-run"},
                }
            )
        )
        forbidden = json.loads(websocket.recv())
        assert forbidden["error"] == {"code": 4209, "message": "method_not_allowed"}
        assert len(dispatched) == 5
    finally:
        websocket.close()
        registration.close()


@pytest.mark.parametrize("missing_claim", ["session_key", "profile"])
def test_private_control_socket_rejects_claims_without_immutable_target(
    tmp_path,
    monkeypatch,
    missing_claim,
) -> None:
    registry = tmp_path / f"registry-{missing_claim}"
    socket_dir = (
        Path("/tmp").resolve(strict=True)
        / f"hctl-unbound-{missing_claim}-{os.getpid()}"
    )
    monkeypatch.setenv("HERMES_CONTROL_REGISTRY_DIR", str(registry))
    monkeypatch.setenv("HERMES_CONTROL_SOCKET_DIR", str(socket_dir))
    registration = control_relay.start_control_endpoint(
        authority=runtime_authority_v2(),
        dispatcher=lambda _request, _transport: None,
    )
    endpoint = control_relay.list_control_endpoints()[0]
    websocket = control_relay.unix_connect(
        str(endpoint.socket_path),
        uri="ws://localhost/control",
    )
    claims = _claims()
    claims.pop(missing_claim)
    try:
        websocket.send(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": "attach",
                    "method": "relay.control.attach",
                    "params": {"claims": claims},
                }
            )
        )

        response = json.loads(websocket.recv())
        assert response["error"]["code"] == 4200
    finally:
        websocket.close()
        registration.close()


def test_private_control_socket_disconnect_runs_owner_transport_cleanup(
    tmp_path,
    monkeypatch,
) -> None:
    registry = tmp_path / "registry"
    socket_dir = Path("/tmp").resolve(strict=True) / f"hctl-clean-{os.getpid()}"
    monkeypatch.setenv("HERMES_CONTROL_REGISTRY_DIR", str(registry))
    monkeypatch.setenv("HERMES_CONTROL_SOCKET_DIR", str(socket_dir))
    cleaned = []
    cleanup_complete = threading.Event()

    def cleanup(transport) -> None:
        cleaned.append(transport)
        cleanup_complete.set()

    registration = control_relay.start_control_endpoint(
        authority=runtime_authority_v2(),
        dispatcher=lambda _request, _transport: None,
        transport_cleanup=cleanup,
    )
    endpoint = control_relay.list_control_endpoints()[0]
    websocket = control_relay.unix_connect(
        str(endpoint.socket_path),
        uri="ws://localhost/control",
    )
    websocket.send(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": "attach",
                "method": "relay.control.attach",
                "params": {"claims": _claims()},
            }
        )
    )
    assert json.loads(websocket.recv())["result"]["attached"] is True

    websocket.close()
    try:
        assert cleanup_complete.wait(timeout=1)
        assert len(cleaned) == 1
        assert cleaned[0].connection_role == "control"
        assert dict(cleaned[0].auth_claims) == {
            key: _claims()[key] for key in _CLAIM_KEYS
        }
    finally:
        registration.close()


def test_private_control_socket_does_not_let_one_blocking_rpc_starve_another(
    tmp_path,
    monkeypatch,
) -> None:
    registry = tmp_path / "registry"
    socket_dir = Path("/tmp").resolve(strict=True) / f"hctl-conc-{os.getpid()}"
    monkeypatch.setenv("HERMES_CONTROL_REGISTRY_DIR", str(registry))
    monkeypatch.setenv("HERMES_CONTROL_SOCKET_DIR", str(socket_dir))
    fast_completed = threading.Event()

    def dispatch(request: dict, _transport) -> dict:
        if request["id"] == "slow":
            completed = "slow" if fast_completed.wait(timeout=0.5) else "starved"
        else:
            fast_completed.set()
            completed = "fast"
        return {
            "jsonrpc": "2.0",
            "id": request["id"],
            "result": {"completed": completed},
        }

    registration = control_relay.start_control_endpoint(
        authority=runtime_authority_v2(), dispatcher=dispatch
    )
    endpoint = control_relay.list_control_endpoints()[0]
    websocket = control_relay.unix_connect(
        str(endpoint.socket_path), uri="ws://localhost/control"
    )
    try:
        websocket.send(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": "attach",
                    "method": "relay.control.attach",
                    "params": {"claims": _claims()},
                }
            )
        )
        assert json.loads(websocket.recv())["result"]["attached"] is True

        for request_id in ("slow", "fast"):
            websocket.send(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "method": "session.control.status",
                        "params": {"session_key": "durable-root-1"},
                    }
                )
            )

        first = json.loads(websocket.recv())
        second = json.loads(websocket.recv())
        assert first == {
            "jsonrpc": "2.0",
            "id": "fast",
            "result": {"completed": "fast"},
        }
        assert second == {
            "jsonrpc": "2.0",
            "id": "slow",
            "result": {"completed": "slow"},
        }
    finally:
        websocket.close()
        registration.close()


def test_relay_hub_reuses_one_upstream_connection_and_filters_attach_claims(
    monkeypatch,
) -> None:
    endpoint = control_relay.ControlEndpoint(
        pid=4312,
        profile="default",
        socket_path=control_relay._socket_dir() / "owner.sock",
        instance_id=_ENDPOINT_ONE,
    )
    upstream = _FakeUpstreamWebSocket()
    monkeypatch.setattr(control_relay, "list_control_endpoints", lambda: [endpoint])
    monkeypatch.setattr(
        control_relay, "unix_connect", lambda *_args, **_kwargs: upstream
    )
    downstream = _DownstreamTransport()
    hub = control_relay.ControlRelayHub(current_pid=9999)

    first = hub.call(
        {
            "jsonrpc": "2.0",
            "id": "first",
            "method": "session.control.status",
            "params": {"session_key": "durable-root-1"},
        },
        transport=downstream,
        auth_claims=_claims(),
        profile="default",
    )
    second = hub.call(
        {
            "jsonrpc": "2.0",
            "id": "second",
            "method": "session.control.status",
            "params": {"session_key": "durable-root-1"},
        },
        transport=downstream,
        auth_claims=_claims(),
        profile="default",
    )

    assert first["result"]["control_revision"] == 0
    assert second["result"]["control_revision"] == 0
    assert [frame["method"] for frame in upstream.sent] == [
        "relay.control.attach",
        "session.control.status",
        "session.control.status",
    ]
    assert upstream.sent[1]["id"] == str(UUID(upstream.sent[1]["id"]))
    assert upstream.sent[2]["id"] == str(UUID(upstream.sent[2]["id"]))
    attach_claims = upstream.sent[0]["params"]["claims"]
    assert "minted_at" not in attach_claims
    assert "unexpected" not in attach_claims
    assert hub.close_transport(downstream) == 1
    assert upstream.closed is True


def test_relay_hub_retries_same_profile_endpoints_until_session_owner(
    monkeypatch,
) -> None:
    endpoints = [
        control_relay.ControlEndpoint(
            pid=4312,
            profile="default",
            socket_path=control_relay._socket_dir() / "non-owner.sock",
            instance_id=_ENDPOINT_ONE,
        ),
        control_relay.ControlEndpoint(
            pid=4313,
            profile="default",
            socket_path=control_relay._socket_dir() / "owner.sock",
            instance_id=_ENDPOINT_TWO,
        ),
    ]
    non_owner = _RoutingUpstreamWebSocket(
        {"error": {"code": 4202, "message": "authoritative live runtime unavailable"}}
    )
    owner = _RoutingUpstreamWebSocket(
        {"result": {"controller_kind": "desktop", "control_revision": 7}}
    )
    upstream_by_path = {
        str(endpoints[0].socket_path): non_owner,
        str(endpoints[1].socket_path): owner,
    }
    monkeypatch.setattr(control_relay, "list_control_endpoints", lambda: endpoints)
    monkeypatch.setattr(
        control_relay,
        "unix_connect",
        lambda path, **_kwargs: upstream_by_path[path],
    )
    downstream = _DownstreamTransport()
    hub = control_relay.ControlRelayHub(current_pid=9999)
    request = {
        "jsonrpc": "2.0",
        "id": "status",
        "method": "session.control.status",
        "params": {"session_key": "durable-root-1"},
    }

    first = hub.call(
        request,
        transport=downstream,
        auth_claims=_claims(),
        profile="default",
    )
    second = hub.call(
        {**request, "id": "status-again"},
        transport=downstream,
        auth_claims=_claims(),
        profile="default",
    )

    assert first["result"]["control_revision"] == 7
    assert second["result"]["control_revision"] == 7
    assert non_owner.closed is True
    assert [frame["method"] for frame in non_owner.sent] == [
        "relay.control.attach",
        "session.control.status",
    ]
    assert [frame["method"] for frame in owner.sent] == [
        "relay.control.attach",
        "session.control.status",
        "session.control.status",
    ]
    assert hub.close_transport(downstream) == 1


def test_relay_hub_uses_one_bounded_endpoint_snapshot_per_call(monkeypatch) -> None:
    endpoints = [
        control_relay.ControlEndpoint(
            pid=4312,
            profile="default",
            socket_path=control_relay._socket_dir() / "non-owner-one.sock",
            instance_id=_ENDPOINT_ONE,
        ),
        control_relay.ControlEndpoint(
            pid=4313,
            profile="default",
            socket_path=control_relay._socket_dir() / "non-owner-two.sock",
            instance_id=_ENDPOINT_TWO,
        ),
    ]
    upstream_by_path = {
        str(endpoint.socket_path): _RoutingUpstreamWebSocket(
            {
                "error": {
                    "code": 4202,
                    "message": "authoritative live runtime unavailable",
                }
            }
        )
        for endpoint in endpoints
    }
    registry_reads = 0

    def list_endpoints():
        nonlocal registry_reads
        registry_reads += 1
        return endpoints

    monkeypatch.setattr(control_relay, "list_control_endpoints", list_endpoints)
    monkeypatch.setattr(
        control_relay,
        "unix_connect",
        lambda path, **_kwargs: upstream_by_path[path],
    )
    downstream = _DownstreamTransport()
    hub = control_relay.ControlRelayHub(current_pid=9999)

    response = hub.call(
        {
            "jsonrpc": "2.0",
            "id": "status",
            "method": "session.control.status",
            "params": {"session_key": "durable-root-1"},
        },
        transport=downstream,
        auth_claims=_claims(),
        profile="default",
    )

    assert response is None
    assert registry_reads == 1
    assert all(upstream.closed for upstream in upstream_by_path.values())


@pytest.mark.parametrize("error_code", [4200, 4201, *range(4203, 4215)])
def test_relay_hub_does_not_fail_over_for_non_owner_routing_errors(
    monkeypatch,
    error_code,
) -> None:
    endpoints = [
        control_relay.ControlEndpoint(
            pid=4312,
            profile="default",
            socket_path=control_relay._socket_dir() / "first-owner.sock",
            instance_id=_ENDPOINT_ONE,
        ),
        control_relay.ControlEndpoint(
            pid=4313,
            profile="default",
            socket_path=control_relay._socket_dir() / "second-owner.sock",
            instance_id=_ENDPOINT_TWO,
        ),
    ]
    first_owner = _RoutingUpstreamWebSocket(
        {"error": {"code": error_code, "message": "authoritative rejection"}}
    )
    second_owner = _RoutingUpstreamWebSocket(
        {"result": {"controller_kind": "desktop", "control_revision": 7}}
    )
    upstream_by_path = {
        str(endpoints[0].socket_path): first_owner,
        str(endpoints[1].socket_path): second_owner,
    }
    monkeypatch.setattr(control_relay, "list_control_endpoints", lambda: endpoints)
    monkeypatch.setattr(
        control_relay,
        "unix_connect",
        lambda path, **_kwargs: upstream_by_path[path],
    )
    downstream = _DownstreamTransport()
    hub = control_relay.ControlRelayHub(current_pid=9999)

    response = hub.call(
        {
            "jsonrpc": "2.0",
            "id": "status",
            "method": "session.control.status",
            "params": {"session_key": "durable-root-1"},
        },
        transport=downstream,
        auth_claims=_claims(),
        profile="default",
    )

    assert response is not None
    assert response["error"]["code"] == error_code
    assert first_owner.closed is False
    assert [frame["method"] for frame in first_owner.sent] == [
        "relay.control.attach",
        "session.control.status",
    ]
    assert second_owner.sent == []
    assert hub.close_transport(downstream) == 1


def test_unexpected_upstream_disconnect_forces_downstream_control_reconnect(
    monkeypatch,
) -> None:
    endpoint = control_relay.ControlEndpoint(
        pid=4312,
        profile="default",
        socket_path=control_relay._socket_dir() / "owner.sock",
        instance_id=_ENDPOINT_ONE,
    )
    upstream = _FakeUpstreamWebSocket()
    monkeypatch.setattr(control_relay, "list_control_endpoints", lambda: [endpoint])
    monkeypatch.setattr(
        control_relay, "unix_connect", lambda *_args, **_kwargs: upstream
    )
    downstream = _DownstreamTransport()
    hub = control_relay.ControlRelayHub(current_pid=9999)

    assert (
        hub.call(
            {
                "jsonrpc": "2.0",
                "id": "status",
                "method": "session.control.status",
                "params": {"session_key": "durable-root-1"},
            },
            transport=downstream,
            auth_claims=_claims(),
            profile="default",
        )
        is not None
    )

    upstream.incoming.put(RuntimeError("owner stopped"))
    assert downstream.disconnected.wait(timeout=1)


def test_failed_downstream_event_write_closes_upstream_and_disconnects(
    monkeypatch,
) -> None:
    endpoint = control_relay.ControlEndpoint(
        pid=4312,
        profile="default",
        socket_path=control_relay._socket_dir() / "owner.sock",
        instance_id=_ENDPOINT_ONE,
    )
    upstream = _FakeUpstreamWebSocket()
    monkeypatch.setattr(control_relay, "list_control_endpoints", lambda: [endpoint])
    monkeypatch.setattr(
        control_relay, "unix_connect", lambda *_args, **_kwargs: upstream
    )
    downstream = _FailingDownstreamTransport()
    hub = control_relay.ControlRelayHub(current_pid=9999)

    assert (
        hub.call(
            {
                "jsonrpc": "2.0",
                "id": "status",
                "method": "session.control.status",
                "params": {"session_key": "durable-root-1"},
            },
            transport=downstream,
            auth_claims=_claims(),
            profile="default",
        )
        is not None
    )

    upstream.incoming.put(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "method": "event",
                "params": {"type": "session.updated", "payload": {}},
            }
        )
    )

    assert downstream.disconnected.wait(timeout=1)
    assert upstream.closed is True
    assert hub.close_transport(downstream) == 0
