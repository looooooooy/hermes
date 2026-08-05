"""Core Local Gateway v1 contract adapter for the Hermes host boundary."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .frame_codec import FrameCodecError, decode_frame, encode_frame
from .profile import validate_profile

LOCAL_ERROR_CODES = {
    "contract_unsupported": 4300,
    "invalid_envelope": 4301,
    "frame_too_large": 4302,
    "invalid_utf8": 4303,
    "capability_not_available": 4304,
}
PLUGIN_LOCAL_CAPABILITIES = frozenset(
    {
        "session.observe",
        "session.control",
    }
)

_HELLO_REQUIRED_FIELDS = frozenset(
    {
        "contract_version",
        "message_type",
        "client_instance_id",
        "profile",
        "required_capabilities",
        "optional_capabilities",
    }
)
_HELLO_ALLOWED_FIELDS = _HELLO_REQUIRED_FIELDS | {"extensions"}
_WELCOME_REQUIRED_FIELDS = frozenset(
    {
        "contract_version",
        "message_type",
        "runtime_generation",
        "profile",
        "accepted_capabilities",
        "unavailable_optional_capabilities",
    }
)
_WELCOME_ALLOWED_FIELDS = _WELCOME_REQUIRED_FIELDS | {"extensions"}
_CANONICAL_UUID_PATTERN = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}"
)
_EXTENSION_NAMESPACE_PATTERN = re.compile(r"[a-z][a-z0-9]*(?:\.[a-z0-9][a-z0-9-]*)+")


class LocalContractError(ValueError):
    """Stable Local Gateway rejection that never retains the input body."""

    def __init__(self, code: int, reason: str) -> None:
        self.code = code
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class LocalHello:
    contract_version: int
    message_type: str
    client_instance_id: str
    profile: str
    required_capabilities: tuple[str, ...]
    optional_capabilities: tuple[str, ...]
    extensions: dict[str, dict[str, Any]]


@dataclass(frozen=True)
class LocalWelcome:
    contract_version: int
    message_type: str
    runtime_generation: str
    profile: str
    accepted_capabilities: tuple[str, ...]
    unavailable_optional_capabilities: tuple[str, ...]
    extensions: dict[str, dict[str, Any]]


def _fail(reason: str) -> None:
    raise LocalContractError(LOCAL_ERROR_CODES[reason], reason)


def _capabilities(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > 64:
        _fail("invalid_envelope")
    if any(not isinstance(item, str) or not 1 <= len(item) <= 128 for item in value):
        _fail("invalid_envelope")
    if len(value) != len(set(value)):
        _fail("invalid_envelope")
    return tuple(sorted(value))


def _extensions(value: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(value, dict) or len(value) > 16:
        _fail("invalid_envelope")
    if any(
        not isinstance(key, str)
        or _EXTENSION_NAMESPACE_PATTERN.fullmatch(key) is None
        or not isinstance(item, dict)
        for key, item in value.items()
    ):
        _fail("invalid_envelope")
    return dict(value)


def _decode_contract_frame(raw: Any) -> dict:
    try:
        return decode_frame(raw)
    except FrameCodecError as error:
        if error.category == "frame_too_large":
            _fail("frame_too_large")
        if error.category == "invalid_utf8":
            _fail("invalid_utf8")
        _fail("invalid_envelope")


def _encode_contract_frame(frame: dict[str, Any]) -> str:
    try:
        return encode_frame(frame)
    except FrameCodecError as error:
        if error.category == "frame_too_large":
            _fail("frame_too_large")
        _fail("invalid_envelope")


def _runtime_generation(value: Any) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 128:
        _fail("invalid_envelope")
    return value


def _validate_contract_version(value: Any) -> None:
    if type(value) is not int:
        _fail("invalid_envelope")
    if value != 1:
        _fail("contract_unsupported")


def decode_local_hello(raw: Any) -> LocalHello:
    frame = _decode_contract_frame(raw)
    if set(frame) - _HELLO_ALLOWED_FIELDS or _HELLO_REQUIRED_FIELDS - set(frame):
        _fail("invalid_envelope")
    _validate_contract_version(frame["contract_version"])
    if frame["message_type"] != "local.hello":
        _fail("invalid_envelope")
    try:
        profile = validate_profile(frame["profile"])
    except ValueError:
        _fail("invalid_envelope")
    required = _capabilities(frame["required_capabilities"])
    optional = _capabilities(frame["optional_capabilities"])
    if set(required) & set(optional):
        _fail("invalid_envelope")
    client_instance_id = frame["client_instance_id"]
    if (
        not isinstance(client_instance_id, str)
        or _CANONICAL_UUID_PATTERN.fullmatch(client_instance_id) is None
    ):
        _fail("invalid_envelope")
    extensions = _extensions(frame.get("extensions", {}))
    return LocalHello(
        contract_version=1,
        message_type="local.hello",
        client_instance_id=client_instance_id,
        profile=profile,
        required_capabilities=required,
        optional_capabilities=optional,
        extensions=dict(extensions),
    )


def decode_local_welcome(raw: Any) -> LocalWelcome:
    frame = _decode_contract_frame(raw)
    if set(frame) - _WELCOME_ALLOWED_FIELDS or _WELCOME_REQUIRED_FIELDS - set(frame):
        _fail("invalid_envelope")
    _validate_contract_version(frame["contract_version"])
    if frame["message_type"] != "local.welcome":
        _fail("invalid_envelope")
    try:
        profile = validate_profile(frame["profile"])
    except ValueError:
        _fail("invalid_envelope")
    accepted = _capabilities(frame["accepted_capabilities"])
    unavailable = _capabilities(frame["unavailable_optional_capabilities"])
    if set(accepted) & set(unavailable):
        _fail("invalid_envelope")
    return LocalWelcome(
        contract_version=1,
        message_type="local.welcome",
        runtime_generation=_runtime_generation(frame["runtime_generation"]),
        profile=profile,
        accepted_capabilities=accepted,
        unavailable_optional_capabilities=unavailable,
        extensions=_extensions(frame.get("extensions", {})),
    )


def negotiate_local_welcome(
    hello: LocalHello,
    *,
    available_capabilities: frozenset[str],
    runtime_generation: str,
) -> LocalWelcome:
    available = frozenset(_capabilities(list(available_capabilities)))
    if set(hello.required_capabilities) - available:
        _fail("capability_not_available")
    requested = set(hello.required_capabilities) | set(hello.optional_capabilities)
    return LocalWelcome(
        contract_version=1,
        message_type="local.welcome",
        runtime_generation=_runtime_generation(runtime_generation),
        profile=hello.profile,
        accepted_capabilities=tuple(sorted(requested & available)),
        unavailable_optional_capabilities=tuple(
            sorted(set(hello.optional_capabilities) - available)
        ),
        extensions={},
    )


def _canonical_json(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _canonical_json(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_canonical_json(item) for item in value]
    return value


def encode_local_hello(hello: LocalHello) -> str:
    frame: dict[str, Any] = {
        "contract_version": hello.contract_version,
        "message_type": hello.message_type,
        "client_instance_id": hello.client_instance_id,
        "profile": hello.profile,
        "required_capabilities": sorted(hello.required_capabilities),
        "optional_capabilities": sorted(hello.optional_capabilities),
    }
    if hello.extensions:
        frame["extensions"] = _canonical_json(hello.extensions)
    encoded = _encode_contract_frame(frame)
    decode_local_hello(encoded)
    return encoded


def encode_local_welcome(welcome: LocalWelcome) -> str:
    frame: dict[str, Any] = {
        "contract_version": welcome.contract_version,
        "message_type": welcome.message_type,
        "runtime_generation": welcome.runtime_generation,
        "profile": welcome.profile,
        "accepted_capabilities": sorted(welcome.accepted_capabilities),
        "unavailable_optional_capabilities": sorted(
            welcome.unavailable_optional_capabilities
        ),
    }
    if welcome.extensions:
        frame["extensions"] = _canonical_json(welcome.extensions)
    encoded = _encode_contract_frame(frame)
    decode_local_welcome(encoded)
    return encoded


class LocalContractV1Adapter:
    """Host-facing LocalHello consumer and LocalWelcome producer."""

    def __init__(
        self,
        *,
        runtime_generation: str,
        available_capabilities: frozenset[str] = PLUGIN_LOCAL_CAPABILITIES,
    ) -> None:
        self._runtime_generation = _runtime_generation(runtime_generation)
        self._available_capabilities = frozenset(
            _capabilities(list(available_capabilities))
        )

    def handle_hello(self, raw: Any) -> str:
        hello = decode_local_hello(raw)
        welcome = negotiate_local_welcome(
            hello,
            available_capabilities=self._available_capabilities,
            runtime_generation=self._runtime_generation,
        )
        return encode_local_welcome(welcome)
