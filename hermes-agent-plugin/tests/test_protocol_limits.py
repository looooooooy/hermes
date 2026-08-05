from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests.test_support.runtime_descriptor_v2 import runtime_authority_v2


def test_frame_codec_round_trips_utf8_object() -> None:
    from hermes_agent_plugin.adapters.local_protocol.frame_codec import (
        decode_frame,
        encode_frame,
    )

    frame = {"jsonrpc": "2.0", "params": {"message": "你好"}}

    encoded = encode_frame(frame)

    assert decode_frame(encoded.encode("utf-8")) == frame


def test_frame_codec_exposes_frozen_protocol_limits() -> None:
    from hermes_agent_plugin.adapters.local_protocol import (
        frame_codec as protocol_codec,
    )

    assert protocol_codec.MAX_FRAME_BYTES == 262_144
    assert protocol_codec.MAX_STRING_BYTES == 131_072
    assert protocol_codec.MAX_NESTING_DEPTH == 32


@pytest.mark.parametrize(
    ("raw", "category"),
    [
        (b'{"value":"\xff"}', "invalid_utf8"),
        (b'{"value":"\\u0000"}', "nul_not_allowed"),
        (b'{"value":"\\ud800"}', "lone_surrogate"),
        (b'{"value":', "invalid_json"),
        (b"[]", "top_level_not_object"),
    ],
)
def test_decode_frame_returns_stable_error_categories(
    raw: bytes,
    category: str,
) -> None:
    from hermes_agent_plugin.adapters.local_protocol.frame_codec import (
        FrameCodecError,
        decode_frame,
    )

    with pytest.raises(FrameCodecError) as exc_info:
        decode_frame(raw)

    assert exc_info.value.category == category


def test_decode_frame_rejects_oversized_frame_before_json_parsing() -> None:
    from hermes_agent_plugin.adapters.local_protocol.frame_codec import (
        MAX_FRAME_BYTES,
        FrameCodecError,
        decode_frame,
    )

    with pytest.raises(FrameCodecError) as exc_info:
        decode_frame(b"{" + (b"x" * MAX_FRAME_BYTES))

    assert exc_info.value.category == "frame_too_large"


def test_frame_codec_rejects_multibyte_string_over_byte_limit() -> None:
    from hermes_agent_plugin.adapters.local_protocol.frame_codec import (
        MAX_STRING_BYTES,
        FrameCodecError,
        encode_frame,
    )

    too_long = "你" * ((MAX_STRING_BYTES // 3) + 1)

    with pytest.raises(FrameCodecError) as exc_info:
        encode_frame({"value": too_long})

    assert exc_info.value.category == "string_too_long"


def test_decode_frame_accepts_string_at_exact_byte_limit() -> None:
    from hermes_agent_plugin.adapters.local_protocol.frame_codec import (
        MAX_STRING_BYTES,
        decode_frame,
    )

    raw = json.dumps(
        {"value": "x" * MAX_STRING_BYTES},
        separators=(",", ":"),
    ).encode("utf-8")

    assert len(decode_frame(raw)["value"].encode("utf-8")) == MAX_STRING_BYTES


def _nested_object(depth: int) -> dict:
    value: object = "leaf"
    for _ in range(depth):
        value = {"child": value}
    assert isinstance(value, dict)
    return value


def test_decode_frame_accepts_maximum_nesting_depth() -> None:
    from hermes_agent_plugin.adapters.local_protocol.frame_codec import (
        MAX_NESTING_DEPTH,
        decode_frame,
    )

    raw = json.dumps(_nested_object(MAX_NESTING_DEPTH), separators=(",", ":"))

    assert decode_frame(raw) == _nested_object(MAX_NESTING_DEPTH)


def test_decode_frame_rejects_excessive_nesting() -> None:
    from hermes_agent_plugin.adapters.local_protocol.frame_codec import (
        MAX_NESTING_DEPTH,
        FrameCodecError,
        decode_frame,
    )

    raw = json.dumps(_nested_object(MAX_NESTING_DEPTH + 1), separators=(",", ":"))

    with pytest.raises(FrameCodecError) as exc_info:
        decode_frame(raw)

    assert exc_info.value.category == "nesting_too_deep"


def test_decode_frame_rejects_object_with_too_many_fields() -> None:
    from hermes_agent_plugin.adapters.local_protocol.frame_codec import (
        MAX_OBJECT_FIELDS,
        FrameCodecError,
        decode_frame,
    )

    raw = json.dumps(
        {str(index): index for index in range(MAX_OBJECT_FIELDS + 1)},
        separators=(",", ":"),
    )

    with pytest.raises(FrameCodecError) as exc_info:
        decode_frame(raw)

    assert exc_info.value.category == "too_many_fields"


def test_decode_frame_rejects_array_with_too_many_items() -> None:
    from hermes_agent_plugin.adapters.local_protocol.frame_codec import (
        MAX_ARRAY_ITEMS,
        FrameCodecError,
        decode_frame,
    )

    raw = json.dumps(
        {"items": list(range(MAX_ARRAY_ITEMS + 1))},
        separators=(",", ":"),
    )

    with pytest.raises(FrameCodecError) as exc_info:
        decode_frame(raw)

    assert exc_info.value.category == "too_many_array_items"


@pytest.mark.parametrize(
    ("frame", "category"),
    [
        ({"value": "\x00"}, "nul_not_allowed"),
        ({"value": "\ud800"}, "lone_surrogate"),
    ],
)
def test_encode_frame_rejects_unsafe_unicode(frame: dict, category: str) -> None:
    from hermes_agent_plugin.adapters.local_protocol.frame_codec import (
        FrameCodecError,
        encode_frame,
    )

    with pytest.raises(FrameCodecError) as exc_info:
        encode_frame(frame)

    assert exc_info.value.category == category


def test_parse_error_does_not_retain_or_render_raw_body() -> None:
    from hermes_agent_plugin.adapters.local_protocol.frame_codec import (
        FrameCodecError,
        decode_frame,
    )

    raw = b'{"token":"extremely-sensitive",'

    with pytest.raises(FrameCodecError) as exc_info:
        decode_frame(raw)

    error = exc_info.value
    assert error.category == "invalid_json"
    assert "extremely-sensitive" not in str(error)
    assert "extremely-sensitive" not in repr(error)
    assert raw not in vars(error).values()


@pytest.mark.parametrize(
    "raw",
    [
        b'{"id":"first","id":"second"}',
        b'{"params":{"secret":"must-not-leak","secret":"second"}}',
        b'{"items":[{"value":1,"value":2}]}',
    ],
)
def test_decode_frame_rejects_duplicate_object_keys_at_any_depth(
    raw: bytes,
) -> None:
    from hermes_agent_plugin.adapters.local_protocol.frame_codec import (
        FrameCodecError,
        decode_frame,
    )

    with pytest.raises(FrameCodecError) as exc_info:
        decode_frame(raw)

    error = exc_info.value
    assert error.category == "invalid_json"
    assert "must-not-leak" not in str(error)
    assert "must-not-leak" not in repr(error)


def test_relays_share_the_strict_frame_codec() -> None:
    from hermes_agent_plugin.adapters.local_protocol import (
        frame_codec as protocol_codec,
    )
    from hermes_agent_plugin.adapters.platform.macos import (
        control_relay,
        observer_relay,
    )

    assert observer_relay._decode_frame is protocol_codec.try_decode_frame
    assert control_relay._decode_frame is protocol_codec.try_decode_frame
    assert observer_relay.encode_frame is protocol_codec.encode_frame
    assert control_relay.encode_frame is protocol_codec.encode_frame


class _RecordingWebSocket:
    def __init__(self, incoming=()) -> None:
        self.incoming = list(incoming)
        self.sent: list[str] = []
        self.closed = False

    def __iter__(self):
        return iter(self.incoming)

    def send(self, value: str) -> None:
        self.sent.append(value)

    def recv(self, timeout=None):
        if not self.incoming:
            raise RuntimeError("closed")
        return self.incoming.pop(0)

    def close(self) -> None:
        self.closed = True


@pytest.mark.parametrize("relay_name", ["observer", "control"])
def test_relay_transport_rejects_unsafe_outbound_frame(relay_name: str) -> None:
    from hermes_agent_plugin.adapters.platform.macos import (
        control_relay,
        observer_relay,
    )

    websocket = _RecordingWebSocket()
    if relay_name == "observer":
        transport = observer_relay._ObserverSocketTransport(websocket)
    else:
        transport = control_relay._ControlSocketTransport(websocket, {})

    assert transport.write({"value": "\x00"}) is False
    assert websocket.sent == []


@pytest.mark.parametrize("relay_name", ["observer", "control"])
def test_relay_parse_error_response_never_echoes_raw_body(relay_name: str) -> None:
    from hermes_agent_plugin.adapters.platform.macos import (
        control_relay,
        observer_relay,
    )

    websocket = _RecordingWebSocket([b'{"secret":"must-not-escape",'])
    if relay_name == "observer":
        observer_relay._handle_observer_connection(
            websocket,
            dispatch=lambda request, transport: None,
            remove_observer_subscriptions=lambda transport: None,
            profile="default",
            runtime_generation="runtime-generation-1",
            instance_id="11111111-1111-4111-8111-111111111111",
        )
    else:
        owner_action_dispatcher = control_relay._BoundedExecutor(
            max_workers=1,
            max_queued=0,
            thread_name_prefix="parse-error-owner-test",
        )
        try:
            control_relay._handle_control_connection(
                websocket,
                dispatcher=lambda request, transport: None,
                owner_action_dispatcher=owner_action_dispatcher,
            )
        finally:
            owner_action_dispatcher.shutdown(
                wait=True,
                cancel_futures=True,
            )

    sent = "".join(websocket.sent)
    assert "parse error" in sent
    assert "must-not-escape" not in sent


class _FakeRelayServer:
    def serve_forever(self) -> None:
        return None

    def shutdown(self) -> None:
        return None


@pytest.mark.parametrize("relay_name", ["observer", "control"])
def test_relay_unix_server_enforces_protocol_frame_limit(
    relay_name: str,
    tmp_path: Path,
    monkeypatch,
) -> None:
    from hermes_agent_plugin.adapters.local_protocol.frame_codec import MAX_FRAME_BYTES
    from hermes_agent_plugin.adapters.platform.macos import (
        control_relay,
        observer_relay,
    )

    module = observer_relay if relay_name == "observer" else control_relay
    registry = tmp_path / relay_name / "registry"
    socket_dir = Path("/tmp").resolve(strict=True) / (
        f"hap-{os.getpid()}-{relay_name}-{tmp_path.stat().st_ino}"
    )
    monkeypatch.setenv(f"HERMES_{relay_name.upper()}_REGISTRY_DIR", str(registry))
    monkeypatch.setenv(f"HERMES_{relay_name.upper()}_SOCKET_DIR", str(socket_dir))
    captured: dict = {}

    def fake_unix_serve(*args, **kwargs):
        captured.update(kwargs)
        Path(kwargs["path"]).touch()
        return _FakeRelayServer()

    monkeypatch.setattr(module, "unix_serve", fake_unix_serve)
    monkeypatch.setattr(
        module,
        "is_private_socket",
        lambda path, *, directory: True,
    )
    if relay_name == "observer":
        registration = observer_relay.start_observer_endpoint(
            authority=runtime_authority_v2(),
            dispatch=lambda request, transport: None,
            remove_observer_subscriptions=lambda transport: None,
        )
    else:
        registration = control_relay.start_control_endpoint(
            authority=runtime_authority_v2(),
            dispatcher=lambda request, transport: None,
        )
    try:
        assert captured["max_size"] == MAX_FRAME_BYTES
    finally:
        registration.close()


def test_observer_unix_client_enforces_protocol_frame_limit(monkeypatch) -> None:
    from hermes_agent_plugin.adapters.local_protocol.frame_codec import MAX_FRAME_BYTES
    from hermes_agent_plugin.adapters.platform.macos import observer_relay

    endpoint = observer_relay.ObserverEndpoint(
        pid=1234,
        profile="default",
        runtime_generation="runtime-generation-1",
        socket_path=observer_relay._socket_dir() / "owner.sock",
        instance_id="11111111-1111-4111-8111-111111111111",
    )
    captured: dict = {}
    websocket = _RecordingWebSocket()

    def fake_unix_connect(*args, **kwargs):
        captured.update(kwargs)
        return websocket

    class FakeSubscription:
        def __init__(self, **kwargs) -> None:
            self.local_id = kwargs["local_id"]
            self.transport = kwargs["transport"]

        def start(self) -> None:
            return None

        def arm_activation_expiry(self) -> None:
            return None

    monkeypatch.setattr(observer_relay, "list_observer_endpoints", lambda: [endpoint])
    monkeypatch.setattr(observer_relay, "unix_connect", fake_unix_connect)
    monkeypatch.setattr(
        observer_relay,
        "_subscribe_upstream",
        lambda *args, **kwargs: (
            {
                "profile": "default",
                "runtime_generation": "runtime-generation-1",
                "session_key": "session-1",
                "runtime_session_id": "runtime-1",
                "snapshot_event_sequence": 0,
                "event_sequence": 0,
                "replay_events": [],
            },
            [],
        ),
    )
    monkeypatch.setattr(observer_relay, "_RelaySubscription", FakeSubscription)

    result = observer_relay.ObserverRelayHub(current_pid=9999).subscribe(
        session_key="session-1",
        profile="default",
        transport=object(),
        runtime_generation="runtime-generation-1",
    )

    assert result is not None
    assert captured["max_size"] == MAX_FRAME_BYTES
    assert captured["max_queue"] == 32


def test_control_unix_client_enforces_protocol_frame_limit(monkeypatch) -> None:
    from hermes_agent_plugin.adapters.local_protocol.frame_codec import MAX_FRAME_BYTES
    from hermes_agent_plugin.adapters.platform.macos import control_relay

    endpoint = control_relay.ControlEndpoint(
        pid=1234,
        profile="default",
        socket_path=control_relay._socket_dir() / "owner.sock",
        instance_id="11111111-1111-4111-8111-111111111111",
    )
    captured: dict = {}
    sentinel = object()

    def fake_unix_connect(*args, **kwargs):
        captured.update(kwargs)
        return _RecordingWebSocket()

    monkeypatch.setattr(control_relay, "unix_connect", fake_unix_connect)
    monkeypatch.setattr(control_relay, "_RelayConnection", lambda **kwargs: sentinel)

    connection = control_relay.ControlRelayHub(current_pid=9999)._connect(
        transport=object(),
        claims={},
        profile="default",
        endpoints=(endpoint,),
        excluded_instance_ids=set(),
    )

    assert connection is sentinel
    assert captured["max_size"] == MAX_FRAME_BYTES
