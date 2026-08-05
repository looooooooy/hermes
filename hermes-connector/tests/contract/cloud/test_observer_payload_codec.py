from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_connector.adapters.cloud.codec import (
    ConnectorProtocolCodec,
    InvalidCloudFrame,
)
from hermes_connector.domain.observer import (
    SessionEvent,
    SessionObserveClose,
    SessionObserveOpen,
    SessionSnapshot,
    StreamAck,
    StreamNack,
)

CONTRACTS = Path(__file__).parents[4] / "contracts"
VALID = CONTRACTS / "fixtures" / "valid"
SCHEMAS = CONTRACTS / "schemas" / "cloud" / "payloads"

_CREDENTIAL_LIKE_VALUES = (
    "Authorization: Basic dXNlcjpwYXNz",
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.c2ln",
    "password=hunter2",
    "secret: swordfish",
    "token=opaque-token-value",
    "api-key: abcdefghijklmnop",
    "AKIAABCDEFGHIJKLMNOP",
    "ASIAABCDEFGHIJKLMNOP",
    "AIzaSyDUMMYKEY012345678901234567890123",
    "ya29.a0AfH6SMB0123456789abcdefghijklmnopqrstuvwxyz",
    "github_pat_11AA0123456789abcdefghijklmnopqrstuvwxyz",
    "glpat-0123456789abcdefghijklmnop",
    "sk-ant-api03-0123456789abcdefghijklmnop",
    "hf_0123456789abcdefghijklmnop",
)

_SAFE_CREDENTIAL_ADJACENT_VALUES = (
    "Basic authentication is disabled.",
    "Basic YWJjZA== is not a user-password credential.",
    "release v1.2.3 is available from api.example.com.",
    "tokenizer/pathology analysis complete",
)


def _fixture(name: str) -> bytes:
    return (VALID / name).read_bytes()


def test_codec_round_trips_frozen_snapshot_and_event_payloads() -> None:
    codec = ConnectorProtocolCodec()

    snapshot = codec.decode_session_snapshot(_fixture("session-snapshot-payload.json"))
    event = codec.decode_session_event(_fixture("session-event-payload.json"))

    assert isinstance(snapshot, SessionSnapshot)
    assert snapshot.profile == "default"
    assert snapshot.replay_events[0].event_sequence == 5
    assert isinstance(event, SessionEvent)
    assert event.runtime_generation == "runtime-20260731-01"
    assert (
        codec.decode_session_snapshot(codec.encode_session_snapshot(snapshot))
        == snapshot
    )
    assert codec.decode_session_event(codec.encode_session_event(event)) == event


def test_codec_decodes_generated_observer_v2_payload_family_exactly() -> None:
    codec = ConnectorProtocolCodec()

    snapshot = codec.decode_session_snapshot_v2(
        _fixture("session-snapshot-v2-payload.json")
    )
    events = tuple(
        codec.decode_session_event_v2(_fixture(name))
        for name in (
            "session-event-v2-todo-upsert.json",
            "session-event-v2-subagent-upsert.json",
            "session-event-v2-tool-upsert.json",
            "session-event-v2-terminal-upsert.json",
        )
    )
    opened = codec.decode_session_observe_open_v2(
        _fixture("session-observe-open-v2-payload.json")
    )
    closed = codec.decode_session_observe_close_v2(
        _fixture("session-observe-close-v2-payload.json")
    )
    ack = codec.decode_stream_ack_v2(_fixture("stream-ack-v2-payload.json"))
    nack = codec.decode_stream_nack_v2(_fixture("stream-nack-v2-payload.json"))

    assert snapshot.observer_contract == 2
    assert len(snapshot.subagents) == 2
    assert [event.type for event in events] == [
        "todo.update",
        "subagent.update",
        "tool.update",
        "terminal.update",
    ]
    assert all(event.observer_contract == 2 for event in events)
    assert opened.observer_contract == closed.observer_contract == 2
    assert ack.observer_message_type == "session.event.v2"
    assert nack.expected_event_sequence == 5
    assert codec.decode_session_snapshot_v2(
        codec.encode_session_snapshot_v2(snapshot)
    ) == snapshot
    for event in events:
        assert codec.decode_session_event_v2(
            codec.encode_session_event_v2(event)
        ) == event


@pytest.mark.parametrize(
    ("v1_decoder", "v2_decoder", "v1_fixture", "v2_fixture"),
    (
        (
            "decode_session_snapshot",
            "decode_session_snapshot_v2",
            "session-snapshot-payload.json",
            "session-snapshot-v2-payload.json",
        ),
        (
            "decode_session_observe_open",
            "decode_session_observe_open_v2",
            "session-observe-open-payload.json",
            "session-observe-open-v2-payload.json",
        ),
        (
            "decode_stream_ack",
            "decode_stream_ack_v2",
            "stream-ack-payload.json",
            "stream-ack-v2-payload.json",
        ),
    ),
)
def test_v1_and_v2_decoders_never_fallback_across_contracts(
    v1_decoder: str,
    v2_decoder: str,
    v1_fixture: str,
    v2_fixture: str,
) -> None:
    codec = ConnectorProtocolCodec()

    with pytest.raises(InvalidCloudFrame):
        getattr(codec, v1_decoder)(_fixture(v2_fixture))
    with pytest.raises(InvalidCloudFrame):
        getattr(codec, v2_decoder)(_fixture(v1_fixture))


def test_envelope_accepts_only_explicit_versioned_v2_observer_message_types() -> None:
    codec = ConnectorProtocolCodec()
    envelope = json.loads(
        (VALID / "cloud-connector-envelope.json").read_text(encoding="utf-8")
    )
    for message_type, payload_name in (
        ("session.snapshot.v2", "session-snapshot-v2-payload.json"),
        ("session.event.v2", "session-event-v2-tool-upsert.json"),
        ("session.observe.open.v2", "session-observe-open-v2-payload.json"),
        ("session.observe.close.v2", "session-observe-close-v2-payload.json"),
        ("stream.ack.v2", "stream-ack-v2-payload.json"),
        ("stream.nack.v2", "stream-nack-v2-payload.json"),
    ):
        envelope["message_type"] = message_type
        envelope["payload"] = json.loads(_fixture(payload_name))
        assert codec.decode_envelope(json.dumps(envelope).encode()).message_type == (
            message_type
        )


def test_v2_extensions_reject_secret_bearing_fields_before_projection() -> None:
    codec = ConnectorProtocolCodec()
    payload = json.loads(_fixture("session-event-v2-tool-upsert.json"))
    payload["extensions"] = {
        "vendor.private": {"access_token": "sk-secret-must-not-cross"}
    }

    with pytest.raises(InvalidCloudFrame, match="generated"):
        codec.decode_session_event_v2(json.dumps(payload).encode())


@pytest.mark.parametrize(
    "sensitive_key",
    (
        "client_secret",
        "api_token",
        "api_key",
        "accessKey",
        "db_credentials",
        "tool_args",
        "tool_output",
        "private_path",
        "rawReasoning",
    ),
)
def test_v2_extensions_reject_compound_sensitive_keys(
    sensitive_key: str,
) -> None:
    codec = ConnectorProtocolCodec()
    payload = json.loads(_fixture("session-event-v2-tool-upsert.json"))
    payload["extensions"] = {
        "vendor.display": {sensitive_key: "must-not-cross"}
    }

    with pytest.raises(InvalidCloudFrame, match="generated"):
        codec.decode_session_event_v2(json.dumps(payload).encode())


def test_v2_extensions_allow_only_nonnegative_aggregate_token_counts() -> None:
    codec = ConnectorProtocolCodec()
    payload = json.loads(_fixture("session-event-v2-tool-upsert.json"))
    payload["extensions"] = {
        "vendor.metrics": {
            "token_counts": {"input": 10, "output": 2, "reasoning": 1}
        }
    }

    assert codec.decode_session_event_v2(json.dumps(payload).encode()).extensions


@pytest.mark.parametrize("credential_like_value", _CREDENTIAL_LIKE_VALUES)
@pytest.mark.parametrize(
    "fixture_name",
    (
        "session-snapshot-v2-payload.json",
        "session-event-v2-tool-upsert.json",
    ),
)
def test_v2_snapshot_and_event_reject_credential_like_extension_values_without_echo(
    fixture_name: str,
    credential_like_value: str,
) -> None:
    codec = ConnectorProtocolCodec()
    payload = json.loads(_fixture(fixture_name))
    payload["extensions"] = {
        "vendor.display": {"note": credential_like_value}
    }
    decoder = (
        codec.decode_session_snapshot_v2
        if fixture_name.startswith("session-snapshot")
        else codec.decode_session_event_v2
    )

    with pytest.raises(InvalidCloudFrame) as raised:
        decoder(json.dumps(payload).encode())

    assert credential_like_value not in str(raised.value)


@pytest.mark.parametrize("safe_value", _SAFE_CREDENTIAL_ADJACENT_VALUES)
def test_v2_event_allows_credential_adjacent_display_text(safe_value: str) -> None:
    codec = ConnectorProtocolCodec()
    payload = json.loads(_fixture("session-event-v2-tool-upsert.json"))
    payload["extensions"] = {"vendor.display": {"note": safe_value}}

    decoded = codec.decode_session_event_v2(json.dumps(payload).encode())

    assert decoded.extensions["vendor.display"]["note"] == safe_value


@pytest.mark.parametrize(
    ("fixture_name", "location"),
    (
        ("session-snapshot-v2-payload.json", "message_content"),
        ("session-snapshot-v2-payload.json", "summary"),
        ("session-event-v2-tool-upsert.json", "summary"),
        ("session-event-v2-tool-upsert.json", "text"),
    ),
)
def test_v2_snapshot_and_event_reject_basic_credentials_in_display_text(
    fixture_name: str,
    location: str,
) -> None:
    codec = ConnectorProtocolCodec()
    payload = json.loads(_fixture(fixture_name))
    credential = "Authorization: Basic dXNlcjpwYXNzd29yZA=="
    if location == "message_content":
        payload["messages"][0]["content"] = credential
    elif location == "summary":
        collection = payload.get("tools")
        target = collection[0] if isinstance(collection, list) else payload["payload"]
        target["summary"] = credential
    else:
        payload["type"] = "message.delta"
        payload["payload"] = {"text": credential}
    decoder = (
        codec.decode_session_snapshot_v2
        if fixture_name.startswith("session-snapshot")
        else codec.decode_session_event_v2
    )

    with pytest.raises(InvalidCloudFrame) as raised:
        decoder(json.dumps(payload).encode())

    assert credential not in str(raised.value)


@pytest.mark.parametrize(
    "fixture_name",
    (
        "session-snapshot-v2-payload.json",
        "session-event-v2-tool-upsert.json",
    ),
)
def test_v2_display_safety_walks_nested_extensions_within_json_bounds(
    fixture_name: str,
) -> None:
    codec = ConnectorProtocolCodec()
    payload = json.loads(_fixture(fixture_name))
    credential = "Authorization: Basic dXNlcjpwYXNzd29yZA=="
    nested: dict[str, object] = {"note": credential}
    for _ in range(8):
        nested = {"child": nested}
    payload["extensions"] = {"vendor.display": nested}
    decoder = (
        codec.decode_session_snapshot_v2
        if fixture_name.startswith("session-snapshot")
        else codec.decode_session_event_v2
    )

    with pytest.raises(InvalidCloudFrame) as raised:
        decoder(json.dumps(payload).encode())

    assert credential not in str(raised.value)


@pytest.mark.parametrize(
    "fixture_name",
    (
        "session-snapshot-v2-payload.json",
        "session-event-v2-tool-upsert.json",
    ),
)
def test_v2_display_safety_rejects_extensions_beyond_json_depth_bound(
    fixture_name: str,
) -> None:
    codec = ConnectorProtocolCodec()
    payload = json.loads(_fixture(fixture_name))
    nested: dict[str, object] = {"note": "completed safely"}
    for _ in range(40):
        nested = {"child": nested}
    payload["extensions"] = {"vendor.display": nested}
    decoder = (
        codec.decode_session_snapshot_v2
        if fixture_name.startswith("session-snapshot")
        else codec.decode_session_event_v2
    )

    with pytest.raises(InvalidCloudFrame, match="nesting exceeds contract limit"):
        decoder(json.dumps(payload).encode())


def test_envelope_allowlist_includes_frozen_observer_messages() -> None:
    codec = ConnectorProtocolCodec()
    envelope = json.loads(
        (VALID / "cloud-connector-envelope.json").read_text(encoding="utf-8")
    )

    for message_type, payload_name in (
        ("session.snapshot", "session-snapshot-payload.json"),
        ("session.event", "session-event-payload.json"),
    ):
        envelope["message_type"] = message_type
        envelope["payload"] = json.loads(
            (VALID / payload_name).read_text(encoding="utf-8")
        )
        assert (
            codec.decode_envelope(json.dumps(envelope).encode("utf-8")).message_type
            == message_type
        )


@pytest.mark.parametrize(
    "mutation",
    (
        {"unexpected": True},
        {"profile": "not allowed"},
        {"snapshot_event_sequence": 6},
        {"replay_events": []},
    ),
)
def test_snapshot_codec_rejects_unknown_profile_or_noncontiguous_replay(
    mutation: dict[str, object],
) -> None:
    codec = ConnectorProtocolCodec()
    payload = json.loads(_fixture("session-snapshot-payload.json"))
    payload.update(mutation)

    with pytest.raises(InvalidCloudFrame):
        codec.decode_session_snapshot(json.dumps(payload).encode("utf-8"))


@pytest.mark.parametrize(
    "mutation",
    (
        {"type": "unfrozen.event"},
        {"event_sequence_start": 7},
        {"type": "message.complete", "event_sequence_start": 5},
        {"payload": {"text": "missing status"}, "type": "message.complete"},
    ),
)
def test_event_codec_rejects_unfrozen_types_ranges_and_payloads(
    mutation: dict[str, object],
) -> None:
    codec = ConnectorProtocolCodec()
    payload = json.loads(_fixture("session-event-payload.json"))
    payload.update(mutation)

    with pytest.raises(InvalidCloudFrame):
        codec.decode_session_event(json.dumps(payload).encode("utf-8"))


def test_codec_consumes_frozen_observe_intent_and_business_ack_fixtures() -> None:
    codec = ConnectorProtocolCodec()

    opened = codec.decode_session_observe_open(
        _fixture("session-observe-open-payload.json")
    )
    closed = codec.decode_session_observe_close(
        _fixture("session-observe-close-payload.json")
    )
    ack = codec.decode_stream_ack(_fixture("stream-ack-payload.json"))
    nack = codec.decode_stream_nack(_fixture("stream-nack-payload.json"))

    assert isinstance(opened, SessionObserveOpen)
    assert opened.target_source == "cloud_authorized_binding"
    assert isinstance(closed, SessionObserveClose)
    assert closed.subscription_id == opened.subscription_id
    assert isinstance(ack, StreamAck)
    assert ack.observer_message_type == "session.event"
    assert isinstance(nack, StreamNack)
    assert nack.recovery == "send_snapshot"


@pytest.mark.parametrize(
    ("decoder", "fixture_name"),
    (
        (
            "decode_session_observe_open",
            "session-observe-open-missing-source.json",
        ),
        (
            "decode_session_observe_close",
            "session-observe-close-missing-subscription.json",
        ),
        ("decode_stream_ack", "stream-ack-heartbeat-cursor.json"),
        (
            "decode_stream_nack",
            "stream-nack-missing-expected-sequence.json",
        ),
    ),
)
def test_codec_rejects_invalid_observe_intent_and_business_ack_fixtures(
    decoder: str,
    fixture_name: str,
) -> None:
    codec = ConnectorProtocolCodec()
    invalid = CONTRACTS / "fixtures" / "invalid" / fixture_name

    with pytest.raises(InvalidCloudFrame):
        getattr(codec, decoder)(invalid.read_bytes())


@pytest.mark.parametrize(
    ("decoder", "fixture_name", "schema_name", "field", "maximum"),
    (
        (
            "decode_session_observe_open",
            "session-observe-open-payload.json",
            "session-observe-open-v1.schema.json",
            "session_key",
            256,
        ),
        (
            "decode_session_observe_close",
            "session-observe-close-payload.json",
            "session-observe-close-v1.schema.json",
            "session_key",
            256,
        ),
        (
            "decode_stream_ack",
            "stream-ack-payload.json",
            "stream-ack-v1.schema.json",
            "session_key",
            256,
        ),
        (
            "decode_stream_ack",
            "stream-ack-payload.json",
            "stream-ack-v1.schema.json",
            "runtime_generation",
            128,
        ),
        (
            "decode_stream_ack",
            "stream-ack-payload.json",
            "stream-ack-v1.schema.json",
            "runtime_session_id",
            256,
        ),
        (
            "decode_stream_nack",
            "stream-nack-payload.json",
            "stream-nack-v1.schema.json",
            "session_key",
            256,
        ),
        (
            "decode_stream_nack",
            "stream-nack-payload.json",
            "stream-nack-v1.schema.json",
            "runtime_generation",
            128,
        ),
        (
            "decode_stream_nack",
            "stream-nack-payload.json",
            "stream-nack-v1.schema.json",
            "runtime_session_id",
            256,
        ),
    ),
)
def test_observer_identity_limits_match_frozen_schema_at_codec_boundary(
    decoder: str,
    fixture_name: str,
    schema_name: str,
    field: str,
    maximum: int,
) -> None:
    codec = ConnectorProtocolCodec()
    decode = getattr(codec, decoder)
    schema = json.loads((SCHEMAS / schema_name).read_text(encoding="utf-8"))
    payload = json.loads(_fixture(fixture_name))

    assert schema["properties"][field]["maxLength"] == maximum
    payload[field] = "x" * maximum
    decode(json.dumps(payload).encode("utf-8"))

    payload[field] = "x" * (maximum + 1)
    with pytest.raises(InvalidCloudFrame, match=field):
        decode(json.dumps(payload).encode("utf-8"))
