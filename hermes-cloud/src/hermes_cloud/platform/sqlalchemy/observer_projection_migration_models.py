"""Frozen ORM shapes used only by Observer projection migrations."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Index,
    PrimaryKeyConstraint,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class ObserverInboxV6Base(DeclarativeBase):
    """Metadata isolated from current projection models."""


class ObserverInboxV6Model(ObserverInboxV6Base):
    """Frozen published v6 shape used by typed migration history."""

    __tablename__ = "observer_inbox_messages"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "message_id"),
        UniqueConstraint(
            "tenant_id",
            "device_id",
            "connector_instance_id",
            "connector_sequence",
        ),
        CheckConstraint("connector_sequence >= 0"),
        Index(
            "observer_inbox_received_idx",
            "tenant_id",
            "received_at",
        ),
    )

    tenant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True))
    message_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True))
    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True))
    agent_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True))
    device_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True))
    connector_instance_id: Mapped[str] = mapped_column(String(36))
    connector_sequence: Mapped[int] = mapped_column(BigInteger)
    message_type: Mapped[str] = mapped_column(String(64))
    payload_digest: Mapped[str] = mapped_column(String(64))
    binding_digest: Mapped[str] = mapped_column(String(64))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ObserverInboxV6RowsBase(DeclarativeBase):
    """Metadata for the temporary renamed v6 table."""


class ObserverInboxV6Rows(ObserverInboxV6RowsBase):
    """Read-only ORM binding after v7 renames the frozen inbox table."""

    __tablename__ = "observer_inbox_messages_v6"

    tenant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    message_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True))
    agent_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True))
    device_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True))
    connector_instance_id: Mapped[str] = mapped_column(String(36))
    connector_sequence: Mapped[int] = mapped_column(BigInteger)
    message_type: Mapped[str] = mapped_column(String(64))
    payload_digest: Mapped[str] = mapped_column(String(64))
    binding_digest: Mapped[str] = mapped_column(String(64))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ObserverInboxV7Base(DeclarativeBase):
    """Metadata isolated from both v6 and current projection models."""


class ObserverInboxV7Model(ObserverInboxV7Base):
    """Frozen published v7/v8 inbox shape without retention metadata."""

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


class ObserverInboxV8RowsBase(DeclarativeBase):
    """Metadata for the temporary renamed v8 inbox table."""


class ObserverInboxV8Rows(ObserverInboxV8RowsBase):
    """Read-only ORM binding used while v9 adds retention metadata."""

    __tablename__ = "observer_inbox_messages_v8"

    tenant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    message_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
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


class ObserverInboxV9Base(DeclarativeBase):
    """Metadata frozen for the published v9 retention shape."""


class ObserverInboxV9Model(ObserverInboxV9Base):
    """Frozen v9 inbox shape with one explicit idempotency-retention window."""

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
