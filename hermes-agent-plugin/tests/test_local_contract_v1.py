from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

_CONTRACTS_ROOT = Path(__file__).resolve().parents[2] / "contracts"
_MANIFEST = json.loads(
    (_CONTRACTS_ROOT / "fixtures/manifest.json").read_text(encoding="utf-8")
)
_HELLO_SCHEMA = "schemas/local/gateway-handshake-v1.schema.json"
_WELCOME_SCHEMA = "schemas/local/gateway-welcome-v1.schema.json"


def _fixture_path(relative_path: str) -> Path:
    return _CONTRACTS_ROOT / relative_path


def _manifest_fixtures(kind: str, schema: str) -> list[Path]:
    return [
        _fixture_path(entry["fixture"])
        for entry in _MANIFEST[kind]
        if entry["schema"] == schema
    ]


def _valid_hello_payload() -> dict:
    fixture = _manifest_fixtures("valid", _HELLO_SCHEMA)[0]
    return json.loads(fixture.read_text(encoding="utf-8"))


def test_local_contract_uses_frozen_core_error_codes() -> None:
    from hermes_agent_plugin.adapters.local_protocol.handshake_v1 import (
        LOCAL_ERROR_CODES,
    )

    assert LOCAL_ERROR_CODES == {
        "contract_unsupported": 4300,
        "invalid_envelope": 4301,
        "frame_too_large": 4302,
        "invalid_utf8": 4303,
        "capability_not_available": 4304,
    }


@pytest.mark.parametrize(
    "fixture_path",
    _manifest_fixtures("valid", _HELLO_SCHEMA),
    ids=lambda path: path.name,
)
def test_decode_local_hello_accepts_root_manifest_fixture(
    fixture_path: Path,
) -> None:
    from hermes_agent_plugin.adapters.local_protocol.handshake_v1 import (
        decode_local_hello,
    )

    hello = decode_local_hello(fixture_path.read_bytes())

    assert hello.contract_version == 1
    assert hello.message_type == "local.hello"
    assert hello.client_instance_id == "11111111-1111-4111-8111-111111111111"
    assert hello.profile == "default"
    assert hello.required_capabilities == ("session.observe",)
    assert hello.optional_capabilities == ("session.control",)
    assert hello.extensions == {}


@pytest.mark.parametrize(
    "fixture_path",
    _manifest_fixtures("invalid", _HELLO_SCHEMA),
    ids=lambda path: path.name,
)
def test_decode_local_hello_rejects_root_manifest_invalid_fixtures(
    fixture_path: Path,
) -> None:
    from hermes_agent_plugin.adapters.local_protocol.handshake_v1 import (
        LocalContractError,
        decode_local_hello,
    )

    with pytest.raises(LocalContractError) as exc_info:
        decode_local_hello(fixture_path.read_bytes())

    assert exc_info.value.code == 4301
    assert exc_info.value.reason == "invalid_envelope"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("contract_version", True),
        (
            "client_instance_id",
            "11111111-1111-9111-8111-111111111111",
        ),
        (
            "client_instance_id",
            "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA",
        ),
        ("profile", "bad/profile"),
        ("required_capabilities", "session.observe"),
        ("required_capabilities", ["session.observe", "session.observe"]),
        ("optional_capabilities", ["x"] * 65),
        ("optional_capabilities", [""]),
        ("optional_capabilities", ["x" * 129]),
        ("extensions", {"not-namespaced": {}}),
        ("extensions", {"vendor.feature": []}),
        (
            "extensions",
            {f"vendor.feature-{index}": {} for index in range(17)},
        ),
    ],
)
def test_decode_local_hello_enforces_core_schema_semantics(
    field: str,
    value: object,
) -> None:
    from hermes_agent_plugin.adapters.local_protocol.handshake_v1 import (
        LocalContractError,
        decode_local_hello,
    )

    payload = _valid_hello_payload()
    payload[field] = value

    with pytest.raises(LocalContractError) as exc_info:
        decode_local_hello(json.dumps(payload))

    assert exc_info.value.code == 4301
    assert exc_info.value.reason == "invalid_envelope"


def test_decode_local_hello_requires_exact_top_level_fields() -> None:
    from hermes_agent_plugin.adapters.local_protocol.handshake_v1 import (
        LocalContractError,
        decode_local_hello,
    )

    payload = _valid_hello_payload()
    payload.pop("profile")

    with pytest.raises(LocalContractError) as exc_info:
        decode_local_hello(json.dumps(payload))

    assert exc_info.value.code == 4301
    assert exc_info.value.reason == "invalid_envelope"


@pytest.mark.parametrize(
    ("raw", "code", "reason"),
    [
        (b'{"profile":"\xff"}', 4303, "invalid_utf8"),
        (b"{" + (b"x" * 262_144), 4302, "frame_too_large"),
        (
            b'{"contract_version":1,"contract_version":1}',
            4301,
            "invalid_envelope",
        ),
    ],
    ids=["invalid-utf8", "frame-too-large", "duplicate-key"],
)
def test_decode_local_hello_maps_codec_failures_to_core_errors(
    raw: bytes,
    code: int,
    reason: str,
) -> None:
    from hermes_agent_plugin.adapters.local_protocol.handshake_v1 import (
        LocalContractError,
        decode_local_hello,
    )

    with pytest.raises(LocalContractError) as exc_info:
        decode_local_hello(raw)

    assert exc_info.value.code == code
    assert exc_info.value.reason == reason


def test_decode_local_hello_maps_unsupported_version_to_4300() -> None:
    from hermes_agent_plugin.adapters.local_protocol.handshake_v1 import (
        LocalContractError,
        decode_local_hello,
    )

    payload = _valid_hello_payload()
    payload["contract_version"] = 2

    with pytest.raises(LocalContractError) as exc_info:
        decode_local_hello(json.dumps(payload))

    assert exc_info.value.code == 4300
    assert exc_info.value.reason == "contract_unsupported"


def test_local_contract_error_never_contains_input_body() -> None:
    from hermes_agent_plugin.adapters.local_protocol.handshake_v1 import (
        LocalContractError,
        decode_local_hello,
    )

    raw = b'{"secret":"must-not-leak",'

    with pytest.raises(LocalContractError) as exc_info:
        decode_local_hello(raw)

    error = exc_info.value
    assert "must-not-leak" not in str(error)
    assert "must-not-leak" not in repr(error)
    assert raw not in vars(error).values()


@pytest.mark.parametrize(
    "fixture_path",
    _manifest_fixtures("valid", _WELCOME_SCHEMA),
    ids=lambda path: path.name,
)
def test_decode_local_welcome_accepts_root_manifest_fixture(
    fixture_path: Path,
) -> None:
    from hermes_agent_plugin.adapters.local_protocol.handshake_v1 import (
        decode_local_welcome,
    )

    welcome = decode_local_welcome(fixture_path.read_bytes())

    assert welcome.contract_version == 1
    assert welcome.message_type == "local.welcome"
    assert welcome.runtime_generation == "runtime-20260730-01"
    assert welcome.profile == "default"
    assert welcome.accepted_capabilities == ("session.observe",)
    assert welcome.unavailable_optional_capabilities == ("session.control",)
    assert welcome.extensions == {}


def test_negotiate_local_welcome_fails_closed_when_required_is_missing() -> None:
    from hermes_agent_plugin.adapters.local_protocol.handshake_v1 import (
        LocalContractError,
        decode_local_hello,
        negotiate_local_welcome,
    )

    hello = decode_local_hello(
        _fixture_path("fixtures/valid/local-gateway-handshake.json").read_bytes()
    )

    with pytest.raises(LocalContractError) as exc_info:
        negotiate_local_welcome(
            hello,
            available_capabilities=frozenset(),
            runtime_generation="runtime-20260730-01",
        )

    assert exc_info.value.code == 4304
    assert exc_info.value.reason == "capability_not_available"


def test_negotiate_local_welcome_marks_missing_optional_unavailable() -> None:
    from hermes_agent_plugin.adapters.local_protocol.handshake_v1 import (
        decode_local_hello,
        encode_local_welcome,
        negotiate_local_welcome,
    )

    hello = decode_local_hello(
        _fixture_path("fixtures/valid/local-gateway-handshake.json").read_bytes()
    )
    welcome = negotiate_local_welcome(
        hello,
        available_capabilities=frozenset({"session.observe"}),
        runtime_generation="runtime-20260730-01",
    )

    encoded = json.loads(encode_local_welcome(welcome))
    expected = json.loads(
        _fixture_path("fixtures/valid/local-gateway-welcome.json").read_text(
            encoding="utf-8"
        )
    )
    assert encoded == expected


def test_host_adapter_negotiates_only_plugin_actual_capabilities() -> None:
    from hermes_agent_plugin.adapters.local_protocol.handshake_v1 import (
        PLUGIN_LOCAL_CAPABILITIES,
        LocalContractV1Adapter,
        decode_local_welcome,
    )

    assert PLUGIN_LOCAL_CAPABILITIES == frozenset(
        {"session.observe", "session.control"}
    )
    adapter = LocalContractV1Adapter(
        runtime_generation="runtime-1",
        available_capabilities=PLUGIN_LOCAL_CAPABILITIES,
    )

    encoded = adapter.handle_hello(
        _fixture_path("fixtures/valid/local-gateway-handshake.json").read_bytes()
    )
    welcome = decode_local_welcome(encoded)

    assert welcome.accepted_capabilities == (
        "session.control",
        "session.observe",
    )
    assert welcome.unavailable_optional_capabilities == ()
    assert set(json.loads(encoded)) == {
        "contract_version",
        "message_type",
        "runtime_generation",
        "profile",
        "accepted_capabilities",
        "unavailable_optional_capabilities",
    }


def _deep_extension_payload() -> dict:
    payload = _valid_hello_payload()
    nested: dict = {}
    value = nested
    for _ in range(33):
        child: dict = {}
        value["child"] = child
        value = child
    payload["extensions"] = {"vendor.deep": nested}
    return payload


def _wide_extension_payload() -> dict:
    payload = _valid_hello_payload()
    payload["extensions"] = {
        "vendor.wide": {str(index): index for index in range(1_025)}
    }
    return payload


def _long_string_payload() -> dict:
    payload = _valid_hello_payload()
    payload["optional_capabilities"] = ["x" * 131_073]
    return payload


def _long_array_payload() -> dict:
    payload = _valid_hello_payload()
    payload["optional_capabilities"] = [f"capability-{index}" for index in range(1_025)]
    return payload


@pytest.mark.parametrize(
    "payload_factory",
    [
        _deep_extension_payload,
        _wide_extension_payload,
        _long_string_payload,
        _long_array_payload,
    ],
    ids=["depth", "object-fields", "string-bytes", "array-items"],
)
def test_decode_local_hello_maps_all_json_limits_to_invalid_envelope(
    payload_factory,
) -> None:
    from hermes_agent_plugin.adapters.local_protocol.handshake_v1 import (
        LocalContractError,
        decode_local_hello,
    )

    raw = json.dumps(payload_factory(), separators=(",", ":")).encode()

    with pytest.raises(LocalContractError) as exc_info:
        decode_local_hello(raw)

    assert exc_info.value.code == 4301
    assert exc_info.value.reason == "invalid_envelope"


def test_local_hello_encoder_is_canonical_and_round_trips() -> None:
    from hermes_agent_plugin.adapters.local_protocol.handshake_v1 import (
        decode_local_hello,
        encode_local_hello,
    )

    payload = _valid_hello_payload()
    payload["required_capabilities"] = [
        "session.observe",
        "session.control",
    ]
    payload["optional_capabilities"] = ["view.card", "a2a.message"]
    payload["extensions"] = {
        "vendor.zeta": {"z": 1, "a": [{"y": 2, "b": 3}]},
        "vendor.alpha": {},
    }
    hello = decode_local_hello(json.dumps(payload))
    alternate = {
        "optional_capabilities": list(reversed(payload["optional_capabilities"])),
        "required_capabilities": list(reversed(payload["required_capabilities"])),
        "profile": payload["profile"],
        "client_instance_id": payload["client_instance_id"],
        "message_type": payload["message_type"],
        "contract_version": payload["contract_version"],
        "extensions": {
            "vendor.alpha": {},
            "vendor.zeta": {"a": [{"b": 3, "y": 2}], "z": 1},
        },
    }

    encoded = encode_local_hello(hello)

    assert encoded == encode_local_hello(decode_local_hello(json.dumps(alternate)))
    assert decode_local_hello(encoded) == hello


def test_local_welcome_encoder_round_trips_root_fixture() -> None:
    from hermes_agent_plugin.adapters.local_protocol.handshake_v1 import (
        decode_local_welcome,
        encode_local_welcome,
    )

    fixture = _fixture_path("fixtures/valid/local-gateway-welcome.json")
    welcome = decode_local_welcome(fixture.read_bytes())

    assert decode_local_welcome(encode_local_welcome(welcome)) == welcome


def test_local_hello_encoder_rejects_an_invalid_producer_model() -> None:
    from hermes_agent_plugin.adapters.local_protocol.handshake_v1 import (
        LocalContractError,
        decode_local_hello,
        encode_local_hello,
    )

    hello = decode_local_hello(
        _fixture_path("fixtures/valid/local-gateway-handshake.json").read_bytes()
    )

    with pytest.raises(LocalContractError) as exc_info:
        encode_local_hello(replace(hello, contract_version=2))

    assert exc_info.value.code == 4300
    assert exc_info.value.reason == "contract_unsupported"


def test_local_welcome_encoder_maps_invalid_model_to_a_core_error() -> None:
    from hermes_agent_plugin.adapters.local_protocol.handshake_v1 import (
        LocalContractError,
        decode_local_welcome,
        encode_local_welcome,
    )

    welcome = decode_local_welcome(
        _fixture_path("fixtures/valid/local-gateway-welcome.json").read_bytes()
    )

    with pytest.raises(LocalContractError) as exc_info:
        encode_local_welcome(replace(welcome, runtime_generation="bad\x00generation"))

    assert exc_info.value.code == 4301
    assert exc_info.value.reason == "invalid_envelope"


def test_shared_decoder_rejects_root_manifest_duplicate_key_fixture() -> None:
    from hermes_agent_plugin.adapters.local_protocol.frame_codec import (
        FrameCodecError,
        decode_frame,
    )

    fixture = next(
        _fixture_path(entry["fixture"])
        for entry in _MANIFEST["invalid"]
        if entry["fixture"].endswith("cloud-envelope-duplicate-key.json")
    )

    with pytest.raises(FrameCodecError) as exc_info:
        decode_frame(fixture.read_bytes())

    assert exc_info.value.category == "invalid_json"
