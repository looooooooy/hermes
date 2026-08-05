from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from uuid import UUID


def _empty_mapping() -> Mapping[str, object]:
    return MappingProxyType({})


@dataclass(frozen=True, slots=True)
class ResumePosition:
    mode: str
    next_outbound_sequence: int
    next_inbound_sequence: int
    previous_connection_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class ConnectorHello:
    connector_instance_id: UUID
    connector_version: str
    runtime_generation: str
    required_capabilities: tuple[str, ...]
    optional_capabilities: tuple[str, ...]
    resume: ResumePosition
    extensions: Mapping[str, object] = field(default_factory=_empty_mapping)


@dataclass(frozen=True, slots=True)
class ConnectorWelcome:
    connection_id: UUID
    server_generation: str
    server_time: datetime
    accepted_capabilities: tuple[str, ...]
    unavailable_optional_capabilities: tuple[str, ...]
    resume_decision: str
    next_connector_sequence: int
    next_cloud_sequence: int
    heartbeat_interval_ms: int
    max_in_flight: int
    extensions: Mapping[str, object] = field(default_factory=_empty_mapping)


@dataclass(frozen=True, slots=True)
class ConnectorHeartbeat:
    connection_id: UUID
    sender_role: str
    observed_at: datetime
    next_outbound_sequence: int
    next_inbound_sequence: int
    session_state: str
    extensions: Mapping[str, object] = field(default_factory=_empty_mapping)


@dataclass(frozen=True, slots=True)
class CommandDelivery:
    command_id: UUID
    connector_instance_id: UUID
    client_instance_id: UUID
    session_key: str
    profile: str
    client_request_id: str
    method: str
    params: Mapping[str, object]
    issued_at: datetime
    expires_at: datetime
    revision: int
    extensions: Mapping[str, object] = field(default_factory=_empty_mapping)


@dataclass(frozen=True, slots=True)
class CommandReceipt:
    command_id: UUID
    message_id: UUID
    connector_instance_id: UUID
    client_instance_id: UUID
    session_key: str
    profile: str
    client_request_id: str
    method: str
    state: str
    stored_at: datetime
    revision: int
    extensions: Mapping[str, object] = field(default_factory=_empty_mapping)


@dataclass(frozen=True, slots=True)
class CommandResult:
    command_id: UUID
    connector_instance_id: UUID
    client_instance_id: UUID
    session_key: str
    profile: str
    client_request_id: str
    method: str
    state: str
    completed_at: datetime
    revision: int
    result: Mapping[str, object] | None = None
    error: Mapping[str, object] | None = None
    extensions: Mapping[str, object] = field(default_factory=_empty_mapping)
