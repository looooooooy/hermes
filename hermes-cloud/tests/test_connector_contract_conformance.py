from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import Any

import pytest

from hermes_cloud.entrypoints import connector_gateway

CONTRACTS_ROOT = Path(__file__).parent / "fixtures/repository_contracts"


def _fixture(relative_path: str) -> bytes:
    return (CONTRACTS_ROOT / "fixtures" / relative_path).read_bytes()


def _valid_envelope() -> dict[str, Any]:
    return json.loads(_fixture("valid/cloud-connector-envelope.json"))


def _error_type() -> type[ValueError]:
    module = importlib.import_module("hermes_cloud.adapters.connector_contract_v1")
    return module.ContractConformanceError


def _decode_mapping(envelope: dict[str, Any]) -> Any:
    raw = json.dumps(
        envelope,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return connector_gateway.decode_connector_frame(raw)


def test_gateway_decodes_authoritative_valid_cloud_fixture() -> None:
    envelope = connector_gateway.decode_connector_frame(
        _fixture("valid/cloud-connector-envelope.json")
    )

    assert envelope.contract_version == 1
    assert envelope.message_type == "connector.hello"
    assert envelope.message_id == "22222222-2222-4222-8222-222222222222"
    assert envelope.tenant_id == "tenant-test"
    assert envelope.device_id == "device-test"
    assert envelope.sequence == 0
    assert envelope.payload == {"connector_version": "0.1.0"}


def test_gateway_rejects_authoritative_invalid_cloud_fixture() -> None:
    with pytest.raises(_error_type()) as caught:
        connector_gateway.decode_connector_frame(
            _fixture("invalid/cloud-envelope-negative-sequence.json")
        )

    assert caught.value.category == "invalid_envelope"


@pytest.mark.parametrize(
    ("message_type", "payload_fixture"),
    [
        ("session.observe.open.v2", None),
        ("session.observe.close.v2", None),
        ("session.snapshot.v2", "valid/session-snapshot-v2-payload.json"),
        ("session.event.v2", "valid/session-event-v2-tool-upsert.json"),
        ("stream.ack.v2", None),
        ("stream.nack.v2", None),
    ],
)
def test_gateway_root_envelope_accepts_authoritative_observer_v2_message_types(
    message_type: str,
    payload_fixture: str | None,
) -> None:
    envelope = _valid_envelope()
    envelope["message_type"] = message_type
    if payload_fixture is not None:
        envelope["payload"] = json.loads(_fixture(payload_fixture))

    assert _decode_mapping(envelope).message_type == message_type


def test_cloud_limits_remain_identical_to_core_contract() -> None:
    module = importlib.import_module("hermes_cloud.adapters.connector_contract_v1")

    assert module.MAX_FRAME_BYTES == 262_144
    assert module.MAX_STRING_BYTES == 131_072
    assert module.MAX_NESTING_DEPTH == 32
    assert module.MAX_OBJECT_FIELDS == 1_024
    assert module.MAX_ARRAY_ITEMS == 1_024


def test_observer_payload_digest_matches_authoritative_utf8_vector() -> None:
    module = importlib.import_module("hermes_cloud.adapters.connector_contract_v1")
    contract = json.loads(
        (CONTRACTS_ROOT / "canonical-payload-digest-v1.json").read_text(
            encoding="utf-8"
        )
    )
    vector = contract["vectors"][0]

    assert module.canonical_payload_digest(vector["payload"]) == vector["sha256"]
    with pytest.raises(ValueError):
        module.canonical_payload_digest({"value": float("nan")})


def test_frame_at_exact_byte_limit_is_accepted_and_next_byte_is_rejected() -> None:
    raw = _fixture("valid/cloud-connector-envelope.json").strip()
    module = importlib.import_module("hermes_cloud.adapters.connector_contract_v1")
    exact = raw + (b" " * (module.MAX_FRAME_BYTES - len(raw)))

    assert connector_gateway.decode_connector_frame(exact).sequence == 0

    with pytest.raises(_error_type()) as caught:
        connector_gateway.decode_connector_frame(exact + b" ")

    assert caught.value.category == "frame_too_large"


def test_multibyte_and_ascii_strings_use_utf8_byte_limit() -> None:
    module = importlib.import_module("hermes_cloud.adapters.connector_contract_v1")
    exact = _valid_envelope()
    exact["payload"] = {"value": "x" * module.MAX_STRING_BYTES}
    assert len(_decode_mapping(exact).payload["value"].encode("utf-8")) == (
        module.MAX_STRING_BYTES
    )

    too_long = _valid_envelope()
    too_long["payload"] = {"value": "你" * ((module.MAX_STRING_BYTES // 3) + 1)}
    with pytest.raises(_error_type()) as caught:
        _decode_mapping(too_long)

    assert caught.value.category == "invalid_envelope"


def _nested_object(depth: int) -> dict[str, Any]:
    value: Any = "leaf"
    for _ in range(depth):
        value = {"child": value}
    return value


def test_depth_array_and_object_boundaries_match_core() -> None:
    module = importlib.import_module("hermes_cloud.adapters.connector_contract_v1")

    exact_depth = _valid_envelope()
    exact_depth["payload"] = _nested_object(module.MAX_NESTING_DEPTH - 1)
    assert _decode_mapping(exact_depth).payload

    excessive_depth = _valid_envelope()
    excessive_depth["payload"] = _nested_object(module.MAX_NESTING_DEPTH)
    with pytest.raises(_error_type()):
        _decode_mapping(excessive_depth)

    exact_array = _valid_envelope()
    exact_array["payload"] = {"items": list(range(module.MAX_ARRAY_ITEMS))}
    assert len(_decode_mapping(exact_array).payload["items"]) == 1_024

    excessive_array = _valid_envelope()
    excessive_array["payload"] = {"items": list(range(module.MAX_ARRAY_ITEMS + 1))}
    with pytest.raises(_error_type()):
        _decode_mapping(excessive_array)

    exact_object = _valid_envelope()
    exact_object["payload"] = {
        str(index): index for index in range(module.MAX_OBJECT_FIELDS)
    }
    assert len(_decode_mapping(exact_object).payload) == 1_024

    excessive_object = _valid_envelope()
    excessive_object["payload"] = {
        str(index): index for index in range(module.MAX_OBJECT_FIELDS + 1)
    }
    with pytest.raises(_error_type()):
        _decode_mapping(excessive_object)


@pytest.mark.parametrize(
    ("raw", "category"),
    [
        (b'{"value":"\xff"}', "invalid_utf8"),
        (b'{"message_type":', "invalid_envelope"),
        (b"[]", "invalid_envelope"),
    ],
)
def test_decode_errors_are_stable_and_body_free(
    raw: bytes,
    category: str,
) -> None:
    with pytest.raises(_error_type()) as caught:
        connector_gateway.decode_connector_frame(raw)

    assert caught.value.category == category
    assert raw not in vars(caught.value).values()
    assert raw.decode("utf-8", errors="ignore") not in str(caught.value)


def test_unknown_version_message_type_and_field_fail_closed() -> None:
    invalid_cases: list[tuple[dict[str, Any], str]] = []

    version = _valid_envelope()
    version["contract_version"] = 2
    invalid_cases.append((version, "contract_unsupported"))

    message_type = _valid_envelope()
    message_type["message_type"] = "android.command"
    invalid_cases.append((message_type, "invalid_envelope"))

    field = _valid_envelope()
    field["web"] = {"renderer": "custom"}
    invalid_cases.append((field, "invalid_envelope"))

    for envelope, category in invalid_cases:
        with pytest.raises(_error_type()) as caught:
            _decode_mapping(envelope)
        assert caught.value.category == category


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("message_id", "22222222222242228222222222222222"),
        ("sent_at", "2026-07-30 00:00:00+00:00"),
    ],
)
def test_uuid_and_datetime_formats_are_not_parsed_permissively(
    field: str,
    value: str,
) -> None:
    envelope = _valid_envelope()
    envelope[field] = value

    with pytest.raises(_error_type()) as caught:
        _decode_mapping(envelope)

    assert caught.value.category == "invalid_envelope"


@pytest.mark.parametrize("platform_field", ["android", "web", "ios", "desktop"])
def test_platform_specific_top_level_fields_are_never_accepted(
    platform_field: str,
) -> None:
    envelope = _valid_envelope()
    envelope[platform_field] = {}

    with pytest.raises(_error_type()) as caught:
        _decode_mapping(envelope)

    assert caught.value.category == "invalid_envelope"


def test_only_namespaced_object_extensions_are_accepted() -> None:
    valid = _valid_envelope()
    valid["extensions"] = {"com.example.feature": {"enabled": True}}
    decoded = _decode_mapping(valid)
    assert decoded.extensions == {"com.example.feature": {"enabled": True}}

    for extensions in (
        {"android": {}},
        {"com.example.feature": "enabled"},
        {f"com.example.feature{index}": {} for index in range(17)},
    ):
        invalid = _valid_envelope()
        invalid["extensions"] = extensions
        with pytest.raises(_error_type()):
            _decode_mapping(invalid)


def test_gateway_application_exposes_same_decode_boundary_without_wss() -> None:
    app = connector_gateway.create_app()

    envelope = app.decode_connector_frame(
        _fixture("valid/cloud-connector-envelope.json")
    )

    assert envelope.message_type == "connector.hello"


def test_observer_payload_codec_accepts_authoritative_snapshot_and_event() -> None:
    module = importlib.import_module("hermes_cloud.adapters.connector_contract_v1")
    codec = module.CloudEnvelopeV1Adapter()
    snapshot = json.loads(_fixture("valid/session-snapshot-payload.json"))
    event = json.loads(_fixture("valid/session-event-payload.json"))

    decoded_snapshot = codec.decode_session_snapshot(snapshot)
    decoded_event = codec.decode_session_event(event)

    assert decoded_snapshot.profile == "default"
    assert decoded_snapshot.runtime_generation == "runtime-20260731-01"
    assert decoded_snapshot.snapshot_event_sequence == 4
    assert decoded_snapshot.event_sequence == 5
    assert len(decoded_snapshot.replay_events) == 1
    assert decoded_event.profile == "default"
    assert decoded_event.runtime_session_id == "runtime-session-1"
    assert decoded_event.event_sequence_start == 6
    assert decoded_event.event_sequence == 6


@pytest.mark.parametrize(
    ("decoder", "fixture"),
    (
        ("decode_session_snapshot", "invalid/session-snapshot-replay-gap.json"),
        (
            "decode_session_snapshot",
            "invalid/session-snapshot-status-mismatch.json",
        ),
        ("decode_session_event", "invalid/session-event-missing-profile.json"),
        ("decode_session_event", "invalid/session-event-local-type.json"),
        (
            "decode_session_event",
            "invalid/session-event-nonmergeable-range.json",
        ),
    ),
)
def test_observer_payload_codec_rejects_schema_and_semantic_drift(
    decoder: str,
    fixture: str,
) -> None:
    module = importlib.import_module("hermes_cloud.adapters.connector_contract_v1")
    codec = module.CloudEnvelopeV1Adapter()

    with pytest.raises(module.ContractConformanceError) as caught:
        getattr(codec, decoder)(json.loads(_fixture(fixture)))

    assert caught.value.category == "invalid_envelope"
