"""Generated-schema authority for Observer output parity v2 frames."""

from __future__ import annotations

import base64
import binascii
import json
import re
from collections.abc import Mapping
from functools import cache
from pathlib import Path
from typing import Any


class ObserverV2ContractError(ValueError):
    """A v2 payload does not conform to the generated Cloud authority."""


_GENERATED = Path(__file__).with_name("generated")
_EVENT_SCHEMA = _GENERATED / "schemas/cloud/payloads/session-event-v2.schema.json"
_PUBLIC_EVENT_SCHEMA = _GENERATED / "schemas/public/session-event-v2.schema.json"
_PRIVATE_KEY_PARTS = frozenset(
    {
        "approval",
        "args",
        "argument",
        "arguments",
        "auth",
        "authorization",
        "cookie",
        "cookies",
        "credential",
        "credentials",
        "passphrase",
        "passwd",
        "password",
        "raw",
        "secret",
        "secrets",
    }
)
_PRIVATE_COMPACT_KEYS = frozenset(
    {
        "accesstoken",
        "apikey",
        "apitoken",
        "approvalpayload",
        "authtoken",
        "bearertoken",
        "clientcredential",
        "clientcredentials",
        "clientsecret",
        "commandargs",
        "encryptionkey",
        "fullapproval",
        "fullapprovalpayload",
        "functionargs",
        "idtoken",
        "privatekey",
        "privatereasoning",
        "rawarguments",
        "rawargs",
        "rawoutput",
        "rawreasoning",
        "rawterminaloutput",
        "rawtooloutput",
        "reasoningtrace",
        "refreshtoken",
        "signingkey",
        "toolargs",
    }
)
_CREDENTIAL_VALUE = re.compile(
    r"""
    (?:
        \bBearer[\t ]+\S+
        |-----BEGIN[\t ]+[A-Z ]*PRIVATE[\t ]+KEY-----
        |\b(?:password|passwd|secret|token|api[\s_-]?key|client[\s_-]?secret)
            \b[\t ]*(?:=|:)[\t ]*["']?[^\s"',;]+
        |\b(?:AKIA|ASIA)[A-Z0-9]{16}\b
        |\bAIza[A-Za-z0-9_-]{20,}\b
        |\b(?:sk|ghp|xox[baprs]|hf|npm)[-_][A-Za-z0-9_-]{8,}\b
        |\bglpat-[A-Za-z0-9_-]{8,}\b
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)
_BASIC_CANDIDATE = re.compile(
    r"\bBasic[\t ]+(?P<token>[A-Za-z0-9+/]{4,}={0,2})(?![A-Za-z0-9+/=])",
    re.IGNORECASE,
)
_JWT_CANDIDATE = re.compile(
    r"(?<![A-Za-z0-9_-])"
    r"(?P<header>[A-Za-z0-9_-]{2,})\."
    r"(?P<payload>[A-Za-z0-9_-]{2,})\."
    r"(?P<signature>[A-Za-z0-9_-]+)"
    r"(?![A-Za-z0-9_-])"
)


def _document(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError("generated Observer v2 contract must be an object")
    return value


@cache
def _event_schema() -> dict[str, Any]:
    return _document(_EVENT_SCHEMA)


@cache
def _cloud_contract() -> dict[str, Any]:
    return _document(_GENERATED / "cloud-realtime-v2.json")


@cache
def _registry() -> Any:
    from referencing import Registry, Resource

    registry = Registry()
    for schema in (_event_schema(), _document(_PUBLIC_EVENT_SCHEMA)):
        identifier = schema.get("$id")
        if not isinstance(identifier, str):
            raise TypeError("generated Observer event schema has no identifier")
        registry = registry.with_resource(identifier, Resource.from_contents(schema))
    return registry


@cache
def _cloud_validator(name: str) -> Any:
    from jsonschema import Draft202012Validator, FormatChecker

    schemas = _cloud_contract().get("schemas")
    if not isinstance(schemas, dict) or name not in schemas:
        raise RuntimeError("generated Cloud realtime schema is unavailable")
    return Draft202012Validator(
        schemas[name],
        registry=_registry(),
        format_checker=FormatChecker(),
    )


@cache
def _payload_validator(name: str) -> Any:
    from jsonschema import Draft202012Validator, FormatChecker

    path = _GENERATED / f"schemas/cloud/payloads/{name}.schema.json"
    return Draft202012Validator(
        _document(path),
        registry=_registry(),
        format_checker=FormatChecker(),
    )


def require_cloud_frame(name: str, value: object) -> dict[str, Any]:
    errors = tuple(_cloud_validator(name).iter_errors(value))
    if errors or not isinstance(value, dict):
        raise ObserverV2ContractError("Observer v2 Cloud frame is invalid")
    require_display_safe(value)
    return value


def require_payload(name: str, value: object) -> dict[str, Any]:
    errors = tuple(_payload_validator(name).iter_errors(value))
    if errors or not isinstance(value, dict):
        raise ObserverV2ContractError("Observer v2 payload is invalid")
    require_display_safe(value)
    return value


def require_display_safe(value: object) -> None:
    """Reject private fields or credential material at any nesting depth."""

    pending: list[tuple[object, frozenset[int], int]] = [(value, frozenset(), 1)]
    while pending:
        current, ancestors, depth = pending.pop()
        if depth > 32:
            raise ObserverV2ContractError("Observer v2 payload is not display-safe")
        if isinstance(current, Mapping):
            identity = id(current)
            if identity in ancestors:
                raise ObserverV2ContractError("Observer v2 payload is not display-safe")
            child_ancestors = ancestors | {identity}
            for key, item in current.items():
                if _private_key(str(key)):
                    raise ObserverV2ContractError(
                        "Observer v2 payload is not display-safe"
                    )
                pending.append((item, child_ancestors, depth + 1))
        elif isinstance(current, (list, tuple)):
            identity = id(current)
            if identity in ancestors:
                raise ObserverV2ContractError("Observer v2 payload is not display-safe")
            child_ancestors = ancestors | {identity}
            pending.extend((item, child_ancestors, depth + 1) for item in current)
        elif isinstance(current, str) and _contains_credential(current):
            raise ObserverV2ContractError("Observer v2 payload is not display-safe")


def _contains_credential(value: str) -> bool:
    return (
        _CREDENTIAL_VALUE.search(value) is not None
        or _contains_basic_credential(value)
        or _contains_structural_jwt(value)
    )


def _contains_basic_credential(value: str) -> bool:
    for candidate in _BASIC_CANDIDATE.finditer(value):
        decoded = _decode_base64(candidate.group("token"), urlsafe=False)
        if decoded is not None and b":" in decoded:
            return True
    return False


def _contains_structural_jwt(value: str) -> bool:
    for candidate in _JWT_CANDIDATE.finditer(value):
        header = _decode_json_segment(candidate.group("header"))
        payload = _decode_json_segment(candidate.group("payload"))
        signature = _decode_base64(candidate.group("signature"), urlsafe=True)
        if (
            isinstance(header, Mapping)
            and isinstance(payload, Mapping)
            and isinstance(header.get("alg"), str)
            and bool(header["alg"])
            and signature
        ):
            return True
    return False


def _decode_json_segment(segment: str) -> object | None:
    decoded = _decode_base64(segment, urlsafe=True)
    if decoded is None:
        return None
    try:
        return json.loads(decoded.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def _decode_base64(segment: str, *, urlsafe: bool) -> bytes | None:
    if len(segment) % 4 == 1:
        return None
    padded = segment + "=" * (-len(segment) % 4)
    try:
        return base64.b64decode(
            padded,
            altchars=b"-_" if urlsafe else None,
            validate=True,
        )
    except (binascii.Error, ValueError):
        return None


def _private_key(key: str) -> bool:
    snake_case = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", key).lower()
    parts = tuple(part for part in re.split(r"[^a-z0-9]+", snake_case) if part)
    compact = "".join(parts)
    if compact in _PRIVATE_COMPACT_KEYS or _PRIVATE_KEY_PARTS.intersection(parts):
        return True
    if "token" in parts and compact != "tokencounts":
        return True
    return compact in {
        "apisecret",
        "clientkey",
        "secretkey",
    }
