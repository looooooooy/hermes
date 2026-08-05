"""Connector Gateway session values with no transport or persistence effects."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


class ConnectorDisconnected(ConnectionError):
    """The Connector peer closed its transport."""


class ConnectorUnsupportedData(ValueError):
    """The Connector used a non-text or otherwise unsupported frame."""


class ConnectorIdentityMismatch(PermissionError):
    """Envelope tenant/device did not match authenticated identity."""


class ConnectorAuthorizationRevoked(PermissionError):
    """The authoritative device lifecycle is terminally revoked."""


class ConnectorAuthorizationSuspended(PermissionError):
    """The authoritative device lifecycle is reversibly suspended."""


class ConnectorAuthorizationUnavailable(PermissionError):
    """The authoritative device lifecycle could not be rechecked."""


class ConnectorAuthenticationExpired(PermissionError):
    """The authenticated Connector token reached its immutable expiry."""


class ConnectorUnsupportedMessage(ValueError):
    """A non-executable message type was received."""


class ConnectorObserverRejected(RuntimeError):
    """An Observer stream position requires explicit Connector recovery."""

    def __init__(
        self,
        *,
        reason: str,
        expected_event_sequence: int,
        recovery: str,
        message: str = "observer projection rejected",
    ) -> None:
        if reason not in {
            "event_gap",
            "projection_conflict",
            "runtime_binding_mismatch",
        }:
            raise ValueError("observer rejection reason is invalid")
        if expected_event_sequence < 0:
            raise ValueError("expected event sequence must not be negative")
        if recovery not in {"send_snapshot", "stop_stream"}:
            raise ValueError("observer recovery action is invalid")
        super().__init__(message)
        self.reason = reason
        self.expected_event_sequence = expected_event_sequence
        self.recovery = recovery


@dataclass(frozen=True, slots=True)
class SessionCatalogEntry:
    session_key: str
    surface: str
    authority_revision: int
    available_actions: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ConnectorSessionCatalogSnapshotPage:
    profile: str
    runtime_generation: str
    snapshot_id: str
    catalog_revision: int
    page_index: int
    is_last: bool
    sessions: tuple[SessionCatalogEntry, ...]


@dataclass(frozen=True, slots=True)
class ConnectorSessionCatalogEvent:
    profile: str
    runtime_generation: str
    catalog_sequence: int
    action: str
    entry: SessionCatalogEntry


@dataclass(frozen=True, slots=True)
class ConnectorSessionCatalogReceiptDelivery:
    catalog_message_id: str
    message_id: str
    message_type: str
    sequence: int
    sent_at: str
    payload: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class ConnectorIdentity:
    tenant_id: str
    device_id: str
    credential_id: str | None = None
    agent_id: str | None = None
    scopes: tuple[str, ...] = ("session.observe",)
    token_id: str | None = None
    legacy_seed: bool = True
    token_issued_at: int | None = None
    token_not_before: int | None = None
    token_expires_at: int | None = None


@dataclass(frozen=True, slots=True)
class ConnectorResumePosition:
    mode: str
    previous_connection_id: str | None
    next_outbound_sequence: int
    next_inbound_sequence: int


@dataclass(frozen=True, slots=True)
class ConnectorHello:
    connector_instance_id: str
    connector_version: str
    runtime_generation: str
    required_capabilities: tuple[str, ...]
    optional_capabilities: tuple[str, ...]
    resume: ConnectorResumePosition


@dataclass(frozen=True, slots=True)
class ConnectorHeartbeat:
    connection_id: str
    sender_role: str
    observed_at: str
    next_outbound_sequence: int
    next_inbound_sequence: int
    session_state: str


@dataclass(frozen=True, slots=True)
class ConnectorResumeResolution:
    decision: str
    next_connector_sequence: int
    next_cloud_sequence: int
    handshake_disposition: str


@dataclass(frozen=True, slots=True)
class ConnectorCommandDelivery:
    command_id: str
    message_id: str
    sent_at: str
    payload: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class ConnectorObserverSubscriptionDelivery:
    request_id: str
    message_id: str
    message_type: str
    sent_at: str
    payload: Mapping[str, object]
    observer_contract: int | None = None
    wire_message_type: str | None = None
    wire_payload_digest: str | None = None


@dataclass(frozen=True, slots=True)
class ConnectorObserverReceiptDelivery:
    observer_message_id: str
    message_id: str
    message_type: str
    sequence: int
    sent_at: str
    payload: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class ConnectorObserverEvent:
    profile: str
    runtime_generation: str
    session_key: str
    runtime_session_id: str
    event_type: str
    event_sequence_start: int
    event_sequence: int
    payload: Mapping[str, object]
    observer_contract: int = 1


@dataclass(frozen=True, slots=True)
class ConnectorObserverSnapshot:
    profile: str
    runtime_generation: str
    session_key: str
    runtime_session_id: str
    running: bool
    status: str
    event_sequence: int
    snapshot_event_sequence: int
    messages: tuple[Mapping[str, object], ...]
    inflight: Mapping[str, object]
    replay_events: tuple[ConnectorObserverEvent, ...]
    todo_sections: tuple[Mapping[str, object], ...] = ()
    subagents: tuple[Mapping[str, object], ...] = ()
    tools: tuple[Mapping[str, object], ...] = ()
    terminals: tuple[Mapping[str, object], ...] = ()
    observer_contract: int = 1
