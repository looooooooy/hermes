"""Persistent Observer-role Session Catalog v1 state machine."""

from __future__ import annotations

import json
import re
import uuid
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from importlib import resources
from typing import Any, Protocol

SESSION_CATALOG_CAPABILITY = "session.catalog.v1"
SESSION_CATALOG_METHODS = frozenset(
    {
        "session.catalog.subscribe",
        "session.catalog.page",
        "session.catalog.unsubscribe",
    }
)
MAX_PAGE_SIZE = 128
MAX_SAFE_INTEGER = 9_007_199_254_740_991
MAX_BUFFERED_EVENTS = 1_024
MAX_CLOSED_SUBSCRIPTION_TOMBSTONES = 256

_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_PROFILE = re.compile(r"^[A-Za-z0-9_.-]+$")
_ACTIONS = frozenset(
    {
        "approval.respond",
        "clarify.respond",
        "prompt.submit",
        "session.interrupt",
        "session.steer",
    }
)
_GENERATED_PACKAGE = "hermes_agent_plugin.contracts.generated"
_POLICY_PATH = "session-catalog-v1.json"
_ENTRY_SCHEMA_PATH = "schemas/session-catalog-entry-v1.schema.json"
_RPC_SCHEMA_PATH = "schemas/local/session-catalog-rpc-v1.schema.json"
_RESET_REASONS = frozenset(
    {
        "buffer_overflow",
        "cursor_stale",
        "event_gap",
        "page_revision_changed",
        "runtime_generation_changed",
        "transport_replaced",
    }
)


class SessionCatalogV1Violation(ValueError):
    """Body-free failure while loading or applying the frozen contract."""


@dataclass(frozen=True)
class SessionCatalogV1Bundle:
    capability: str
    page_size_maximum: int
    event_buffer_maximum: int
    frame_maximum_utf8_bytes: int
    _output_validator: Callable[[object], None] = field(repr=False, compare=False)

    def validate_output(self, frame: object) -> None:
        """Validate one runtime-produced frame against the frozen generated schema."""

        self._output_validator(frame)


_SCHEMA_KEYS = frozenset(
    {
        "$defs",
        "$id",
        "$ref",
        "$schema",
        "additionalProperties",
        "allOf",
        "const",
        "else",
        "enum",
        "format",
        "if",
        "items",
        "maxItems",
        "maxLength",
        "maximum",
        "minItems",
        "minLength",
        "minimum",
        "oneOf",
        "pattern",
        "properties",
        "required",
        "then",
        "title",
        "type",
        "uniqueItems",
    }
)


def _assert_supported_schema(schema: object) -> None:
    if not isinstance(schema, Mapping):
        raise SessionCatalogV1Violation("session catalog contract resources drifted")
    if not set(schema).issubset(_SCHEMA_KEYS):
        raise SessionCatalogV1Violation("session catalog contract resources drifted")
    for collection in ("$defs", "properties"):
        children = schema.get(collection)
        if children is None:
            continue
        if not isinstance(children, Mapping):
            raise SessionCatalogV1Violation(
                "session catalog contract resources drifted"
            )
        for child in children.values():
            _assert_supported_schema(child)
    for collection in ("allOf", "oneOf"):
        children = schema.get(collection)
        if children is None:
            continue
        if not isinstance(children, list):
            raise SessionCatalogV1Violation(
                "session catalog contract resources drifted"
            )
        for child in children:
            _assert_supported_schema(child)
    for child_name in ("else", "if", "items", "then"):
        child = schema.get(child_name)
        if child is not None:
            _assert_supported_schema(child)
    schema_type = schema.get("type")
    supported_types = {"array", "boolean", "integer", "null", "object", "string"}
    if isinstance(schema_type, str):
        schema_types = {schema_type}
    elif isinstance(schema_type, list) and all(
        isinstance(item, str) for item in schema_type
    ):
        schema_types = set(schema_type)
    elif schema_type is None:
        schema_types = set()
    else:
        raise SessionCatalogV1Violation("session catalog contract resources drifted")
    if not schema_types.issubset(supported_types):
        raise SessionCatalogV1Violation("session catalog contract resources drifted")
    if schema.get("format") not in {None, "uuid"}:
        raise SessionCatalogV1Violation("session catalog contract resources drifted")


def _json_equal(left: object, right: object) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    return left == right


class _GeneratedOutputValidator:
    def __init__(self, rpc_schema: Mapping[str, Any], entry_schema: Mapping[str, Any]):
        _assert_supported_schema(rpc_schema)
        _assert_supported_schema(entry_schema)
        self._rpc = rpc_schema
        self._entry = entry_schema
        self._entry_id = entry_schema.get("$id")

    def __call__(self, value: object) -> None:
        self._validate(value, self._rpc)

    def _resolve(self, reference: object) -> Mapping[str, Any]:
        if reference == self._entry_id:
            return self._entry
        if isinstance(reference, str) and reference.startswith("#/$defs/"):
            definitions = self._rpc.get("$defs")
            definition = (
                definitions.get(reference.removeprefix("#/$defs/"))
                if isinstance(definitions, Mapping)
                else None
            )
            if isinstance(definition, Mapping):
                return definition
        raise SessionCatalogV1Violation("session catalog contract resources drifted")

    def _matches(self, value: object, schema: object) -> bool:
        try:
            self._validate(value, schema)
        except SessionCatalogV1Violation:
            return False
        return True

    def _validate(self, value: object, schema: object) -> None:
        if not isinstance(schema, Mapping):
            raise SessionCatalogV1Violation("session catalog output is invalid")
        reference = schema.get("$ref")
        if reference is not None:
            self._validate(value, self._resolve(reference))
        alternatives = schema.get("oneOf")
        if alternatives is not None:
            if (
                not isinstance(alternatives, list)
                or sum(
                    self._matches(value, alternative) for alternative in alternatives
                )
                != 1
            ):
                raise SessionCatalogV1Violation("session catalog output is invalid")
        for conjunct in schema.get("allOf", ()):
            self._validate(value, conjunct)
        condition = schema.get("if")
        if condition is not None:
            branch = (
                schema.get("then")
                if self._matches(value, condition)
                else schema.get("else")
            )
            if branch is not None:
                self._validate(value, branch)
        expected_type = schema.get("type")
        expected_types = (
            (expected_type,)
            if isinstance(expected_type, str)
            else tuple(expected_type or ())
        )
        if expected_types and not any(
            self._type_matches(value, item) for item in expected_types
        ):
            raise SessionCatalogV1Violation("session catalog output is invalid")
        if "const" in schema and not _json_equal(value, schema["const"]):
            raise SessionCatalogV1Violation("session catalog output is invalid")
        if "enum" in schema and not any(
            _json_equal(value, candidate) for candidate in schema["enum"]
        ):
            raise SessionCatalogV1Violation("session catalog output is invalid")
        if isinstance(value, str):
            if len(value) < schema.get("minLength", 0) or len(value) > schema.get(
                "maxLength", len(value)
            ):
                raise SessionCatalogV1Violation("session catalog output is invalid")
            pattern = schema.get("pattern")
            if isinstance(pattern, str) and re.search(pattern, value) is None:
                raise SessionCatalogV1Violation("session catalog output is invalid")
        if type(value) is int and (
            value < schema.get("minimum", value) or value > schema.get("maximum", value)
        ):
            raise SessionCatalogV1Violation("session catalog output is invalid")
        if isinstance(value, list):
            if len(value) < schema.get("minItems", 0) or len(value) > schema.get(
                "maxItems", len(value)
            ):
                raise SessionCatalogV1Violation("session catalog output is invalid")
            item_schema = schema.get("items")
            if item_schema is not None:
                for item in value:
                    self._validate(item, item_schema)
            if schema.get("uniqueItems") is True:
                canonical = [
                    json.dumps(item, sort_keys=True, separators=(",", ":"))
                    for item in value
                ]
                if len(canonical) != len(set(canonical)):
                    raise SessionCatalogV1Violation("session catalog output is invalid")
        if isinstance(value, Mapping):
            required = schema.get("required")
            if required is not None and (
                not isinstance(required, list) or not set(required).issubset(value)
            ):
                raise SessionCatalogV1Violation("session catalog output is invalid")
            properties = schema.get("properties", {})
            if not isinstance(properties, Mapping):
                raise SessionCatalogV1Violation("session catalog output is invalid")
            if schema.get("additionalProperties") is False and not set(value).issubset(
                properties
            ):
                raise SessionCatalogV1Violation("session catalog output is invalid")
            for name, item in value.items():
                item_schema = properties.get(name)
                if item_schema is not None:
                    self._validate(item, item_schema)

    @staticmethod
    def _type_matches(value: object, expected: object) -> bool:
        return {
            "array": isinstance(value, list),
            "boolean": type(value) is bool,
            "integer": type(value) is int,
            "null": value is None,
            "object": isinstance(value, Mapping),
            "string": isinstance(value, str),
        }.get(expected, False)


def _read_generated_json(path: str) -> dict[str, Any]:
    try:
        value = json.loads(
            resources.files(_GENERATED_PACKAGE)
            .joinpath(path)
            .read_text(encoding="utf-8")
        )
    except (
        FileNotFoundError,
        ModuleNotFoundError,
        OSError,
        json.JSONDecodeError,
    ) as error:
        raise SessionCatalogV1Violation(
            "session catalog contract resource is unavailable"
        ) from error
    if not isinstance(value, dict):
        raise SessionCatalogV1Violation("session catalog contract resource is invalid")
    return value


def load_session_catalog_v1_bundle() -> SessionCatalogV1Bundle:
    """Load synchronized resources and reject any incompatible contract drift."""

    policy = _read_generated_json(_POLICY_PATH)
    entry = _read_generated_json(_ENTRY_SCHEMA_PATH)
    rpc = _read_generated_json(_RPC_SCHEMA_PATH)
    definitions = rpc.get("$defs")
    if not isinstance(definitions, Mapping):
        raise SessionCatalogV1Violation("session catalog contract resources drifted")
    reset = definitions.get("resetNotification")
    error = definitions.get("errorResponse")
    try:
        reset_reasons = frozenset(
            reset["properties"]["params"]["properties"]["reason"]["enum"]
        )
        error_reasons = frozenset(
            error["properties"]["error"]["properties"]["reason"]["enum"]
        )
        method_constants = {
            definitions[name]["properties"]["method"]["const"]
            for name in ("subscribeRequest", "pageRequest", "unsubscribeRequest")
        }
    except (KeyError, TypeError):
        raise SessionCatalogV1Violation(
            "session catalog contract resources drifted"
        ) from None
    if (
        policy.get("contract_version") != 1
        or policy.get("capability") != SESSION_CATALOG_CAPABILITY
        or policy.get("page_size_maximum") != MAX_PAGE_SIZE
        or policy.get("event_buffer_maximum") != MAX_BUFFERED_EVENTS
        or policy.get("frame_maximum_utf8_bytes") != 262_144
        or policy.get("host_cursor_scope") != "local_only_never_cloud"
        or policy.get("local_transport")
        != "persistent_observer_role_uds_not_local_gateway_handshake"
        or policy.get("state_machine")
        != [
            "disconnected",
            "subscribing",
            "staging_pages",
            "snapshot_committed",
            "live",
        ]
        or method_constants != SESSION_CATALOG_METHODS
        or reset_reasons != _RESET_REASONS
        or error_reasons != _RESET_REASONS
        or entry.get("$id")
        != "https://contracts.hermes.local/session-catalog-entry-v1.schema.json"
        or entry.get("additionalProperties") is not False
        or frozenset(entry.get("required", ()))
        != {
            "session_key",
            "surface",
            "authority_revision",
            "available_actions",
        }
        or rpc.get("$id")
        != "https://contracts.hermes.local/local/session-catalog-rpc-v1.schema.json"
    ):
        raise SessionCatalogV1Violation("session catalog contract resources drifted")
    return SessionCatalogV1Bundle(
        capability=SESSION_CATALOG_CAPABILITY,
        page_size_maximum=MAX_PAGE_SIZE,
        event_buffer_maximum=MAX_BUFFERED_EVENTS,
        frame_maximum_utf8_bytes=262_144,
        _output_validator=_GeneratedOutputValidator(rpc, entry),
    )


class _Registration(Protocol):
    def close(self) -> None: ...


class _Binding(Protocol):
    def supports_version(self, capability: str, version: int) -> bool: ...

    def require(self, *, profile: object, runtime_generation: object) -> None: ...

    def matches(self, *, profile: str, runtime_generation: str) -> bool: ...


class _CatalogResetRequired(RuntimeError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__("session catalog reset required")


@dataclass
class _Subscription:
    transport: object
    subscription_id: str
    snapshot_id: str
    profile: str
    runtime_generation: str
    page_size: int
    phase: str = "subscribing"
    listener: _Registration | None = None
    catalog_revision: int | None = None
    next_cursor: str | None = None
    next_page_index: int = 0
    seen_session_keys: set[str] = field(default_factory=set)
    buffered_events: list[object] = field(default_factory=list)
    next_sequence: int | None = None
    draining: bool = False
    page_in_flight: bool = False
    terminating: bool = False
    terminal_frame_claimed: bool = False
    terminal_frame_started: bool = False
    terminal_reason: str | None = None
    closed: bool = False


def _field(value: object, name: str) -> object:
    if isinstance(value, Mapping):
        if name not in value:
            raise ValueError("session catalog Host value is incomplete")
        return value[name]
    try:
        return getattr(value, name)
    except AttributeError as error:
        raise ValueError("session catalog Host value is incomplete") from error


def _canonical_uuid(value: object, name: str) -> str:
    if not isinstance(value, str) or _UUID.fullmatch(value) is None:
        raise ValueError(f"{name} must be a canonical UUID")
    return value


def _profile(value: object) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 128
        or _PROFILE.fullmatch(value) is None
    ):
        raise ValueError("profile must match the Session Catalog v1 schema")
    return value


def _text(value: object, name: str, maximum: int) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= maximum:
        raise ValueError(f"{name} must match the Session Catalog v1 schema")
    return value


def _integer(value: object, name: str, minimum: int) -> int:
    if type(value) is not int or value < minimum or value > MAX_SAFE_INTEGER:
        raise ValueError(f"{name} must match the Session Catalog v1 schema")
    return value


def _registration(value: object) -> _Registration:
    if not callable(getattr(value, "close", None)):
        raise TypeError("Host session catalog listener must return a Registration")
    return value


class SessionCatalogV1Controller:
    """Own catalog subscriptions for one Host extension generation."""

    def __init__(
        self,
        *,
        host: object,
        binding: _Binding,
        request_factory: Callable[..., object],
        lock: Any,
        id_factory: Callable[[], str] = lambda: str(uuid.uuid4()),
        contract: SessionCatalogV1Bundle | None = None,
    ) -> None:
        self._host = host
        self._binding = binding
        self._request_factory = request_factory
        self._id_factory = id_factory
        self._contract = contract or load_session_catalog_v1_bundle()
        self._lock = lock
        self._subscriptions: dict[object, dict[str, _Subscription]] = {}
        self._closed_subscription_ids: dict[object, OrderedDict[str, None]] = {}

    def dispatch(
        self,
        request: dict[str, Any],
        transport: object,
    ) -> None:
        if not isinstance(request, dict):
            raise TypeError("session catalog request must be an object")
        method = request.get("method")
        if method == "session.catalog.subscribe":
            self._subscribe(request, transport)
            return
        if method == "session.catalog.page":
            self._page(request, transport)
            return
        if method == "session.catalog.unsubscribe":
            self._unsubscribe(request, transport)
            return
        raise ValueError("session catalog method is unavailable")

    def _subscribe(self, request: dict[str, Any], transport: object) -> None:
        self._require_capability()
        if set(request) != {"jsonrpc", "id", "method", "params"}:
            raise ValueError("session catalog request must use the exact schema")
        if request.get("jsonrpc") != "2.0":
            raise ValueError("session catalog request must use JSON-RPC 2.0")
        request_id = _canonical_uuid(request.get("id"), "request id")
        params = request.get("params")
        if not isinstance(params, dict) or set(params) != {
            "profile",
            "runtime_generation",
            "page_size",
        }:
            raise ValueError("session catalog params must use the exact schema")
        profile = _profile(params.get("profile"))
        runtime_generation = _text(
            params.get("runtime_generation"),
            "runtime_generation",
            128,
        )
        page_size = _integer(params.get("page_size"), "page_size", 1)
        if page_size > self._contract.page_size_maximum:
            raise ValueError("page_size must not exceed 128")
        self._binding.require(
            profile=profile,
            runtime_generation=runtime_generation,
        )
        self._replace_transport_subscriptions(transport)
        subscription = _Subscription(
            transport=transport,
            subscription_id=_canonical_uuid(
                self._id_factory(),
                "subscription_id",
            ),
            snapshot_id=_canonical_uuid(self._id_factory(), "snapshot_id"),
            profile=profile,
            runtime_generation=runtime_generation,
            page_size=page_size,
        )
        with self._lock:
            self._subscriptions.setdefault(transport, {})[
                subscription.subscription_id
            ] = subscription
        try:
            listener = self._host.add_session_catalog_listener(
                lambda event: self._on_event(subscription, event)
            )
            listener_registration = _registration(listener)
            with self._lock:
                if subscription.closed or subscription.terminating:
                    close_listener = True
                else:
                    subscription.listener = listener_registration
                    close_listener = False
            if close_listener:
                listener_registration.close()
                return
            page = self._host.session_catalog(
                self._request_factory(
                    profile=profile,
                    runtime_generation=runtime_generation,
                    page_size=page_size,
                    cursor=None,
                )
            )
            result = self._page_result(subscription, page, page_index=0)
            with self._lock:
                if subscription.closed or subscription.terminating:
                    return
                if not self._binding.matches(
                    profile=subscription.profile,
                    runtime_generation=subscription.runtime_generation,
                ):
                    return
                wrote = self._write(
                    transport,
                    {"jsonrpc": "2.0", "id": request_id, "result": result},
                )
            if wrote:
                self._page_write_committed(subscription, bool(result["is_last"]))
                return
        except _CatalogResetRequired as error:
            self._error_and_close(subscription, request_id, error.reason)
            return
        except Exception:
            reason = (
                "page_revision_changed"
                if self._binding.matches(
                    profile=subscription.profile,
                    runtime_generation=subscription.runtime_generation,
                )
                else "runtime_generation_changed"
            )
            self._error_and_close(subscription, request_id, reason)
            return
        self._close(subscription)
        self._fail_transport(transport)

    def _unsubscribe(self, request: dict[str, Any], transport: object) -> None:
        self._require_capability()
        if set(request) != {"jsonrpc", "id", "method", "params"}:
            raise ValueError("session catalog request must use the exact schema")
        if request.get("jsonrpc") != "2.0":
            raise ValueError("session catalog request must use JSON-RPC 2.0")
        request_id = _canonical_uuid(request.get("id"), "request id")
        params = request.get("params")
        if not isinstance(params, dict) or set(params) != {"subscription_id"}:
            raise ValueError("session catalog params must use the exact schema")
        subscription_id = _canonical_uuid(
            params.get("subscription_id"),
            "subscription_id",
        )
        with self._lock:
            subscription = self._subscriptions.get(transport, {}).get(subscription_id)
            already_closed = subscription_id in self._closed_subscription_ids.get(
                transport,
                OrderedDict(),
            )
        if subscription is None and not already_closed:
            if not self._write_error(transport, request_id, "transport_replaced"):
                self._fail_transport(transport)
            return
        if subscription is not None:
            self._close(subscription)
        if not self._write(
            transport,
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "subscription_id": subscription_id,
                    "closed": True,
                },
            },
        ):
            self._fail_transport(transport)

    def _page(self, request: dict[str, Any], transport: object) -> None:
        self._require_capability()
        if set(request) != {"jsonrpc", "id", "method", "params"}:
            raise ValueError("session catalog request must use the exact schema")
        if request.get("jsonrpc") != "2.0":
            raise ValueError("session catalog request must use JSON-RPC 2.0")
        request_id = _canonical_uuid(request.get("id"), "request id")
        params = request.get("params")
        if not isinstance(params, dict) or set(params) != {
            "subscription_id",
            "snapshot_id",
            "page_index",
            "cursor",
        }:
            raise ValueError("session catalog params must use the exact schema")
        subscription_id = _canonical_uuid(
            params.get("subscription_id"),
            "subscription_id",
        )
        snapshot_id = _canonical_uuid(params.get("snapshot_id"), "snapshot_id")
        page_index = _integer(params.get("page_index"), "page_index", 1)
        cursor = _text(params.get("cursor"), "cursor", 512)
        with self._lock:
            subscription = self._subscriptions.get(transport, {}).get(subscription_id)
            tombstoned = subscription_id in self._closed_subscription_ids.get(
                transport,
                OrderedDict(),
            )
        if tombstoned or (
            subscription is not None
            and (subscription.closed or subscription.terminating)
        ):
            return
        if subscription is None:
            if not self._write_error(transport, request_id, "transport_replaced"):
                self._fail_transport(transport)
            return
        if (
            subscription.phase != "staging_pages"
            or subscription.snapshot_id != snapshot_id
        ):
            self._error_and_close(subscription, request_id, "transport_replaced")
            return
        if (
            subscription.next_page_index != page_index
            or subscription.next_cursor != cursor
        ):
            self._error_and_close(subscription, request_id, "cursor_stale")
            return
        with self._lock:
            if subscription.closed or subscription.terminating:
                closed_before_page = True
                concurrent_page = False
            elif subscription.page_in_flight:
                closed_before_page = False
                concurrent_page = True
            else:
                closed_before_page = False
                subscription.page_in_flight = True
                concurrent_page = False
        if closed_before_page:
            if not self._write_error(transport, request_id, "transport_replaced"):
                self._fail_transport(transport)
            return
        if concurrent_page:
            self._error_and_close(subscription, request_id, "cursor_stale")
            return
        try:
            page = self._host.session_catalog(
                self._request_factory(
                    profile=subscription.profile,
                    runtime_generation=subscription.runtime_generation,
                    page_size=subscription.page_size,
                    cursor=cursor,
                )
            )
        except Exception:
            with self._lock:
                if subscription.closed or subscription.terminating:
                    return
            reason = (
                "cursor_stale"
                if self._binding.matches(
                    profile=subscription.profile,
                    runtime_generation=subscription.runtime_generation,
                )
                else "runtime_generation_changed"
            )
            self._error_and_close(subscription, request_id, reason)
            return
        with self._lock:
            if subscription.closed or subscription.terminating:
                return
        try:
            result = self._page_result(
                subscription,
                page,
                page_index=page_index,
            )
            with self._lock:
                if subscription.closed or subscription.terminating:
                    return
                if not self._binding.matches(
                    profile=subscription.profile,
                    runtime_generation=subscription.runtime_generation,
                ):
                    return
                wrote = self._write(
                    transport,
                    {"jsonrpc": "2.0", "id": request_id, "result": result},
                )
            if wrote:
                self._page_write_committed(subscription, bool(result["is_last"]))
                return
        except _CatalogResetRequired as error:
            self._error_and_close(subscription, request_id, error.reason)
            return
        except (TypeError, ValueError):
            self._error_and_close(
                subscription,
                request_id,
                "page_revision_changed",
            )
            return
        self._close(subscription)
        self._fail_transport(transport)

    def _page_result(
        self,
        subscription: _Subscription,
        page: object,
        *,
        page_index: int,
    ) -> dict[str, object]:
        profile = _profile(_field(page, "profile"))
        generation = _text(
            _field(page, "runtime_generation"),
            "runtime_generation",
            128,
        )
        if (profile, generation) != (
            subscription.profile,
            subscription.runtime_generation,
        ):
            raise ValueError("Host session catalog scope changed")
        revision = _integer(
            _field(page, "catalog_revision"),
            "catalog_revision",
            0,
        )
        raw_sessions = _field(page, "sessions")
        if isinstance(raw_sessions, (str, bytes, Mapping)):
            raise TypeError("Host session catalog sessions must be a collection")
        sessions = [self._entry(item, subscription) for item in raw_sessions]
        if len(sessions) > subscription.page_size or len(sessions) > MAX_PAGE_SIZE:
            raise ValueError("Host session catalog page exceeds the requested maximum")
        next_cursor_value = _field(page, "next_cursor")
        next_cursor = (
            None
            if next_cursor_value is None
            else _text(next_cursor_value, "next_cursor", 512)
        )
        is_last = next_cursor is None
        if not is_last and not sessions:
            raise ValueError("nonterminal session catalog page must not be empty")
        for entry in sessions:
            key = str(entry["session_key"])
            if key in subscription.seen_session_keys:
                raise ValueError(
                    "Host session catalog snapshot contains a duplicate key"
                )
            subscription.seen_session_keys.add(key)
        if subscription.catalog_revision is None:
            subscription.catalog_revision = revision
        elif subscription.catalog_revision != revision:
            raise _CatalogResetRequired("page_revision_changed")
        subscription.next_cursor = next_cursor
        subscription.next_page_index = page_index + 1
        return {
            "subscription_id": subscription.subscription_id,
            "snapshot_id": subscription.snapshot_id,
            "profile": profile,
            "runtime_generation": generation,
            "catalog_revision": revision,
            "page_index": page_index,
            "is_last": is_last,
            "sessions": sessions,
            "next_cursor": next_cursor,
        }

    @staticmethod
    def _entry(entry: object, subscription: _Subscription) -> dict[str, object]:
        if (
            _profile(_field(entry, "profile")) != subscription.profile
            or _text(
                _field(entry, "runtime_generation"),
                "runtime_generation",
                128,
            )
            != subscription.runtime_generation
        ):
            raise ValueError("Host session catalog entry scope changed")
        raw_actions = _field(entry, "available_actions")
        if isinstance(raw_actions, (str, bytes, Mapping)):
            raise TypeError("available_actions must be a collection")
        actions = sorted(raw_actions)
        if (
            len(actions) > 5
            or len(actions) != len(set(actions))
            or any(action not in _ACTIONS for action in actions)
        ):
            raise ValueError("available_actions contains an unavailable action")
        return {
            "session_key": _text(
                _field(entry, "durable_session_key"),
                "session_key",
                256,
            ),
            "surface": _text(_field(entry, "surface"), "surface", 64),
            "authority_revision": _integer(
                _field(entry, "authority_revision"),
                "authority_revision",
                1,
            ),
            "available_actions": actions,
        }

    def _on_event(self, subscription: _Subscription, event: object) -> None:
        drain = False
        reset_reason: str | None = None
        with self._lock:
            if subscription.closed or subscription.terminating:
                return
            if not self._binding.matches(
                profile=subscription.profile,
                runtime_generation=subscription.runtime_generation,
            ):
                reset_reason = "runtime_generation_changed"
            elif (
                len(subscription.buffered_events) >= self._contract.event_buffer_maximum
            ):
                reset_reason = "buffer_overflow"
            else:
                subscription.buffered_events.append(event)
                if subscription.phase == "live" and not subscription.draining:
                    subscription.draining = True
                    drain = True
        if reset_reason is not None:
            self._reset(subscription, reset_reason)
        elif drain:
            self._drain_events(subscription)

    def _page_write_committed(
        self,
        subscription: _Subscription,
        is_last: bool,
    ) -> None:
        with self._lock:
            if subscription.closed or subscription.terminating:
                return
            subscription.page_in_flight = False
            if not is_last:
                subscription.phase = "staging_pages"
                subscription.page_in_flight = False
                return
            subscription.phase = "snapshot_committed"
            subscription.next_sequence = subscription.catalog_revision + 1  # type: ignore[operator]
            subscription.draining = True
        self._drain_events(subscription)

    def _drain_events(self, subscription: _Subscription) -> None:
        while True:
            try:
                with self._lock:
                    if subscription.closed or subscription.terminating:
                        return
                    revision = subscription.catalog_revision
                    if revision is None or subscription.next_sequence is None:
                        raise ValueError("session catalog snapshot is not committed")
                    if subscription.phase == "snapshot_committed":
                        subscription.buffered_events = [
                            event
                            for event in subscription.buffered_events
                            if _integer(
                                _field(event, "sequence"),
                                "catalog_sequence",
                                1,
                            )
                            > revision
                        ]
                        subscription.buffered_events.sort(
                            key=lambda event: _integer(
                                _field(event, "sequence"),
                                "catalog_sequence",
                                1,
                            )
                        )
                    if not subscription.buffered_events:
                        subscription.phase = "live"
                        subscription.draining = False
                        return
                    event = subscription.buffered_events.pop(0)
                    sequence = _integer(
                        _field(event, "sequence"),
                        "catalog_sequence",
                        1,
                    )
                    if sequence != subscription.next_sequence:
                        raise ValueError(
                            "session catalog event sequence is not contiguous"
                        )
                    frame = self._event_frame(subscription, event, sequence)
                    if not self._binding.matches(
                        profile=subscription.profile,
                        runtime_generation=subscription.runtime_generation,
                    ):
                        raise _CatalogResetRequired("runtime_generation_changed")
                    wrote = self._write(subscription.transport, frame)
                    if wrote and not subscription.terminating:
                        subscription.next_sequence = sequence + 1
                if not wrote:
                    self._close(subscription)
                    self._fail_transport(subscription.transport)
                    return
            except _CatalogResetRequired as error:
                self._reset(subscription, error.reason)
                return
            except (TypeError, ValueError):
                self._reset(subscription, "event_gap")
                return

    def _event_frame(
        self,
        subscription: _Subscription,
        event: object,
        sequence: int,
    ) -> dict[str, object]:
        profile = _profile(_field(event, "profile"))
        generation = _text(
            _field(event, "runtime_generation"),
            "runtime_generation",
            128,
        )
        if (profile, generation) != (
            subscription.profile,
            subscription.runtime_generation,
        ):
            raise ValueError("Host session catalog event scope changed")
        action = _field(event, "action")
        if action not in {"upsert", "remove"}:
            raise ValueError("Host session catalog event action is unavailable")
        return {
            "jsonrpc": "2.0",
            "method": "session.catalog.event",
            "params": {
                "subscription_id": subscription.subscription_id,
                "profile": profile,
                "runtime_generation": generation,
                "catalog_sequence": sequence,
                "action": action,
                "entry": self._entry(_field(event, "entry"), subscription),
            },
        }

    def _reset(self, subscription: _Subscription, reason: str) -> None:
        if not self._claim_terminal_for_send(subscription, reason):
            return
        self._send_claimed_reset(subscription, reason)

    def _send_claimed_reset(
        self,
        subscription: _Subscription,
        reason: str,
    ) -> None:
        frame = {
            "jsonrpc": "2.0",
            "method": "session.catalog.reset_required",
            "params": {
                "subscription_id": subscription.subscription_id,
                "reason": reason,
            },
        }
        wrote = self._write(subscription.transport, frame)
        self._close(subscription)
        if not wrote:
            self._fail_transport(subscription.transport)

    def _error_and_close(
        self,
        subscription: _Subscription,
        request_id: str,
        reason: str,
    ) -> None:
        if not self._claim_terminal_for_send(subscription, reason):
            return
        wrote = self._write_error(subscription.transport, request_id, reason)
        self._close(subscription)
        if not wrote:
            self._fail_transport(subscription.transport)

    @staticmethod
    def _claim_terminal_locked(
        subscription: _Subscription,
        reason: str,
    ) -> bool:
        if subscription.closed or subscription.terminal_frame_claimed:
            return False
        subscription.terminating = True
        subscription.terminal_frame_claimed = True
        subscription.terminal_reason = reason
        subscription.page_in_flight = False
        subscription.draining = False
        return True

    def _claim_terminal_for_send(
        self,
        subscription: _Subscription,
        reason: str,
    ) -> bool:
        with self._lock:
            if not self._claim_terminal_locked(subscription, reason):
                return False
            subscription.terminal_frame_started = True
            return True

    def _begin_claimed_terminal_send(
        self,
        subscription: _Subscription,
        reason: str,
    ) -> bool:
        with self._lock:
            if (
                subscription.closed
                or not subscription.terminal_frame_claimed
                or subscription.terminal_reason != reason
                or subscription.terminal_frame_started
            ):
                return False
            subscription.terminal_frame_started = True
            return True

    def _write_error(
        self,
        transport: object,
        request_id: str,
        reason: str,
    ) -> bool:
        frame = {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {
                "code": 4400,
                "message": "session catalog reset required",
                "reason": reason,
            },
        }
        return self._write(transport, frame)

    def close_transport(self, transport: object) -> None:
        with self._lock:
            subscriptions = tuple(self._subscriptions.get(transport, {}).values())
        first_error: BaseException | None = None
        for subscription in subscriptions:
            try:
                self._close(subscription)
            except BaseException as error:  # noqa: BLE001
                first_error = first_error or error
        with self._lock:
            self._subscriptions.pop(transport, None)
            self._closed_subscription_ids.pop(transport, None)
        if first_error is not None:
            raise first_error

    def _replace_transport_subscriptions(self, transport: object) -> None:
        with self._lock:
            subscriptions = tuple(self._subscriptions.get(transport, {}).values())
        first_error: BaseException | None = None
        for subscription in subscriptions:
            try:
                self._reset(subscription, "transport_replaced")
            except BaseException as error:  # noqa: BLE001
                first_error = first_error or error
        if first_error is not None:
            raise first_error

    def fence_rollover(self) -> tuple[object, ...]:
        """Fence old subscriptions while the shared generation lock is held."""

        with self._lock:
            candidates = tuple(
                candidate
                for registrations in self._subscriptions.values()
                for candidate in registrations.values()
            )
            subscriptions = tuple(
                candidate
                for candidate in candidates
                if self._claim_terminal_locked(
                    candidate,
                    "runtime_generation_changed",
                )
            )
            return subscriptions

    def complete_rollover(self, fence: tuple[object, ...]) -> None:
        first_error: BaseException | None = None
        for candidate in fence:
            if not isinstance(candidate, _Subscription):
                raise TypeError("session catalog rollover fence is invalid")
            if not self._begin_claimed_terminal_send(
                candidate,
                "runtime_generation_changed",
            ):
                continue
            try:
                self._send_claimed_reset(candidate, "runtime_generation_changed")
            except BaseException as error:  # noqa: BLE001
                first_error = first_error or error
        if first_error is not None:
            raise first_error

    def rollover(self) -> None:
        self.complete_rollover(self.fence_rollover())

    def close(self) -> None:
        with self._lock:
            transports = tuple(self._subscriptions)
        first_error: BaseException | None = None
        for transport in transports:
            try:
                self.close_transport(transport)
            except BaseException as error:  # noqa: BLE001
                first_error = first_error or error
        if first_error is not None:
            raise first_error

    def _close(self, subscription: _Subscription) -> None:
        with self._lock:
            if subscription.closed:
                return
            subscription.terminating = True
            subscription.closed = True
            subscription.page_in_flight = False
            subscription.draining = False
            registrations = self._subscriptions.get(subscription.transport)
            if registrations is not None:
                registrations.pop(subscription.subscription_id, None)
                if not registrations:
                    self._subscriptions.pop(subscription.transport, None)
            tombstones = self._closed_subscription_ids.setdefault(
                subscription.transport,
                OrderedDict(),
            )
            tombstones[subscription.subscription_id] = None
            tombstones.move_to_end(subscription.subscription_id)
            while len(tombstones) > MAX_CLOSED_SUBSCRIPTION_TOMBSTONES:
                tombstones.popitem(last=False)
            listener = subscription.listener
            subscription.listener = None
            subscription.buffered_events.clear()
            subscription.next_cursor = None
        if listener is not None:
            listener.close()

    def _fail_transport(self, transport: object) -> None:
        try:
            self.close_transport(transport)
        except BaseException:
            pass
        self._disconnect(transport)

    def _require_capability(self) -> None:
        if not self._binding.supports_version(SESSION_CATALOG_CAPABILITY, 1):
            raise RuntimeError("session catalog capability is unavailable")

    def _write(self, transport: object, frame: dict[str, object]) -> bool:
        try:
            self._contract.validate_output(frame)
            write = getattr(transport, "write", None)
            return callable(write) and write(frame) is True
        except BaseException:  # transport failure is always fail-closed
            return False

    @staticmethod
    def _disconnect(transport: object) -> None:
        disconnect = getattr(transport, "disconnect", None)
        if callable(disconnect):
            try:
                disconnect()
            except BaseException:
                pass


__all__ = [
    "MAX_CLOSED_SUBSCRIPTION_TOMBSTONES",
    "SESSION_CATALOG_CAPABILITY",
    "SESSION_CATALOG_METHODS",
    "SessionCatalogV1Bundle",
    "SessionCatalogV1Controller",
    "SessionCatalogV1Violation",
    "load_session_catalog_v1_bundle",
]
