from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from uuid import UUID


def _empty_mapping() -> Mapping[str, object]:
    return MappingProxyType({})


@dataclass(frozen=True, slots=True)
class ObserverEvent:
    type: str
    session_id: str
    session_key: str
    event_sequence: int
    payload: Mapping[str, object]
    event_sequence_start: int | None = None
    observer_contract: int = 1


@dataclass(frozen=True, slots=True)
class SessionEvent:
    profile: str
    runtime_generation: str
    session_key: str
    session_id: str
    type: str
    event_sequence: int
    payload: Mapping[str, object]
    event_sequence_start: int | None = None
    extensions: Mapping[str, object] = field(default_factory=_empty_mapping)
    observer_contract: int = 1


@dataclass(frozen=True, slots=True)
class SessionSnapshot:
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
    replay_events: tuple[ObserverEvent, ...]
    extensions: Mapping[str, object] = field(default_factory=_empty_mapping)
    observer_contract: int = 1
    todo_sections: tuple[Mapping[str, object], ...] = ()
    subagents: tuple[Mapping[str, object], ...] = ()
    tools: tuple[Mapping[str, object], ...] = ()
    terminals: tuple[Mapping[str, object], ...] = ()


ObserverSnapshot = SessionSnapshot


@dataclass(frozen=True, slots=True)
class SessionObserveOpen:
    request_id: UUID
    subscription_id: UUID
    profile: str
    session_key: str
    target_source: str
    requested_at: datetime
    extensions: Mapping[str, object] = field(default_factory=_empty_mapping)
    observer_contract: int = 1


@dataclass(frozen=True, slots=True)
class SessionObserveClose:
    request_id: UUID
    subscription_id: UUID
    profile: str
    session_key: str
    target_source: str
    reason: str
    closed_at: datetime
    extensions: Mapping[str, object] = field(default_factory=_empty_mapping)
    observer_contract: int = 1


@dataclass(frozen=True, slots=True)
class StreamAck:
    observer_message_id: UUID
    payload_digest: str
    connector_sequence: int
    observer_message_type: str
    profile: str
    session_key: str
    runtime_generation: str
    runtime_session_id: str
    event_sequence: int
    committed_at: datetime
    extensions: Mapping[str, object] = field(default_factory=_empty_mapping)
    observer_contract: int = 1


@dataclass(frozen=True, slots=True)
class StreamNack:
    observer_message_id: UUID
    payload_digest: str
    connector_sequence: int
    observer_message_type: str
    profile: str
    session_key: str
    runtime_generation: str
    runtime_session_id: str
    event_sequence: int
    reason: str
    expected_event_sequence: int
    recovery: str
    rejected_at: datetime
    extensions: Mapping[str, object] = field(default_factory=_empty_mapping)
    observer_contract: int = 1


__all__ = [
    "ObserverEvent",
    "ObserverSnapshot",
    "SessionEvent",
    "SessionObserveClose",
    "SessionObserveOpen",
    "SessionSnapshot",
    "StreamAck",
    "StreamNack",
]
