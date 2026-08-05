"""Infrastructure-neutral values for the authoritative server projection."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID


def _require_aware(value: datetime, field: str) -> None:
    if value.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone")


def _require_retention(
    retention_until: datetime,
    source_time: datetime,
) -> None:
    _require_aware(retention_until, "retention_until")
    if retention_until < source_time:
        raise ValueError("projection retention cannot predate its source")


class ProjectionWriteResult(StrEnum):
    APPLIED = "applied"
    IDEMPOTENT = "idempotent"


class ProjectionRegression(RuntimeError):
    """A projection revision or sequence attempted to move backwards."""


class ProjectionConflict(RuntimeError):
    """The same revision or sequence was reused with different content."""


class ProjectionTenantMismatch(RuntimeError):
    """A projection child does not belong to the supplied tenant session."""


class ProjectionScopeAmbiguous(RuntimeError):
    """A projection read spans more than one Agent/profile scope."""


@dataclass(frozen=True, slots=True)
class AgentProjection:
    tenant_id: UUID
    agent_id: UUID
    workspace_id: UUID
    agent_key: str
    status: str
    last_seen_at: datetime | None


@dataclass(frozen=True, slots=True)
class CatalogSessionProjection:
    session_id: UUID
    agent_id: UUID
    workspace_id: UUID
    profile: str
    session_key: str
    runtime_generation: str
    surface: str
    authority_revision: int
    available_actions: tuple[str, ...]
    active: bool


@dataclass(frozen=True, slots=True)
class SessionProjection:
    tenant_id: UUID
    session_id: UUID
    session_key: str
    workspace_id: UUID
    agent_id: UUID
    profile: str
    title: str
    state: str
    revision: int
    lineage_tip_message_id: UUID | None
    lineage_tip_sequence: int
    started_at: datetime
    updated_at: datetime
    closed_at: datetime | None
    retention_until: datetime

    def __post_init__(self) -> None:
        if not self.session_key:
            raise ValueError("session_key must not be empty")
        if not isinstance(self.agent_id, UUID):
            raise TypeError("session projection agent_id is required")
        if (
            not isinstance(self.profile, str)
            or not 1 <= len(self.profile) <= 128
            or self.profile != self.profile.strip()
        ):
            raise ValueError("session projection profile is invalid")
        if self.state not in {
            "created",
            "active",
            "waiting",
            "completed",
            "failed",
            "cancelled",
        }:
            raise ValueError("session projection state is invalid")
        if self.revision < 0 or self.lineage_tip_sequence < 0:
            raise ValueError("projection positions must not be negative")
        if (self.lineage_tip_message_id is None) != (self.lineage_tip_sequence == 0):
            raise ValueError(
                "lineage tip id and sequence must be present or absent together"
            )
        _require_aware(self.started_at, "started_at")
        _require_aware(self.updated_at, "updated_at")
        if self.closed_at is not None:
            _require_aware(self.closed_at, "closed_at")
        _require_retention(self.retention_until, self.updated_at)

    def as_record(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class SessionMessageProjection:
    tenant_id: UUID
    session_id: UUID
    message_id: UUID
    sequence: int
    role: str
    content: Mapping[str, object]
    parent_message_id: UUID | None
    created_at: datetime
    retention_until: datetime

    def __post_init__(self) -> None:
        if self.sequence <= 0:
            raise ValueError("message sequence must be positive")
        if self.role not in {"system", "user", "assistant", "tool"}:
            raise ValueError("message role is invalid")
        if not isinstance(self.content, Mapping):
            raise TypeError("message content must be an object")
        _require_aware(self.created_at, "created_at")
        _require_retention(self.retention_until, self.created_at)

    def as_record(self) -> dict[str, object]:
        record = asdict(self)
        record["content"] = dict(self.content)
        return record


@dataclass(frozen=True, slots=True)
class SessionEventProjection:
    tenant_id: UUID
    session_id: UUID
    event_id: UUID
    sequence: int
    event_type: str
    payload: Mapping[str, object]
    occurred_at: datetime
    retention_until: datetime

    def __post_init__(self) -> None:
        if self.sequence <= 0:
            raise ValueError("event sequence must be positive")
        if not self.event_type:
            raise ValueError("event type must not be empty")
        if not isinstance(self.payload, Mapping):
            raise TypeError("event payload must be an object")
        _require_aware(self.occurred_at, "occurred_at")
        _require_retention(self.retention_until, self.occurred_at)

    def as_record(self) -> dict[str, object]:
        record = asdict(self)
        record["payload"] = dict(self.payload)
        return record
