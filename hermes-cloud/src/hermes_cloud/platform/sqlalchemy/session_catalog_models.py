"""ORM mappings for the authoritative Session Catalog v1 projection."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Index,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class SessionCatalogBase(DeclarativeBase):
    """Metadata isolated so published schemas only change via a new migration."""


class SessionCatalogAuthorityModel(SessionCatalogBase):
    __tablename__ = "session_catalog_authorities"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "agent_id", "profile"),
        CheckConstraint("writer_fence > 0"),
        CheckConstraint("catalog_revision >= 0"),
        CheckConstraint("catalog_sequence >= 0"),
        CheckConstraint("expected_page_index >= 0"),
        Index(
            "session_catalog_authority_writer_idx",
            "tenant_id",
            "writer_id",
            "updated_at",
        ),
        Index(
            "session_catalog_authority_recovery_idx",
            "tenant_id",
            "require_full_snapshot",
            "staging_deadline",
        ),
        {"schema": "projection"},
    )

    tenant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True))
    agent_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True))
    profile: Mapped[str] = mapped_column(String(128))
    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True))
    writer_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True))
    writer_fence: Mapped[int] = mapped_column(BigInteger)
    runtime_generation: Mapped[str | None] = mapped_column(String(128), nullable=True)
    catalog_revision: Mapped[int] = mapped_column(BigInteger)
    catalog_sequence: Mapped[int] = mapped_column(BigInteger)
    staging_snapshot_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True
    )
    staging_runtime_generation: Mapped[str | None] = mapped_column(
        String(128), nullable=True
    )
    staging_catalog_revision: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True
    )
    staging_deadline: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    require_full_snapshot: Mapped[bool] = mapped_column(Boolean, default=False)
    expected_page_index: Mapped[int] = mapped_column(BigInteger)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SessionCatalogGenerationModel(SessionCatalogBase):
    __tablename__ = "session_catalog_generations"
    __table_args__ = (
        PrimaryKeyConstraint(
            "tenant_id", "agent_id", "profile", "runtime_generation"
        ),
        CheckConstraint("writer_fence > 0"),
        CheckConstraint("ordinal > 0"),
        UniqueConstraint("tenant_id", "agent_id", "profile", "ordinal"),
        {"schema": "projection"},
    )

    tenant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True))
    agent_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True))
    profile: Mapped[str] = mapped_column(String(128))
    runtime_generation: Mapped[str] = mapped_column(String(128))
    writer_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True))
    writer_fence: Mapped[int] = mapped_column(BigInteger)
    ordinal: Mapped[int] = mapped_column(BigInteger)
    active: Mapped[bool] = mapped_column(Boolean)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SessionCatalogSnapshotPageModel(SessionCatalogBase):
    __tablename__ = "session_catalog_snapshot_pages"
    __table_args__ = (
        PrimaryKeyConstraint(
            "tenant_id", "agent_id", "profile", "snapshot_id", "page_index"
        ),
        CheckConstraint("catalog_revision >= 0"),
        CheckConstraint("page_index >= 0"),
        Index(
            "session_catalog_snapshot_staging_idx",
            "tenant_id",
            "agent_id",
            "profile",
            "snapshot_id",
            "page_index",
        ),
        Index(
            "session_catalog_snapshot_page_retention_idx",
            "tenant_id",
            "created_at",
            "snapshot_id",
        ),
        {"schema": "projection"},
    )

    tenant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True))
    agent_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True))
    profile: Mapped[str] = mapped_column(String(128))
    snapshot_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True))
    page_index: Mapped[int] = mapped_column(BigInteger)
    runtime_generation: Mapped[str] = mapped_column(String(128))
    catalog_revision: Mapped[int] = mapped_column(BigInteger)
    is_last: Mapped[bool] = mapped_column(Boolean)
    sessions: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    payload_digest: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SessionCatalogEntryModel(SessionCatalogBase):
    __tablename__ = "session_catalog_entries"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "session_id"),
        UniqueConstraint("tenant_id", "agent_id", "profile", "session_key"),
        CheckConstraint("authority_revision > 0"),
        CheckConstraint("writer_fence > 0"),
        Index(
            "session_catalog_entries_list_idx",
            "tenant_id",
            "agent_id",
            "profile",
            "active",
            "updated_at",
        ),
        {"schema": "projection"},
    )

    tenant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True))
    session_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True))
    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True))
    agent_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True))
    profile: Mapped[str] = mapped_column(String(128))
    session_key: Mapped[str] = mapped_column(Text)
    surface: Mapped[str] = mapped_column(String(64))
    authority_revision: Mapped[int] = mapped_column(BigInteger)
    available_actions: Mapped[list[str]] = mapped_column(JSON)
    runtime_generation: Mapped[str] = mapped_column(String(128))
    writer_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True))
    writer_fence: Mapped[int] = mapped_column(BigInteger)
    content_digest: Mapped[str] = mapped_column(String(64))
    active: Mapped[bool] = mapped_column(Boolean)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SessionCatalogInboxModel(SessionCatalogBase):
    __tablename__ = "session_catalog_inbox"
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
        CheckConstraint(
            "receipt_state IS NULL OR receipt_state IN "
            "('pending', 'settled', 'retired')",
            name="session_catalog_inbox_receipt_state_check",
        ),
        CheckConstraint(
            "dispatch_sequence IS NULL OR dispatch_sequence >= 0",
            name="session_catalog_inbox_dispatch_sequence_check",
        ),
        CheckConstraint(
            "dispatch_attempts >= 0",
            name="session_catalog_inbox_dispatch_attempts_check",
        ),
        CheckConstraint(
            "receipt_type IS NULL OR receipt_type IN "
            "('session.catalog.ack', 'session.catalog.nack')"
        ),
        Index(
            "session_catalog_inbox_received_idx",
            "tenant_id",
            "received_at",
            "message_id",
        ),
        Index(
            "session_catalog_inbox_retention_idx",
            "tenant_id",
            "retention_until",
            "message_id",
        ),
        Index(
            "session_catalog_inbox_pending_receipt_idx",
            "tenant_id",
            "device_id",
            "connector_instance_id",
            "runtime_generation",
            "receipt_state",
            "updated_at",
        ),
        {"schema": "projection"},
    )

    tenant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True))
    message_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True))
    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True))
    agent_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True))
    device_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True))
    connector_instance_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True))
    runtime_generation: Mapped[str] = mapped_column(String(128))
    connector_sequence: Mapped[int] = mapped_column(BigInteger)
    message_type: Mapped[str] = mapped_column(String(64))
    payload_digest: Mapped[str] = mapped_column(String(64))
    receipt_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    receipt_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    receipt_state: Mapped[str | None] = mapped_column(String(16), nullable=True)
    dispatch_connection_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True
    )
    dispatch_message_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True
    )
    dispatch_sequence: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    dispatch_attempts: Mapped[int] = mapped_column(BigInteger, default=0)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    receipt_sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    receipt_settled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    receipt_retired_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    receipt_retirement_reason: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    retention_until: Mapped[datetime] = mapped_column(DateTime(timezone=True))
