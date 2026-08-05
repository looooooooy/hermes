"""Cloud Envelope contract_version=1 codec for Observer v1 and v2 payloads."""

from __future__ import annotations

import json
import math
import re
from datetime import datetime
from typing import Any
from uuid import UUID

from hermes_cloud.contracts.observer_v2 import (
    ObserverV2ContractError,
    require_payload,
)
from hermes_cloud.domain.canonical_json import canonical_payload_digest  # noqa: F401
from hermes_cloud.domain.connector_gateway import (
    ConnectorHeartbeat,
    ConnectorHello,
    ConnectorObserverEvent,
    ConnectorObserverSnapshot,
    ConnectorResumePosition,
    ConnectorSessionCatalogEvent,
    ConnectorSessionCatalogSnapshotPage,
    SessionCatalogEntry,
)
from hermes_cloud.domain.contract_errors import CoreContractError
from hermes_cloud.domain.contract_models import CloudEnvelope

MAX_FRAME_BYTES = 262_144
MAX_STRING_BYTES = 131_072
MAX_NESTING_DEPTH = 32
MAX_OBJECT_FIELDS = 1_024
MAX_ARRAY_ITEMS = 1_024

_REQUIRED_FIELDS = frozenset(
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
_OPTIONAL_FIELDS = frozenset({"traceparent", "idempotency_key", "extensions"})
_MESSAGE_TYPES = frozenset(
    {
        "connector.hello",
        "connector.welcome",
        "connector.heartbeat",
        "command.deliver",
        "command.receipt",
        "command.result",
        "control.request",
        "control.response",
        "session.snapshot",
        "session.event",
        "session.observe.open",
        "session.observe.close",
        "session.observe.open.v2",
        "session.observe.close.v2",
        "session.snapshot.v2",
        "session.event.v2",
        "session.catalog.snapshot.page",
        "session.catalog.event",
        "session.catalog.ack",
        "session.catalog.nack",
        "stream.ack",
        "stream.nack",
        "stream.ack.v2",
        "stream.nack.v2",
        "file.transfer",
        "a2a.message",
        "view.card.invalidate",
    }
)
_OBSERVER_MESSAGE_CONTRACTS = {
    "session.snapshot": 1,
    "session.event": 1,
    "session.snapshot.v2": 2,
    "session.event.v2": 2,
}
_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_DATE_TIME = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"
    r"(?:\.\d+)?Z$"
)
_TRACEPARENT = re.compile(r"^00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}$")
_EXTENSION_NAMESPACE = re.compile(r"^[a-z][a-z0-9]*(\.[a-z0-9][a-z0-9-]*)+$")
_SEMVER = re.compile(
    r"^[0-9]+\.[0-9]+\.[0-9]+"
    r"(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$"
)
_PROFILE = re.compile(r"^[A-Za-z0-9_.-]+$")
_OBSERVER_EVENT_TYPES = frozenset(
    {
        "message.start",
        "message.delta",
        "message.complete",
        "agent.terminal.output",
        "reasoning.delta",
        "status.update",
        "thinking.delta",
        "tool.output.delta",
    }
)
_MERGEABLE_OBSERVER_EVENT_TYPES = frozenset(
    {
        "message.delta",
        "agent.terminal.output",
        "reasoning.delta",
        "status.update",
        "thinking.delta",
        "tool.output.delta",
    }
)
_RUNNING_STATUSES = frozenset({"running", "working", "streaming"})
_SESSION_CATALOG_ACTIONS = frozenset(
    {
        "approval.respond",
        "clarify.respond",
        "prompt.submit",
        "session.interrupt",
        "session.steer",
    }
)
_MAX_SAFE_INTEGER = 9_007_199_254_740_991


class ContractConformanceError(CoreContractError):
    """Cloud consumer rejection using the core error catalog."""


def _raise(category: str) -> None:
    raise ContractConformanceError(category)


def _reject_non_json_constant(_value: str) -> None:
    _raise("invalid_envelope")


def _object_without_duplicates(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, member in pairs:
        if key in value:
            _raise("invalid_envelope")
        value[key] = member
    return value


def _validate_string(value: str) -> None:
    if "\x00" in value:
        _raise("invalid_envelope")
    if any("\ud800" <= character <= "\udfff" for character in value):
        _raise("invalid_envelope")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        _raise("invalid_envelope")
    if len(encoded) > MAX_STRING_BYTES:
        _raise("invalid_envelope")


def _validate_json_value(frame: object) -> None:
    pending: list[tuple[object, int]] = [(frame, 1)]
    while pending:
        value, depth = pending.pop()
        if isinstance(value, dict):
            if depth > MAX_NESTING_DEPTH:
                _raise("invalid_envelope")
            if len(value) > MAX_OBJECT_FIELDS:
                _raise("invalid_envelope")
            for key, item in value.items():
                if not isinstance(key, str):
                    _raise("invalid_envelope")
                _validate_string(key)
                pending.append((item, depth + 1))
            continue
        if isinstance(value, list):
            if depth > MAX_NESTING_DEPTH:
                _raise("invalid_envelope")
            if len(value) > MAX_ARRAY_ITEMS:
                _raise("invalid_envelope")
            pending.extend((item, depth + 1) for item in value)
            continue
        if isinstance(value, str):
            _validate_string(value)
            continue
        if value is None or isinstance(value, (bool, int)):
            continue
        if isinstance(value, float) and math.isfinite(value):
            continue
        _raise("invalid_envelope")


def _decode_json_object(raw: object) -> dict[str, Any]:
    if isinstance(raw, bytes):
        if len(raw) > MAX_FRAME_BYTES:
            _raise("frame_too_large")
        try:
            text = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            _raise("invalid_utf8")
    elif isinstance(raw, str):
        try:
            encoded = raw.encode("utf-8", errors="strict")
        except UnicodeEncodeError:
            _raise("invalid_envelope")
        if len(encoded) > MAX_FRAME_BYTES:
            _raise("frame_too_large")
        text = raw
    else:
        _raise("invalid_envelope")

    try:
        decoded = json.loads(
            text,
            parse_constant=_reject_non_json_constant,
            object_pairs_hook=_object_without_duplicates,
        )
    except ContractConformanceError:
        raise
    except (RecursionError, TypeError, ValueError, json.JSONDecodeError):
        _raise("invalid_envelope")
    if not isinstance(decoded, dict):
        _raise("invalid_envelope")
    _validate_json_value(decoded)
    return decoded


def _require_string(
    envelope: dict[str, Any],
    field: str,
    *,
    minimum: int = 1,
    maximum: int = 128,
) -> str:
    value = envelope[field]
    if not isinstance(value, str):
        _raise("invalid_envelope")
    if not minimum <= len(value) <= maximum:
        _raise("invalid_envelope")
    return value


def _validate_uuid(value: str) -> None:
    if not _UUID.fullmatch(value):
        _raise("invalid_envelope")
    try:
        UUID(value)
    except (AttributeError, TypeError, ValueError):
        _raise("invalid_envelope")


def _validate_datetime(value: str) -> None:
    if not _DATE_TIME.fullmatch(value):
        _raise("invalid_envelope")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        _raise("invalid_envelope")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        _raise("invalid_envelope")


def _validate_extensions(extensions: object) -> None:
    if not isinstance(extensions, dict) or len(extensions) > 16:
        _raise("invalid_envelope")
    for namespace, extension in extensions.items():
        if not isinstance(namespace, str):
            _raise("invalid_envelope")
        if not _EXTENSION_NAMESPACE.fullmatch(namespace):
            _raise("invalid_envelope")
        if not isinstance(extension, dict):
            _raise("invalid_envelope")


def _validate_capability_array(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > 64:
        _raise("invalid_envelope")
    capabilities: list[str] = []
    for capability in value:
        if not isinstance(capability, str) or not 1 <= len(capability) <= 128:
            _raise("invalid_envelope")
        capabilities.append(capability)
    if len(capabilities) != len(set(capabilities)):
        _raise("invalid_envelope")
    return tuple(capabilities)


def _require_non_negative_integer(
    value: dict[str, Any],
    field: str,
) -> int:
    member = value[field]
    if type(member) is not int or member < 0:
        _raise("invalid_envelope")
    return member


def _bounded_text(
    value: dict[str, Any],
    field: str,
    *,
    maximum: int,
) -> str:
    member = value.get(field)
    if not isinstance(member, str) or not 1 <= len(member) <= maximum:
        _raise("invalid_envelope")
    return member


def _optional_text(
    value: dict[str, Any],
    field: str,
    *,
    maximum: int,
) -> str | None:
    member = value.get(field)
    if member is None:
        return None
    if not isinstance(member, str) or len(member) > maximum:
        _raise("invalid_envelope")
    return member


def _validate_observer_event_payload(
    event_type: str,
    payload: object,
) -> dict[str, object]:
    if not isinstance(payload, dict):
        _raise("invalid_envelope")
    fields = set(payload)
    if event_type == "message.start":
        if not fields <= {"message_id", "role"}:
            _raise("invalid_envelope")
        if "message_id" in payload:
            _bounded_text(payload, "message_id", maximum=256)
        if "role" in payload and payload["role"] != "assistant":
            _raise("invalid_envelope")
    elif event_type in {"message.delta", "reasoning.delta", "thinking.delta"}:
        if fields != {"text"}:
            _raise("invalid_envelope")
        _bounded_text(payload, "text", maximum=MAX_STRING_BYTES)
    elif event_type == "message.complete":
        if "status" not in fields or not fields <= {"text", "status", "error"}:
            _raise("invalid_envelope")
        if payload["status"] not in {"complete", "error"}:
            _raise("invalid_envelope")
        if "text" in payload:
            _bounded_text(payload, "text", maximum=MAX_STRING_BYTES)
        if "error" in payload and payload["error"] is not None:
            _bounded_text(payload, "error", maximum=4096)
    elif event_type == "agent.terminal.output":
        if "text" not in fields or not fields <= {
            "process_id",
            "stream",
            "text",
            "sequence",
        }:
            _raise("invalid_envelope")
        if "process_id" in payload:
            _bounded_text(payload, "process_id", maximum=256)
        if "stream" in payload and payload["stream"] not in {"stdout", "stderr"}:
            _raise("invalid_envelope")
        _bounded_text(payload, "text", maximum=MAX_STRING_BYTES)
        if "sequence" in payload and (
            type(payload["sequence"]) is not int or payload["sequence"] < 0
        ):
            _raise("invalid_envelope")
    elif event_type == "status.update":
        if not {"status", "running"} <= fields or not fields <= {
            "status",
            "running",
            "text",
        }:
            _raise("invalid_envelope")
        status = _bounded_text(payload, "status", maximum=64)
        running = payload["running"]
        if type(running) is not bool or running != (status in _RUNNING_STATUSES):
            _raise("invalid_envelope")
        if "text" in payload:
            _bounded_text(payload, "text", maximum=MAX_STRING_BYTES)
    elif event_type == "tool.output.delta":
        if "text" not in fields or not fields <= {
            "tool_call_id",
            "tool_name",
            "text",
            "sequence",
        }:
            _raise("invalid_envelope")
        for field in ("tool_call_id", "tool_name"):
            if field in payload:
                _bounded_text(payload, field, maximum=256)
        _bounded_text(payload, "text", maximum=MAX_STRING_BYTES)
        if "sequence" in payload and (
            type(payload["sequence"]) is not int or payload["sequence"] < 0
        ):
            _raise("invalid_envelope")
    else:
        _raise("invalid_envelope")
    return dict(payload)


def _decode_observer_event(
    payload: object,
    *,
    inherited_profile: str | None = None,
    inherited_generation: str | None = None,
) -> ConnectorObserverEvent:
    _validate_json_value(payload)
    if not isinstance(payload, dict):
        _raise("invalid_envelope")
    replay = inherited_profile is not None or inherited_generation is not None
    required = {"session_key", "session_id", "type", "event_sequence", "payload"}
    allowed = {*required, "event_sequence_start"}
    if not replay:
        required.update({"profile", "runtime_generation"})
        allowed.update({"profile", "runtime_generation", "extensions"})
    if not required <= set(payload) or not set(payload) <= allowed:
        _raise("invalid_envelope")
    if "extensions" in payload:
        _validate_extensions(payload["extensions"])
    profile = inherited_profile or _bounded_text(payload, "profile", maximum=128)
    if not _PROFILE.fullmatch(profile):
        _raise("invalid_envelope")
    runtime_generation = inherited_generation or _bounded_text(
        payload,
        "runtime_generation",
        maximum=128,
    )
    session_key = _bounded_text(payload, "session_key", maximum=256)
    runtime_session_id = _bounded_text(payload, "session_id", maximum=256)
    event_type = payload["type"]
    if event_type not in _OBSERVER_EVENT_TYPES:
        _raise("invalid_envelope")
    event_sequence = _require_non_negative_integer(payload, "event_sequence")
    if event_sequence < 1:
        _raise("invalid_envelope")
    event_sequence_start = payload.get("event_sequence_start", event_sequence)
    if (
        type(event_sequence_start) is not int
        or event_sequence_start < 1
        or event_sequence_start > event_sequence
        or (
            event_sequence_start < event_sequence
            and event_type not in _MERGEABLE_OBSERVER_EVENT_TYPES
        )
    ):
        _raise("invalid_envelope")
    return ConnectorObserverEvent(
        profile=profile,
        runtime_generation=runtime_generation,
        session_key=session_key,
        runtime_session_id=runtime_session_id,
        event_type=event_type,
        event_sequence_start=event_sequence_start,
        event_sequence=event_sequence,
        payload=_validate_observer_event_payload(event_type, payload["payload"]),
    )


def _decode_observer_event_v2(payload: object) -> ConnectorObserverEvent:
    try:
        decoded = require_payload("session-event-v2", payload)
    except ObserverV2ContractError:
        _raise("invalid_envelope")
    event_sequence = int(decoded["event_sequence"])
    event_sequence_start = int(decoded.get("event_sequence_start", event_sequence))
    return ConnectorObserverEvent(
        profile=str(decoded["profile"]),
        runtime_generation=str(decoded["runtime_generation"]),
        session_key=str(decoded["session_key"]),
        runtime_session_id=str(decoded["session_id"]),
        event_type=str(decoded["type"]),
        event_sequence_start=event_sequence_start,
        event_sequence=event_sequence,
        payload=dict(decoded["payload"]),
        observer_contract=2,
    )


def _validate_observer_message_contract(
    message_type: str,
    payload: dict[str, object],
) -> None:
    expected = _OBSERVER_MESSAGE_CONTRACTS.get(message_type)
    if expected is None:
        return
    if expected == 2:
        if type(payload.get("observer_contract")) is not int:
            _raise("invalid_envelope")
        if payload["observer_contract"] != 2:
            _raise("invalid_envelope")
        return
    if "observer_contract" in payload:
        _raise("invalid_envelope")


class CloudEnvelopeV1Adapter:
    """Decode the root Connector Cloud Envelope schema without jsonschema."""

    def decode_connector_frame(self, raw: object) -> CloudEnvelope:
        envelope = _decode_json_object(raw)
        version = envelope.get("contract_version")
        if type(version) is not int or version != 1:
            _raise("contract_unsupported")
        fields = frozenset(envelope)
        if not _REQUIRED_FIELDS <= fields:
            _raise("invalid_envelope")
        if not fields <= _REQUIRED_FIELDS | _OPTIONAL_FIELDS:
            _raise("invalid_envelope")

        message_id = _require_string(envelope, "message_id")
        _validate_uuid(message_id)
        message_type = _require_string(envelope, "message_type")
        if message_type not in _MESSAGE_TYPES:
            _raise("invalid_envelope")
        tenant_id = _require_string(envelope, "tenant_id")
        device_id = _require_string(envelope, "device_id")

        sequence = envelope["sequence"]
        if type(sequence) is not int or sequence < 0:
            _raise("invalid_envelope")

        sent_at = _require_string(
            envelope,
            "sent_at",
            maximum=MAX_STRING_BYTES,
        )
        _validate_datetime(sent_at)

        payload = envelope["payload"]
        if not isinstance(payload, dict) or len(payload) > MAX_OBJECT_FIELDS:
            _raise("invalid_envelope")
        _validate_observer_message_contract(message_type, payload)

        traceparent = envelope.get("traceparent")
        if traceparent is not None and (
            not isinstance(traceparent, str) or not _TRACEPARENT.fullmatch(traceparent)
        ):
            _raise("invalid_envelope")

        idempotency_key = envelope.get("idempotency_key")
        if idempotency_key is not None:
            if not isinstance(idempotency_key, str):
                _raise("invalid_envelope")
            if not 1 <= len(idempotency_key) <= 128:
                _raise("invalid_envelope")

        extensions = envelope.get("extensions")
        if extensions is not None:
            _validate_extensions(extensions)

        return CloudEnvelope(
            contract_version=version,
            message_id=message_id,
            message_type=message_type,
            tenant_id=tenant_id,
            device_id=device_id,
            sequence=sequence,
            sent_at=sent_at,
            traceparent=traceparent,
            idempotency_key=idempotency_key,
            payload=dict(payload),
            extensions=(
                None
                if extensions is None
                else {
                    namespace: dict(extension)
                    for namespace, extension in extensions.items()
                }
            ),
        )

    def decode_hello(self, payload: object) -> ConnectorHello:
        _validate_json_value(payload)
        if not isinstance(payload, dict):
            _raise("invalid_envelope")
        required_fields = {
            "connector_instance_id",
            "connector_version",
            "runtime_generation",
            "required_capabilities",
            "optional_capabilities",
            "resume",
        }
        if not required_fields <= set(payload):
            _raise("invalid_envelope")
        if not set(payload) <= required_fields | {"extensions"}:
            _raise("invalid_envelope")
        if "extensions" in payload:
            _validate_extensions(payload["extensions"])

        connector_instance_id = _require_string(
            payload,
            "connector_instance_id",
        )
        _validate_uuid(connector_instance_id)
        connector_version = _require_string(
            payload,
            "connector_version",
            maximum=64,
        )
        if len(connector_version) < 5 or not _SEMVER.fullmatch(connector_version):
            _raise("invalid_envelope")
        runtime_generation = _require_string(
            payload,
            "runtime_generation",
        )
        required_capabilities = _validate_capability_array(
            payload["required_capabilities"]
        )
        optional_capabilities = _validate_capability_array(
            payload["optional_capabilities"]
        )
        if set(required_capabilities).intersection(optional_capabilities):
            _raise("invalid_envelope")

        resume = payload["resume"]
        if not isinstance(resume, dict):
            _raise("invalid_envelope")
        resume_required = {
            "mode",
            "next_outbound_sequence",
            "next_inbound_sequence",
        }
        if not resume_required <= set(resume):
            _raise("invalid_envelope")
        if not set(resume) <= resume_required | {"previous_connection_id"}:
            _raise("invalid_envelope")
        mode = resume["mode"]
        if mode not in {"fresh", "resume"}:
            _raise("invalid_envelope")
        previous_connection_id = resume.get("previous_connection_id")
        if mode == "resume":
            if not isinstance(previous_connection_id, str):
                _raise("invalid_envelope")
            _validate_uuid(previous_connection_id)
        elif previous_connection_id is not None:
            _raise("invalid_envelope")

        return ConnectorHello(
            connector_instance_id=connector_instance_id,
            connector_version=connector_version,
            runtime_generation=runtime_generation,
            required_capabilities=required_capabilities,
            optional_capabilities=optional_capabilities,
            resume=ConnectorResumePosition(
                mode=mode,
                previous_connection_id=previous_connection_id,
                next_outbound_sequence=_require_non_negative_integer(
                    resume,
                    "next_outbound_sequence",
                ),
                next_inbound_sequence=_require_non_negative_integer(
                    resume,
                    "next_inbound_sequence",
                ),
            ),
        )

    def decode_heartbeat(self, payload: object) -> ConnectorHeartbeat:
        _validate_json_value(payload)
        if not isinstance(payload, dict):
            _raise("invalid_envelope")
        required_fields = {
            "connection_id",
            "sender_role",
            "observed_at",
            "next_outbound_sequence",
            "next_inbound_sequence",
            "session_state",
        }
        if not required_fields <= set(payload):
            _raise("invalid_envelope")
        if not set(payload) <= required_fields | {"extensions"}:
            _raise("invalid_envelope")
        if "extensions" in payload:
            _validate_extensions(payload["extensions"])
        connection_id = _require_string(payload, "connection_id")
        _validate_uuid(connection_id)
        sender_role = payload["sender_role"]
        if sender_role not in {"connector", "cloud"}:
            _raise("invalid_envelope")
        observed_at = _require_string(
            payload,
            "observed_at",
            maximum=MAX_STRING_BYTES,
        )
        _validate_datetime(observed_at)
        session_state = payload["session_state"]
        if session_state not in {"active", "reconciling", "draining"}:
            _raise("invalid_envelope")
        return ConnectorHeartbeat(
            connection_id=connection_id,
            sender_role=sender_role,
            observed_at=observed_at,
            next_outbound_sequence=_require_non_negative_integer(
                payload,
                "next_outbound_sequence",
            ),
            next_inbound_sequence=_require_non_negative_integer(
                payload,
                "next_inbound_sequence",
            ),
            session_state=session_state,
        )

    def decode_session_snapshot(self, payload: object) -> ConnectorObserverSnapshot:
        if isinstance(payload, dict) and payload.get("observer_contract") == 2:
            return self._decode_session_snapshot_v2(payload)
        _validate_json_value(payload)
        if not isinstance(payload, dict):
            _raise("invalid_envelope")
        required = {
            "profile",
            "runtime_generation",
            "session_key",
            "runtime_session_id",
            "running",
            "status",
            "event_sequence",
            "snapshot_event_sequence",
            "messages",
            "inflight",
            "replay_events",
        }
        if not required <= set(payload) or not set(payload) <= required | {
            "extensions"
        }:
            _raise("invalid_envelope")
        if "extensions" in payload:
            _validate_extensions(payload["extensions"])
        profile = _bounded_text(payload, "profile", maximum=128)
        if not _PROFILE.fullmatch(profile):
            _raise("invalid_envelope")
        runtime_generation = _bounded_text(
            payload,
            "runtime_generation",
            maximum=128,
        )
        session_key = _bounded_text(payload, "session_key", maximum=256)
        runtime_session_id = _bounded_text(
            payload,
            "runtime_session_id",
            maximum=256,
        )
        status = _bounded_text(payload, "status", maximum=64)
        running = payload["running"]
        if type(running) is not bool or running != (status in _RUNNING_STATUSES):
            _raise("invalid_envelope")
        snapshot_sequence = _require_non_negative_integer(
            payload,
            "snapshot_event_sequence",
        )
        event_sequence = _require_non_negative_integer(payload, "event_sequence")
        if snapshot_sequence > event_sequence:
            _raise("invalid_envelope")

        raw_messages = payload["messages"]
        if not isinstance(raw_messages, list) or len(raw_messages) > 500:
            _raise("invalid_envelope")
        messages: list[dict[str, object]] = []
        for message in raw_messages:
            if not isinstance(message, dict):
                _raise("invalid_envelope")
            if "role" not in message or not set(message) <= {"role", "content"}:
                _raise("invalid_envelope")
            _bounded_text(message, "role", maximum=64)
            if "content" in message and message["content"] is not None:
                _bounded_text(message, "content", maximum=MAX_STRING_BYTES)
            messages.append(dict(message))

        inflight = payload["inflight"]
        if not isinstance(inflight, dict) or set(inflight) != {
            "user",
            "assistant",
            "streaming",
            "error",
        }:
            _raise("invalid_envelope")
        for field in ("user", "assistant"):
            if inflight[field] is not None:
                _bounded_text(inflight, field, maximum=MAX_STRING_BYTES)
        if type(inflight["streaming"]) is not bool:
            _raise("invalid_envelope")
        if inflight["error"] is not None:
            _bounded_text(inflight, "error", maximum=4096)

        raw_replay = payload["replay_events"]
        if not isinstance(raw_replay, list) or len(raw_replay) > MAX_ARRAY_ITEMS:
            _raise("invalid_envelope")
        replay: list[ConnectorObserverEvent] = []
        previous = snapshot_sequence
        for raw_event in raw_replay:
            event = _decode_observer_event(
                raw_event,
                inherited_profile=profile,
                inherited_generation=runtime_generation,
            )
            if (
                event.session_key != session_key
                or event.runtime_session_id != runtime_session_id
                or event.event_sequence_start != previous + 1
                or event.event_sequence > event_sequence
            ):
                _raise("invalid_envelope")
            replay.append(event)
            previous = event.event_sequence
        if previous != event_sequence:
            _raise("invalid_envelope")
        return ConnectorObserverSnapshot(
            profile=profile,
            runtime_generation=runtime_generation,
            session_key=session_key,
            runtime_session_id=runtime_session_id,
            running=running,
            status=status,
            event_sequence=event_sequence,
            snapshot_event_sequence=snapshot_sequence,
            messages=tuple(messages),
            inflight=dict(inflight),
            replay_events=tuple(replay),
        )

    def _decode_session_snapshot_v2(
        self,
        payload: dict[str, object],
    ) -> ConnectorObserverSnapshot:
        try:
            decoded = require_payload("session-snapshot-v2", payload)
        except ObserverV2ContractError:
            _raise("invalid_envelope")
        snapshot_sequence = int(decoded["snapshot_event_sequence"])
        event_sequence = int(decoded["event_sequence"])
        replay: list[ConnectorObserverEvent] = []
        previous = snapshot_sequence
        for raw_event in decoded["replay_events"]:
            event = _decode_observer_event_v2(raw_event)
            if (
                event.profile != decoded["profile"]
                or event.runtime_generation != decoded["runtime_generation"]
                or event.session_key != decoded["session_key"]
                or event.runtime_session_id != decoded["runtime_session_id"]
                or event.event_sequence_start != previous + 1
                or event.event_sequence > event_sequence
            ):
                _raise("invalid_envelope")
            replay.append(event)
            previous = event.event_sequence
        if previous != event_sequence:
            _raise("invalid_envelope")
        return ConnectorObserverSnapshot(
            profile=str(decoded["profile"]),
            runtime_generation=str(decoded["runtime_generation"]),
            session_key=str(decoded["session_key"]),
            runtime_session_id=str(decoded["runtime_session_id"]),
            running=bool(decoded["running"]),
            status=str(decoded["status"]),
            event_sequence=event_sequence,
            snapshot_event_sequence=snapshot_sequence,
            messages=tuple(dict(item) for item in decoded["messages"]),
            inflight=dict(decoded["inflight"]),
            replay_events=tuple(replay),
            todo_sections=tuple(dict(item) for item in decoded["todo_sections"]),
            subagents=tuple(dict(item) for item in decoded["subagents"]),
            tools=tuple(dict(item) for item in decoded["tools"]),
            terminals=tuple(dict(item) for item in decoded["terminals"]),
            observer_contract=2,
        )

    def decode_session_event(self, payload: object) -> ConnectorObserverEvent:
        if isinstance(payload, dict) and payload.get("observer_contract") == 2:
            return _decode_observer_event_v2(payload)
        return _decode_observer_event(payload)

    def decode_session_catalog_snapshot_page(
        self,
        payload: object,
    ) -> ConnectorSessionCatalogSnapshotPage:
        value = _catalog_object(
            payload,
            required={
                "profile",
                "runtime_generation",
                "snapshot_id",
                "catalog_revision",
                "page_index",
                "is_last",
                "sessions",
            },
        )
        profile = _catalog_profile(value)
        runtime_generation = _bounded_text(
            value, "runtime_generation", maximum=128
        )
        snapshot_id = _bounded_text(value, "snapshot_id", maximum=36)
        _validate_uuid(snapshot_id)
        catalog_revision = _catalog_integer(value, "catalog_revision", minimum=0)
        page_index = _catalog_integer(value, "page_index", minimum=0)
        is_last = value["is_last"]
        if type(is_last) is not bool:
            _raise("invalid_envelope")
        sessions = value["sessions"]
        if not isinstance(sessions, list) or len(sessions) > 128:
            _raise("invalid_envelope")
        if not is_last and not sessions:
            _raise("invalid_envelope")
        decoded = tuple(_decode_catalog_entry(item) for item in sessions)
        keys = tuple(item.session_key for item in decoded)
        if len(keys) != len(set(keys)):
            _raise("invalid_envelope")
        return ConnectorSessionCatalogSnapshotPage(
            profile=profile,
            runtime_generation=runtime_generation,
            snapshot_id=snapshot_id,
            catalog_revision=catalog_revision,
            page_index=page_index,
            is_last=is_last,
            sessions=decoded,
        )

    def decode_session_catalog_event(
        self,
        payload: object,
    ) -> ConnectorSessionCatalogEvent:
        value = _catalog_object(
            payload,
            required={
                "profile",
                "runtime_generation",
                "catalog_sequence",
                "action",
                "entry",
            },
        )
        action = value["action"]
        if action not in {"upsert", "remove"}:
            _raise("invalid_envelope")
        return ConnectorSessionCatalogEvent(
            profile=_catalog_profile(value),
            runtime_generation=_bounded_text(
                value, "runtime_generation", maximum=128
            ),
            catalog_sequence=_catalog_integer(
                value, "catalog_sequence", minimum=1
            ),
            action=action,
            entry=_decode_catalog_entry(value["entry"]),
        )

    def encode_connector_frame(self, envelope: CloudEnvelope) -> str:
        value: dict[str, object] = {
            "contract_version": envelope.contract_version,
            "message_id": envelope.message_id,
            "message_type": envelope.message_type,
            "tenant_id": envelope.tenant_id,
            "device_id": envelope.device_id,
            "sequence": envelope.sequence,
            "sent_at": envelope.sent_at,
            "payload": envelope.payload,
        }
        if envelope.traceparent is not None:
            value["traceparent"] = envelope.traceparent
        if envelope.idempotency_key is not None:
            value["idempotency_key"] = envelope.idempotency_key
        if envelope.extensions is not None:
            value["extensions"] = envelope.extensions
        try:
            text = json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        except (TypeError, ValueError):
            _raise("invalid_envelope")
        self.decode_connector_frame(text)
        return text


def _catalog_object(
    payload: object,
    *,
    required: set[str],
) -> dict[str, object]:
    _validate_json_value(payload)
    if not isinstance(payload, dict):
        _raise("invalid_envelope")
    if not required <= set(payload) or not set(payload) <= required | {"extensions"}:
        _raise("invalid_envelope")
    if "extensions" in payload:
        _validate_extensions(payload["extensions"])
    return dict(payload)


def _catalog_profile(value: dict[str, object]) -> str:
    profile = _bounded_text(value, "profile", maximum=128)
    if not _PROFILE.fullmatch(profile):
        _raise("invalid_envelope")
    return profile


def _catalog_integer(
    value: dict[str, object],
    field: str,
    *,
    minimum: int,
) -> int:
    item = value[field]
    if type(item) is not int or not minimum <= item <= _MAX_SAFE_INTEGER:
        _raise("invalid_envelope")
    return item


def _decode_catalog_entry(payload: object) -> SessionCatalogEntry:
    _validate_json_value(payload)
    if not isinstance(payload, dict) or set(payload) != {
        "session_key",
        "surface",
        "authority_revision",
        "available_actions",
    }:
        _raise("invalid_envelope")
    session_key = _bounded_text(payload, "session_key", maximum=256)
    surface = _bounded_text(payload, "surface", maximum=64)
    authority_revision = _catalog_integer(
        payload, "authority_revision", minimum=1
    )
    actions = payload["available_actions"]
    if not isinstance(actions, list) or len(actions) > 5:
        _raise("invalid_envelope")
    if any(not isinstance(action, str) for action in actions):
        _raise("invalid_envelope")
    if len(actions) != len(set(actions)) or not set(actions) <= _SESSION_CATALOG_ACTIONS:
        _raise("invalid_envelope")
    return SessionCatalogEntry(
        session_key=session_key,
        surface=surface,
        authority_revision=authority_revision,
        available_actions=tuple(actions),
    )
