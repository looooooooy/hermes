"""Generated observer output-parity v2 schema authority."""

from __future__ import annotations

import base64
import binascii
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from importlib import resources
from typing import Any
from uuid import UUID

OUTPUT_PARITY_CAPABILITY = "session.observe.output-parity.v1"
_PACKAGE = "hermes_connector.contracts.generated"
_SCHEMA_DIRECTORY = "schemas/cloud/payloads"
_SCHEMA_FILES = {
    "session.event.v2": "session-event-v2.schema.json",
    "session.snapshot.v2": "session-snapshot-v2.schema.json",
    "session.observe.open.v2": "session-observe-open-v2.schema.json",
    "session.observe.close.v2": "session-observe-close-v2.schema.json",
    "stream.ack.v2": "stream-ack-v2.schema.json",
    "stream.nack.v2": "stream-nack-v2.schema.json",
}
_FORBIDDEN_DISPLAY_KEYS = frozenset(
    {
        "access_token",
        "approval_payload",
        "authorization",
        "cookie",
        "credential",
        "credentials",
        "full_approval",
        "password",
        "private_reasoning",
        "raw_args",
        "raw_arguments",
        "raw_output",
        "reasoning_trace",
        "refresh_token",
        "secret",
        "secrets",
        "token",
    }
)
_SENSITIVE_EXTENSION_TOKENS = frozenset(
    {
        "approval",
        "arg",
        "args",
        "argument",
        "arguments",
        "argv",
        "auth",
        "authorization",
        "bearer",
        "cookie",
        "credential",
        "credentials",
        "env",
        "environment",
        "output",
        "passphrase",
        "password",
        "path",
        "private",
        "raw",
        "reasoning",
        "secret",
        "secrets",
        "stderr",
        "stdin",
        "stdout",
        "token",
        "tokens",
    }
)
_TOKEN_COUNT_FIELDS = frozenset({"input", "output", "reasoning"})
_CREDENTIAL_KEY_QUALIFIERS = frozenset(
    {"access", "api", "client", "private", "secret", "signing"}
)
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_NON_KEY_TOKEN = re.compile(r"[^A-Za-z0-9]+")
_BASIC_CANDIDATE = re.compile(
    r"(?P<prefix>\bBasic\s+)(?P<token>[A-Za-z0-9+/]+={0,2})(?![A-Za-z0-9+/=])",
    re.IGNORECASE,
)
_JWT_CANDIDATE = re.compile(
    r"(?<![A-Za-z0-9_-])"
    r"(?P<header>[A-Za-z0-9_-]+)\."
    r"(?P<payload>[A-Za-z0-9_-]+)\."
    r"(?P<signature>[A-Za-z0-9_-]+)"
    r"(?![A-Za-z0-9_-])"
)
_STATIC_CREDENTIAL_PATTERN = re.compile(
    r"""
    (?:
        \bBearer\s+[A-Za-z0-9._~+/=-]{4,}
        |\b(?:password|passwd|passphrase|secret|token|api[-_]?key|access[-_]?token|refresh[-_]?token|client[-_]?secret)\b\s*[:=]\s*["']?[^\s"',;}]{4,}
        |\b(?:AKIA|ASIA)[A-Z0-9]{16}\b
        |\bAIza[0-9A-Za-z_-]{20,}
        |\bya29\.[0-9A-Za-z_-]{16,}
        |(?<![A-Za-z0-9_-])(?:sk|gh[pousr]|github_pat|xox[baprs]|glpat|hf)[-_][A-Za-z0-9_-]{8,}(?![A-Za-z0-9_-])
        |-----BEGIN [A-Z ]*PRIVATE KEY-----
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)


class ObserverV2ContractError(ValueError):
    """A generated v2 resource or payload failed closed."""


@dataclass(frozen=True)
class ObserverV2Contracts:
    policy: Mapping[str, Any]
    schemas: Mapping[str, Mapping[str, Any]]

    @property
    def capability(self) -> str:
        return OUTPUT_PARITY_CAPABILITY

    def validate(self, message_type: str, payload: object) -> None:
        schema = self.schemas.get(message_type)
        if schema is None:
            raise ObserverV2ContractError("observer v2 message type is unavailable")
        external = {
            _SCHEMA_FILES[name]: value for name, value in self.schemas.items()
        }
        _SchemaValidator(schema, external=external).validate(payload)
        _validate_display_safe(payload)


def _validate_display_safe(value: object, *, in_extensions: bool = False) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = _normalized_key(str(key))
            if normalized in _FORBIDDEN_DISPLAY_KEYS:
                raise ObserverV2ContractError(
                    "observer v2 payload contains a private field"
                )
            child_in_extensions = in_extensions or normalized == "extensions"
            if child_in_extensions and normalized != "extensions":
                if normalized == "token_counts":
                    if not _is_aggregate_token_counts(item):
                        raise ObserverV2ContractError(
                            "observer v2 extension token counts are invalid"
                        )
                    continue
                tokens = frozenset(normalized.split("_"))
                sensitive_credential_key = (
                    "key" in tokens
                    and bool(tokens.intersection(_CREDENTIAL_KEY_QUALIFIERS))
                )
                if (
                    tokens.intersection(_SENSITIVE_EXTENSION_TOKENS)
                    or sensitive_credential_key
                ):
                    raise ObserverV2ContractError(
                        "observer v2 extension contains a sensitive field"
                    )
            _validate_display_safe(item, in_extensions=child_in_extensions)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _validate_display_safe(item, in_extensions=in_extensions)
    elif isinstance(value, str) and _contains_credential_material(value):
        raise ObserverV2ContractError("observer v2 payload contains credential material")


def _contains_credential_material(value: str) -> bool:
    return (
        any(_is_basic_credential(match) for match in _BASIC_CANDIDATE.finditer(value))
        or any(_is_structured_jwt(match) for match in _JWT_CANDIDATE.finditer(value))
        or _STATIC_CREDENTIAL_PATTERN.search(value) is not None
    )


def _is_basic_credential(match: re.Match[str]) -> bool:
    decoded = _decoded_base64(match.group("token"), urlsafe=False)
    if decoded is None:
        return False
    username, separator, _password = decoded.partition(b":")
    return bool(username and separator)


def _is_structured_jwt(match: re.Match[str]) -> bool:
    header_bytes = _decoded_base64(match.group("header"), urlsafe=True)
    payload_bytes = _decoded_base64(match.group("payload"), urlsafe=True)
    signature = _decoded_base64(match.group("signature"), urlsafe=True)
    if header_bytes is None or payload_bytes is None or not signature:
        return False
    try:
        header = json.loads(header_bytes)
        payload = json.loads(payload_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    return (
        isinstance(header, dict)
        and isinstance(header.get("alg"), str)
        and bool(header["alg"])
        and isinstance(payload, dict)
    )


def _decoded_base64(value: str, *, urlsafe: bool) -> bytes | None:
    padded = value + "=" * (-len(value) % 4)
    try:
        return base64.b64decode(
            padded,
            altchars=b"-_" if urlsafe else None,
            validate=True,
        )
    except (binascii.Error, ValueError):
        return None


def _normalized_key(value: str) -> str:
    words = _CAMEL_BOUNDARY.sub("_", value)
    return _NON_KEY_TOKEN.sub("_", words).strip("_").lower()


def _is_aggregate_token_counts(value: object) -> bool:
    return (
        isinstance(value, Mapping)
        and bool(value)
        and set(value).issubset(_TOKEN_COUNT_FIELDS)
        and all(type(item) is int and item >= 0 for item in value.values())
    )


def _read_json(path: str) -> dict[str, Any]:
    try:
        value = json.loads(
            resources.files(_PACKAGE).joinpath(path).read_text(encoding="utf-8")
        )
    except (FileNotFoundError, ModuleNotFoundError, OSError, json.JSONDecodeError) as error:
        raise ObserverV2ContractError(
            "observer v2 generated resource is unavailable"
        ) from error
    if not isinstance(value, dict):
        raise ObserverV2ContractError("observer v2 generated resource is invalid")
    return value


@lru_cache(maxsize=1)
def load_observer_v2_contracts() -> ObserverV2Contracts:
    policy = _read_json("observer-output-parity-v2.json")
    schemas = {
        message_type: _read_json(f"{_SCHEMA_DIRECTORY}/{filename}")
        for message_type, filename in _SCHEMA_FILES.items()
    }
    expected_ids = {
        message_type: f"https://contracts.hermes.local/cloud/payloads/{filename}"
        for message_type, filename in _SCHEMA_FILES.items()
    }
    limits = policy.get("limits")
    required_limits = {
        "max_todo_sections",
        "max_subagents",
        "max_tools",
        "max_terminals",
    }
    if (
        policy.get("contract") != "observer-output-parity"
        or policy.get("version") != 2
        or policy.get("capability") != OUTPUT_PARITY_CAPABILITY
        or tuple(policy.get("snapshot", {}).get("collections", ()))
        != ("todo_sections", "subagents", "tools", "terminals")
        or not isinstance(limits, Mapping)
        or any(
            type(limits.get(name)) is not int or limits[name] < 1
            for name in required_limits
        )
        or any(
            schemas[name].get("$id") != expected_id
            or schemas[name].get("properties", {}).get("observer_contract")
            != {"const": 2}
            for name, expected_id in expected_ids.items()
        )
        or schemas["session.snapshot.v2"]
        .get("properties", {})
        .get("replay_events", {})
        .get("items")
        != {"$ref": "session-event-v2.schema.json"}
    ):
        raise ObserverV2ContractError("observer v2 generated resources drifted")
    return ObserverV2Contracts(policy=policy, schemas=schemas)


def _type_matches(value: object, expected: str) -> bool:
    return {
        "null": value is None,
        "object": isinstance(value, Mapping),
        "array": isinstance(value, (list, tuple)),
        "string": isinstance(value, str),
        "boolean": isinstance(value, bool),
        "integer": isinstance(value, int) and not isinstance(value, bool),
    }.get(expected, False)


class _SchemaValidator:
    def __init__(
        self,
        root: Mapping[str, Any],
        *,
        external: Mapping[str, Mapping[str, Any]],
    ) -> None:
        self._root = root
        self._external = external

    def validate(self, value: object) -> None:
        self._validate(value, self._root, self._root)

    def _validate(
        self,
        value: object,
        schema: Mapping[str, Any],
        root: Mapping[str, Any],
    ) -> None:
        reference = schema.get("$ref")
        if isinstance(reference, str):
            if reference.startswith("#/$defs/"):
                target = root.get("$defs", {}).get(reference.removeprefix("#/$defs/"))
            else:
                target = self._external.get(reference)
            if not isinstance(target, Mapping):
                raise ObserverV2ContractError("observer v2 schema reference drifted")
            self._validate(value, target, target if not reference.startswith("#") else root)
            return

        expected = schema.get("type")
        if expected is not None:
            expected_types = (expected,) if isinstance(expected, str) else tuple(expected)
            if not any(_type_matches(value, item) for item in expected_types):
                self._invalid()
        if "const" in schema and value != schema["const"]:
            self._invalid()
        if "enum" in schema and value not in schema["enum"]:
            self._invalid()
        if isinstance(value, str):
            if len(value) < schema.get("minLength", 0) or len(value) > schema.get(
                "maxLength", len(value)
            ):
                self._invalid()
            pattern = schema.get("pattern")
            if isinstance(pattern, str) and re.search(pattern, value) is None:
                self._invalid()
            self._format(value, schema.get("format"))
        if (
            isinstance(value, int)
            and not isinstance(value, bool)
            and (
                value < schema.get("minimum", value)
                or value > schema.get("maximum", value)
            )
        ):
            self._invalid()
        if isinstance(value, (list, tuple)):
            if len(value) < schema.get("minItems", 0) or len(value) > schema.get(
                "maxItems", len(value)
            ):
                self._invalid()
            item_schema = schema.get("items")
            if isinstance(item_schema, Mapping):
                for item in value:
                    self._validate(item, item_schema, root)
            if schema.get("uniqueItems"):
                canonical = [
                    json.dumps(item, sort_keys=True, separators=(",", ":"))
                    for item in value
                ]
                if len(canonical) != len(set(canonical)):
                    self._invalid()
        if isinstance(value, Mapping):
            if len(value) > schema.get("maxProperties", len(value)):
                self._invalid()
            required = schema.get("required", ())
            if any(field not in value for field in required):
                self._invalid()
            properties = schema.get("properties", {})
            if schema.get("additionalProperties") is False and any(
                field not in properties for field in value
            ):
                self._invalid()
            additional = schema.get("additionalProperties")
            for field, item in value.items():
                field_schema = properties.get(field)
                if isinstance(field_schema, Mapping):
                    self._validate(item, field_schema, root)
                elif isinstance(additional, Mapping):
                    self._validate(item, additional, root)
            names = schema.get("propertyNames")
            if isinstance(names, Mapping):
                for field in value:
                    self._validate(field, names, root)
        for branch in schema.get("allOf", ()):
            self._validate(value, branch, root)
        for keyword in ("anyOf", "oneOf"):
            branches = schema.get(keyword)
            if branches:
                matches = 0
                for branch in branches:
                    try:
                        self._validate(value, branch, root)
                    except ObserverV2ContractError:
                        continue
                    matches += 1
                if matches == 0 or (keyword == "oneOf" and matches != 1):
                    self._invalid()
        condition = schema.get("if")
        if isinstance(condition, Mapping):
            try:
                self._validate(value, condition, root)
            except ObserverV2ContractError:
                pass
            else:
                consequence = schema.get("then")
                if isinstance(consequence, Mapping):
                    self._validate(value, consequence, root)
        forbidden = schema.get("not")
        if isinstance(forbidden, Mapping):
            try:
                self._validate(value, forbidden, root)
            except ObserverV2ContractError:
                pass
            else:
                self._invalid()

    @staticmethod
    def _format(value: str, name: object) -> None:
        try:
            if name == "uuid" and str(UUID(value)) != value:
                raise ValueError
            if name == "date-time":
                parsed = datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
                if parsed.tzinfo is None:
                    raise ValueError
        except ValueError:
            _SchemaValidator._invalid()

    @staticmethod
    def _invalid() -> None:
        raise ObserverV2ContractError("payload does not match generated observer v2 schema")


__all__ = [
    "OUTPUT_PARITY_CAPABILITY",
    "ObserverV2ContractError",
    "ObserverV2Contracts",
    "load_observer_v2_contracts",
]
