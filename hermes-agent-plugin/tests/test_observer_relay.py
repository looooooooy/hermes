import json
import logging
import os
import queue
import stat
import threading
from pathlib import Path
from typing import ClassVar
from uuid import UUID

import pytest

from hermes_agent_plugin.adapters.platform.macos import observer_relay
from tests.test_support.runtime_descriptor_v2 import runtime_authority_v2

_INSTANCE_ID = "11111111-1111-4111-8111-111111111111"


class _ConnectionWebSocket:
    def __init__(self, incoming: tuple[dict, ...] = ()) -> None:
        self.incoming = tuple(json.dumps(frame) for frame in incoming)
        self.sent: list[dict] = []
        self.closed = False

    def __iter__(self):
        return iter(self.incoming)

    def send(self, value: str) -> None:
        self.sent.append(json.loads(value))

    def close(self) -> None:
        self.closed = True


def test_observer_send_executor_bounds_workers_across_repeated_stuck_connections() -> (
    None
):
    executor = observer_relay._BoundedCallExecutor(
        worker_limit=2,
        queue_limit=2,
        thread_name_prefix="hermes-test-bounded-send",
    )

    class NeverReturningWebSocket:
        def __init__(self) -> None:
            self.started = threading.Event()
            self.never_release = threading.Event()

        def send(self, _value: str) -> None:
            self.started.set()
            self.never_release.wait()

        def close(self) -> None:
            return None

    sockets = [NeverReturningWebSocket() for _ in range(12)]
    for websocket in sockets:
        transport = observer_relay._ObserverSocketTransport(
            websocket,
            send_timeout_s=0.01,
            send_abort_grace_s=0.01,
            send_executor=executor,
        )
        assert transport.write({"jsonrpc": "2.0", "method": "event"}) is False

    workers = [
        thread
        for thread in threading.enumerate()
        if thread.name.startswith("hermes-test-bounded-send")
    ]
    assert executor.worker_count == 2
    assert len(workers) == 2
    assert sum(websocket.started.is_set() for websocket in sockets) == 2


def test_observer_send_timeout_closes_raw_socket_and_recovers_worker_capacity() -> None:
    executor = observer_relay._BoundedCallExecutor(
        worker_limit=1,
        queue_limit=1,
        thread_name_prefix="hermes-test-recover-send",
    )

    class ClosingSocket:
        def __init__(self, release: threading.Event) -> None:
            self.release = release
            self.shutdown_calls = 0
            self.close_calls = 0

        def shutdown(self, _how: int) -> None:
            self.shutdown_calls += 1
            self.release.set()

        def close(self) -> None:
            self.close_calls += 1
            self.release.set()

    class SocketBlockedWebSocket:
        def __init__(self) -> None:
            self.release = threading.Event()
            self.send_finished = threading.Event()
            self.socket = ClosingSocket(self.release)

        def send(self, _value: str) -> None:
            self.release.wait()
            self.send_finished.set()

        def close(self) -> None:
            self.release.set()

    blocked = SocketBlockedWebSocket()
    transport = observer_relay._ObserverSocketTransport(
        blocked,
        send_timeout_s=0.01,
        send_abort_grace_s=0.2,
        send_executor=executor,
    )

    assert transport.write({"jsonrpc": "2.0", "method": "event"}) is False
    assert blocked.send_finished.wait(timeout=0.2)
    assert blocked.socket.shutdown_calls == 1
    assert blocked.socket.close_calls == 1

    healthy = _ConnectionWebSocket()
    recovered = observer_relay._ObserverSocketTransport(
        healthy,
        send_timeout_s=0.1,
        send_executor=executor,
    )
    assert recovered.write({"jsonrpc": "2.0", "method": "event"}) is True


@pytest.mark.parametrize(
    "signal_type",
    (KeyboardInterrupt, SystemExit, GeneratorExit),
)
def test_observer_transport_does_not_swallow_process_control_exceptions(
    monkeypatch,
    signal_type: type[BaseException],
) -> None:
    websocket = _ConnectionWebSocket()
    transport = observer_relay._ObserverSocketTransport(websocket)

    def raise_signal(_frame: dict) -> str:
        raise signal_type()

    monkeypatch.setattr(observer_relay, "encode_frame", raise_signal)

    with pytest.raises(signal_type):
        transport.write({"jsonrpc": "2.0", "method": "event"})

    assert websocket.closed is False


@pytest.mark.parametrize(
    ("observer_contract", "expected_payload"),
    (
        (
            1,
            {
                "local_gateway_protocol": 1,
                "observer_contract": 1,
                "connection_role": "observer",
                "profile": "default",
                "runtime_generation": "runtime-generation-1",
                "instance_id": _INSTANCE_ID,
            },
        ),
        (
            2,
            {
                "observer_contract": 2,
                "connection_role": "observer",
            },
        ),
    ),
)
def test_observer_ready_payload_is_exact_for_selected_contract(
    observer_contract: int,
    expected_payload: dict[str, object],
) -> None:
    websocket = _ConnectionWebSocket()

    observer_relay._handle_observer_connection(
        websocket,
        dispatch=lambda _request, _transport: None,
        remove_observer_subscriptions=lambda _transport: None,
        profile="default",
        runtime_generation="runtime-generation-1",
        instance_id=_INSTANCE_ID,
        observer_contract=observer_contract,
    )

    assert websocket.sent == [
        {
            "jsonrpc": "2.0",
            "method": "event",
            "params": {
                "type": "gateway.ready",
                "payload": expected_payload,
            },
        }
    ]


def test_v2_observer_requests_are_exact_before_host_dispatch() -> None:
    websocket = _ConnectionWebSocket(
        (
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "session.observe.subscribe",
                "params": {
                    "observer_contract": 2,
                    "session_key": "durable-session-1",
                    "profile": "default",
                },
            },
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "session.observe.unsubscribe",
                "params": {
                    "observer_contract": 2,
                    "subscription_id": "subscription-1",
                },
            },
        )
    )
    dispatched: list[dict] = []

    def dispatch(request: dict, _transport: object) -> dict:
        dispatched.append(request)
        return {"jsonrpc": "2.0", "id": request["id"], "result": {}}

    observer_relay._handle_observer_connection(
        websocket,
        dispatch=dispatch,
        remove_observer_subscriptions=lambda _transport: None,
        profile="default",
        runtime_generation="runtime-generation-1",
        instance_id=_INSTANCE_ID,
        observer_contract=2,
    )

    assert dispatched == [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "session.observe.subscribe",
            "params": {
                "observer_contract": 2,
                "session_key": "durable-session-1",
                "profile": "default",
                "runtime_generation": "runtime-generation-1",
            },
        },
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "session.observe.unsubscribe",
            "params": {
                "observer_contract": 2,
                "subscription_id": "subscription-1",
            },
        },
    ]


@pytest.mark.parametrize("observer_contract", (1, 2))
def test_catalog_request_uses_the_same_persistent_observer_transport(
    observer_contract: int,
) -> None:
    request = {
        "jsonrpc": "2.0",
        "id": "11111111-1111-4111-8111-111111111111",
        "method": "session.catalog.subscribe",
        "params": {
            "profile": "default",
            "runtime_generation": "runtime-generation-1",
            "page_size": 128,
        },
    }
    websocket = _ConnectionWebSocket((request,))
    dispatched: list[dict] = []

    def dispatch(value: dict, transport: object) -> None:
        dispatched.append(value)
        assert transport.write(
            {
                "jsonrpc": "2.0",
                "id": value["id"],
                "error": {
                    "code": 4400,
                    "message": "session catalog reset required",
                    "reason": "transport_replaced",
                },
            }
        )

    observer_relay._handle_observer_connection(
        websocket,
        dispatch=dispatch,
        remove_observer_subscriptions=lambda _transport: None,
        profile="default",
        runtime_generation="runtime-generation-1",
        instance_id=_INSTANCE_ID,
        observer_contract=observer_contract,
    )

    assert dispatched == [request]
    assert websocket.sent[-1]["id"] == request["id"]


def test_invalid_catalog_request_maps_to_body_free_jsonrpc_error() -> None:
    request = {
        "jsonrpc": "2.0",
        "id": "11111111-1111-4111-8111-111111111111",
        "method": "session.catalog.subscribe",
        "params": {
            "profile": "default",
            "runtime_generation": "runtime-generation-1",
            "page_size": 129,
        },
    }
    websocket = _ConnectionWebSocket((request,))

    def reject(_value: dict, _transport: object) -> None:
        raise ValueError("token=secret invalid body")

    observer_relay._handle_observer_connection(
        websocket,
        dispatch=reject,
        remove_observer_subscriptions=lambda _transport: None,
        profile="default",
        runtime_generation="runtime-generation-1",
        instance_id=_INSTANCE_ID,
        observer_contract=2,
    )

    assert websocket.sent[-1] == {
        "jsonrpc": "2.0",
        "id": request["id"],
        "error": {"code": -32602, "message": "invalid params"},
    }
    assert "secret" not in json.dumps(websocket.sent)


@pytest.mark.parametrize(
    ("method", "params"),
    (
        (
            "session.observe.subscribe",
            {
                "observer_contract": 2,
                "session_key": "durable-session-1",
                "profile": "default",
                "relay_local_only": True,
            },
        ),
        (
            "session.observe.unsubscribe",
            {
                "observer_contract": 2,
                "subscription_id": "subscription-1",
                "relay_local_only": True,
            },
        ),
    ),
)
def test_v2_observer_rejects_nonexact_request_before_host_dispatch(
    method: str,
    params: dict[str, object],
) -> None:
    websocket = _ConnectionWebSocket(
        (
            {
                "jsonrpc": "2.0",
                "id": "invalid-v2",
                "method": method,
                "params": params,
            },
        )
    )
    dispatched: list[dict] = []

    observer_relay._handle_observer_connection(
        websocket,
        dispatch=lambda request, _transport: dispatched.append(request),
        remove_observer_subscriptions=lambda _transport: None,
        profile="default",
        runtime_generation="runtime-generation-1",
        instance_id=_INSTANCE_ID,
        observer_contract=2,
    )

    assert dispatched == []
    assert websocket.sent[-1] == {
        "jsonrpc": "2.0",
        "id": "invalid-v2",
        "error": {"code": -32602, "message": "invalid params"},
    }


def test_observer_callback_exception_log_never_contains_sensitive_exception_text(
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
                            "id": "subscribe",
                            "method": "session.observe.subscribe",
                            "params": {},
                        }
                    ),
                )
            )

        def send(self, value: str) -> None:
            self.sent.append(value)

        def close(self) -> None:
            return None

    def fail_dispatch(_request, _transport):
        raise RuntimeError(sentinel)

    caplog.set_level(logging.DEBUG)
    observer_relay._handle_observer_connection(
        WebSocket(),
        dispatch=fail_dispatch,
        remove_observer_subscriptions=lambda _transport: None,
        profile="default",
        runtime_generation="runtime-generation-1",
        instance_id=_INSTANCE_ID,
    )

    assert sentinel not in caplog.text


def _start_observer_endpoint(*, profile: str):
    return observer_relay.start_observer_endpoint(
        authority=runtime_authority_v2(profile=profile),
        dispatch=lambda request, transport: None,
        remove_observer_subscriptions=lambda transport: None,
    )


class _DownstreamTransport:
    def __init__(self):
        self.frames = []
        self.frame_received = threading.Event()
        self.disconnected = threading.Event()

    def write(self, frame: dict) -> bool:
        self.frames.append(frame)
        self.frame_received.set()
        return True

    def disconnect(self) -> None:
        self.disconnected.set()


class _FakeUpstreamWebSocket:
    def __init__(
        self,
        subscription_result: dict,
        *,
        before_result: tuple[dict, ...] = (),
        ready_profile: str = "default",
        ready_generation: str = "runtime-generation-1",
        ready_overrides: dict[str, object] | None = None,
        ready_omit: frozenset[str] = frozenset(),
        scope_snapshot: bool = True,
    ):
        self.sent = []
        self.closed = False
        self.closed_event = threading.Event()
        self.incoming = queue.Queue()
        if scope_snapshot:
            subscription_result = {
                "profile": ready_profile,
                "runtime_generation": ready_generation,
                **subscription_result,
            }
        ready_payload = {
            "local_gateway_protocol": 1,
            "observer_contract": 1,
            "connection_role": "observer",
            "profile": ready_profile,
            "runtime_generation": ready_generation,
            "instance_id": _INSTANCE_ID,
        }
        ready_payload.update(ready_overrides or {})
        for field in ready_omit:
            ready_payload.pop(field, None)
        self.incoming.put(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "method": "event",
                    "params": {
                        "type": "gateway.ready",
                        "payload": ready_payload,
                    },
                }
            )
        )
        for frame in before_result:
            self.incoming.put(json.dumps(frame))
        self.incoming.put(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": "relay-subscribe",
                    "result": subscription_result,
                }
            )
        )

    def send(self, value: str) -> None:
        self.sent.append(json.loads(value))

    def recv(self, timeout=None):
        item = self.incoming.get(timeout=timeout)
        if isinstance(item, BaseException):
            raise item
        return item

    def close(self) -> None:
        self.closed = True
        self.closed_event.set()
        self.incoming.put(RuntimeError("closed"))


class _ManualTimer:
    created: ClassVar[list["_ManualTimer"]] = []

    def __init__(self, interval, function):
        self.interval = interval
        self.function = function
        self.cancelled = False
        self.daemon = False
        self.started = False
        self.created.append(self)

    def start(self) -> None:
        self.started = True

    def cancel(self) -> None:
        self.cancelled = True


def test_relay_rejects_stale_descriptor_or_ready_generation_before_subscribe(
    monkeypatch,
) -> None:
    endpoint = observer_relay.ObserverEndpoint(
        pid=4312,
        profile="default",
        runtime_generation="runtime-generation-2",
        socket_path=observer_relay._socket_dir() / "owner.sock",
        instance_id=_INSTANCE_ID,
    )
    snapshot = {
        "profile": "default",
        "runtime_generation": "runtime-generation-2",
        "subscription_id": "upstream-subscription",
        "session_key": "durable-key",
        "runtime_session_id": "runtime-id",
        "snapshot_event_sequence": 0,
        "event_sequence": 0,
        "replay_events": [],
    }
    stale_ready = _FakeUpstreamWebSocket(
        snapshot,
        ready_generation="runtime-generation-1",
    )
    current_ready = _FakeUpstreamWebSocket(
        snapshot,
        ready_generation="runtime-generation-2",
    )
    upstreams = iter((stale_ready, current_ready))
    monkeypatch.setattr(observer_relay, "list_observer_endpoints", lambda: [endpoint])
    monkeypatch.setattr(
        observer_relay,
        "unix_connect",
        lambda *_args, **_kwargs: next(upstreams),
    )
    hub = observer_relay.ObserverRelayHub(current_pid=9999)
    downstream = _DownstreamTransport()

    assert (
        hub.subscribe(
            "durable-key",
            "default",
            downstream,
            runtime_generation="runtime-generation-1",
        )
        is None
    )
    assert stale_ready.sent == []
    assert (
        hub.subscribe(
            "durable-key",
            "default",
            downstream,
            runtime_generation="runtime-generation-2",
        )
        is None
    )
    assert stale_ready.sent == []
    current = hub.subscribe(
        "durable-key",
        "default",
        downstream,
        runtime_generation="runtime-generation-2",
    )

    assert current is not None
    assert current_ready.sent[0]["params"]["runtime_generation"] == (
        "runtime-generation-2"
    )
    assert hub.close_transport(downstream) == 1
    assert current_ready.closed is True


@pytest.mark.parametrize(
    "ready_override",
    [
        {"local_gateway_protocol": 2},
        {"observer_contract": 2},
        {"connection_role": "control"},
        {"instance_id": "22222222-2222-4222-8222-222222222222"},
        {"profile": None},
        {"runtime_generation": None},
        {"unexpected": True},
    ],
)
def test_upstream_ready_requires_exact_observer_identity(ready_override) -> None:
    upstream = _FakeUpstreamWebSocket(
        {
            "profile": "default",
            "runtime_generation": "runtime-generation-1",
            "session_key": "durable-key",
            "runtime_session_id": "runtime-id",
            "snapshot_event_sequence": 0,
            "event_sequence": 0,
            "replay_events": [],
        },
        ready_overrides=ready_override,
    )

    result, pending = observer_relay._subscribe_upstream(
        upstream,
        session_key="durable-key",
        profile="default",
        runtime_generation="runtime-generation-1",
        instance_id=_INSTANCE_ID,
    )

    assert result is None
    assert pending == []
    assert upstream.sent == []


@pytest.mark.parametrize(
    "missing_field",
    [
        "local_gateway_protocol",
        "observer_contract",
        "connection_role",
        "profile",
        "runtime_generation",
        "instance_id",
    ],
)
def test_upstream_ready_rejects_every_missing_identity_field(missing_field) -> None:
    upstream = _FakeUpstreamWebSocket(
        {
            "session_key": "durable-key",
            "runtime_session_id": "runtime-id",
            "snapshot_event_sequence": 0,
            "event_sequence": 0,
            "replay_events": [],
        },
        ready_omit=frozenset({missing_field}),
    )

    result, pending = observer_relay._subscribe_upstream(
        upstream,
        session_key="durable-key",
        profile="default",
        runtime_generation="runtime-generation-1",
        instance_id=_INSTANCE_ID,
    )

    assert result is None
    assert pending == []
    assert upstream.sent == []


@pytest.mark.parametrize(
    "snapshot",
    [
        {
            "runtime_generation": "runtime-generation-1",
            "session_key": "durable-key",
            "runtime_session_id": "runtime-id",
            "snapshot_event_sequence": 0,
            "event_sequence": 0,
            "replay_events": [],
        },
        {
            "profile": "default",
            "runtime_generation": "runtime-generation-2",
            "session_key": "durable-key",
            "runtime_session_id": "runtime-id",
            "snapshot_event_sequence": 0,
            "event_sequence": 0,
            "replay_events": [],
        },
    ],
)
def test_relay_rejects_snapshot_without_exact_scope(snapshot, monkeypatch) -> None:
    endpoint = observer_relay.ObserverEndpoint(
        pid=4312,
        profile="default",
        runtime_generation="runtime-generation-1",
        socket_path=observer_relay._socket_dir() / "owner.sock",
        instance_id=_INSTANCE_ID,
    )
    upstream = _FakeUpstreamWebSocket(snapshot, scope_snapshot=False)
    monkeypatch.setattr(observer_relay, "list_observer_endpoints", lambda: [endpoint])
    monkeypatch.setattr(
        observer_relay,
        "unix_connect",
        lambda *_args, **_kwargs: upstream,
    )

    result = observer_relay.ObserverRelayHub(current_pid=9999).subscribe(
        "durable-key",
        "default",
        _DownstreamTransport(),
        runtime_generation="runtime-generation-1",
    )

    assert result is None
    assert upstream.closed is True


def test_observer_endpoint_registration_is_private_and_removed(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_OBSERVER_REGISTRY_DIR", str(tmp_path))
    socket_dir = Path("/tmp").resolve(strict=True) / f"hobs-test-{os.getpid()}"
    monkeypatch.setenv("HERMES_OBSERVER_SOCKET_DIR", str(socket_dir))

    registration = _start_observer_endpoint(profile="default")

    try:
        files = list(tmp_path.glob("*.json"))
        assert len(files) == 1
        assert stat.S_IMODE(files[0].stat().st_mode) == 0o600
        registry_text = files[0].read_text(encoding="utf-8")
        assert "token" not in registry_text
        assert "internal" not in registry_text
        assert "ws_url" not in registry_text
        endpoint = observer_relay.list_observer_endpoints()[0]
        assert endpoint.pid > 0
        assert endpoint.profile == "default"
        assert endpoint.instance_id == str(UUID(endpoint.instance_id))
        assert endpoint.socket_path.exists()
        assert endpoint.socket_path.parent == socket_dir.resolve(strict=False)
        assert stat.S_IMODE(socket_dir.stat().st_mode) == 0o700
        assert stat.S_IMODE(endpoint.socket_path.stat().st_mode) == 0o600
    finally:
        registration.close()

    assert list(tmp_path.glob("*.json")) == []
    assert list(tmp_path.glob("*.sock")) == []


def test_relay_subscription_preserves_snapshot_and_forwards_upstream_events(
    monkeypatch,
):
    endpoint = observer_relay.ObserverEndpoint(
        pid=4312,
        profile="default",
        runtime_generation="runtime-generation-1",
        socket_path=observer_relay._socket_dir() / "owner.sock",
        instance_id=_INSTANCE_ID,
    )
    snapshot = {
        "subscription_id": "upstream-subscription",
        "session_key": "durable-key",
        "runtime_session_id": "runtime-id",
        "running": True,
        "status": "working",
        "messages": [],
        "snapshot_event_sequence": 4,
        "event_sequence": 4,
        "replay_events": [],
    }
    upstream = _FakeUpstreamWebSocket(snapshot)
    monkeypatch.setattr(observer_relay, "list_observer_endpoints", lambda: [endpoint])
    connect_options = {}

    def connect(*_args, **kwargs):
        connect_options.update(kwargs)
        return upstream

    monkeypatch.setattr(observer_relay, "unix_connect", connect)
    downstream = _DownstreamTransport()
    hub = observer_relay.ObserverRelayHub(current_pid=9999)

    result = hub.subscribe(
        session_key="durable-key",
        profile="default",
        transport=downstream,
        runtime_generation="runtime-generation-1",
    )

    assert result is not None
    assert result["subscription_id"] != "upstream-subscription"
    assert result["subscription_id"] == str(UUID(result["subscription_id"]))
    assert result["session_key"] == "durable-key"
    assert result["runtime_session_id"] == "runtime-id"
    assert result["snapshot_event_sequence"] == 4
    assert result["event_sequence"] == 4
    assert upstream.sent == [
        {
            "jsonrpc": "2.0",
            "id": "relay-subscribe",
            "method": "session.observe.subscribe",
            "params": {
                "session_key": "durable-key",
                "profile": "default",
                "runtime_generation": "runtime-generation-1",
                "relay_local_only": True,
            },
        }
    ]
    assert connect_options["max_queue"] == 32

    assert hub.activate(result["subscription_id"], downstream) is True
    upstream.incoming.put(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "method": "event",
                "params": {
                    "type": "message.delta",
                    "profile": "default",
                    "runtime_generation": "runtime-generation-1",
                    "session_id": "runtime-id",
                    "session_key": "durable-key",
                    "event_sequence": 5,
                    "payload": {"text": "hello"},
                },
            }
        )
    )
    assert downstream.frame_received.wait(timeout=1)
    assert downstream.frames[-1]["params"]["event_sequence"] == 5
    assert downstream.frames[-1]["params"]["payload"] == {"text": "hello"}

    assert hub.unsubscribe(result["subscription_id"], downstream) is True
    assert upstream.closed is True
    assert downstream.disconnected.is_set() is False


@pytest.mark.parametrize(
    "scope",
    [
        {"runtime_generation": "runtime-generation-1"},
        {"profile": "default", "runtime_generation": "runtime-generation-2"},
        {
            "profile": "default",
            "runtime_generation": "runtime-generation-1",
            "session_key": "other",
        },
        {
            "profile": "default",
            "runtime_generation": "runtime-generation-1",
            "session_id": "other",
        },
    ],
)
def test_relay_closes_on_event_without_exact_scope(scope, monkeypatch) -> None:
    endpoint = observer_relay.ObserverEndpoint(
        pid=4312,
        profile="default",
        runtime_generation="runtime-generation-1",
        socket_path=observer_relay._socket_dir() / "owner.sock",
        instance_id=_INSTANCE_ID,
    )
    upstream = _FakeUpstreamWebSocket(
        {
            "session_key": "durable-key",
            "runtime_session_id": "runtime-id",
            "snapshot_event_sequence": 0,
            "event_sequence": 0,
            "replay_events": [],
        }
    )
    monkeypatch.setattr(observer_relay, "list_observer_endpoints", lambda: [endpoint])
    monkeypatch.setattr(
        observer_relay,
        "unix_connect",
        lambda *_args, **_kwargs: upstream,
    )
    downstream = _DownstreamTransport()
    hub = observer_relay.ObserverRelayHub(current_pid=9999)
    result = hub.subscribe(
        "durable-key",
        "default",
        downstream,
        runtime_generation="runtime-generation-1",
    )
    assert result is not None
    assert hub.activate(result["subscription_id"], downstream) is True
    params = {
        "type": "message.delta",
        "session_id": "runtime-id",
        "session_key": "durable-key",
        "event_sequence": 1,
        "payload": {"text": "must not forward"},
        **scope,
    }
    upstream.incoming.put(
        json.dumps({"jsonrpc": "2.0", "method": "event", "params": params})
    )

    assert downstream.disconnected.wait(timeout=1)
    assert downstream.frames == []
    assert upstream.closed is True


def test_unexpected_upstream_disconnect_forces_downstream_reconnect(monkeypatch):
    endpoint = observer_relay.ObserverEndpoint(
        pid=4312,
        profile="default",
        runtime_generation="runtime-generation-1",
        socket_path=observer_relay._socket_dir() / "owner.sock",
        instance_id=_INSTANCE_ID,
    )
    snapshot = {
        "subscription_id": "upstream-subscription",
        "session_key": "durable-key",
        "runtime_session_id": "runtime-id",
        "running": True,
        "status": "working",
        "messages": [],
        "snapshot_event_sequence": 0,
        "event_sequence": 0,
        "replay_events": [],
    }
    upstream = _FakeUpstreamWebSocket(snapshot)
    monkeypatch.setattr(observer_relay, "list_observer_endpoints", lambda: [endpoint])
    monkeypatch.setattr(
        observer_relay, "unix_connect", lambda *_args, **_kwargs: upstream
    )
    downstream = _DownstreamTransport()
    hub = observer_relay.ObserverRelayHub(current_pid=9999)

    result = hub.subscribe(
        "durable-key",
        "default",
        downstream,
        runtime_generation="runtime-generation-1",
    )
    assert result is not None

    assert hub.activate(result["subscription_id"], downstream) is True
    upstream.incoming.put(RuntimeError("owner stopped"))
    assert downstream.disconnected.wait(timeout=1)
    assert upstream.closed is True


def test_relay_holds_pending_and_live_events_until_snapshot_response_is_written(
    monkeypatch,
):
    endpoint = observer_relay.ObserverEndpoint(
        pid=4312,
        profile="default",
        runtime_generation="runtime-generation-1",
        socket_path=observer_relay._socket_dir() / "owner.sock",
        instance_id=_INSTANCE_ID,
    )
    snapshot = {
        "subscription_id": "upstream-subscription",
        "session_key": "durable-key",
        "runtime_session_id": "runtime-id",
        "running": True,
        "status": "working",
        "messages": [],
        "snapshot_event_sequence": 4,
        "event_sequence": 4,
        "replay_events": [],
    }
    pending = {
        "jsonrpc": "2.0",
        "method": "event",
        "params": {
            "type": "message.delta",
            "profile": "default",
            "runtime_generation": "runtime-generation-1",
            "session_id": "runtime-id",
            "session_key": "durable-key",
            "event_sequence": 5,
            "payload": {"text": "pending"},
        },
    }
    upstream = _FakeUpstreamWebSocket(snapshot, before_result=(pending,))
    monkeypatch.setattr(observer_relay, "list_observer_endpoints", lambda: [endpoint])
    monkeypatch.setattr(
        observer_relay, "unix_connect", lambda *_args, **_kwargs: upstream
    )
    downstream = _DownstreamTransport()
    hub = observer_relay.ObserverRelayHub(current_pid=9999)

    result = hub.subscribe(
        "durable-key",
        "default",
        downstream,
        runtime_generation="runtime-generation-1",
    )
    assert result is not None
    assert downstream.frame_received.wait(timeout=0.05) is False
    assert downstream.frames == []

    assert hub.activate(result["subscription_id"], downstream) is True
    assert downstream.frame_received.wait(timeout=1)
    assert downstream.frames == [pending]
    assert hub.unsubscribe(result["subscription_id"], downstream) is True


def test_upstream_sequence_gap_is_not_forwarded_and_forces_snapshot_reconnect(
    monkeypatch,
):
    endpoint = observer_relay.ObserverEndpoint(
        pid=4312,
        profile="default",
        runtime_generation="runtime-generation-1",
        socket_path=observer_relay._socket_dir() / "owner.sock",
        instance_id=_INSTANCE_ID,
    )
    snapshot = {
        "subscription_id": "upstream-subscription",
        "session_key": "durable-key",
        "runtime_session_id": "runtime-id",
        "running": True,
        "status": "working",
        "messages": [],
        "snapshot_event_sequence": 4,
        "event_sequence": 4,
        "replay_events": [],
    }
    upstream = _FakeUpstreamWebSocket(snapshot)
    monkeypatch.setattr(observer_relay, "list_observer_endpoints", lambda: [endpoint])
    monkeypatch.setattr(
        observer_relay, "unix_connect", lambda *_args, **_kwargs: upstream
    )
    downstream = _DownstreamTransport()
    hub = observer_relay.ObserverRelayHub(current_pid=9999)

    result = hub.subscribe(
        "durable-key",
        "default",
        downstream,
        runtime_generation="runtime-generation-1",
    )
    assert result is not None

    try:
        assert hub.activate(result["subscription_id"], downstream) is True
        upstream.incoming.put(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "method": "event",
                    "params": {
                        "type": "message.delta",
                        "profile": "default",
                        "runtime_generation": "runtime-generation-1",
                        "session_id": "runtime-id",
                        "session_key": "durable-key",
                        "event_sequence": 6,
                        "payload": {"text": "must not cross the gap"},
                    },
                }
            )
        )

        assert downstream.disconnected.wait(timeout=1)
        assert downstream.frames == []
        assert upstream.closed is True
    finally:
        hub.close_transport(downstream)


def test_subscribe_pending_frames_are_bounded():
    pending = tuple(
        {
            "jsonrpc": "2.0",
            "method": "event",
            "params": {
                "type": "message.delta",
                "profile": "default",
                "runtime_generation": "runtime-generation-1",
                "session_id": "runtime-id",
                "session_key": "durable-key",
                "event_sequence": sequence,
                "payload": {"text": "pending"},
            },
        }
        for sequence in range(1, 34)
    )
    upstream = _FakeUpstreamWebSocket(
        {
            "subscription_id": "upstream-subscription",
            "session_key": "durable-key",
            "runtime_session_id": "runtime-id",
            "snapshot_event_sequence": 0,
            "event_sequence": 0,
            "replay_events": [],
        },
        before_result=pending,
    )

    with pytest.raises(RuntimeError, match="pending frame limit"):
        observer_relay._subscribe_upstream(
            upstream,
            session_key="durable-key",
            profile="default",
            runtime_generation="runtime-generation-1",
            instance_id=_INSTANCE_ID,
        )


def test_subscribe_uses_one_total_rpc_deadline(monkeypatch):
    class EndlessEvents:
        def __init__(self) -> None:
            self.calls = 0
            self.timeouts = []

        def recv(self, timeout=None):
            self.calls += 1
            self.timeouts.append(timeout)
            if self.calls > 4:
                raise RuntimeError("per-frame timeout was reset")
            if self.calls == 1:
                return json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "method": "event",
                        "params": {
                            "type": "gateway.ready",
                            "payload": {
                                "local_gateway_protocol": 1,
                                "observer_contract": 1,
                                "connection_role": "observer",
                                "profile": "default",
                                "runtime_generation": "runtime-generation-1",
                                "instance_id": _INSTANCE_ID,
                            },
                        },
                    }
                )
            return json.dumps(
                {
                    "jsonrpc": "2.0",
                    "method": "event",
                    "params": {
                        "type": "message.delta",
                        "profile": "default",
                        "runtime_generation": "runtime-generation-1",
                        "session_id": "runtime-id",
                        "session_key": "durable-key",
                        "event_sequence": self.calls - 1,
                        "payload": {"text": "pending"},
                    },
                }
            )

        def send(self, _value):
            return None

    clock = iter((0.0, 0.0, 1.0, 2.0, 3.0))
    monkeypatch.setattr(observer_relay, "monotonic", lambda: next(clock), raising=False)
    upstream = EndlessEvents()

    with pytest.raises(TimeoutError):
        observer_relay._subscribe_upstream(
            upstream,
            session_key="durable-key",
            profile="default",
            runtime_generation="runtime-generation-1",
            instance_id=_INSTANCE_ID,
        )

    assert upstream.timeouts == [3.0, 2.0, 1.0]


def test_activation_failure_rolls_back_subscription_and_closes_upstream(
    monkeypatch,
):
    endpoint = observer_relay.ObserverEndpoint(
        pid=4312,
        profile="default",
        runtime_generation="runtime-generation-1",
        socket_path=observer_relay._socket_dir() / "owner.sock",
        instance_id=_INSTANCE_ID,
    )
    snapshot = {
        "subscription_id": "upstream-subscription",
        "session_key": "durable-key",
        "runtime_session_id": "runtime-id",
        "snapshot_event_sequence": 0,
        "event_sequence": 0,
        "replay_events": [],
    }
    upstream = _FakeUpstreamWebSocket(snapshot)
    monkeypatch.setattr(observer_relay, "list_observer_endpoints", lambda: [endpoint])
    monkeypatch.setattr(
        observer_relay, "unix_connect", lambda *_args, **_kwargs: upstream
    )
    monkeypatch.setattr(
        observer_relay._RelaySubscription,
        "start",
        lambda _self: (_ for _ in ()).throw(RuntimeError("cannot start")),
    )
    downstream = _DownstreamTransport()
    hub = observer_relay.ObserverRelayHub(current_pid=9999)

    result = hub.subscribe(
        "durable-key",
        "default",
        downstream,
        runtime_generation="runtime-generation-1",
    )
    assert result is not None
    with pytest.raises(RuntimeError, match="cannot start"):
        hub.activate(result["subscription_id"], downstream)

    assert upstream.closed is True
    assert hub.close_transport(downstream) == 0


def test_prepared_subscription_expires_and_closes_upstream(monkeypatch):
    endpoint = observer_relay.ObserverEndpoint(
        pid=4312,
        profile="default",
        runtime_generation="runtime-generation-1",
        socket_path=observer_relay._socket_dir() / "owner.sock",
        instance_id=_INSTANCE_ID,
    )
    upstream = _FakeUpstreamWebSocket(
        {
            "subscription_id": "upstream-subscription",
            "session_key": "durable-key",
            "runtime_session_id": "runtime-id",
            "snapshot_event_sequence": 0,
            "event_sequence": 0,
            "replay_events": [],
        }
    )
    monkeypatch.setattr(observer_relay, "_ACTIVATION_TIMEOUT_S", 0.02)
    monkeypatch.setattr(observer_relay, "list_observer_endpoints", lambda: [endpoint])
    monkeypatch.setattr(
        observer_relay, "unix_connect", lambda *_args, **_kwargs: upstream
    )
    downstream = _DownstreamTransport()
    hub = observer_relay.ObserverRelayHub(current_pid=9999)

    result = hub.subscribe(
        "durable-key",
        "default",
        downstream,
        runtime_generation="runtime-generation-1",
    )
    assert result is not None
    assert upstream.closed_event.wait(timeout=1)

    assert hub.activate(result["subscription_id"], downstream) is None
    assert hub.close_transport(downstream) == 0


def test_timer_start_failure_rolls_back_prepared_subscription(monkeypatch):
    class FailingTimer:
        def __init__(self, _interval, _function):
            self.daemon = False

        def start(self) -> None:
            raise RuntimeError("timer unavailable")

        def cancel(self) -> None:
            return None

    endpoint = observer_relay.ObserverEndpoint(
        pid=4312,
        profile="default",
        runtime_generation="runtime-generation-1",
        socket_path=observer_relay._socket_dir() / "owner.sock",
        instance_id=_INSTANCE_ID,
    )
    upstream = _FakeUpstreamWebSocket(
        {
            "subscription_id": "upstream-subscription",
            "session_key": "durable-key",
            "runtime_session_id": "runtime-id",
            "snapshot_event_sequence": 0,
            "event_sequence": 0,
            "replay_events": [],
        }
    )
    monkeypatch.setattr(observer_relay.threading, "Timer", FailingTimer)
    monkeypatch.setattr(observer_relay, "list_observer_endpoints", lambda: [endpoint])
    monkeypatch.setattr(
        observer_relay, "unix_connect", lambda *_args, **_kwargs: upstream
    )
    downstream = _DownstreamTransport()
    hub = observer_relay.ObserverRelayHub(current_pid=9999)

    assert (
        hub.subscribe(
            "durable-key",
            "default",
            downstream,
            runtime_generation="runtime-generation-1",
        )
        is None
    )
    assert upstream.closed is True
    assert hub.close_transport(downstream) == 0


def test_activation_checks_total_deadline_when_timer_delivery_is_delayed(monkeypatch):
    endpoint = observer_relay.ObserverEndpoint(
        pid=4312,
        profile="default",
        runtime_generation="runtime-generation-1",
        socket_path=observer_relay._socket_dir() / "owner.sock",
        instance_id=_INSTANCE_ID,
    )
    upstream = _FakeUpstreamWebSocket(
        {
            "subscription_id": "upstream-subscription",
            "session_key": "durable-key",
            "runtime_session_id": "runtime-id",
            "snapshot_event_sequence": 0,
            "event_sequence": 0,
            "replay_events": [],
        }
    )
    _ManualTimer.created.clear()
    monkeypatch.setattr(observer_relay, "_ACTIVATION_TIMEOUT_S", -1.0)
    monkeypatch.setattr(observer_relay.threading, "Timer", _ManualTimer)
    monkeypatch.setattr(observer_relay, "list_observer_endpoints", lambda: [endpoint])
    monkeypatch.setattr(
        observer_relay, "unix_connect", lambda *_args, **_kwargs: upstream
    )
    downstream = _DownstreamTransport()
    hub = observer_relay.ObserverRelayHub(current_pid=9999)

    result = hub.subscribe(
        "durable-key",
        "default",
        downstream,
        runtime_generation="runtime-generation-1",
    )
    assert result is not None
    assert _ManualTimer.created[0].started is True

    assert hub.activate(result["subscription_id"], downstream) is None
    assert upstream.closed is True
    assert hub.close_transport(downstream) == 0


def test_activation_and_expiry_race_has_one_owner_and_no_leak(monkeypatch):
    endpoint = observer_relay.ObserverEndpoint(
        pid=4312,
        profile="default",
        runtime_generation="runtime-generation-1",
        socket_path=observer_relay._socket_dir() / "owner.sock",
        instance_id=_INSTANCE_ID,
    )
    upstream = _FakeUpstreamWebSocket(
        {
            "subscription_id": "upstream-subscription",
            "session_key": "durable-key",
            "runtime_session_id": "runtime-id",
            "snapshot_event_sequence": 0,
            "event_sequence": 0,
            "replay_events": [],
        }
    )
    _ManualTimer.created.clear()
    monkeypatch.setattr(observer_relay.threading, "Timer", _ManualTimer)
    monkeypatch.setattr(observer_relay, "list_observer_endpoints", lambda: [endpoint])
    monkeypatch.setattr(
        observer_relay, "unix_connect", lambda *_args, **_kwargs: upstream
    )
    downstream = _DownstreamTransport()
    hub = observer_relay.ObserverRelayHub(current_pid=9999)
    result = hub.subscribe(
        "durable-key",
        "default",
        downstream,
        runtime_generation="runtime-generation-1",
    )
    assert result is not None
    timer = _ManualTimer.created[0]
    barrier = threading.Barrier(3)
    activation_results = []
    errors = []

    def activate():
        try:
            barrier.wait()
            activation_results.append(
                hub.activate(result["subscription_id"], downstream)
            )
        except (RuntimeError, threading.BrokenBarrierError) as error:
            errors.append(error)

    def expire():
        try:
            barrier.wait()
            timer.function()
        except (RuntimeError, threading.BrokenBarrierError) as error:
            errors.append(error)

    activate_thread = threading.Thread(target=activate)
    expiry_thread = threading.Thread(target=expire)
    activate_thread.start()
    expiry_thread.start()
    barrier.wait()
    activate_thread.join(timeout=1)
    expiry_thread.join(timeout=1)

    assert errors == []
    assert activation_results in ([True], [None])
    if activation_results == [True]:
        assert hub.unsubscribe(result["subscription_id"], downstream) is True
    else:
        assert upstream.closed is True
        assert hub.close_transport(downstream) == 0


def test_close_transport_continues_after_failure_and_retains_failed_subscription(
    monkeypatch,
):
    endpoint = observer_relay.ObserverEndpoint(
        pid=4312,
        profile="default",
        runtime_generation="runtime-generation-1",
        socket_path=observer_relay._socket_dir() / "owner.sock",
        instance_id=_INSTANCE_ID,
    )
    snapshot = {
        "subscription_id": "upstream-subscription",
        "session_key": "durable-key",
        "runtime_session_id": "runtime-id",
        "snapshot_event_sequence": 0,
        "event_sequence": 0,
        "replay_events": [],
    }
    upstream_one = _FakeUpstreamWebSocket(snapshot)
    upstream_two = _FakeUpstreamWebSocket(snapshot)
    upstream_connections = iter((upstream_one, upstream_two))
    monkeypatch.setattr(observer_relay, "list_observer_endpoints", lambda: [endpoint])
    monkeypatch.setattr(
        observer_relay,
        "unix_connect",
        lambda *_args, **_kwargs: next(upstream_connections),
    )
    downstream = _DownstreamTransport()
    hub = observer_relay.ObserverRelayHub(current_pid=9999)
    assert (
        hub.subscribe(
            "durable-key",
            "default",
            downstream,
            runtime_generation="runtime-generation-1",
        )
        is not None
    )
    assert (
        hub.subscribe(
            "durable-key",
            "default",
            downstream,
            runtime_generation="runtime-generation-1",
        )
        is not None
    )
    original_close = observer_relay._RelaySubscription.close
    failed_once = False

    def flaky_close(subscription):
        nonlocal failed_once
        if subscription.websocket is upstream_one and not failed_once:
            failed_once = True
            raise RuntimeError("first close failed")
        original_close(subscription)

    monkeypatch.setattr(observer_relay._RelaySubscription, "close", flaky_close)

    with pytest.raises(RuntimeError, match="first close failed"):
        hub.close_transport(downstream)

    assert upstream_one.closed is False
    assert upstream_two.closed is True
    assert hub.close_transport(downstream) == 1
    assert upstream_one.closed is True


def test_unsubscribe_close_failure_keeps_subscription_retriable(monkeypatch):
    endpoint = observer_relay.ObserverEndpoint(
        pid=4312,
        profile="default",
        runtime_generation="runtime-generation-1",
        socket_path=observer_relay._socket_dir() / "owner.sock",
        instance_id=_INSTANCE_ID,
    )
    upstream = _FakeUpstreamWebSocket(
        {
            "subscription_id": "upstream-subscription",
            "session_key": "durable-key",
            "runtime_session_id": "runtime-id",
            "snapshot_event_sequence": 0,
            "event_sequence": 0,
            "replay_events": [],
        }
    )
    monkeypatch.setattr(observer_relay, "list_observer_endpoints", lambda: [endpoint])
    monkeypatch.setattr(
        observer_relay, "unix_connect", lambda *_args, **_kwargs: upstream
    )
    downstream = _DownstreamTransport()
    hub = observer_relay.ObserverRelayHub(current_pid=9999)
    result = hub.subscribe(
        "durable-key",
        "default",
        downstream,
        runtime_generation="runtime-generation-1",
    )
    assert result is not None
    original_close = observer_relay._RelaySubscription.close
    attempts = 0

    def flaky_close(subscription):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("close failed")
        original_close(subscription)

    monkeypatch.setattr(observer_relay._RelaySubscription, "close", flaky_close)

    with pytest.raises(RuntimeError, match="close failed"):
        hub.unsubscribe(result["subscription_id"], downstream)

    assert hub.unsubscribe(result["subscription_id"], downstream) is True
    assert upstream.closed is True


def test_activation_failure_keeps_subscription_retriable_when_close_fails(
    monkeypatch,
):
    endpoint = observer_relay.ObserverEndpoint(
        pid=4312,
        profile="default",
        runtime_generation="runtime-generation-1",
        socket_path=observer_relay._socket_dir() / "owner.sock",
        instance_id=_INSTANCE_ID,
    )
    upstream = _FakeUpstreamWebSocket(
        {
            "subscription_id": "upstream-subscription",
            "session_key": "durable-key",
            "runtime_session_id": "runtime-id",
            "snapshot_event_sequence": 0,
            "event_sequence": 0,
            "replay_events": [],
        }
    )
    monkeypatch.setattr(observer_relay, "list_observer_endpoints", lambda: [endpoint])
    monkeypatch.setattr(
        observer_relay, "unix_connect", lambda *_args, **_kwargs: upstream
    )
    monkeypatch.setattr(
        observer_relay._RelaySubscription,
        "start",
        lambda _self: (_ for _ in ()).throw(RuntimeError("cannot start")),
    )
    original_close = observer_relay._RelaySubscription.close
    attempts = 0

    def flaky_close(subscription):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("close failed")
        original_close(subscription)

    monkeypatch.setattr(observer_relay._RelaySubscription, "close", flaky_close)
    downstream = _DownstreamTransport()
    hub = observer_relay.ObserverRelayHub(current_pid=9999)
    result = hub.subscribe(
        "durable-key",
        "default",
        downstream,
        runtime_generation="runtime-generation-1",
    )
    assert result is not None

    with pytest.raises(RuntimeError, match="cannot start"):
        hub.activate(result["subscription_id"], downstream)

    assert hub.close_transport(downstream) == 1
    assert upstream.closed is True


def test_relay_ignores_socket_outside_private_registry(monkeypatch, tmp_path):
    registry = tmp_path / "registry"
    registry.mkdir()
    monkeypatch.setenv("HERMES_OBSERVER_REGISTRY_DIR", str(registry))
    monkeypatch.setenv("HERMES_OBSERVER_SOCKET_DIR", str(registry))
    endpoint = observer_relay.ObserverEndpoint(
        pid=4312,
        profile="default",
        runtime_generation="runtime-generation-1",
        socket_path=tmp_path / "outside.sock",
        instance_id=_INSTANCE_ID,
    )
    attempts = []
    monkeypatch.setattr(observer_relay, "list_observer_endpoints", lambda: [endpoint])
    monkeypatch.setattr(
        observer_relay,
        "unix_connect",
        lambda *_args, **_kwargs: attempts.append(True),
    )

    result = observer_relay.ObserverRelayHub(current_pid=9999).subscribe(
        "durable-key",
        "default",
        _DownstreamTransport(),
        runtime_generation="runtime-generation-1",
    )

    assert result is None
    assert attempts == []


def test_legacy_url_registration_is_removed(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_OBSERVER_REGISTRY_DIR", str(tmp_path))
    legacy = tmp_path / "gateway-123-legacy.json"
    legacy.write_text(
        json.dumps(
            {
                "version": 1,
                "pid": 123,
                "profile": "default",
                "ws_url": "ws://127.0.0.1:1/api/ws?token=legacy-secret",
                "instance_id": "legacy",
            }
        ),
        encoding="utf-8",
    )
    legacy.chmod(0o600)

    assert observer_relay.list_observer_endpoints() == []
    assert legacy.exists() is False


def test_private_observer_socket_rejects_non_observer_rpc(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_OBSERVER_REGISTRY_DIR", str(tmp_path))
    socket_dir = Path("/tmp").resolve(strict=True) / f"hobs-test-{os.getpid()}"
    monkeypatch.setenv("HERMES_OBSERVER_SOCKET_DIR", str(socket_dir))
    registration = _start_observer_endpoint(profile="default")
    endpoint = observer_relay.list_observer_endpoints()[0]
    websocket = None

    try:
        websocket = observer_relay.unix_connect(
            str(endpoint.socket_path),
            uri="ws://localhost/observer",
        )
        ready = json.loads(websocket.recv())
        assert ready["params"]["type"] == "gateway.ready"
        assert ready["params"]["payload"] == {
            "local_gateway_protocol": 1,
            "observer_contract": 1,
            "connection_role": "observer",
            "profile": "default",
            "runtime_generation": "runtime-generation-1",
            "instance_id": endpoint.instance_id,
        }
        websocket.send(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": "forbidden",
                    "method": "prompt.submit",
                    "params": {"prompt": "must not run"},
                }
            )
        )
        response = json.loads(websocket.recv())
        assert response["error"] == {
            "code": 4003,
            "message": "observer connection is read-only",
        }
    finally:
        if websocket is not None:
            websocket.close()
        registration.close()
