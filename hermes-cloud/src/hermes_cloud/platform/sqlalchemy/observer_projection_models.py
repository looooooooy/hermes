"""SQLAlchemy ORM mappings for the SQLite-first Observer projection."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, TypedDict
from uuid import UUID

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class ObserverProjectionBase(DeclarativeBase):
    """Metadata isolated from the already-published PostgreSQL catalog."""


class EncryptedEnvelope(TypedDict):
    """Persisted v1 JSON envelope; projection bodies never use plaintext shapes."""

    version: Literal[1]
    algorithm: Literal["A256GCM"]
    key_version: str
    kek_fingerprint: str
    wrap_nonce: str
    wrapped_dek: str
    wrap_tag: str
    payload_nonce: str
    ciphertext: str
    payload_tag: str


class ObserverInboxModel(ObserverProjectionBase):
    """Bounded transport-idempotency window; this is not a permanent ledger."""

    __tablename__ = "observer_inbox_messages"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "message_id"),
        UniqueConstraint(
            "tenant_id",
            "device_id",
            "connector_instance_id",
            "runtime_generation",
            "connector_sequence",
        ),
        CheckConstraint("connector_sequence >= 0"),
        Index(
            "observer_inbox_received_idx",
            "tenant_id",
            "received_at",
        ),
        Index(
            "observer_inbox_retention_idx",
            "tenant_id",
            "retention_until",
            "message_id",
        ),
    )

    tenant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True))
    message_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True))
    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True))
    agent_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True))
    device_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True))
    connector_instance_id: Mapped[str] = mapped_column(String(36))
    runtime_generation: Mapped[str] = mapped_column(String(128))
    connector_sequence: Mapped[int] = mapped_column(BigInteger)
    message_type: Mapped[str] = mapped_column(String(64))
    payload_digest: Mapped[str] = mapped_column(String(64))
    binding_digest: Mapped[str] = mapped_column(String(64))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    retention_until: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ObserverDeletionLedgerModel(ObserverProjectionBase):
    __tablename__ = "observer_deletion_ledger"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "session_id"),
        CheckConstraint("state IN ('pending', 'failed', 'deleted')"),
        CheckConstraint("attempts > 0"),
        Index(
            "observer_deletion_retry_idx",
            "state",
            "available_at",
        ),
    )

    tenant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True))
    session_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True))
    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True))
    agent_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True))
    profile: Mapped[str] = mapped_column(String(128))
    session_key: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String(16))
    attempts: Mapped[int] = mapped_column(Integer)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )


class ObserverSessionModel(ObserverProjectionBase):
    __tablename__ = "observer_sessions"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "session_id"),
        UniqueConstraint("tenant_id", "agent_id", "profile", "session_key"),
        CheckConstraint("event_sequence >= snapshot_event_sequence"),
        CheckConstraint("snapshot_head_sequence >= snapshot_event_sequence"),
        CheckConstraint("event_sequence >= snapshot_head_sequence"),
        Index(
            "observer_sessions_lookup_idx",
            "tenant_id",
            "session_key",
            "profile",
        ),
        Index(
            "observer_sessions_retention_idx",
            "tenant_id",
            "retention_until",
        ),
    )

    tenant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True))
    session_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True))
    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True))
    agent_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True))
    device_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True))
    profile: Mapped[str] = mapped_column(String(128))
    session_key: Mapped[str] = mapped_column(Text)
    runtime_session_id: Mapped[str] = mapped_column(Text)
    runtime_generation: Mapped[str] = mapped_column(Text)
    connector_instance_id: Mapped[str] = mapped_column(String(36))
    connection_id: Mapped[str] = mapped_column(String(36))
    running: Mapped[bool] = mapped_column(Boolean)
    status: Mapped[str] = mapped_column(String(64))
    event_sequence: Mapped[int] = mapped_column(BigInteger)
    snapshot_event_sequence: Mapped[int] = mapped_column(BigInteger)
    snapshot_head_sequence: Mapped[int] = mapped_column(BigInteger)
    messages: Mapped[EncryptedEnvelope] = mapped_column(JSON)
    inflight: Mapped[EncryptedEnvelope] = mapped_column(JSON)
    replay_events: Mapped[EncryptedEnvelope] = mapped_column(JSON)
    payload_digest: Mapped[str] = mapped_column(String(64))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    retention_until: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ObserverV2StateModel(ObserverProjectionBase):
    __tablename__ = "observer_v2_states"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "session_id"),
        ForeignKeyConstraint(
            ["tenant_id", "session_id"],
            ["observer_sessions.tenant_id", "observer_sessions.session_id"],
            ondelete="CASCADE",
        ),
        CheckConstraint("observer_contract = 2"),
    )

    tenant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True))
    session_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True))
    observer_contract: Mapped[int] = mapped_column(Integer, default=2)
    lifecycle_projection: Mapped[EncryptedEnvelope] = mapped_column(JSON)


class ObserverEventModel(ObserverProjectionBase):
    __tablename__ = "observer_events"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "session_id", "event_sequence"),
        CheckConstraint("event_sequence_start > 0"),
        CheckConstraint("event_sequence >= event_sequence_start"),
        Index(
            "observer_events_stream_idx",
            "tenant_id",
            "session_id",
            "event_sequence",
        ),
        Index(
            "observer_events_retention_idx",
            "tenant_id",
            "retention_until",
        ),
    )

    tenant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True))
    session_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True))
    event_sequence: Mapped[int] = mapped_column(BigInteger)
    event_sequence_start: Mapped[int] = mapped_column(BigInteger)
    session_key: Mapped[str] = mapped_column(Text)
    runtime_session_id: Mapped[str] = mapped_column(Text)
    event_type: Mapped[str] = mapped_column(String(64))
    payload: Mapped[EncryptedEnvelope] = mapped_column(JSON)
    payload_digest: Mapped[str] = mapped_column(String(64))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    retention_until: Mapped[datetime] = mapped_column(DateTime(timezone=True))
