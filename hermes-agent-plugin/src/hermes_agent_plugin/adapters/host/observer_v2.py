"""Fail-closed output-parity v2 projection at the Host SPI boundary."""

from __future__ import annotations

import base64
import binascii
import copy
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from importlib import resources
from typing import Any

OUTPUT_PARITY_CAPABILITY = "session.observe.output-parity.v1"
_GENERATED_PACKAGE = "hermes_agent_plugin.contracts.generated"
_EVENT_SCHEMA_PATH = "schemas/cloud/payloads/session-event-v2.schema.json"
_SNAPSHOT_SCHEMA_PATH = "schemas/cloud/payloads/session-snapshot-v2.schema.json"
_POLICY_PATH = "observer-output-parity-v2.json"
_COLLECTIONS = ("todo_sections", "subagents", "tools", "terminals")
_LIFECYCLE_TYPES = frozenset(
    {"todo.update", "subagent.update", "tool.update", "terminal.update"}
)
_TERMINAL_STATUSES = {
    "todo_sections": frozenset({"completed", "cancelled"}),
    "subagents": frozenset({"completed", "failed", "interrupted"}),
    "tools": frozenset({"completed", "failed", "interrupted"}),
    "terminals": frozenset({"completed", "failed", "interrupted"}),
}
_EVENT_COLLECTION = {
    "todo.update": "todo_sections",
    "subagent.update": "subagents",
    "tool.update": "tools",
    "terminal.update": "terminals",
}
_IDENTITY_FIELDS = {
    "todo_sections": ("turn_id", "section_id"),
    "subagents": ("turn_id", "subagent_id"),
    "tools": ("turn_id", "tool_call_id"),
    "terminals": ("turn_id", "process_id"),
}
_FORBIDDEN_FACT_FIELDS = frozenset(
    {
        "approval_payload",
        "approval",
        "arguments",
        "args",
        "authorization",
        "credential",
        "credentials",
        "full_approval_payload",
        "password",
        "private_reasoning",
        "raw_args",
        "raw_output",
        "raw_reasoning",
        "raw_terminal_output",
        "raw_tool_output",
        "reasoning",
        "secret",
        "token",
        "token_value",
        "tool_output",
        "terminal_output",
        "output",
    }
)
_DISPLAY_FIELDS = frozenset(
    {
        "assistant",
        "call_label",
        "content",
        "error",
        "goal",
        "label",
        "name",
        "summary",
        "text",
        "tool_name",
        "user",
    }
)
_NAME_DISPLAY_FIELDS = frozenset({"name", "tool_name"})
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
_CREDENTIAL_KEY_QUALIFIERS = frozenset(
    {"access", "api", "client", "private", "secret", "signing"}
)
_TOKEN_COUNT_FIELDS = frozenset({"input", "output", "reasoning"})
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_NON_KEY_TOKEN = re.compile(r"[^A-Za-z0-9]+")
_BASIC_CANDIDATE = re.compile(
    r"(?i)\b(?P<prefix>(?:authorization\s*:\s*)?basic\s+)"
    r"(?P<token>[A-Za-z0-9+/]+={0,2})(?=$|[\s,;])"
)
_JWT_CANDIDATE = re.compile(
    r"(?<![A-Za-z0-9_-])(?P<header>[A-Za-z0-9_-]+)\."
    r"(?P<payload>[A-Za-z0-9_-]+)\.(?P<signature>[A-Za-z0-9_-]+)"
    r"(?![A-Za-z0-9_-])"
)
_SECRET_PATTERNS = (
    (
        re.compile(r"(?i)\b((?:authorization\s*:\s*)?bearer\s+)[^\s,;]+"),
        r"\1[REDACTED]",
    ),
    (
        re.compile(
            r"(?i)\b((?:api[\s_-]*key|access[\s_-]*token|password|secret|token)"
            r"\s*[:=]\s*)[^\s,;]+"
        ),
        r"\1[REDACTED]",
    ),
    (
        re.compile(
            r"(?i)(?<![A-Za-z0-9_])(?:"
            r"(?:sk|gh[pousr]|github_pat|xox[baprs])[-_][A-Za-z0-9_-]{8,}|"
            r"(?:AKIA|ASIA)[0-9A-Z]{12,}|"
            r"AIza[0-9A-Za-z_-]{16,}|"
            r"ya29\.[0-9A-Za-z_-]{8,}|"
            r"(?:glpat-|hf_|npm_|pypi-)[A-Za-z0-9_-]{8,}|"
            r"SG\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"
            r")(?![A-Za-z0-9_-])"
        ),
        "[REDACTED]",
    ),
    (
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?"
            r"(?:-----END [A-Z ]*PRIVATE KEY-----|$)",
            re.DOTALL,
        ),
        "[REDACTED]",
    ),
)
_STATIC_CREDENTIAL_PATTERNS = tuple(pattern for pattern, _ in _SECRET_PATTERNS)


class ObserverV2Violation(ValueError):
    """Safe validation failure that never includes the rejected Host fact."""


@dataclass(frozen=True)
class ObserverV2Bundle:
    capability: str
    policy: Mapping[str, Any]
    event_schema: Mapping[str, Any]
    snapshot_schema: Mapping[str, Any]


@dataclass(frozen=True)
class _SafetyLimits:
    max_array_items: int
    max_display_text: int
    max_frame_bytes: int
    max_name: int
    max_nesting_depth: int
    max_object_fields: int


def _read_json(path: str) -> dict[str, Any]:
    try:
        body = resources.files(_GENERATED_PACKAGE).joinpath(path).read_text(
            encoding="utf-8"
        )
        value = json.loads(body)
    except (FileNotFoundError, ModuleNotFoundError, OSError, json.JSONDecodeError) as error:
        raise ObserverV2Violation("observer v2 contract resource is unavailable") from error
    if not isinstance(value, dict):
        raise ObserverV2Violation("observer v2 contract resource is invalid")
    return value


def load_observer_v2_bundle() -> ObserverV2Bundle:
    """Load and cross-check every generated v2 resource without fallback."""

    policy = _read_json(_POLICY_PATH)
    event_schema = _read_json(_EVENT_SCHEMA_PATH)
    snapshot_schema = _read_json(_SNAPSHOT_SCHEMA_PATH)
    _safety_limits(policy)
    security = policy.get("security")
    if (
        policy.get("contract") != "observer-output-parity"
        or policy.get("version") != 2
        or policy.get("base_observer_contract") != 1
        or policy.get("capability") != OUTPUT_PARITY_CAPABILITY
        or tuple(policy.get("snapshot", {}).get("collections", ())) != _COLLECTIONS
        or not isinstance(security, Mapping)
        or security.get("credentials") != "forbidden"
        or security.get("display_safe_text_only") is not True
        or security.get("raw_args") != "forbidden"
        or security.get("raw_reasoning") != "forbidden"
        or security.get("raw_terminal_output") != "forbidden"
        or security.get("raw_tool_output") != "forbidden"
        or security.get("token_counts") != "nonnegative_aggregate_only"
        or security.get("token_values") != "forbidden"
        or frozenset(policy.get("non_mergeable_lifecycle_event_types", ()))
        != _LIFECYCLE_TYPES
        or event_schema.get("$id")
        != "https://contracts.hermes.local/cloud/payloads/session-event-v2.schema.json"
        or snapshot_schema.get("$id")
        != "https://contracts.hermes.local/cloud/payloads/session-snapshot-v2.schema.json"
        or event_schema.get("properties", {}).get("observer_contract") != {"const": 2}
        or snapshot_schema.get("properties", {}).get("observer_contract")
        != {"const": 2}
        or snapshot_schema.get("properties", {})
        .get("replay_events", {})
        .get("items")
        != {"$ref": "session-event-v2.schema.json"}
    ):
        raise ObserverV2Violation("observer v2 contract resources drifted")
    return ObserverV2Bundle(
        capability=OUTPUT_PARITY_CAPABILITY,
        policy=policy,
        event_schema=event_schema,
        snapshot_schema=snapshot_schema,
    )


def _safety_limits(policy: Mapping[str, Any]) -> _SafetyLimits:
    limits = policy.get("limits")
    names = {
        "max_array_items",
        "max_display_text_code_points",
        "max_frame_bytes",
        "max_name_code_points",
        "max_nesting_depth",
        "max_object_fields",
    }
    if not isinstance(limits, Mapping) or any(
        type(limits.get(name)) is not int or limits[name] < 1 for name in names
    ):
        raise ObserverV2Violation("observer v2 contract resources drifted")
    return _SafetyLimits(
        max_array_items=limits["max_array_items"],
        max_display_text=limits["max_display_text_code_points"],
        max_frame_bytes=limits["max_frame_bytes"],
        max_name=limits["max_name_code_points"],
        max_nesting_depth=limits["max_nesting_depth"],
        max_object_fields=limits["max_object_fields"],
    )


def _schema_type_matches(value: object, expected: str) -> bool:
    if expected == "null":
        return value is None
    if expected == "object":
        return isinstance(value, Mapping)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    return False


class _SchemaValidator:
    def __init__(
        self,
        root: Mapping[str, Any],
        *,
        external: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> None:
        self._root = root
        self._external = dict(external or {})

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
                definition = root.get("$defs", {}).get(reference.removeprefix("#/$defs/"))
                if not isinstance(definition, Mapping):
                    raise ObserverV2Violation("observer v2 schema reference drifted")
                self._validate(value, definition, root)
                return
            target = self._external.get(reference)
            if target is None:
                raise ObserverV2Violation("observer v2 schema reference drifted")
            self._validate(value, target, target)
            return

        expected = schema.get("type")
        if expected is not None:
            expected_types = (expected,) if isinstance(expected, str) else tuple(expected)
            if not any(_schema_type_matches(value, item) for item in expected_types):
                raise ObserverV2Violation("Host fact does not match observer v2 schema")
        if "const" in schema and value != schema["const"]:
            raise ObserverV2Violation("Host fact does not match observer v2 schema")
        if "enum" in schema and value not in schema["enum"]:
            raise ObserverV2Violation("Host fact does not match observer v2 schema")
        if isinstance(value, str):
            if len(value) < schema.get("minLength", 0) or len(value) > schema.get(
                "maxLength", len(value)
            ):
                raise ObserverV2Violation("Host fact does not match observer v2 schema")
            pattern = schema.get("pattern")
            if isinstance(pattern, str) and re.fullmatch(pattern, value) is None:
                raise ObserverV2Violation("Host fact does not match observer v2 schema")
        if isinstance(value, int) and not isinstance(value, bool):
            if value < schema.get("minimum", value) or value > schema.get(
                "maximum", value
            ):
                raise ObserverV2Violation("Host fact does not match observer v2 schema")
        if isinstance(value, list):
            if len(value) < schema.get("minItems", 0) or len(value) > schema.get(
                "maxItems", len(value)
            ):
                raise ObserverV2Violation("Host fact does not match observer v2 schema")
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
                    raise ObserverV2Violation("Host fact does not match observer v2 schema")
        if isinstance(value, Mapping):
            if len(value) > schema.get("maxProperties", len(value)):
                raise ObserverV2Violation("Host fact does not match observer v2 schema")
            required = schema.get("required", ())
            if any(field not in value for field in required):
                raise ObserverV2Violation("Host fact does not match observer v2 schema")
            properties = schema.get("properties", {})
            if schema.get("additionalProperties") is False and any(
                field not in properties for field in value
            ):
                raise ObserverV2Violation("Host fact does not match observer v2 schema")
            additional = schema.get("additionalProperties")
            for field, item in value.items():
                field_schema = properties.get(field)
                if isinstance(field_schema, Mapping):
                    self._validate(item, field_schema, root)
                elif isinstance(additional, Mapping):
                    self._validate(item, additional, root)
            property_names = schema.get("propertyNames")
            if isinstance(property_names, Mapping):
                for field in value:
                    self._validate(field, property_names, root)
        for branch in schema.get("allOf", ()):
            self._validate(value, branch, root)
        for keyword in ("anyOf", "oneOf"):
            branches = schema.get(keyword)
            if branches:
                matches = 0
                for branch in branches:
                    try:
                        self._validate(value, branch, root)
                    except ObserverV2Violation:
                        continue
                    matches += 1
                if matches == 0 or (keyword == "oneOf" and matches != 1):
                    raise ObserverV2Violation("Host fact does not match observer v2 schema")
        condition = schema.get("if")
        if isinstance(condition, Mapping):
            try:
                self._validate(value, condition, root)
            except ObserverV2Violation:
                pass
            else:
                consequence = schema.get("then")
                if isinstance(consequence, Mapping):
                    self._validate(value, consequence, root)
        forbidden = schema.get("not")
        if isinstance(forbidden, Mapping):
            try:
                self._validate(value, forbidden, root)
            except ObserverV2Violation:
                pass
            else:
                raise ObserverV2Violation("Host fact does not match observer v2 schema")


def _decoded_base64(value: str, *, urlsafe: bool) -> bytes | None:
    padding = "=" * (-len(value) % 4)
    try:
        return base64.b64decode(
            value + padding,
            altchars=b"-_" if urlsafe else None,
            validate=True,
        )
    except (binascii.Error, ValueError):
        return None


def _is_basic_credential(match: re.Match[str]) -> bool:
    decoded = _decoded_base64(match.group("token"), urlsafe=False)
    if decoded is None:
        return False
    user, separator, _password = decoded.partition(b":")
    return bool(user and separator)


def _is_structured_jwt(match: re.Match[str]) -> bool:
    header_bytes = _decoded_base64(match.group("header"), urlsafe=True)
    payload_bytes = _decoded_base64(match.group("payload"), urlsafe=True)
    signature = _decoded_base64(match.group("signature"), urlsafe=True)
    if header_bytes is None or payload_bytes is None or not signature:
        return False
    try:
        header = json.loads(header_bytes.decode("utf-8"))
        payload = json.loads(payload_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    return (
        isinstance(header, dict)
        and isinstance(header.get("alg"), str)
        and bool(header["alg"])
        and isinstance(payload, dict)
    )


def _contains_credential_material(value: str) -> bool:
    return (
        any(pattern.search(value) for pattern in _STATIC_CREDENTIAL_PATTERNS)
        or any(_is_basic_credential(match) for match in _BASIC_CANDIDATE.finditer(value))
        or any(_is_structured_jwt(match) for match in _JWT_CANDIDATE.finditer(value))
    )


def _redact_text(value: str, *, limit: int) -> str:
    redacted = _BASIC_CANDIDATE.sub(
        lambda match: (
            f"{match.group('prefix')}[REDACTED]"
            if _is_basic_credential(match)
            else match.group(0)
        ),
        value,
    )
    redacted = _JWT_CANDIDATE.sub(
        lambda match: "[REDACTED]"
        if _is_structured_jwt(match)
        else match.group(0),
        redacted,
    )
    for pattern, replacement in _SECRET_PATTERNS:
        redacted = pattern.sub(replacement, redacted)
    return redacted[:limit]


def _safe_copy(
    value: object,
    *,
    limits: _SafetyLimits,
    field: str | None = None,
    depth: int = 0,
    in_extensions: bool = False,
) -> object:
    if depth > limits.max_nesting_depth:
        raise ObserverV2Violation("Host fact exceeds generated nesting depth")
    if isinstance(value, Mapping):
        if len(value) > limits.max_object_fields:
            raise ObserverV2Violation("Host fact exceeds object field bound")
        if _normalized_key(field or "") == "token_counts":
            if not _is_aggregate_token_counts(value):
                raise ObserverV2Violation("Host fact token counts are invalid")
            return dict(value)
        result: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ObserverV2Violation("forbidden Host fact field")
            normalized = _normalized_key(key)
            child_in_extensions = in_extensions or normalized == "extensions"
            if normalized in _FORBIDDEN_FACT_FIELDS:
                raise ObserverV2Violation("forbidden Host fact field")
            if child_in_extensions and normalized != "extensions":
                if normalized == "token_counts":
                    if not isinstance(item, Mapping) or not _is_aggregate_token_counts(
                        item
                    ):
                        raise ObserverV2Violation("Host fact token counts are invalid")
                else:
                    tokens = frozenset(normalized.split("_"))
                    sensitive_key = (
                        "key" in tokens
                        and bool(tokens.intersection(_CREDENTIAL_KEY_QUALIFIERS))
                    )
                    if tokens.intersection(_SENSITIVE_EXTENSION_TOKENS) or sensitive_key:
                        raise ObserverV2Violation("forbidden Host fact field")
            result[key] = _safe_copy(
                item,
                limits=limits,
                field=key,
                depth=depth + 1,
                in_extensions=child_in_extensions,
            )
        return result
    if isinstance(value, list):
        if len(value) > limits.max_array_items:
            raise ObserverV2Violation("Host fact exceeds array item bound")
        return [
            _safe_copy(
                item,
                limits=limits,
                depth=depth + 1,
                in_extensions=in_extensions,
            )
            for item in value
        ]
    if isinstance(value, str) and in_extensions:
        if (
            _contains_unsafe_control(value)
            or _contains_credential_material(value)
            or len(value) > limits.max_display_text
        ):
            raise ObserverV2Violation("forbidden Host fact value")
        return value
    normalized_field = _normalized_key(field or "")
    if isinstance(value, str) and normalized_field in _DISPLAY_FIELDS:
        limit = (
            limits.max_name
            if normalized_field in _NAME_DISPLAY_FIELDS
            else limits.max_display_text
        )
        return _redact_text(value, limit=limit)
    if isinstance(value, str):
        redacted = _redact_text(value, limit=len(value))
        if redacted != value:
            raise ObserverV2Violation("forbidden Host fact value")
    return copy.deepcopy(value)


def _normalized_key(value: str) -> str:
    words = _CAMEL_BOUNDARY.sub("_", value)
    return _NON_KEY_TOKEN.sub("_", words).strip("_").lower()


def _is_aggregate_token_counts(value: Mapping[object, object]) -> bool:
    return (
        bool(value)
        and set(value).issubset(_TOKEN_COUNT_FIELDS)
        and all(type(item) is int and item >= 0 for item in value.values())
    )


def _contains_unsafe_control(value: str) -> bool:
    return any(
        (ord(character) < 32 and character not in "\n\r\t")
        or 127 <= ord(character) <= 159
        or ord(character) in {0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF}
        for character in value
    )


def _ensure_bounded_json(value: object, *, max_frame_bytes: int) -> None:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ObserverV2Violation("Host fact must be canonical JSON") from error
    if len(encoded) > max_frame_bytes:
        raise ObserverV2Violation("Host fact exceeds observer v2 frame bound")


def _identity(collection: str, value: Mapping[str, Any]) -> tuple[str, str]:
    fields = _IDENTITY_FIELDS[collection]
    return value[fields[0]], value[fields[1]]


class ObserverV2Projection:
    """Validated snapshot plus an atomic, contiguous lifecycle projection."""

    def __init__(self, bundle: ObserverV2Bundle) -> None:
        self._bundle = bundle
        self._safety_limits = _safety_limits(bundle.policy)
        self._event_validator = _SchemaValidator(bundle.event_schema)
        self._snapshot_validator = _SchemaValidator(
            bundle.snapshot_schema,
            external={"session-event-v2.schema.json": bundle.event_schema},
        )
        self._event_sequence = 0
        self._scope: tuple[str, str, str, str] | None = None
        self._states: dict[str, dict[tuple[str, str], dict[str, Any]]] = {
            collection: {} for collection in _COLLECTIONS
        }
        self._tombstones: dict[str, set[tuple[str, str]]] = {
            collection: set() for collection in _COLLECTIONS
        }

    @classmethod
    def from_snapshot(
        cls,
        snapshot: object,
        *,
        bundle: ObserverV2Bundle | None = None,
    ) -> ObserverV2Projection:
        projection = cls(bundle or load_observer_v2_bundle())
        projection.install_snapshot(snapshot)
        return projection

    @property
    def event_sequence(self) -> int:
        return self._event_sequence

    def install_snapshot(self, value: object) -> dict[str, Any]:
        normalized = _safe_copy(value, limits=self._safety_limits)
        _apply_safe_defaults(normalized)
        _ensure_bounded_json(
            normalized,
            max_frame_bytes=self._safety_limits.max_frame_bytes,
        )
        self._snapshot_validator.validate(normalized)
        assert isinstance(normalized, dict)
        base_sequence = normalized["snapshot_event_sequence"]
        final_sequence = normalized["event_sequence"]
        if base_sequence > final_sequence:
            raise ObserverV2Violation("snapshot sequence range is invalid")
        snapshot_scope = (
            normalized["profile"],
            normalized["runtime_generation"],
            normalized["session_key"],
            normalized["runtime_session_id"],
        )
        if self._scope is not None and snapshot_scope != self._scope:
            raise ObserverV2Violation("observer v2 snapshot scope changed")

        candidate_states: dict[str, dict[tuple[str, str], dict[str, Any]]] = {
            collection: {} for collection in _COLLECTIONS
        }
        for collection in _COLLECTIONS:
            for state in normalized[collection]:
                key = _identity(collection, state)
                if key in candidate_states[collection]:
                    raise ObserverV2Violation("composite entity identity must be unique")
                if state["first_event_sequence"] > base_sequence:
                    raise ObserverV2Violation("first event sequence is invalid")
                self._validate_entity(collection, state)
                candidate_states[collection][key] = copy.deepcopy(state)
        self._validate_subagent_tree(candidate_states["subagents"])

        old_scope = self._scope
        old_sequence = self._event_sequence
        old_states = self._states
        old_tombstones = self._tombstones
        self._scope = snapshot_scope
        self._event_sequence = base_sequence
        self._states = candidate_states
        self._tombstones = {collection: set() for collection in _COLLECTIONS}
        try:
            for event in normalized["replay_events"]:
                self.accept_event(event)
            if self._event_sequence != final_sequence:
                raise ObserverV2Violation("snapshot replay must cover its sequence range")
        except BaseException:
            self._scope = old_scope
            self._event_sequence = old_sequence
            self._states = old_states
            self._tombstones = old_tombstones
            raise
        return normalized

    def accept_event(self, value: object) -> dict[str, Any]:
        normalized = _safe_copy(value, limits=self._safety_limits)
        _apply_safe_defaults(normalized)
        _ensure_bounded_json(
            normalized,
            max_frame_bytes=self._safety_limits.max_frame_bytes,
        )
        self._event_validator.validate(normalized)
        assert isinstance(normalized, dict)
        if self._scope is None:
            raise ObserverV2Violation("observer v2 snapshot is not installed")
        event_scope = (
            normalized["profile"],
            normalized["runtime_generation"],
            normalized["session_key"],
            normalized["session_id"],
        )
        if event_scope != self._scope:
            raise ObserverV2Violation("observer v2 event scope changed")
        expected = self._event_sequence + 1
        if normalized["event_sequence"] != expected:
            raise ObserverV2Violation("observer v2 event sequence must be contiguous")
        sequence_start = normalized.get("event_sequence_start")
        if sequence_start is not None and sequence_start > expected:
            raise ObserverV2Violation("event sequence start exceeds event sequence")
        if normalized["type"] in _LIFECYCLE_TYPES:
            self._accept_lifecycle(normalized)
        else:
            self._validate_scoped_output(normalized)
        self._event_sequence = expected
        return normalized

    def _accept_lifecycle(self, event: dict[str, Any]) -> None:
        collection = _EVENT_COLLECTION[event["type"]]
        payload = event["payload"]
        key = _identity(collection, payload)
        states = self._states[collection]
        previous = states.get(key)
        if key in self._tombstones[collection]:
            raise ObserverV2Violation("deleted entity cannot be recreated before snapshot")
        expected_revision = 1 if previous is None else previous["revision"] + 1
        if payload["revision"] != expected_revision:
            raise ObserverV2Violation("entity revision must be exactly previous plus one")
        if previous is None:
            if payload["first_event_sequence"] != event["event_sequence"]:
                raise ObserverV2Violation("initial first event sequence is invalid")
        elif payload["first_event_sequence"] != previous["first_event_sequence"]:
            raise ObserverV2Violation("first event sequence must remain stable")

        if payload["operation"] == "delete":
            if previous is None:
                raise ObserverV2Violation("delete requires an existing entity")
            if not self._is_terminal(collection, previous):
                raise ObserverV2Violation("delete requires terminal entity state")
            if collection == "subagents" and any(
                state.get("parent_subagent_id") == key[1] and state["turn_id"] == key[0]
                for child_key, state in states.items()
                if child_key != key
            ):
                raise ObserverV2Violation("subagent delete requires a leaf")
            del states[key]
            self._tombstones[collection].add(key)
            return

        candidate = {
            field: copy.deepcopy(item)
            for field, item in payload.items()
            if field != "operation"
        }
        self._validate_entity(collection, candidate)
        if previous is not None:
            self._validate_absorbing(collection, previous, candidate)
        candidate_states = dict(states)
        candidate_states[key] = candidate
        if collection == "subagents":
            parent = candidate.get("parent_subagent_id")
            if parent is not None and (key[0], parent) not in states:
                raise ObserverV2Violation("live subagent parent must already exist")
            self._validate_subagent_tree(candidate_states)
        states[key] = candidate

    def _validate_scoped_output(self, event: Mapping[str, Any]) -> None:
        payload = event["payload"]
        if event["type"] == "tool.output.delta":
            key = payload["turn_id"], payload["tool_call_id"]
            if key not in self._states["tools"]:
                raise ObserverV2Violation("tool output requires a turn-scoped tool")
        elif event["type"] == "agent.terminal.output":
            key = payload["turn_id"], payload["process_id"]
            if key not in self._states["terminals"]:
                raise ObserverV2Violation("terminal output requires a turn-scoped terminal")

    def _validate_entity(self, collection: str, state: Mapping[str, Any]) -> None:
        if state["first_event_sequence"] < 1:
            raise ObserverV2Violation("first event sequence is invalid")
        if collection == "todo_sections":
            item_ids = [item["id"] for item in state["items"]]
            if len(item_ids) != len(set(item_ids)):
                raise ObserverV2Violation("todo item ids must be unique within section")
            if state["status"] in {"completed", "cancelled"} and any(
                item["status"] not in {"completed", "cancelled"}
                for item in state["items"]
            ):
                raise ObserverV2Violation("terminal todo section has active items")
        if collection == "subagents":
            progress = state.get("progress")
            if progress is not None and progress["current"] > progress["total"]:
                raise ObserverV2Violation("subagent progress exceeds total")
        if collection == "terminals":
            status = state["status"]
            exit_code = state.get("exit_code")
            if (status == "completed" and exit_code != 0) or (
                status == "failed" and (exit_code is None or exit_code == 0)
            ):
                raise ObserverV2Violation("terminal exit code is inconsistent")
            if status in {"running", "interrupted", "unknown"} and exit_code is not None:
                raise ObserverV2Violation("terminal exit code is inconsistent")

    def _is_terminal(self, collection: str, state: Mapping[str, Any]) -> bool:
        if collection == "todo_sections":
            return state["status"] in _TERMINAL_STATUSES[collection] and all(
                item["status"] in {"completed", "cancelled"}
                for item in state["items"]
            )
        return state["status"] in _TERMINAL_STATUSES[collection]

    def _validate_absorbing(
        self,
        collection: str,
        previous: Mapping[str, Any],
        candidate: Mapping[str, Any],
    ) -> None:
        if self._is_terminal(collection, previous):
            for field, old_value in previous.items():
                if field == "revision":
                    continue
                if candidate.get(field) != old_value:
                    raise ObserverV2Violation("terminal lifecycle state is absorbing")
        if collection == "todo_sections":
            old_items = {item["id"]: item for item in previous["items"]}
            old_order = [item["id"] for item in previous["items"]]
            candidate_order = [item["id"] for item in candidate["items"]]
            if candidate_order[: len(old_order)] != old_order:
                raise ObserverV2Violation(
                    "existing todo order must be retained and new items appended"
                )
            for item in candidate["items"]:
                old_item = old_items.get(item["id"])
                if old_item is not None and old_item["status"] in {
                    "completed",
                    "cancelled",
                } and item != old_item:
                    raise ObserverV2Violation("terminal todo item state is absorbing")

    def _validate_subagent_tree(
        self,
        states: Mapping[tuple[str, str], Mapping[str, Any]],
    ) -> None:
        if len(states) > 128:
            raise ObserverV2Violation("subagent tree exceeds 128 nodes")
        for key, state in states.items():
            parent = state.get("parent_subagent_id")
            if parent is not None and (key[0], parent) not in states:
                raise ObserverV2Violation("subagent tree contains an orphan")
            seen = {key[1]}
            depth = 1
            while parent is not None:
                if parent in seen:
                    raise ObserverV2Violation("subagent tree contains a cycle")
                seen.add(parent)
                depth += 1
                if depth > 8:
                    raise ObserverV2Violation("subagent tree exceeds depth 8")
                parent = states[(key[0], parent)].get("parent_subagent_id")


def _apply_safe_defaults(value: object) -> None:
    if not isinstance(value, dict):
        return
    if value.get("observer_contract") != 2:
        return
    if "replay_events" in value:
        for section in value.get("todo_sections", ()):
            _todo_defaults(section)
        for subagent in value.get("subagents", ()):
            _subagent_defaults(subagent)
        for event in value.get("replay_events", ()):
            _event_defaults(event)
        return
    _event_defaults(value)


def _event_defaults(event: object) -> None:
    if not isinstance(event, dict):
        return
    payload = event.get("payload")
    if not isinstance(payload, dict) or payload.get("operation") != "upsert":
        return
    if event.get("type") == "todo.update":
        _todo_defaults(payload)
    elif event.get("type") == "subagent.update":
        _subagent_defaults(payload)


def _todo_defaults(section: object) -> None:
    if not isinstance(section, dict):
        return
    for item in section.get("items", ()):
        if isinstance(item, dict) and (
            not isinstance(item.get("label"), str) or not item["label"].strip()
        ):
            item["label"] = "Task"


def _subagent_defaults(subagent: object) -> None:
    if not isinstance(subagent, dict):
        return
    if (
        not isinstance(subagent.get("name"), str)
        or not subagent["name"].strip()
    ):
        subagent["name"] = "Subagent"
    if not isinstance(subagent.get("goal"), str):
        subagent["goal"] = ""


__all__ = [
    "OUTPUT_PARITY_CAPABILITY",
    "ObserverV2Bundle",
    "ObserverV2Projection",
    "ObserverV2Violation",
    "load_observer_v2_bundle",
]
