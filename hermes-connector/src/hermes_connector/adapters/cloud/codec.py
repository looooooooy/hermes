from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any
from uuid import UUID

from hermes_connector.contracts.mobile_control import CONTROL_ERROR_REASONS
from hermes_connector.contracts.observer_v2 import (
    ObserverV2ContractError,
    load_observer_v2_contracts,
)
from hermes_connector.domain.canonical_json import (
    CanonicalJSONError,
    canonical_json_bytes,
)
from hermes_connector.domain.cloud_protocol import (
    CommandDelivery,
    CommandReceipt,
    CommandResult,
    ConnectorHeartbeat,
    ConnectorHello,
    ConnectorWelcome,
    ResumePosition,
)
from hermes_connector.domain.contract_messages import CloudEnvelope
from hermes_connector.domain.observer import (
    ObserverEvent,
    SessionEvent,
    SessionObserveClose,
    SessionObserveOpen,
    SessionSnapshot,
    StreamAck,
    StreamNack,
)
from hermes_connector.domain.owner_control import (
    OwnerControlRequest,
    OwnerControlResponse,
)
from hermes_connector.domain.session_catalog import (
    SessionCatalogAck,
    SessionCatalogEntry,
    SessionCatalogEvent,
    SessionCatalogNack,
    SessionCatalogSnapshotPage,
)

MAX_CLOUD_FRAME_BYTES = 262_144
MAX_JSON_STRING_BYTES = 131_072
MAX_JSON_DEPTH = 32
MAX_JSON_COLLECTION_ITEMS = 1_024
_MAX_CAPABILITIES = 64
_MAX_EXTENSIONS = 16
_PROFILE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_OBSERVER_PROFILE = re.compile(r"^[A-Za-z0-9_.-]+$")
_COMMAND_METHODS = frozenset({"prompt.submit", "session.interrupt"})
_OWNER_ACTION_METHODS = frozenset(
    {
        "prompt.submit",
        "session.interrupt",
        "session.steer",
        "approval.respond",
        "clarify.respond",
    }
)
_COMMAND_RESULT_STATES = frozenset({"succeeded", "failed", "unknown"})
_COMMAND_ERROR_CODES = frozenset(
    {
        "control_role_required",
        "control_contract_unsupported",
        "live_runtime_unavailable",
        "controller_conflict",
        "lease_required",
        "lease_expired",
        "lease_mismatch",
        "request_id_payload_conflict",
        "pending_request_conflict",
        "method_not_allowed",
        "command_unknown",
        "revision_conflict",
        "session_binding_mismatch",
        "invalid_pending_response",
        "owner_adapter_unavailable",
        "relay_overloaded",
        "internal_temporary",
    }
)
_CONTROL_OPERATIONS = frozenset(
    {
        "control.transport.open",
        "session.control.acquire",
        "session.control.renew",
        "session.control.release",
        "session.control.status",
        "session.command.status",
        "prompt.submit",
        "session.interrupt",
        "session.steer",
        "approval.respond",
        "clarify.respond",
        "control.transport.close",
    }
)
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
_MERGEABLE_OBSERVER_TYPES = frozenset(
    {
        "agent.terminal.output",
        "message.delta",
        "reasoning.delta",
        "status.update",
        "thinking.delta",
        "tool.output.delta",
    }
)
_STREAM_RECEIPT_FIELDS = {
    "observer_message_id",
    "payload_digest",
    "connector_sequence",
    "observer_message_type",
    "profile",
    "session_key",
    "runtime_generation",
    "runtime_session_id",
    "event_sequence",
}
_CONTROL_ERROR_REASONS = CONTROL_ERROR_REASONS
_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_SEMVER = re.compile(
    r"^[0-9]+\.[0-9]+\.[0-9]+"
    r"(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$"
)
_UTC = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T"
    r"[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?Z$"
)
_TRACEPARENT = re.compile(r"^00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}$")
_EXTENSION = re.compile(r"^[a-z][a-z0-9]*(?:\.[a-z0-9][a-z0-9-]*)+$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
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
        "session.catalog.snapshot.page",
        "session.catalog.event",
        "session.catalog.ack",
        "session.catalog.nack",
        "session.snapshot",
        "session.event",
        "session.observe.open",
        "session.observe.close",
        "stream.ack",
        "stream.nack",
        "session.snapshot.v2",
        "session.event.v2",
        "session.observe.open.v2",
        "session.observe.close.v2",
        "stream.ack.v2",
        "stream.nack.v2",
        "file.transfer",
        "a2a.message",
        "view.card.invalidate",
    }
)


class InvalidCloudFrame(ValueError):
    """A Connector Protocol frame failed strict contract validation."""


class ConnectorProtocolCodec:
    def decode_session_catalog_snapshot_page_payload(
        self,
        payload: Mapping[str, object],
    ) -> SessionCatalogSnapshotPage:
        return self.decode_session_catalog_snapshot_page(_encode(payload))

    def decode_session_catalog_event_payload(
        self,
        payload: Mapping[str, object],
    ) -> SessionCatalogEvent:
        return self.decode_session_catalog_event(_encode(payload))

    def decode_session_catalog_ack_payload(
        self,
        payload: Mapping[str, object],
    ) -> SessionCatalogAck:
        return self.decode_session_catalog_ack(_encode(payload))

    def decode_session_catalog_nack_payload(
        self,
        payload: Mapping[str, object],
    ) -> SessionCatalogNack:
        return self.decode_session_catalog_nack(_encode(payload))

    def session_catalog_snapshot_page_payload(
        self,
        message: SessionCatalogSnapshotPage,
    ) -> Mapping[str, object]:
        return _freeze(_object(self.encode_session_catalog_snapshot_page(message)))

    def session_catalog_event_payload(
        self,
        message: SessionCatalogEvent,
    ) -> Mapping[str, object]:
        return _freeze(_object(self.encode_session_catalog_event(message)))

    def decode_session_snapshot_v2_payload(
        self,
        payload: Mapping[str, object],
    ) -> SessionSnapshot:
        return self.decode_session_snapshot_v2(_encode(payload))

    def decode_session_event_v2_payload(
        self,
        payload: Mapping[str, object],
    ) -> SessionEvent:
        return self.decode_session_event_v2(_encode(payload))

    def decode_session_observe_open_v2_payload(
        self,
        payload: Mapping[str, object],
    ) -> SessionObserveOpen:
        return self.decode_session_observe_open_v2(_encode(payload))

    def decode_session_observe_close_v2_payload(
        self,
        payload: Mapping[str, object],
    ) -> SessionObserveClose:
        return self.decode_session_observe_close_v2(_encode(payload))

    def decode_stream_ack_v2_payload(
        self,
        payload: Mapping[str, object],
    ) -> StreamAck:
        return self.decode_stream_ack_v2(_encode(payload))

    def decode_stream_nack_v2_payload(
        self,
        payload: Mapping[str, object],
    ) -> StreamNack:
        return self.decode_stream_nack_v2(_encode(payload))

    def decode_session_observe_open_payload(
        self,
        payload: Mapping[str, object],
    ) -> SessionObserveOpen:
        return self.decode_session_observe_open(_encode(payload))

    def decode_session_observe_close_payload(
        self,
        payload: Mapping[str, object],
    ) -> SessionObserveClose:
        return self.decode_session_observe_close(_encode(payload))

    def decode_stream_ack_payload(
        self,
        payload: Mapping[str, object],
    ) -> StreamAck:
        return self.decode_stream_ack(_encode(payload))

    def decode_stream_nack_payload(
        self,
        payload: Mapping[str, object],
    ) -> StreamNack:
        return self.decode_stream_nack(_encode(payload))

    def decode_session_snapshot_payload(
        self,
        payload: Mapping[str, object],
    ) -> SessionSnapshot:
        return self.decode_session_snapshot(_encode(payload))

    def decode_session_event_payload(
        self,
        payload: Mapping[str, object],
    ) -> SessionEvent:
        return self.decode_session_event(_encode(payload))

    def session_snapshot_payload(
        self,
        message: SessionSnapshot,
    ) -> Mapping[str, object]:
        return _freeze(_object(self.encode_session_snapshot(message)))

    def session_event_payload(
        self,
        message: SessionEvent,
    ) -> Mapping[str, object]:
        return _freeze(_object(self.encode_session_event(message)))

    def decode_control_request_payload(
        self,
        payload: Mapping[str, object],
    ) -> OwnerControlRequest:
        return self.decode_control_request(_encode(payload))

    def decode_control_response_payload(
        self,
        payload: Mapping[str, object],
    ) -> OwnerControlResponse:
        return self.decode_control_response(_encode(payload))

    def control_response_payload(
        self,
        message: OwnerControlResponse,
    ) -> Mapping[str, object]:
        return _freeze(_object(self.encode_control_response(message)))

    def decode_command_delivery_payload(
        self,
        payload: Mapping[str, object],
    ) -> CommandDelivery:
        return self.decode_command_delivery(_encode(payload))

    def decode_command_receipt_payload(
        self,
        payload: Mapping[str, object],
    ) -> CommandReceipt:
        return self.decode_command_receipt(_encode(payload))

    def decode_command_result_payload(
        self,
        payload: Mapping[str, object],
    ) -> CommandResult:
        return self.decode_command_result(_encode(payload))

    def command_receipt_payload(
        self,
        message: CommandReceipt,
    ) -> Mapping[str, object]:
        return _freeze(_object(self.encode_command_receipt(message)))

    def command_result_payload(
        self,
        message: CommandResult,
    ) -> Mapping[str, object]:
        return _freeze(_object(self.encode_command_result(message)))

    def encode_command_delivery(self, message: CommandDelivery) -> bytes:
        value: dict[str, object] = {
            "command_id": str(message.command_id),
            "connector_instance_id": str(message.connector_instance_id),
            "client_instance_id": str(message.client_instance_id),
            "session_key": message.session_key,
            "profile": message.profile,
            "client_request_id": message.client_request_id,
            "method": message.method,
            "params": _thaw(message.params),
            "issued_at": _format_instant(message.issued_at),
            "expires_at": _format_instant(message.expires_at),
            "revision": message.revision,
        }
        _include_extensions(value, message.extensions)
        encoded = _encode(value)
        self.decode_command_delivery(encoded)
        return encoded

    def hello_payload(
        self,
        message: ConnectorHello,
    ) -> Mapping[str, object]:
        return _freeze(_object(self.encode_hello(message)))

    def decode_welcome_payload(
        self,
        payload: Mapping[str, object],
    ) -> ConnectorWelcome:
        return self.decode_welcome(_encode(payload))

    def decode_hello_payload(
        self,
        payload: Mapping[str, object],
    ) -> ConnectorHello:
        return self.decode_hello(_encode(payload))

    def decode_heartbeat_payload(
        self,
        payload: Mapping[str, object],
    ) -> ConnectorHeartbeat:
        return self.decode_heartbeat(_encode(payload))

    def heartbeat_payload(
        self,
        message: ConnectorHeartbeat,
    ) -> Mapping[str, object]:
        return _freeze(_object(self.encode_heartbeat(message)))

    def decode_envelope(self, frame: bytes) -> CloudEnvelope:
        value = _object(frame)
        _fields(
            value,
            required={
                "contract_version",
                "message_id",
                "message_type",
                "tenant_id",
                "device_id",
                "sequence",
                "sent_at",
                "payload",
            },
            optional={"traceparent", "idempotency_key", "extensions"},
        )
        if value["contract_version"] != 1 or type(value["contract_version"]) is not int:
            raise InvalidCloudFrame("contract_version must be 1")
        message_type = _text(value["message_type"], "message_type", 128)
        if message_type not in _MESSAGE_TYPES:
            raise InvalidCloudFrame("message_type is not in Connector Protocol v1")
        payload = value["payload"]
        if not isinstance(payload, dict) or len(payload) > 1024:
            raise InvalidCloudFrame("payload must be a bounded object")
        sequence = _nonnegative(value["sequence"], "sequence")
        return CloudEnvelope(
            contract_version=1,
            message_id=_uuid(value["message_id"], "message_id"),
            message_type=message_type,
            tenant_id=_text(value["tenant_id"], "tenant_id", 128),
            device_id=_text(value["device_id"], "device_id", 128),
            sequence=sequence,
            sent_at=_instant(value["sent_at"], "sent_at"),
            payload=_freeze(payload),
            traceparent=_optional_traceparent(value),
            idempotency_key=_optional_text(value, "idempotency_key", 128),
            extensions=_extensions(value.get("extensions")),
        )

    def decode_hello(self, frame: bytes) -> ConnectorHello:
        value = _object(frame)
        _fields(
            value,
            required={
                "connector_instance_id",
                "connector_version",
                "runtime_generation",
                "required_capabilities",
                "optional_capabilities",
                "resume",
            },
            optional={"extensions"},
        )
        required = _capabilities(
            value["required_capabilities"],
            "required_capabilities",
        )
        optional = _capabilities(
            value["optional_capabilities"],
            "optional_capabilities",
        )
        _disjoint(required, optional)
        version = _text(value["connector_version"], "connector_version", 64)
        if len(version) < 5 or _SEMVER.fullmatch(version) is None:
            raise InvalidCloudFrame("connector_version must be SemVer")
        return ConnectorHello(
            connector_instance_id=_uuid(
                value["connector_instance_id"],
                "connector_instance_id",
            ),
            connector_version=version,
            runtime_generation=_text(
                value["runtime_generation"],
                "runtime_generation",
                128,
            ),
            required_capabilities=required,
            optional_capabilities=optional,
            resume=_resume(value["resume"]),
            extensions=_extensions(value.get("extensions")),
        )

    def decode_welcome(self, frame: bytes) -> ConnectorWelcome:
        value = _object(frame)
        _fields(
            value,
            required={
                "connection_id",
                "server_generation",
                "server_time",
                "accepted_capabilities",
                "unavailable_optional_capabilities",
                "resume_decision",
                "next_connector_sequence",
                "next_cloud_sequence",
                "heartbeat_interval_ms",
                "max_in_flight",
            },
            optional={"extensions"},
        )
        accepted = _capabilities(
            value["accepted_capabilities"],
            "accepted_capabilities",
        )
        unavailable = _capabilities(
            value["unavailable_optional_capabilities"],
            "unavailable_optional_capabilities",
        )
        _disjoint(accepted, unavailable)
        resume_decision = value["resume_decision"]
        if resume_decision not in {"fresh", "resumed", "reset_required"}:
            raise InvalidCloudFrame("resume_decision is invalid")
        heartbeat = _integer_range(
            value["heartbeat_interval_ms"],
            "heartbeat_interval_ms",
            5_000,
            120_000,
        )
        window = _integer_range(
            value["max_in_flight"],
            "max_in_flight",
            1,
            256,
        )
        return ConnectorWelcome(
            connection_id=_uuid(value["connection_id"], "connection_id"),
            server_generation=_text(
                value["server_generation"],
                "server_generation",
                128,
            ),
            server_time=_instant(value["server_time"], "server_time"),
            accepted_capabilities=accepted,
            unavailable_optional_capabilities=unavailable,
            resume_decision=str(resume_decision),
            next_connector_sequence=_nonnegative(
                value["next_connector_sequence"],
                "next_connector_sequence",
            ),
            next_cloud_sequence=_nonnegative(
                value["next_cloud_sequence"],
                "next_cloud_sequence",
            ),
            heartbeat_interval_ms=heartbeat,
            max_in_flight=window,
            extensions=_extensions(value.get("extensions")),
        )

    def decode_heartbeat(self, frame: bytes) -> ConnectorHeartbeat:
        value = _object(frame)
        _fields(
            value,
            required={
                "connection_id",
                "sender_role",
                "observed_at",
                "next_outbound_sequence",
                "next_inbound_sequence",
                "session_state",
            },
            optional={"extensions"},
        )
        sender_role = value["sender_role"]
        if sender_role not in {"connector", "cloud"}:
            raise InvalidCloudFrame("sender_role is invalid")
        session_state = value["session_state"]
        if session_state not in {"active", "reconciling", "draining"}:
            raise InvalidCloudFrame("session_state is invalid")
        return ConnectorHeartbeat(
            connection_id=_uuid(value["connection_id"], "connection_id"),
            sender_role=str(sender_role),
            observed_at=_instant(value["observed_at"], "observed_at"),
            next_outbound_sequence=_nonnegative(
                value["next_outbound_sequence"],
                "next_outbound_sequence",
            ),
            next_inbound_sequence=_nonnegative(
                value["next_inbound_sequence"],
                "next_inbound_sequence",
            ),
            session_state=str(session_state),
            extensions=_extensions(value.get("extensions")),
        )

    def decode_command_delivery(self, frame: bytes) -> CommandDelivery:
        value = _object(frame)
        _fields(
            value,
            required={
                "command_id",
                "connector_instance_id",
                "client_instance_id",
                "session_key",
                "profile",
                "client_request_id",
                "method",
                "params",
                "issued_at",
                "expires_at",
                "revision",
            },
            optional={"extensions"},
        )
        method = _text(value["method"], "method", 128)
        if method not in _COMMAND_METHODS:
            raise InvalidCloudFrame("method is not allowed by command contract v1")
        issued_at = _instant(value["issued_at"], "issued_at")
        expires_at = _instant(value["expires_at"], "expires_at")
        if expires_at <= issued_at:
            raise InvalidCloudFrame("expires_at must be after issued_at")
        profile = _text(value["profile"], "profile", 128)
        if _PROFILE.fullmatch(profile) is None:
            raise InvalidCloudFrame("profile is invalid")
        return CommandDelivery(
            command_id=_uuid(value["command_id"], "command_id"),
            connector_instance_id=_uuid(
                value["connector_instance_id"],
                "connector_instance_id",
            ),
            client_instance_id=_uuid(
                value["client_instance_id"],
                "client_instance_id",
            ),
            session_key=_text(value["session_key"], "session_key", 256),
            profile=profile,
            client_request_id=_text(
                value["client_request_id"],
                "client_request_id",
                128,
            ),
            method=method,
            params=_command_params(value["params"], method),
            issued_at=issued_at,
            expires_at=expires_at,
            revision=_integer_range(value["revision"], "revision", 1, 2**63 - 1),
            extensions=_extensions(value.get("extensions")),
        )

    def decode_session_snapshot_v2(self, frame: bytes) -> SessionSnapshot:
        value = _observer_v2_object(frame, "session.snapshot.v2")
        replay = tuple(
            ObserverEvent(
                type=event.type,
                session_id=event.session_id,
                session_key=event.session_key,
                event_sequence=event.event_sequence,
                event_sequence_start=event.event_sequence_start,
                payload=event.payload,
                observer_contract=2,
            )
            for event in (
                _session_event_v2(item) for item in value["replay_events"]
            )
        )
        return SessionSnapshot(
            profile=str(value["profile"]),
            runtime_generation=str(value["runtime_generation"]),
            session_key=str(value["session_key"]),
            runtime_session_id=str(value["runtime_session_id"]),
            running=bool(value["running"]),
            status=str(value["status"]),
            event_sequence=int(value["event_sequence"]),
            snapshot_event_sequence=int(value["snapshot_event_sequence"]),
            messages=tuple(_freeze(item) for item in value["messages"]),
            inflight=_freeze(value["inflight"]),
            replay_events=replay,
            extensions=_extensions(value.get("extensions")),
            observer_contract=2,
            todo_sections=tuple(_freeze(item) for item in value["todo_sections"]),
            subagents=tuple(_freeze(item) for item in value["subagents"]),
            tools=tuple(_freeze(item) for item in value["tools"]),
            terminals=tuple(_freeze(item) for item in value["terminals"]),
        )

    def decode_session_catalog_snapshot_page(
        self,
        frame: bytes,
    ) -> SessionCatalogSnapshotPage:
        value = _object(frame)
        _fields(
            value,
            required={
                "profile",
                "runtime_generation",
                "snapshot_id",
                "catalog_revision",
                "page_index",
                "is_last",
                "sessions",
            },
            optional={"extensions"},
        )
        is_last = value["is_last"]
        if type(is_last) is not bool:
            raise InvalidCloudFrame("is_last must be a boolean")
        sessions_value = value["sessions"]
        if not isinstance(sessions_value, list) or len(sessions_value) > 128:
            raise InvalidCloudFrame("catalog sessions must be a bounded array")
        if not is_last and not sessions_value:
            raise InvalidCloudFrame("non-terminal catalog page must not be empty")
        return SessionCatalogSnapshotPage(
            profile=_observer_profile(value["profile"]),
            runtime_generation=_text(
                value["runtime_generation"], "runtime_generation", 128
            ),
            snapshot_id=_uuid(value["snapshot_id"], "snapshot_id"),
            catalog_revision=_integer_range(
                value["catalog_revision"], "catalog_revision", 0, 2**53 - 1
            ),
            page_index=_integer_range(
                value["page_index"], "page_index", 0, 2**53 - 1
            ),
            is_last=is_last,
            sessions=tuple(_session_catalog_entry(item) for item in sessions_value),
        )

    def decode_session_catalog_event(self, frame: bytes) -> SessionCatalogEvent:
        value = _object(frame)
        _fields(
            value,
            required={
                "profile",
                "runtime_generation",
                "catalog_sequence",
                "action",
                "entry",
            },
            optional={"extensions"},
        )
        action = value["action"]
        if action not in {"upsert", "remove"}:
            raise InvalidCloudFrame("catalog action is invalid")
        return SessionCatalogEvent(
            profile=_observer_profile(value["profile"]),
            runtime_generation=_text(
                value["runtime_generation"], "runtime_generation", 128
            ),
            catalog_sequence=_integer_range(
                value["catalog_sequence"], "catalog_sequence", 1, 2**53 - 1
            ),
            action=str(action),
            entry=_session_catalog_entry(value["entry"]),
        )

    def decode_session_catalog_ack(self, frame: bytes) -> SessionCatalogAck:
        value = _object(frame)
        common = {
            "profile",
            "runtime_generation",
            "acked_message_id",
            "acked_payload_digest",
            "acked_connector_sequence",
            "ack_kind",
        }
        optional = {
            "snapshot_id",
            "catalog_revision",
            "page_index",
            "is_last",
            "catalog_sequence",
            "extensions",
        }
        _fields(value, required=common, optional=optional)
        digest = _canonical_digest(value["acked_payload_digest"], "acked_payload_digest")
        ack_kind = value["ack_kind"]
        if ack_kind == "snapshot_committed":
            required_snapshot = {"snapshot_id", "catalog_revision", "page_index", "is_last"}
            if not required_snapshot.issubset(value) or "catalog_sequence" in value:
                raise InvalidCloudFrame("catalog snapshot ACK position is invalid")
            if value["is_last"] is not True:
                raise InvalidCloudFrame("catalog snapshot ACK must bind the final page")
            snapshot_id = _uuid(value["snapshot_id"], "snapshot_id")
            catalog_revision = _integer_range(
                value["catalog_revision"], "catalog_revision", 0, 2**53 - 1
            )
            page_index = _integer_range(value["page_index"], "page_index", 0, 2**53 - 1)
            is_last: bool | None = True
            catalog_sequence: int | None = None
        elif ack_kind == "event_applied":
            if "catalog_sequence" not in value or any(
                field in value
                for field in ("snapshot_id", "catalog_revision", "page_index", "is_last")
            ):
                raise InvalidCloudFrame("catalog event ACK position is invalid")
            snapshot_id = None
            catalog_revision = None
            page_index = None
            is_last = None
            catalog_sequence = _integer_range(
                value["catalog_sequence"], "catalog_sequence", 1, 2**53 - 1
            )
        else:
            raise InvalidCloudFrame("catalog ACK kind is invalid")
        return SessionCatalogAck(
            profile=_observer_profile(value["profile"]),
            runtime_generation=_text(value["runtime_generation"], "runtime_generation", 128),
            acked_message_id=_uuid(value["acked_message_id"], "acked_message_id"),
            acked_payload_digest=digest,
            acked_connector_sequence=_integer_range(
                value["acked_connector_sequence"], "acked_connector_sequence", 0, 2**53 - 1
            ),
            ack_kind=str(ack_kind),
            snapshot_id=snapshot_id,
            catalog_revision=catalog_revision,
            page_index=page_index,
            is_last=is_last,
            catalog_sequence=catalog_sequence,
        )

    def decode_session_catalog_nack(self, frame: bytes) -> SessionCatalogNack:
        value = _object(frame)
        _fields(
            value,
            required={
                "profile",
                "runtime_generation",
                "rejected_message_id",
                "rejected_payload_digest",
                "rejected_connector_sequence",
                "reason",
                "reset_required",
                "snapshot_id",
                "expected_page_index",
                "expected_catalog_sequence",
            },
            optional={"extensions"},
        )
        if value["reset_required"] is not True:
            raise InvalidCloudFrame("catalog NACK must require reset")
        reason = value["reason"]
        if reason not in {
            "page_gap",
            "event_gap",
            "runtime_mismatch",
            "stale_writer",
            "contract_mismatch",
            "revision_conflict",
        }:
            raise InvalidCloudFrame("catalog NACK reason is invalid")
        snapshot_id: UUID | None = None
        expected_page_index: int | None = None
        expected_catalog_sequence: int | None = None
        if reason in {"page_gap", "revision_conflict"}:
            snapshot_id = _uuid(value["snapshot_id"], "snapshot_id")
            expected_page_index = _integer_range(
                value["expected_page_index"], "expected_page_index", 0, 2**53 - 1
            )
            if value["expected_catalog_sequence"] is not None:
                raise InvalidCloudFrame("catalog NACK page position conflicts")
        elif reason == "event_gap":
            if value["snapshot_id"] is not None or value["expected_page_index"] is not None:
                raise InvalidCloudFrame("catalog NACK event position conflicts")
            expected_catalog_sequence = _integer_range(
                value["expected_catalog_sequence"],
                "expected_catalog_sequence",
                1,
                2**53 - 1,
            )
        elif any(
            value[field] is not None
            for field in ("snapshot_id", "expected_page_index", "expected_catalog_sequence")
        ):
            raise InvalidCloudFrame("catalog NACK position conflicts")
        return SessionCatalogNack(
            profile=_observer_profile(value["profile"]),
            runtime_generation=_text(value["runtime_generation"], "runtime_generation", 128),
            rejected_message_id=_uuid(value["rejected_message_id"], "rejected_message_id"),
            rejected_payload_digest=_canonical_digest(
                value["rejected_payload_digest"], "rejected_payload_digest"
            ),
            rejected_connector_sequence=_integer_range(
                value["rejected_connector_sequence"],
                "rejected_connector_sequence",
                0,
                2**53 - 1,
            ),
            reason=str(reason),
            snapshot_id=snapshot_id,
            expected_page_index=expected_page_index,
            expected_catalog_sequence=expected_catalog_sequence,
        )
    def decode_session_event_v2(self, frame: bytes) -> SessionEvent:
        return _session_event_v2(_observer_v2_object(frame, "session.event.v2"))

    def decode_session_observe_open_v2(self, frame: bytes) -> SessionObserveOpen:
        value = _observer_v2_object(frame, "session.observe.open.v2")
        return SessionObserveOpen(
            request_id=_uuid(value["request_id"], "request_id"),
            subscription_id=_uuid(value["subscription_id"], "subscription_id"),
            profile=str(value["profile"]),
            session_key=str(value["session_key"]),
            target_source="cloud_authorized_binding",
            requested_at=_instant(value["requested_at"], "requested_at"),
            extensions=_extensions(value.get("extensions")),
            observer_contract=2,
        )

    def decode_session_observe_close_v2(self, frame: bytes) -> SessionObserveClose:
        value = _observer_v2_object(frame, "session.observe.close.v2")
        return SessionObserveClose(
            request_id=_uuid(value["request_id"], "request_id"),
            subscription_id=_uuid(value["subscription_id"], "subscription_id"),
            profile=str(value["profile"]),
            session_key=str(value["session_key"]),
            target_source="cloud_authorized_binding",
            reason=str(value["reason"]),
            closed_at=_instant(value["closed_at"], "closed_at"),
            extensions=_extensions(value.get("extensions")),
            observer_contract=2,
        )

    def decode_stream_ack_v2(self, frame: bytes) -> StreamAck:
        value = _observer_v2_object(frame, "stream.ack.v2")
        return StreamAck(
            **_stream_receipt_identity_v2(value),
            committed_at=_instant(value["committed_at"], "committed_at"),
            extensions=_extensions(value.get("extensions")),
            observer_contract=2,
        )

    def decode_stream_nack_v2(self, frame: bytes) -> StreamNack:
        value = _observer_v2_object(frame, "stream.nack.v2")
        return StreamNack(
            **_stream_receipt_identity_v2(value),
            reason=str(value["reason"]),
            expected_event_sequence=int(value["expected_event_sequence"]),
            recovery=str(value["recovery"]),
            rejected_at=_instant(value["rejected_at"], "rejected_at"),
            extensions=_extensions(value.get("extensions")),
            observer_contract=2,
        )

    def decode_session_snapshot(self, frame: bytes) -> SessionSnapshot:
        value = _object(frame)
        _fields(
            value,
            required={
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
            },
            optional={"extensions"},
        )
        profile = _observer_profile(value["profile"])
        session_key = _text(value["session_key"], "session_key", 256)
        runtime_session_id = _text(
            value["runtime_session_id"],
            "runtime_session_id",
            256,
        )
        event_sequence = _nonnegative(value["event_sequence"], "event_sequence")
        snapshot_sequence = _nonnegative(
            value["snapshot_event_sequence"],
            "snapshot_event_sequence",
        )
        if snapshot_sequence > event_sequence:
            raise InvalidCloudFrame("snapshot_event_sequence exceeds event_sequence")
        replay = _observer_replay_events(
            value["replay_events"],
            session_key=session_key,
            runtime_session_id=runtime_session_id,
            snapshot_sequence=snapshot_sequence,
            event_sequence=event_sequence,
        )
        if type(value["running"]) is not bool:
            raise InvalidCloudFrame("running must be a boolean")
        return SessionSnapshot(
            profile=profile,
            runtime_generation=_text(
                value["runtime_generation"],
                "runtime_generation",
                128,
            ),
            session_key=session_key,
            runtime_session_id=runtime_session_id,
            running=value["running"],
            status=_text(value["status"], "status", 64),
            event_sequence=event_sequence,
            snapshot_event_sequence=snapshot_sequence,
            messages=_observer_messages(value["messages"]),
            inflight=_observer_inflight(value["inflight"]),
            replay_events=replay,
            extensions=_extensions(value.get("extensions")),
        )

    def decode_session_event(self, frame: bytes) -> SessionEvent:
        value = _object(frame)
        _fields(
            value,
            required={
                "profile",
                "runtime_generation",
                "session_key",
                "session_id",
                "type",
                "event_sequence",
                "payload",
            },
            optional={"event_sequence_start", "extensions"},
        )
        event = _observer_event(value)
        return SessionEvent(
            profile=_observer_profile(value["profile"]),
            runtime_generation=_text(
                value["runtime_generation"],
                "runtime_generation",
                128,
            ),
            session_key=event.session_key,
            session_id=event.session_id,
            type=event.type,
            event_sequence=event.event_sequence,
            event_sequence_start=event.event_sequence_start,
            payload=event.payload,
            extensions=_extensions(value.get("extensions")),
        )

    def decode_session_observe_open(self, frame: bytes) -> SessionObserveOpen:
        value = _object(frame)
        _fields(
            value,
            required={
                "request_id",
                "subscription_id",
                "profile",
                "session_key",
                "target_source",
                "requested_at",
            },
            optional={"extensions"},
        )
        _cloud_authorized_target(value)
        return SessionObserveOpen(
            request_id=_uuid(value["request_id"], "request_id"),
            subscription_id=_uuid(value["subscription_id"], "subscription_id"),
            profile=_observer_profile(value["profile"]),
            session_key=_text(value["session_key"], "session_key", 256),
            target_source="cloud_authorized_binding",
            requested_at=_instant(value["requested_at"], "requested_at"),
            extensions=_extensions(value.get("extensions")),
        )

    def decode_session_observe_close(self, frame: bytes) -> SessionObserveClose:
        value = _object(frame)
        _fields(
            value,
            required={
                "request_id",
                "subscription_id",
                "profile",
                "session_key",
                "target_source",
                "reason",
                "closed_at",
            },
            optional={"extensions"},
        )
        _cloud_authorized_target(value)
        reason = _text(value["reason"], "reason", 64)
        if reason not in {
            "client_unsubscribe",
            "subscription_replaced",
            "authorization_revoked",
            "gateway_shutdown",
            "reconciliation",
        }:
            raise InvalidCloudFrame("Observer close reason is invalid")
        return SessionObserveClose(
            request_id=_uuid(value["request_id"], "request_id"),
            subscription_id=_uuid(value["subscription_id"], "subscription_id"),
            profile=_observer_profile(value["profile"]),
            session_key=_text(value["session_key"], "session_key", 256),
            target_source="cloud_authorized_binding",
            reason=reason,
            closed_at=_instant(value["closed_at"], "closed_at"),
            extensions=_extensions(value.get("extensions")),
        )

    def decode_stream_ack(self, frame: bytes) -> StreamAck:
        value = _object(frame)
        _fields(
            value,
            required=_STREAM_RECEIPT_FIELDS | {"committed_at"},
            optional={"extensions"},
        )
        identity = _stream_receipt_identity(value)
        return StreamAck(
            **identity,
            committed_at=_instant(value["committed_at"], "committed_at"),
            extensions=_extensions(value.get("extensions")),
        )

    def decode_stream_nack(self, frame: bytes) -> StreamNack:
        value = _object(frame)
        _fields(
            value,
            required=_STREAM_RECEIPT_FIELDS
            | {
                "reason",
                "expected_event_sequence",
                "recovery",
                "rejected_at",
            },
            optional={"extensions"},
        )
        identity = _stream_receipt_identity(value)
        reason = _text(value["reason"], "reason", 64)
        if reason not in {
            "event_gap",
            "projection_conflict",
            "runtime_binding_mismatch",
        }:
            raise InvalidCloudFrame("stream.nack reason is invalid")
        recovery = _text(value["recovery"], "recovery", 32)
        if recovery not in {"send_snapshot", "stop_stream"}:
            raise InvalidCloudFrame("stream.nack recovery is invalid")
        return StreamNack(
            **identity,
            reason=reason,
            expected_event_sequence=_nonnegative(
                value["expected_event_sequence"],
                "expected_event_sequence",
            ),
            recovery=recovery,
            rejected_at=_instant(value["rejected_at"], "rejected_at"),
            extensions=_extensions(value.get("extensions")),
        )

    def decode_command_receipt(self, frame: bytes) -> CommandReceipt:
        value = _object(frame)
        _fields(
            value,
            required={
                "command_id",
                "message_id",
                "connector_instance_id",
                "client_instance_id",
                "session_key",
                "profile",
                "client_request_id",
                "method",
                "state",
                "stored_at",
                "revision",
            },
            optional={"extensions"},
        )
        identity = _command_identity(value)
        if value["state"] != "delivered":
            raise InvalidCloudFrame("command receipt state must be delivered")
        return CommandReceipt(
            **identity,
            message_id=_uuid(value["message_id"], "message_id"),
            state="delivered",
            stored_at=_instant(value["stored_at"], "stored_at"),
            revision=_integer_range(value["revision"], "revision", 1, 2**63 - 1),
            extensions=_extensions(value.get("extensions")),
        )

    def decode_command_result(self, frame: bytes) -> CommandResult:
        value = _object(frame)
        _fields(
            value,
            required={
                "command_id",
                "connector_instance_id",
                "client_instance_id",
                "session_key",
                "profile",
                "client_request_id",
                "method",
                "state",
                "completed_at",
                "revision",
            },
            optional={"result", "error", "extensions"},
        )
        identity = _command_identity(value)
        state = _text(value["state"], "state", 16)
        if state not in _COMMAND_RESULT_STATES:
            raise InvalidCloudFrame("command result state is invalid")
        result: Mapping[str, object] | None = None
        error: Mapping[str, object] | None = None
        if state == "succeeded":
            if "error" in value or "result" not in value:
                raise InvalidCloudFrame("succeeded command requires only result")
            result = _bounded_object(value["result"], "result", 32)
        else:
            if "result" in value or "error" not in value:
                raise InvalidCloudFrame("non-success command requires only error")
            error = _command_error(value["error"])
        return CommandResult(
            **identity,
            state=state,
            completed_at=_instant(value["completed_at"], "completed_at"),
            revision=_integer_range(value["revision"], "revision", 1, 2**63 - 1),
            result=result,
            error=error,
            extensions=_extensions(value.get("extensions")),
        )

    def decode_control_request(self, frame: bytes) -> OwnerControlRequest:
        value = _object(frame)
        _fields(
            value,
            required={
                "request_id",
                "control_transport_id",
                "operation",
                "issued_at",
                "expires_at",
                "body",
            },
            optional={"extensions"},
        )
        operation = _control_operation(value["operation"])
        issued_at = _instant(value["issued_at"], "issued_at")
        expires_at = _instant(value["expires_at"], "expires_at")
        if expires_at <= issued_at:
            raise InvalidCloudFrame("expires_at must be after issued_at")
        return OwnerControlRequest(
            request_id=_uuid(value["request_id"], "request_id"),
            control_transport_id=_uuid(
                value["control_transport_id"],
                "control_transport_id",
            ),
            operation=operation,
            issued_at=issued_at,
            expires_at=expires_at,
            body=_control_request_body(value["body"], operation),
            extensions=_extensions(value.get("extensions")),
        )

    def decode_control_response(self, frame: bytes) -> OwnerControlResponse:
        value = _object(frame)
        _fields(
            value,
            required={
                "request_id",
                "control_transport_id",
                "operation",
                "state",
                "completed_at",
            },
            optional={"result", "error", "extensions"},
        )
        operation = _control_operation(value["operation"])
        state = _text(value["state"], "state", 16)
        if state not in {"succeeded", "failed", "unknown"}:
            raise InvalidCloudFrame("control response state is invalid")
        result: Mapping[str, object] | None = None
        error: Mapping[str, object] | None = None
        if state == "succeeded":
            if "error" in value or "result" not in value:
                raise InvalidCloudFrame(
                    "succeeded control response requires only result"
                )
            result = _control_result(value["result"], operation)
        else:
            if "result" in value or "error" not in value:
                raise InvalidCloudFrame(
                    "non-success control response requires only error"
                )
            error = _control_error(value["error"], state)
        return OwnerControlResponse(
            request_id=_uuid(value["request_id"], "request_id"),
            control_transport_id=_uuid(
                value["control_transport_id"],
                "control_transport_id",
            ),
            operation=operation,
            state=state,
            completed_at=_instant(value["completed_at"], "completed_at"),
            result=result,
            error=error,
            extensions=_extensions(value.get("extensions")),
        )

    def encode_hello(self, message: ConnectorHello) -> bytes:
        value: dict[str, object] = {
            "connector_instance_id": str(message.connector_instance_id),
            "connector_version": message.connector_version,
            "runtime_generation": message.runtime_generation,
            "required_capabilities": list(message.required_capabilities),
            "optional_capabilities": list(message.optional_capabilities),
            "resume": {
                "mode": message.resume.mode,
                "next_outbound_sequence": message.resume.next_outbound_sequence,
                "next_inbound_sequence": message.resume.next_inbound_sequence,
            },
        }
        if message.resume.previous_connection_id is not None:
            resume = value["resume"]
            assert isinstance(resume, dict)
            resume["previous_connection_id"] = str(
                message.resume.previous_connection_id
            )
        _include_extensions(value, message.extensions)
        encoded = _encode(value)
        self.decode_hello(encoded)
        return encoded

    def encode_welcome(self, message: ConnectorWelcome) -> bytes:
        value: dict[str, object] = {
            "connection_id": str(message.connection_id),
            "server_generation": message.server_generation,
            "server_time": _format_instant(message.server_time),
            "accepted_capabilities": list(message.accepted_capabilities),
            "unavailable_optional_capabilities": list(
                message.unavailable_optional_capabilities
            ),
            "resume_decision": message.resume_decision,
            "next_connector_sequence": message.next_connector_sequence,
            "next_cloud_sequence": message.next_cloud_sequence,
            "heartbeat_interval_ms": message.heartbeat_interval_ms,
            "max_in_flight": message.max_in_flight,
        }
        _include_extensions(value, message.extensions)
        encoded = _encode(value)
        self.decode_welcome(encoded)
        return encoded

    def encode_heartbeat(self, message: ConnectorHeartbeat) -> bytes:
        value: dict[str, object] = {
            "connection_id": str(message.connection_id),
            "sender_role": message.sender_role,
            "observed_at": _format_instant(message.observed_at),
            "next_outbound_sequence": message.next_outbound_sequence,
            "next_inbound_sequence": message.next_inbound_sequence,
            "session_state": message.session_state,
        }
        _include_extensions(value, message.extensions)
        encoded = _encode(value)
        self.decode_heartbeat(encoded)
        return encoded

    def encode_command_receipt(self, message: CommandReceipt) -> bytes:
        value = _command_identity_value(message)
        value.update(
            {
                "message_id": str(message.message_id),
                "state": message.state,
                "stored_at": _format_instant(message.stored_at),
                "revision": message.revision,
            }
        )
        _include_extensions(value, message.extensions)
        encoded = _encode(value)
        self.decode_command_receipt(encoded)
        return encoded

    def encode_session_snapshot(self, message: SessionSnapshot) -> bytes:
        if message.observer_contract == 2:
            return self.encode_session_snapshot_v2(message)
        if message.observer_contract != 1:
            raise InvalidCloudFrame("observer snapshot contract is unsupported")
        value: dict[str, object] = {
            "profile": message.profile,
            "runtime_generation": message.runtime_generation,
            "session_key": message.session_key,
            "runtime_session_id": message.runtime_session_id,
            "running": message.running,
            "status": message.status,
            "event_sequence": message.event_sequence,
            "snapshot_event_sequence": message.snapshot_event_sequence,
            "messages": [_thaw(item) for item in message.messages],
            "inflight": _thaw(message.inflight),
            "replay_events": [
                _observer_event_value(event) for event in message.replay_events
            ],
        }
        _include_extensions(value, message.extensions)
        encoded = _encode(value)
        self.decode_session_snapshot(encoded)
        return encoded

    def encode_session_catalog_snapshot_page(
        self,
        message: SessionCatalogSnapshotPage,
    ) -> bytes:
        value: dict[str, object] = {
            "profile": message.profile,
            "runtime_generation": message.runtime_generation,
            "snapshot_id": str(message.snapshot_id),
            "catalog_revision": message.catalog_revision,
            "page_index": message.page_index,
            "is_last": message.is_last,
            "sessions": [_session_catalog_entry_value(item) for item in message.sessions],
        }
        encoded = _encode(value)
        self.decode_session_catalog_snapshot_page(encoded)
        return encoded

    def encode_session_catalog_event(self, message: SessionCatalogEvent) -> bytes:
        encoded = _encode(
            {
                "profile": message.profile,
                "runtime_generation": message.runtime_generation,
                "catalog_sequence": message.catalog_sequence,
                "action": message.action,
                "entry": _session_catalog_entry_value(message.entry),
            }
        )
        self.decode_session_catalog_event(encoded)
        return encoded

    def encode_session_snapshot_v2(self, message: SessionSnapshot) -> bytes:
        if message.observer_contract != 2:
            raise InvalidCloudFrame("observer snapshot v2 contract is required")
        value: dict[str, object] = {
            "observer_contract": 2,
            "profile": message.profile,
            "runtime_generation": message.runtime_generation,
            "session_key": message.session_key,
            "runtime_session_id": message.runtime_session_id,
            "running": message.running,
            "status": message.status,
            "event_sequence": message.event_sequence,
            "snapshot_event_sequence": message.snapshot_event_sequence,
            "messages": [_thaw(item) for item in message.messages],
            "inflight": _thaw(message.inflight),
            "todo_sections": [_thaw(item) for item in message.todo_sections],
            "subagents": [_thaw(item) for item in message.subagents],
            "tools": [_thaw(item) for item in message.tools],
            "terminals": [_thaw(item) for item in message.terminals],
            "replay_events": [
                _observer_event_v2_value(
                    event,
                    profile=message.profile,
                    runtime_generation=message.runtime_generation,
                )
                for event in message.replay_events
            ],
        }
        _include_extensions(value, message.extensions)
        encoded = _encode(value)
        self.decode_session_snapshot_v2(encoded)
        return encoded

    def encode_session_event(self, message: SessionEvent) -> bytes:
        if message.observer_contract == 2:
            return self.encode_session_event_v2(message)
        if message.observer_contract != 1:
            raise InvalidCloudFrame("observer event contract is unsupported")
        value: dict[str, object] = {
            "profile": message.profile,
            "runtime_generation": message.runtime_generation,
            "session_key": message.session_key,
            "session_id": message.session_id,
            "type": message.type,
            "event_sequence": message.event_sequence,
            "payload": _thaw(message.payload),
        }
        if message.event_sequence_start is not None:
            value["event_sequence_start"] = message.event_sequence_start
        _include_extensions(value, message.extensions)
        encoded = _encode(value)
        self.decode_session_event(encoded)
        return encoded

    def encode_session_event_v2(self, message: SessionEvent) -> bytes:
        if message.observer_contract != 2:
            raise InvalidCloudFrame("observer event v2 contract is required")
        value = {
            "observer_contract": 2,
            "profile": message.profile,
            "runtime_generation": message.runtime_generation,
            "session_key": message.session_key,
            "session_id": message.session_id,
            "type": message.type,
            "event_sequence": message.event_sequence,
            "payload": _thaw(message.payload),
        }
        if message.event_sequence_start is not None:
            value["event_sequence_start"] = message.event_sequence_start
        _include_extensions(value, message.extensions)
        encoded = _encode(value)
        self.decode_session_event_v2(encoded)
        return encoded

    def encode_command_result(self, message: CommandResult) -> bytes:
        value = _command_identity_value(message)
        value.update(
            {
                "state": message.state,
                "completed_at": _format_instant(message.completed_at),
                "revision": message.revision,
            }
        )
        if message.result is not None:
            value["result"] = _thaw(message.result)
        if message.error is not None:
            value["error"] = _thaw(message.error)
        _include_extensions(value, message.extensions)
        encoded = _encode(value)
        self.decode_command_result(encoded)
        return encoded

    def encode_control_request(self, message: OwnerControlRequest) -> bytes:
        value: dict[str, object] = {
            "request_id": str(message.request_id),
            "control_transport_id": str(message.control_transport_id),
            "operation": message.operation,
            "issued_at": _format_instant(message.issued_at),
            "expires_at": _format_instant(message.expires_at),
            "body": _thaw(message.body),
        }
        _include_extensions(value, message.extensions)
        encoded = _encode(value)
        self.decode_control_request(encoded)
        return encoded

    def encode_control_response(self, message: OwnerControlResponse) -> bytes:
        value: dict[str, object] = {
            "request_id": str(message.request_id),
            "control_transport_id": str(message.control_transport_id),
            "operation": message.operation,
            "state": message.state,
            "completed_at": _format_instant(message.completed_at),
        }
        if message.result is not None:
            value["result"] = _thaw(message.result)
        if message.error is not None:
            value["error"] = _thaw(message.error)
        _include_extensions(value, message.extensions)
        encoded = _encode(value)
        self.decode_control_response(encoded)
        return encoded

    def encode_envelope(self, message: CloudEnvelope) -> bytes:
        value: dict[str, object] = {
            "contract_version": message.contract_version,
            "message_id": str(message.message_id),
            "message_type": message.message_type,
            "tenant_id": message.tenant_id,
            "device_id": message.device_id,
            "sequence": message.sequence,
            "sent_at": _format_instant(message.sent_at),
            "payload": _thaw(message.payload),
        }
        if message.traceparent is not None:
            value["traceparent"] = message.traceparent
        if message.idempotency_key is not None:
            value["idempotency_key"] = message.idempotency_key
        _include_extensions(value, message.extensions)
        encoded = _encode(value)
        self.decode_envelope(encoded)
        return encoded


def _object(frame: bytes) -> dict[str, Any]:
    if not isinstance(frame, bytes):
        raise InvalidCloudFrame("frame must be bytes")
    if len(frame) > MAX_CLOUD_FRAME_BYTES:
        raise InvalidCloudFrame("frame exceeds contract limit")
    try:
        text = frame.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise InvalidCloudFrame("frame must be strict UTF-8") from None
    try:
        value = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_invalid_number,
        )
    except InvalidCloudFrame:
        raise
    except (ValueError, TypeError, RecursionError, json.JSONDecodeError):
        raise InvalidCloudFrame("frame must be strict JSON") from None
    if not isinstance(value, dict):
        raise InvalidCloudFrame("frame must contain an object")
    _validate_json_tree(value)
    return value


def _validate_json_tree(root: object) -> None:
    stack: list[tuple[object, int]] = [(root, 1)]
    while stack:
        value, depth = stack.pop()
        if depth > MAX_JSON_DEPTH:
            raise InvalidCloudFrame("JSON nesting exceeds contract limit")
        if isinstance(value, str):
            _validate_json_string(value)
            continue
        if isinstance(value, dict):
            if len(value) > MAX_JSON_COLLECTION_ITEMS:
                raise InvalidCloudFrame("JSON object exceeds contract limit")
            for key, item in value.items():
                _validate_json_string(key)
                stack.append((item, depth + 1))
            continue
        if isinstance(value, list):
            if len(value) > MAX_JSON_COLLECTION_ITEMS:
                raise InvalidCloudFrame("JSON array exceeds contract limit")
            stack.extend((item, depth + 1) for item in value)


def _validate_json_string(value: str) -> None:
    if "\x00" in value:
        raise InvalidCloudFrame("JSON string contains NUL")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        raise InvalidCloudFrame("JSON string contains an invalid surrogate") from None
    if len(encoded) > MAX_JSON_STRING_BYTES:
        raise InvalidCloudFrame("JSON string exceeds contract limit")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise InvalidCloudFrame("duplicate object field")
        value[key] = item
    return value


def _invalid_number(_value: str) -> None:
    raise InvalidCloudFrame("non-JSON number")


def _fields(
    value: Mapping[str, object],
    *,
    required: set[str],
    optional: set[str],
) -> None:
    fields = set(value)
    if required - fields:
        raise InvalidCloudFrame("required field is missing")
    if fields - required - optional:
        raise InvalidCloudFrame("unexpected field")


def _text(value: object, field: str, maximum: int) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= maximum:
        raise InvalidCloudFrame(f"{field} is invalid")
    return value


def _text_allow_empty(value: object, field: str, maximum: int) -> str:
    if not isinstance(value, str) or len(value) > maximum:
        raise InvalidCloudFrame(f"{field} is invalid")
    return value


def _optional_text(
    value: Mapping[str, object],
    field: str,
    maximum: int,
) -> str | None:
    if field not in value:
        return None
    return _text(value[field], field, maximum)


def _optional_traceparent(value: Mapping[str, object]) -> str | None:
    traceparent = _optional_text(value, "traceparent", 128)
    if traceparent is not None and _TRACEPARENT.fullmatch(traceparent) is None:
        raise InvalidCloudFrame("traceparent is invalid")
    return traceparent


def _uuid(value: object, field: str) -> UUID:
    if not isinstance(value, str) or _UUID.fullmatch(value) is None:
        raise InvalidCloudFrame(f"{field} must be a canonical UUID")
    try:
        return UUID(value)
    except ValueError:
        raise InvalidCloudFrame(f"{field} must be a canonical UUID") from None


def _instant(value: object, field: str) -> datetime:
    if not isinstance(value, str) or _UTC.fullmatch(value) is None:
        raise InvalidCloudFrame(f"{field} must be an RFC 3339 UTC instant")
    try:
        result = datetime.fromisoformat(value)
    except ValueError:
        raise InvalidCloudFrame(f"{field} must be an RFC 3339 UTC instant") from None
    if result.utcoffset() != UTC.utcoffset(result):
        raise InvalidCloudFrame(f"{field} must use UTC")
    return result


def _format_instant(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise InvalidCloudFrame("instant must use UTC")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _nonnegative(value: object, field: str) -> int:
    return _integer_range(value, field, 0, 2**63 - 1)


def _integer_range(value: object, field: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise InvalidCloudFrame(f"{field} is outside contract limits")
    return value


def _capabilities(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > _MAX_CAPABILITIES:
        raise InvalidCloudFrame(f"{field} must be a bounded array")
    result = tuple(_text(item, field, 128) for item in value)
    if len(result) != len(set(result)):
        raise InvalidCloudFrame(f"{field} must contain unique values")
    return result


def _disjoint(first: tuple[str, ...], second: tuple[str, ...]) -> None:
    if set(first).intersection(second):
        raise InvalidCloudFrame("capability sets must be disjoint")


def _command_params(value: object, method: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise InvalidCloudFrame("params must be an object")
    common = {"runtime_session_id", "runtime_generation"}
    if method == "prompt.submit":
        required = common | {"client_turn_id", "text"}
    else:
        required = common
    _fields(value, required=required, optional=set())
    for field in common:
        _text(value[field], field, 128)
    if method == "prompt.submit":
        _text(value["client_turn_id"], "client_turn_id", 128)
        _text(value["text"], "text", 65_536)
    return _freeze(value)


def _command_identity(value: Mapping[str, object]) -> dict[str, Any]:
    method = _text(value["method"], "method", 128)
    if method not in _COMMAND_METHODS:
        raise InvalidCloudFrame("method is not allowed by command contract v1")
    profile = _text(value["profile"], "profile", 128)
    if _PROFILE.fullmatch(profile) is None:
        raise InvalidCloudFrame("profile is invalid")
    return {
        "command_id": _uuid(value["command_id"], "command_id"),
        "connector_instance_id": _uuid(
            value["connector_instance_id"],
            "connector_instance_id",
        ),
        "client_instance_id": _uuid(
            value["client_instance_id"],
            "client_instance_id",
        ),
        "session_key": _text(value["session_key"], "session_key", 256),
        "profile": profile,
        "client_request_id": _text(
            value["client_request_id"],
            "client_request_id",
            128,
        ),
        "method": method,
    }


def _command_identity_value(
    message: CommandReceipt | CommandResult,
) -> dict[str, object]:
    return {
        "command_id": str(message.command_id),
        "connector_instance_id": str(message.connector_instance_id),
        "client_instance_id": str(message.client_instance_id),
        "session_key": message.session_key,
        "profile": message.profile,
        "client_request_id": message.client_request_id,
        "method": message.method,
    }


def _bounded_object(
    value: object,
    field: str,
    maximum: int,
) -> Mapping[str, object]:
    if not isinstance(value, dict) or len(value) > maximum:
        raise InvalidCloudFrame(f"{field} must be a bounded object")
    return _freeze(value)


def _observer_profile(value: object) -> str:
    profile = _text(value, "profile", 128)
    if _OBSERVER_PROFILE.fullmatch(profile) is None:
        raise InvalidCloudFrame("profile is invalid")
    return profile


def _canonical_digest(value: object, field: str) -> str:
    digest = _text(value, field, 64)
    if _SHA256.fullmatch(digest) is None:
        raise InvalidCloudFrame(f"{field} must be canonical SHA-256")
    return digest


def _session_catalog_entry(value: object) -> SessionCatalogEntry:
    if not isinstance(value, dict):
        raise InvalidCloudFrame("catalog entry must be an object")
    _fields(
        value,
        required={
            "session_key",
            "surface",
            "authority_revision",
            "available_actions",
        },
        optional=set(),
    )
    actions = value["available_actions"]
    allowed = {
        "approval.respond",
        "clarify.respond",
        "prompt.submit",
        "session.interrupt",
        "session.steer",
    }
    if (
        not isinstance(actions, list)
        or len(actions) > 5
        or len(actions) != len(set(actions))
        or any(action not in allowed for action in actions)
    ):
        raise InvalidCloudFrame("catalog available_actions are invalid")
    return SessionCatalogEntry(
        session_key=_text(value["session_key"], "session_key", 256),
        surface=_text(value["surface"], "surface", 64),
        authority_revision=_integer_range(
            value["authority_revision"], "authority_revision", 1, 2**53 - 1
        ),
        available_actions=tuple(str(action) for action in actions),
    )


def _session_catalog_entry_value(
    entry: SessionCatalogEntry,
) -> dict[str, object]:
    return {
        "session_key": entry.session_key,
        "surface": entry.surface,
        "authority_revision": entry.authority_revision,
        "available_actions": list(entry.available_actions),
    }


def _cloud_authorized_target(value: Mapping[str, object]) -> None:
    if value["target_source"] != "cloud_authorized_binding":
        raise InvalidCloudFrame("Observer target source is not authoritative")


def _stream_receipt_identity(value: Mapping[str, object]) -> dict[str, Any]:
    digest = _text(value["payload_digest"], "payload_digest", 64)
    if _SHA256.fullmatch(digest) is None:
        raise InvalidCloudFrame("payload_digest must be canonical SHA-256")
    message_type = _text(
        value["observer_message_type"],
        "observer_message_type",
        32,
    )
    if message_type not in {"session.snapshot", "session.event"}:
        raise InvalidCloudFrame("observer_message_type is invalid")
    return {
        "observer_message_id": _uuid(
            value["observer_message_id"],
            "observer_message_id",
        ),
        "payload_digest": digest,
        "connector_sequence": _nonnegative(
            value["connector_sequence"],
            "connector_sequence",
        ),
        "observer_message_type": message_type,
        "profile": _observer_profile(value["profile"]),
        "session_key": _text(value["session_key"], "session_key", 256),
        "runtime_generation": _text(
            value["runtime_generation"],
            "runtime_generation",
            128,
        ),
        "runtime_session_id": _text(
            value["runtime_session_id"],
            "runtime_session_id",
            256,
        ),
        "event_sequence": _nonnegative(
            value["event_sequence"],
            "event_sequence",
        ),
    }


def _observer_messages(value: object) -> tuple[Mapping[str, object], ...]:
    if not isinstance(value, list) or len(value) > 500:
        raise InvalidCloudFrame("messages must be a bounded array")
    messages: list[Mapping[str, object]] = []
    for item in value:
        if not isinstance(item, dict):
            raise InvalidCloudFrame("message must be an object")
        _fields(item, required={"role"}, optional={"content"})
        _text(item["role"], "message.role", 64)
        if "content" in item and item["content"] is not None:
            _text_allow_empty(item["content"], "message.content", 131_072)
        messages.append(_freeze(item))
    return tuple(messages)


def _observer_inflight(value: object) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise InvalidCloudFrame("inflight must be an object")
    _fields(
        value,
        required={"user", "assistant", "streaming", "error"},
        optional=set(),
    )
    for field in ("user", "assistant"):
        if value[field] is not None:
            _text_allow_empty(value[field], f"inflight.{field}", 131_072)
    if type(value["streaming"]) is not bool:
        raise InvalidCloudFrame("inflight.streaming must be a boolean")
    if value["error"] is not None:
        _text_allow_empty(value["error"], "inflight.error", 4_096)
    return _freeze(value)


def _observer_replay_events(
    value: object,
    *,
    session_key: str,
    runtime_session_id: str,
    snapshot_sequence: int,
    event_sequence: int,
) -> tuple[ObserverEvent, ...]:
    if not isinstance(value, list) or len(value) > 1_024:
        raise InvalidCloudFrame("replay_events must be a bounded array")
    events: list[ObserverEvent] = []
    replay_cursor = snapshot_sequence
    for item in value:
        if not isinstance(item, dict):
            raise InvalidCloudFrame("replay event must be an object")
        if set(item) - {
            "type",
            "session_id",
            "session_key",
            "event_sequence_start",
            "event_sequence",
            "payload",
        }:
            raise InvalidCloudFrame("replay event contains an unexpected field")
        event = _observer_event(item)
        if event.session_key != session_key or event.session_id != runtime_session_id:
            raise InvalidCloudFrame("replay event identity does not match snapshot")
        sequence_start = event.event_sequence_start or event.event_sequence
        if sequence_start != replay_cursor + 1:
            raise InvalidCloudFrame("replay event sequence has a gap")
        if event.event_sequence > event_sequence:
            raise InvalidCloudFrame("replay event exceeds snapshot cursor")
        replay_cursor = event.event_sequence
        events.append(event)
    if replay_cursor != event_sequence:
        raise InvalidCloudFrame("replay events do not reach snapshot cursor")
    return tuple(events)


def _observer_v2_object(frame: bytes, message_type: str) -> dict[str, object]:
    value = _object(frame)
    try:
        load_observer_v2_contracts().validate(message_type, value)
    except ObserverV2ContractError:
        raise InvalidCloudFrame(
            f"payload does not match generated {message_type} contract"
        ) from None
    return value


def _session_event_v2(value: Mapping[str, object]) -> SessionEvent:
    return SessionEvent(
        profile=str(value["profile"]),
        runtime_generation=str(value["runtime_generation"]),
        session_key=str(value["session_key"]),
        session_id=str(value["session_id"]),
        type=str(value["type"]),
        event_sequence=int(value["event_sequence"]),
        event_sequence_start=(
            int(value["event_sequence_start"])
            if "event_sequence_start" in value
            else None
        ),
        payload=_freeze(value["payload"]),
        extensions=_extensions(value.get("extensions")),
        observer_contract=2,
    )


def _stream_receipt_identity_v2(
    value: Mapping[str, object],
) -> dict[str, object]:
    return {
        "observer_message_id": _uuid(
            value["observer_message_id"], "observer_message_id"
        ),
        "payload_digest": str(value["payload_digest"]),
        "connector_sequence": int(value["connector_sequence"]),
        "observer_message_type": str(value["observer_message_type"]),
        "profile": str(value["profile"]),
        "session_key": str(value["session_key"]),
        "runtime_generation": str(value["runtime_generation"]),
        "runtime_session_id": str(value["runtime_session_id"]),
        "event_sequence": int(value["event_sequence"]),
    }


def _observer_event(value: Mapping[str, object]) -> ObserverEvent:
    _fields(
        value,
        required={
            "type",
            "session_id",
            "session_key",
            "event_sequence",
            "payload",
        },
        optional={
            "profile",
            "runtime_generation",
            "event_sequence_start",
            "extensions",
        },
    )
    event_type = _text(value["type"], "type", 64)
    if event_type not in _OBSERVER_EVENT_TYPES:
        raise InvalidCloudFrame("observer event type is invalid")
    sequence = _integer_range(
        value["event_sequence"],
        "event_sequence",
        1,
        2**63 - 1,
    )
    sequence_start: int | None = None
    if "event_sequence_start" in value:
        sequence_start = _integer_range(
            value["event_sequence_start"],
            "event_sequence_start",
            1,
            2**63 - 1,
        )
        if sequence_start > sequence:
            raise InvalidCloudFrame("observer event range is reversed")
        if sequence_start < sequence and event_type not in _MERGEABLE_OBSERVER_TYPES:
            raise InvalidCloudFrame("observer event range is not mergeable")
    return ObserverEvent(
        type=event_type,
        session_id=_text(value["session_id"], "session_id", 256),
        session_key=_text(value["session_key"], "session_key", 256),
        event_sequence=sequence,
        event_sequence_start=sequence_start,
        payload=_observer_event_payload(event_type, value["payload"]),
    )


def _observer_event_payload(
    event_type: str,
    value: object,
) -> Mapping[str, object]:
    if not isinstance(value, dict) or len(value) > 1_024:
        raise InvalidCloudFrame("observer event payload must be a bounded object")
    if event_type == "message.start":
        _fields(value, required=set(), optional={"message_id", "role"})
        if "message_id" in value:
            _text(value["message_id"], "payload.message_id", 256)
        if "role" in value and value["role"] != "assistant":
            raise InvalidCloudFrame("payload.role is invalid")
    elif event_type in {"message.delta", "reasoning.delta", "thinking.delta"}:
        _fields(value, required={"text"}, optional=set())
        _text_allow_empty(value["text"], "payload.text", 131_072)
    elif event_type == "message.complete":
        _fields(value, required={"status"}, optional={"text", "error"})
        if value["status"] not in {"complete", "error"}:
            raise InvalidCloudFrame("payload.status is invalid")
        if "text" in value:
            _text_allow_empty(value["text"], "payload.text", 131_072)
        if "error" in value and value["error"] is not None:
            _text_allow_empty(value["error"], "payload.error", 4_096)
    elif event_type == "agent.terminal.output":
        _fields(
            value,
            required={"text"},
            optional={"process_id", "stream", "sequence"},
        )
        _optional_observer_output_fields(value)
    elif event_type == "status.update":
        _fields(value, required={"status", "running"}, optional={"text"})
        _text(value["status"], "payload.status", 64)
        if type(value["running"]) is not bool:
            raise InvalidCloudFrame("payload.running must be a boolean")
        if "text" in value:
            _text_allow_empty(value["text"], "payload.text", 131_072)
    else:
        _fields(
            value,
            required={"text"},
            optional={"tool_call_id", "tool_name", "sequence"},
        )
        _optional_observer_output_fields(value)
    return _freeze(value)


def _optional_observer_output_fields(value: Mapping[str, object]) -> None:
    _text_allow_empty(value["text"], "payload.text", 131_072)
    for field in ("process_id", "tool_call_id", "tool_name"):
        if field in value:
            _text(value[field], f"payload.{field}", 256)
    if "stream" in value and value["stream"] not in {"stdout", "stderr"}:
        raise InvalidCloudFrame("payload.stream is invalid")
    if "sequence" in value:
        _nonnegative(value["sequence"], "payload.sequence")


def _observer_event_value(event: ObserverEvent) -> dict[str, object]:
    value: dict[str, object] = {
        "type": event.type,
        "session_id": event.session_id,
        "session_key": event.session_key,
        "event_sequence": event.event_sequence,
        "payload": _thaw(event.payload),
    }
    if event.event_sequence_start is not None:
        value["event_sequence_start"] = event.event_sequence_start
    return value


def _observer_event_v2_value(
    event: ObserverEvent,
    *,
    profile: str,
    runtime_generation: str,
) -> dict[str, object]:
    if event.observer_contract != 2:
        raise InvalidCloudFrame("observer replay event v2 contract is required")
    value = _observer_event_value(event)
    return {
        "observer_contract": 2,
        "profile": profile,
        "runtime_generation": runtime_generation,
        **value,
    }


def _command_error(value: object) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise InvalidCloudFrame("error must be an object")
    _fields(
        value,
        required={"code", "message", "retryable"},
        optional=set(),
    )
    code = _text(value["code"], "error.code", 64)
    if code not in _COMMAND_ERROR_CODES:
        raise InvalidCloudFrame("error.code is not in command contract v1")
    _text(value["message"], "error.message", 256)
    if type(value["retryable"]) is not bool:
        raise InvalidCloudFrame("error.retryable must be a boolean")
    return _freeze(value)


def _control_operation(value: object) -> str:
    operation = _text(value, "operation", 128)
    if operation not in _CONTROL_OPERATIONS:
        raise InvalidCloudFrame("operation is not in owner-control contract v1")
    return operation


def _control_request_body(
    value: object,
    operation: str,
) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise InvalidCloudFrame("body must be an object")
    if operation == "control.transport.open":
        _fields(
            value,
            required={
                "principal_id",
                "client_instance_id",
                "session_key",
                "profile",
            },
            optional=set(),
        )
        _text(value["principal_id"], "principal_id", 256)
        _uuid(value["client_instance_id"], "client_instance_id")
        _text(value["session_key"], "session_key", 256)
        profile = _text(value["profile"], "profile", 128)
        if _PROFILE.fullmatch(profile) is None:
            raise InvalidCloudFrame("profile is invalid")
    elif operation == "session.control.acquire":
        _fields(
            value,
            required=set(),
            optional={"runtime_session_id"},
        )
        if "runtime_session_id" in value:
            _text(value["runtime_session_id"], "runtime_session_id", 256)
    elif operation in {"session.control.renew", "session.control.release"}:
        _fields(
            value,
            required={"lease_id"},
            optional={"runtime_session_id"},
        )
        _text(value["lease_id"], "lease_id", 256)
        if "runtime_session_id" in value:
            _text(value["runtime_session_id"], "runtime_session_id", 256)
    elif operation == "session.control.status":
        _fields(value, required=set(), optional={"runtime_session_id"})
        if "runtime_session_id" in value:
            _text(value["runtime_session_id"], "runtime_session_id", 256)
    elif operation == "session.command.status":
        _fields(
            value,
            required={"method", "client_request_id"},
            optional={"runtime_session_id"},
        )
        _text(value["method"], "method", 256)
        if value["method"] not in _OWNER_ACTION_METHODS:
            raise InvalidCloudFrame("invalid command status method")
        _text(value["client_request_id"], "client_request_id", 256)
        if "runtime_session_id" in value:
            _text(value["runtime_session_id"], "runtime_session_id", 256)
    elif operation in {
        "prompt.submit",
        "session.interrupt",
        "session.steer",
        "approval.respond",
        "clarify.respond",
    }:
        _control_action_body(value, operation)
    else:
        _fields(value, required={"reason"}, optional=set())
        if value["reason"] not in {
            "client_requested",
            "client_disconnected",
            "gateway_shutdown",
            "protocol_error",
        }:
            raise InvalidCloudFrame("control transport close reason is invalid")
    return _freeze(value)


def _control_result(
    value: object,
    operation: str,
) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise InvalidCloudFrame("result must be an object")
    if operation == "control.transport.open":
        _fields(
            value,
            required={"attached", "connection_role"},
            optional=set(),
        )
        if value["attached"] is not True or value["connection_role"] != "control":
            raise InvalidCloudFrame("control transport open result is invalid")
    elif operation in {"session.control.acquire", "session.control.renew"}:
        _fields(
            value,
            required={
                "lease_id",
                "expires_at_epoch_ms",
                "control_revision",
                "controller_kind",
                "controller_label",
                "pending_input",
            },
            optional=set(),
        )
        _text(value["lease_id"], "lease_id", 256)
        _nonnegative(value["expires_at_epoch_ms"], "expires_at_epoch_ms")
        _nonnegative(value["control_revision"], "control_revision")
        if value["controller_kind"] != "mobile":
            raise InvalidCloudFrame("lease controller_kind is invalid")
        _text(value["controller_label"], "controller_label", 128)
        _control_pending_input(value["pending_input"])
    elif operation == "session.control.release":
        _fields(
            value,
            required={"released", "control_revision"},
            optional=set(),
        )
        if value["released"] is not True:
            raise InvalidCloudFrame("release result is invalid")
        _nonnegative(value["control_revision"], "control_revision")
    elif operation == "session.control.status":
        _fields(
            value,
            required={
                "controller_kind",
                "controller_label",
                "control_revision",
                "lease_expires_at_epoch_ms",
                "pending_input",
            },
            optional=set(),
        )
        if value["controller_kind"] not in {"mobile", "desktop", "none"}:
            raise InvalidCloudFrame("status controller_kind is invalid")
        label = value["controller_label"]
        if label is None:
            if value["controller_kind"] != "none":
                raise InvalidCloudFrame("controller_label is invalid")
        else:
            if value["controller_kind"] == "none":
                raise InvalidCloudFrame("controller_label is invalid")
            _text(label, "controller_label", 128)
        _nonnegative(value["control_revision"], "control_revision")
        _nonnegative(
            value["lease_expires_at_epoch_ms"],
            "lease_expires_at_epoch_ms",
        )
        _control_pending_input(value["pending_input"])
    elif operation == "session.command.status":
        _control_command_result(value, include_turn=True)
    elif operation == "prompt.submit":
        _control_prompt_result(value)
    elif operation in {"session.interrupt", "session.steer"}:
        _control_command_result(value, include_turn=False)
    elif operation in {"approval.respond", "clarify.respond"}:
        _control_pending_response(value, operation)
    else:
        _fields(value, required={"closed"}, optional=set())
        if value["closed"] is not True:
            raise InvalidCloudFrame("control transport close result is invalid")
    return _freeze(value)


def _control_action_body(value: Mapping[str, object], operation: str) -> None:
    common = {"lease_id", "client_request_id"}
    optional = {"runtime_session_id"}
    required = set(common)
    if operation == "prompt.submit":
        required |= {"client_turn_id", "text"}
    elif operation == "session.steer":
        required.add("text")
    elif operation == "approval.respond":
        required |= {"request_id", "choice"}
    elif operation == "clarify.respond":
        required.add("request_id")
        optional |= {"choice_id", "other_text"}
    _fields(value, required=required, optional=optional)
    for field in common | ({"client_turn_id", "request_id"} & required):
        _text(value[field], field, 256)
    if "runtime_session_id" in value:
        _text(value["runtime_session_id"], "runtime_session_id", 256)
    if "text" in required:
        text = _text(value["text"], "text", 131_072)
        if not text.strip():
            raise InvalidCloudFrame("text is invalid")
    if operation == "approval.respond" and value["choice"] not in {
        "allow_once",
        "allow_session",
        "allow_always",
        "deny",
    }:
        raise InvalidCloudFrame("approval choice is invalid")
    if operation == "clarify.respond":
        answer_fields = {"choice_id", "other_text"} & set(value)
        if len(answer_fields) != 1:
            raise InvalidCloudFrame("clarify answer form is invalid")
        field = next(iter(answer_fields))
        answer = _text(value[field], field, 131_072 if field == "other_text" else 256)
        if not answer.strip():
            raise InvalidCloudFrame("clarify answer is invalid")


def _control_command_result(
    value: Mapping[str, object],
    *,
    include_turn: bool,
) -> None:
    optional = {"client_turn_id", "server_turn_id"} if include_turn else set()
    _fields(
        value,
        required={"status", "client_request_id"},
        optional=optional,
    )
    if value["status"] not in {"accepted", "queued", "rejected"}:
        raise InvalidCloudFrame("control command status is invalid")
    _text(value["client_request_id"], "client_request_id", 256)
    for field in optional & set(value):
        _text(value[field], field, 256)


def _control_prompt_result(value: Mapping[str, object]) -> None:
    _fields(
        value,
        required={"status", "client_request_id", "client_turn_id"},
        optional={"server_turn_id"},
    )
    if value["status"] not in {"accepted", "queued", "rejected"}:
        raise InvalidCloudFrame("prompt status is invalid")
    for field in ("client_request_id", "client_turn_id"):
        _text(value[field], field, 256)
    if "server_turn_id" in value:
        _text(value["server_turn_id"], "server_turn_id", 256)


def _control_pending_response(
    value: Mapping[str, object],
    operation: str,
) -> None:
    _fields(
        value,
        required={
            "status",
            "kind",
            "request_id",
            "client_request_id",
            "control_revision",
        },
        optional=set(),
    )
    expected_kind = operation.split(".", 1)[0]
    if value["status"] != "accepted" or value["kind"] != expected_kind:
        raise InvalidCloudFrame("pending response identity is invalid")
    _text(value["request_id"], "request_id", 256)
    _text(value["client_request_id"], "client_request_id", 256)
    _nonnegative(value["control_revision"], "control_revision")


def _control_pending_input(value: object) -> None:
    if value is None:
        return
    if not isinstance(value, Mapping):
        raise InvalidCloudFrame("pending_input is invalid")
    kind = value.get("kind")
    if kind == "approval":
        _fields(
            value,
            required={
                "request_id",
                "kind",
                "title",
                "description",
                "command",
                "choices",
                "expires_at_epoch_ms",
            },
            optional=set(),
        )
        _text(value["request_id"], "pending_input.request_id", 256)
        _text(value["title"], "pending_input.title", 256)
        _text_allow_empty(value["description"], "pending_input.description", 4096)
        _text_allow_empty(value["command"], "pending_input.command", 131_072)
        choices = value["choices"]
        allowed = {"allow_once", "allow_session", "allow_always", "deny"}
        if (
            not isinstance(choices, (list, tuple))
            or not choices
            or any(not isinstance(choice, str) for choice in choices)
            or len(set(choices)) != len(choices)
            or not set(choices) <= allowed
        ):
            raise InvalidCloudFrame("pending approval choices are invalid")
    elif kind == "clarify":
        _fields(
            value,
            required={
                "request_id",
                "kind",
                "question",
                "choices",
                "allow_other",
                "expires_at_epoch_ms",
            },
            optional=set(),
        )
        _text(value["request_id"], "pending_input.request_id", 256)
        _text(value["question"], "pending_input.question", 4096)
        choices = value["choices"]
        if not isinstance(choices, (list, tuple)) or len(choices) > 64:
            raise InvalidCloudFrame("pending clarify choices are invalid")
        ids: set[str] = set()
        labels: set[str] = set()
        for choice in choices:
            if not isinstance(choice, Mapping):
                raise InvalidCloudFrame("pending clarify choice is invalid")
            _fields(choice, required={"id", "label"}, optional=set())
            ids.add(_text(choice["id"], "pending_input.choice.id", 256))
            labels.add(_text(choice["label"], "pending_input.choice.label", 256))
        if len(ids) != len(choices) or len(labels) != len(choices):
            raise InvalidCloudFrame("pending clarify choices are invalid")
        if type(value["allow_other"]) is not bool or (
            not choices and not value["allow_other"]
        ):
            raise InvalidCloudFrame("pending clarify answer form is invalid")
    else:
        raise InvalidCloudFrame("pending_input kind is invalid")
    _nonnegative(value["expires_at_epoch_ms"], "pending_input.expires_at_epoch_ms")


def _control_error(value: object, state: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise InvalidCloudFrame("error must be an object")
    _fields(value, required={"code", "reason"}, optional=set())
    code = value["code"]
    if type(code) is not int or _CONTROL_ERROR_REASONS.get(code) != value["reason"]:
        raise InvalidCloudFrame("control error code and reason are invalid")
    if state == "unknown" and code != 4307:
        raise InvalidCloudFrame("unknown control response requires effect_unknown")
    if state == "failed" and code == 4307:
        raise InvalidCloudFrame("effect_unknown requires unknown response state")
    return _freeze(value)


def _resume(value: object) -> ResumePosition:
    if not isinstance(value, dict):
        raise InvalidCloudFrame("resume must be an object")
    _fields(
        value,
        required={
            "mode",
            "next_outbound_sequence",
            "next_inbound_sequence",
        },
        optional={"previous_connection_id"},
    )
    mode = value["mode"]
    if mode not in {"fresh", "resume"}:
        raise InvalidCloudFrame("resume mode is invalid")
    previous = None
    if mode == "resume":
        if "previous_connection_id" not in value:
            raise InvalidCloudFrame("resume requires previous_connection_id")
        previous = _uuid(
            value["previous_connection_id"],
            "previous_connection_id",
        )
    elif "previous_connection_id" in value:
        raise InvalidCloudFrame("fresh resume cannot name a prior connection")
    return ResumePosition(
        mode=str(mode),
        previous_connection_id=previous,
        next_outbound_sequence=_nonnegative(
            value["next_outbound_sequence"],
            "next_outbound_sequence",
        ),
        next_inbound_sequence=_nonnegative(
            value["next_inbound_sequence"],
            "next_inbound_sequence",
        ),
    )


def _extensions(value: object) -> Mapping[str, object]:
    if value is None:
        return MappingProxyType({})
    if not isinstance(value, dict) or len(value) > _MAX_EXTENSIONS:
        raise InvalidCloudFrame("extensions must be a bounded object")
    for name, extension in value.items():
        if _EXTENSION.fullmatch(name) is None or not isinstance(extension, dict):
            raise InvalidCloudFrame("extension namespace is invalid")
    return _freeze(value)


def _freeze(value: Mapping[str, object]) -> Mapping[str, object]:
    def freeze_item(item: object) -> object:
        if isinstance(item, dict):
            return MappingProxyType(
                {key: freeze_item(child) for key, child in item.items()}
            )
        if isinstance(item, list):
            return tuple(freeze_item(child) for child in item)
        return item

    return MappingProxyType({key: freeze_item(item) for key, item in value.items()})


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


def _encode(value: Mapping[str, object]) -> bytes:
    try:
        encoded = canonical_json_bytes(_thaw(value))
    except CanonicalJSONError:
        raise InvalidCloudFrame("message cannot be encoded") from None
    if len(encoded) > MAX_CLOUD_FRAME_BYTES:
        raise InvalidCloudFrame("frame exceeds contract limit")
    return encoded
