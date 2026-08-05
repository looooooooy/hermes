from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any
from uuid import UUID

from hermes_connector.domain.contract_messages import (
    CloudEnvelope,
    LocalGatewayErrorResponse,
    LocalHello,
    LocalWelcome,
)

MAX_FRAME_BYTES = 262_144
MAX_STRING_BYTES = 131_072
MAX_DEPTH = 32
MAX_ARRAY_ITEMS = 1_024
MAX_OBJECT_FIELDS = 1_024
MAX_CAPABILITIES = 64
MAX_EXTENSIONS = 16

_PROFILE_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")
_CANONICAL_UUID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_EXTENSION_PATTERN = re.compile(r"^[a-z][a-z0-9]*(\.[a-z0-9][a-z0-9-]*)+$")
_TRACEPARENT_PATTERN = re.compile(r"^00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}$")
_UTC_DATETIME_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$")

_CLOUD_MESSAGE_TYPES = frozenset(
    {
        "connector.hello",
        "connector.welcome",
        "connector.heartbeat",
        "command.deliver",
        "command.receipt",
        "command.result",
        "file.transfer",
        "a2a.message",
        "view.card.invalidate",
    }
)

_LOCAL_HELLO_REQUIRED = frozenset(
    {
        "contract_version",
        "message_type",
        "client_instance_id",
        "profile",
        "required_capabilities",
        "optional_capabilities",
    }
)
_LOCAL_WELCOME_REQUIRED = frozenset(
    {
        "contract_version",
        "message_type",
        "runtime_generation",
        "profile",
        "accepted_capabilities",
        "unavailable_optional_capabilities",
    }
)
_CLOUD_REQUIRED = frozenset(
    {
        "contract_version",
        "message_id",
        "message_type",
        "tenant_id",
        "device_id",
        "sequence",
        "sent_at",
        "payload",
    }
)
_LOCAL_ERROR_REASONS = MappingProxyType(
    {
        4300: "contract_unsupported",
        4301: "invalid_envelope",
        4302: "frame_too_large",
        4303: "invalid_utf8",
        4304: "capability_not_available",
        4305: "overloaded",
        4306: "deadline_exceeded_before_effect",
        4307: "effect_unknown",
        4308: "idempotency_conflict",
        4309: "authorization_denied",
    }
)


class ContractCodecError(ValueError):
    code: int
    error_name: str

    def __init__(self, message: str) -> None:
        super().__init__(message)


class ContractUnsupported(ContractCodecError):
    code = 4300
    error_name = "contract_unsupported"


class InvalidEnvelope(ContractCodecError):
    code = 4301
    error_name = "invalid_envelope"


class FrameTooLarge(ContractCodecError):
    code = 4302
    error_name = "frame_too_large"

    def __init__(self) -> None:
        super().__init__("encoded JSON frame exceeds 262144 bytes")


class InvalidUtf8(ContractCodecError):
    code = 4303
    error_name = "invalid_utf8"

    def __init__(self) -> None:
        super().__init__("frame is not strict UTF-8")


class UnsupportedMessageType(ContractUnsupported):
    def __init__(self) -> None:
        super().__init__("cloud message type is not supported by contract v1")


def decode_local_hello(frame: bytes) -> LocalHello:
    value = _decode_frame(frame)
    _exact_top_level(value, _LOCAL_HELLO_REQUIRED)
    _require_contract_version(value["contract_version"])
    if value["message_type"] != "local.hello":
        raise InvalidEnvelope("message_type must be local.hello")

    required = _capability_array(
        value["required_capabilities"], "required_capabilities"
    )
    optional = _capability_array(
        value["optional_capabilities"], "optional_capabilities"
    )
    if set(required).intersection(optional):
        raise InvalidEnvelope("required and optional capabilities overlap")

    return LocalHello(
        contract_version=1,
        message_type="local.hello",
        client_instance_id=_uuid(value["client_instance_id"], "client_instance_id"),
        profile=_profile(value["profile"]),
        required_capabilities=required,
        optional_capabilities=optional,
        extensions=_optional_extensions(value),
    )


def decode_local_welcome(frame: bytes) -> LocalWelcome:
    return _decode_local_welcome_value(_decode_frame(frame))


def decode_local_gateway_response(
    frame: bytes,
) -> LocalWelcome | LocalGatewayErrorResponse:
    value = _decode_frame(frame)
    if "error" in value:
        return _decode_local_gateway_error_value(value)
    return _decode_local_welcome_value(value)


def _decode_local_welcome_value(value: Mapping[str, object]) -> LocalWelcome:
    _exact_top_level(value, _LOCAL_WELCOME_REQUIRED)
    _require_contract_version(value["contract_version"])
    if value["message_type"] != "local.welcome":
        raise InvalidEnvelope("message_type must be local.welcome")

    return LocalWelcome(
        contract_version=1,
        message_type="local.welcome",
        runtime_generation=_bounded_string(
            value["runtime_generation"],
            "runtime_generation",
            maximum=128,
        ),
        profile=_profile(value["profile"]),
        accepted_capabilities=_capability_array(
            value["accepted_capabilities"],
            "accepted_capabilities",
        ),
        unavailable_optional_capabilities=_capability_array(
            value["unavailable_optional_capabilities"],
            "unavailable_optional_capabilities",
        ),
        extensions=_optional_extensions(value),
    )


def _decode_local_gateway_error_value(
    value: Mapping[str, object],
) -> LocalGatewayErrorResponse:
    _exact_top_level(
        value,
        frozenset({"error"}),
        optional=frozenset(),
    )
    error = value["error"]
    if not isinstance(error, dict) or frozenset(error) != frozenset({"code", "reason"}):
        raise InvalidEnvelope("local gateway error body is invalid")
    code = error["code"]
    reason = error["reason"]
    if type(code) is not int or not isinstance(reason, str):
        raise InvalidEnvelope("local gateway error code and reason are invalid")
    if _LOCAL_ERROR_REASONS.get(code) != reason:
        raise InvalidEnvelope("local gateway error does not match catalog")
    return LocalGatewayErrorResponse(code=code, reason=reason)


def decode_cloud_envelope(frame: bytes) -> CloudEnvelope:
    value = _decode_frame(frame)
    _exact_top_level(
        value,
        _CLOUD_REQUIRED,
        optional=frozenset({"traceparent", "idempotency_key", "extensions"}),
    )
    _require_contract_version(value["contract_version"])

    message_type = value["message_type"]
    if not isinstance(message_type, str) or message_type not in _CLOUD_MESSAGE_TYPES:
        raise UnsupportedMessageType()

    sequence = value["sequence"]
    if type(sequence) is not int or not 0 <= sequence <= 2**53 - 1:
        raise InvalidEnvelope("sequence must be a JavaScript-safe non-negative integer")

    payload = value["payload"]
    if not isinstance(payload, dict):
        raise InvalidEnvelope("payload must be an object")

    traceparent = None
    if "traceparent" in value:
        traceparent = _bounded_string(
            value["traceparent"],
            "traceparent",
            maximum=128,
            minimum=0,
        )
        if _TRACEPARENT_PATTERN.fullmatch(traceparent) is None:
            raise InvalidEnvelope("traceparent does not match contract v1")

    idempotency_key = None
    if "idempotency_key" in value:
        idempotency_key = _bounded_string(
            value["idempotency_key"],
            "idempotency_key",
            maximum=128,
        )

    return CloudEnvelope(
        contract_version=1,
        message_id=_uuid(value["message_id"], "message_id"),
        message_type=message_type,
        tenant_id=_bounded_string(value["tenant_id"], "tenant_id", maximum=128),
        device_id=_bounded_string(value["device_id"], "device_id", maximum=128),
        sequence=sequence,
        sent_at=_utc_datetime(value["sent_at"]),
        traceparent=traceparent,
        idempotency_key=idempotency_key,
        payload=_freeze_mapping(payload),
        extensions=_optional_extensions(value),
    )


def encode_local_hello(message: LocalHello) -> bytes:
    value: dict[str, object] = {
        "contract_version": message.contract_version,
        "message_type": message.message_type,
        "client_instance_id": str(message.client_instance_id),
        "profile": message.profile,
        "required_capabilities": list(message.required_capabilities),
        "optional_capabilities": list(message.optional_capabilities),
    }
    _include_extensions(value, message.extensions)
    encoded = _encode_frame(value)
    decode_local_hello(encoded)
    return encoded


def encode_local_welcome(message: LocalWelcome) -> bytes:
    value: dict[str, object] = {
        "contract_version": message.contract_version,
        "message_type": message.message_type,
        "runtime_generation": message.runtime_generation,
        "profile": message.profile,
        "accepted_capabilities": list(message.accepted_capabilities),
        "unavailable_optional_capabilities": list(
            message.unavailable_optional_capabilities
        ),
    }
    _include_extensions(value, message.extensions)
    encoded = _encode_frame(value)
    decode_local_welcome(encoded)
    return encoded


def encode_cloud_envelope(message: CloudEnvelope) -> bytes:
    value: dict[str, object] = {
        "contract_version": message.contract_version,
        "message_id": str(message.message_id),
        "message_type": message.message_type,
        "tenant_id": message.tenant_id,
        "device_id": message.device_id,
        "sequence": message.sequence,
        "sent_at": _format_utc(message.sent_at),
        "payload": _thaw(message.payload),
    }
    if message.traceparent is not None:
        value["traceparent"] = message.traceparent
    if message.idempotency_key is not None:
        value["idempotency_key"] = message.idempotency_key
    _include_extensions(value, message.extensions)
    encoded = _encode_frame(value)
    decode_cloud_envelope(encoded)
    return encoded


def _decode_frame(frame: bytes) -> dict[str, Any]:
    if not isinstance(frame, bytes):
        raise InvalidEnvelope("frame must be bytes")
    if len(frame) > MAX_FRAME_BYTES:
        raise FrameTooLarge()
    try:
        text = frame.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise InvalidUtf8() from None
    try:
        value = json.loads(
            text,
            object_pairs_hook=_object_without_duplicates,
            parse_constant=_reject_non_json_number,
        )
    except ContractCodecError:
        raise
    except (json.JSONDecodeError, ValueError, TypeError, RecursionError):
        raise InvalidEnvelope("frame must contain strict JSON") from None
    _validate_shared_limits(value)
    if not isinstance(value, dict):
        raise InvalidEnvelope("top-level JSON value must be an object")
    return value


def _encode_frame(value: object) -> bytes:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    if len(encoded) > MAX_FRAME_BYTES:
        raise FrameTooLarge()
    return encoded


def _object_without_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise InvalidEnvelope("duplicate object field is not allowed")
        value[key] = item
    return value


def _reject_non_json_number(value: str) -> None:
    raise InvalidEnvelope(f"non-JSON number is not allowed: {value}")


def _validate_shared_limits(value: object, *, depth: int = 1) -> None:
    if depth > MAX_DEPTH:
        raise InvalidEnvelope("JSON nesting depth exceeds limit")
    if isinstance(value, str):
        if len(value.encode("utf-8")) > MAX_STRING_BYTES:
            raise InvalidEnvelope("JSON string exceeds UTF-8 string limit")
        return
    if isinstance(value, list):
        if len(value) > MAX_ARRAY_ITEMS:
            raise InvalidEnvelope("JSON array exceeds item limit")
        for item in value:
            _validate_shared_limits(item, depth=depth + 1)
        return
    if isinstance(value, dict):
        if len(value) > MAX_OBJECT_FIELDS:
            raise InvalidEnvelope("JSON object exceeds field limit")
        for key, item in value.items():
            _validate_shared_limits(key, depth=depth + 1)
            _validate_shared_limits(item, depth=depth + 1)


def _exact_top_level(
    value: Mapping[str, object],
    required: frozenset[str],
    *,
    optional: frozenset[str] = frozenset({"extensions"}),
) -> None:
    fields = set(value)
    missing = required - fields
    if missing:
        raise InvalidEnvelope("required top-level field is missing")
    unexpected = fields - required - optional
    if unexpected:
        raise InvalidEnvelope("unexpected top-level field is not allowed")


def _require_contract_version(value: object) -> None:
    if type(value) is not int or value != 1:
        raise ContractUnsupported("contract_version must be 1")


def _uuid(value: object, field_name: str) -> UUID:
    if not isinstance(value, str) or _CANONICAL_UUID_PATTERN.fullmatch(value) is None:
        raise InvalidEnvelope(f"{field_name} must be a canonical UUID string")
    try:
        return UUID(value)
    except (ValueError, AttributeError):
        raise InvalidEnvelope(f"{field_name} must be a canonical UUID string") from None


def _bounded_string(
    value: object,
    field_name: str,
    *,
    maximum: int,
    minimum: int = 1,
) -> str:
    if not isinstance(value, str):
        raise InvalidEnvelope(f"{field_name} must be a string")
    if len(value) < minimum or len(value) > maximum:
        raise InvalidEnvelope(f"{field_name} length is outside contract limits")
    return value


def _profile(value: object) -> str:
    profile = _bounded_string(value, "profile", maximum=128)
    if _PROFILE_PATTERN.fullmatch(profile) is None:
        raise InvalidEnvelope("profile does not match contract v1")
    return profile


def _capability_array(value: object, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise InvalidEnvelope(f"{field_name} must be an array")
    if len(value) > MAX_CAPABILITIES:
        raise InvalidEnvelope(f"{field_name} exceeds capability limit")
    capabilities = tuple(
        _bounded_string(item, field_name, maximum=128) for item in value
    )
    if len(capabilities) != len(set(capabilities)):
        raise InvalidEnvelope(f"{field_name} must contain unique capabilities")
    return capabilities


def _optional_extensions(value: Mapping[str, object]) -> Mapping[str, object]:
    if "extensions" not in value:
        return MappingProxyType({})
    return _extensions(value["extensions"])


def _extensions(value: object) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise InvalidEnvelope("extensions must be an object")
    if len(value) > MAX_EXTENSIONS:
        raise InvalidEnvelope("extensions exceed namespace limit")
    for namespace, extension_value in value.items():
        if _EXTENSION_PATTERN.fullmatch(namespace) is None:
            raise InvalidEnvelope("extension name must use a reverse-domain namespace")
        if not isinstance(extension_value, dict):
            raise InvalidEnvelope("extension value must be an object")
    return _freeze_mapping(value)


def _utc_datetime(value: object) -> datetime:
    if not isinstance(value, str) or _UTC_DATETIME_PATTERN.fullmatch(value) is None:
        raise InvalidEnvelope("sent_at must be an RFC 3339 UTC date-time")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        raise InvalidEnvelope("sent_at must be an RFC 3339 UTC date-time") from None
    if parsed.utcoffset() != UTC.utcoffset(parsed):
        raise InvalidEnvelope("sent_at must use UTC")
    return parsed.astimezone(UTC)


def _format_utc(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise InvalidEnvelope("sent_at must use UTC")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _freeze_mapping(value: Mapping[str, object]) -> Mapping[str, object]:
    return MappingProxyType({key: _freeze(item) for key, item in value.items()})


def _freeze(value: object) -> object:
    if isinstance(value, dict):
        return _freeze_mapping(value)
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _include_extensions(
    value: dict[str, object],
    extensions: Mapping[str, object],
) -> None:
    if extensions:
        value["extensions"] = _thaw(extensions)
