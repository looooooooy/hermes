"""Frozen revision-10 session projection mappings for immutable migrations."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKeyConstraint,
    Index,
    PrimaryKeyConstraint,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class SessionProjectionV10Base(DeclarativeBase):
    """Metadata containing only the published revision-10 table shapes."""


Table(
    "tenants",
    SessionProjectionV10Base.metadata,
    Column("tenant_id", PG_UUID(as_uuid=True), primary_key=True),
    schema="identity",
)
Table(
    "workspaces",
    SessionProjectionV10Base.metadata,
    Column("tenant_id", PG_UUID(as_uuid=True), primary_key=True),
    Column("workspace_id", PG_UUID(as_uuid=True), primary_key=True),
    schema="workspace",
)
Table(
    "agents",
    SessionProjectionV10Base.metadata,
    Column("tenant_id", PG_UUID(as_uuid=True), primary_key=True),
    Column("agent_id", PG_UUID(as_uuid=True), primary_key=True),
    schema="device",
)
Table(
    "refresh_sessions",
    SessionProjectionV10Base.metadata,
    Column("tenant_id", PG_UUID(as_uuid=True), primary_key=True),
    Column("refresh_session_id", PG_UUID(as_uuid=True), primary_key=True),
    schema="identity",
)


class SessionProjectionV10Model(SessionProjectionV10Base):
    __tablename__ = "sessions"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "session_id"),
        ForeignKeyConstraint(["tenant_id"], ["identity.tenants.tenant_id"]),
        ForeignKeyConstraint(
            ["tenant_id", "workspace_id"],
            ["workspace.workspaces.tenant_id", "workspace.workspaces.workspace_id"],
        ),
        ForeignKeyConstraint(
            ["tenant_id", "agent_id"],
            ["device.agents.tenant_id", "device.agents.agent_id"],
        ),
        UniqueConstraint("tenant_id", "session_key"),
        CheckConstraint("revision >= 0"),
        CheckConstraint("lineage_tip_sequence >= 0"),
        CheckConstraint(
            "state IN ('created', 'active', 'waiting', 'completed', 'failed', "
            "'cancelled')"
        ),
        CheckConstraint("retention_until >= updated_at"),
        Index(
            "session_projection_acl_idx",
            "tenant_id",
            "workspace_id",
            "updated_at",
        ),
        Index(
            "session_projection_retention_idx",
            "tenant_id",
            "retention_until",
        ),
        {"schema": "projection"},
    )

    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    session_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    session_key: Mapped[str] = mapped_column(Text)
    workspace_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    agent_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    title: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(Text)
    revision: Mapped[int] = mapped_column(BigInteger)
    lineage_tip_message_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    lineage_tip_sequence: Mapped[int] = mapped_column(
        BigInteger,
        default=0,
        server_default="0",
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retention_until: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SessionMessageProjectionV10Model(SessionProjectionV10Base):
    __tablename__ = "session_messages"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "session_id", "message_id"),
        ForeignKeyConstraint(["tenant_id"], ["identity.tenants.tenant_id"]),
        ForeignKeyConstraint(
            ["tenant_id", "session_id"],
            ["projection.sessions.tenant_id", "projection.sessions.session_id"],
        ),
        UniqueConstraint("tenant_id", "session_id", "sequence"),
        CheckConstraint("sequence > 0"),
        CheckConstraint("role IN ('system', 'user', 'assistant', 'tool')"),
        CheckConstraint("jsonb_typeof(content) = 'object'"),
        CheckConstraint("retention_until >= created_at"),
        Index(
            "session_messages_sequence_idx",
            "tenant_id",
            "session_id",
            "sequence",
        ),
        Index(
            "session_messages_retention_idx",
            "tenant_id",
            "retention_until",
        ),
        {"schema": "projection"},
    )

    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    session_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    message_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    sequence: Mapped[int] = mapped_column(BigInteger)
    role: Mapped[str] = mapped_column(Text)
    content: Mapped[dict[str, Any]] = mapped_column(JSONB)
    parent_message_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    retention_until: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SessionEventProjectionV10Model(SessionProjectionV10Base):
    __tablename__ = "session_events"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "session_id", "event_id"),
        ForeignKeyConstraint(["tenant_id"], ["identity.tenants.tenant_id"]),
        ForeignKeyConstraint(
            ["tenant_id", "session_id"],
            ["projection.sessions.tenant_id", "projection.sessions.session_id"],
        ),
        UniqueConstraint("tenant_id", "session_id", "sequence"),
        CheckConstraint("sequence > 0"),
        CheckConstraint("jsonb_typeof(payload) = 'object'"),
        CheckConstraint("retention_until >= occurred_at"),
        Index(
            "session_events_sequence_idx",
            "tenant_id",
            "session_id",
            "sequence",
        ),
        Index(
            "session_events_retention_idx",
            "tenant_id",
            "retention_until",
        ),
        {"schema": "projection"},
    )

    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    session_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    event_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    sequence: Mapped[int] = mapped_column(BigInteger)
    event_type: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    retention_until: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SessionProjectionCursorV10Model(SessionProjectionV10Base):
    __tablename__ = "session_cursors"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "session_id", "stream"),
        ForeignKeyConstraint(["tenant_id"], ["identity.tenants.tenant_id"]),
        ForeignKeyConstraint(
            ["tenant_id", "session_id"],
            ["projection.sessions.tenant_id", "projection.sessions.session_id"],
        ),
        UniqueConstraint("tenant_id", "session_id", "stream"),
        CheckConstraint("stream IN ('messages', 'events')"),
        CheckConstraint("last_sequence >= 0"),
        Index(
            "session_cursors_updated_idx",
            "tenant_id",
            "updated_at",
        ),
        {"schema": "projection"},
    )

    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    session_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    stream: Mapped[str] = mapped_column(Text)
    last_sequence: Mapped[int] = mapped_column(BigInteger)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class WebSocketTicketV10Model(SessionProjectionV10Base):
    __tablename__ = "websocket_tickets"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "ticket_id"),
        ForeignKeyConstraint(["tenant_id"], ["identity.tenants.tenant_id"]),
        ForeignKeyConstraint(
            ["tenant_id", "refresh_session_id"],
            [
                "identity.refresh_sessions.tenant_id",
                "identity.refresh_sessions.refresh_session_id",
            ],
        ),
        ForeignKeyConstraint(
            ["tenant_id", "session_key"],
            ["projection.sessions.tenant_id", "projection.sessions.session_key"],
        ),
        UniqueConstraint("tenant_id", "ticket_digest"),
        CheckConstraint("ticket_digest ~ '^[0-9a-f]{64}$'"),
        CheckConstraint("principal_type IN ('user', 'device', 'connector')"),
        CheckConstraint("jsonb_typeof(observer_scope) = 'array'"),
        CheckConstraint("expires_at > issued_at"),
        CheckConstraint("consumed_at IS NULL OR consumed_at >= issued_at"),
        CheckConstraint("retention_until >= expires_at"),
        Index(
            "websocket_tickets_consume_idx",
            "tenant_id",
            "ticket_digest",
            "consumed_at",
            "expires_at",
        ),
        Index(
            "websocket_tickets_retention_idx",
            "tenant_id",
            "retention_until",
        ),
        {"schema": "identity"},
    )

    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    ticket_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    ticket_digest: Mapped[str] = mapped_column(String(64))
    principal_type: Mapped[str] = mapped_column(Text)
    principal_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    refresh_session_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    session_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    observer_scope: Mapped[list[str]] = mapped_column(JSONB)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retention_until: Mapped[datetime] = mapped_column(DateTime(timezone=True))


SESSION_PROJECTION_V10_MODELS = (
    SessionProjectionV10Model,
    SessionMessageProjectionV10Model,
    SessionEventProjectionV10Model,
    SessionProjectionCursorV10Model,
    WebSocketTicketV10Model,
)


class SessionProjectionV10RowsBase(DeclarativeBase):
    """Read-only ORM bindings used after revision 11 renames v10 tables."""


class SessionProjectionV10Rows(SessionProjectionV10RowsBase):
    __tablename__ = "sessions_v10"

    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    session_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    session_key: Mapped[str] = mapped_column(Text)
    workspace_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    agent_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    title: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(Text)
    revision: Mapped[int] = mapped_column(BigInteger)
    lineage_tip_message_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    lineage_tip_sequence: Mapped[int] = mapped_column(BigInteger)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retention_until: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SessionMessageProjectionV10Rows(SessionProjectionV10RowsBase):
    __tablename__ = "session_messages_v10"

    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    session_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    message_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    sequence: Mapped[int] = mapped_column(BigInteger)
    role: Mapped[str] = mapped_column(Text)
    content: Mapped[dict[str, Any]] = mapped_column(JSONB)
    parent_message_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    retention_until: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SessionEventProjectionV10Rows(SessionProjectionV10RowsBase):
    __tablename__ = "session_events_v10"

    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    session_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    event_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    sequence: Mapped[int] = mapped_column(BigInteger)
    event_type: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    retention_until: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SessionProjectionCursorV10Rows(SessionProjectionV10RowsBase):
    __tablename__ = "session_cursors_v10"

    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    session_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    stream: Mapped[str] = mapped_column(Text, primary_key=True)
    last_sequence: Mapped[int] = mapped_column(BigInteger)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class WebSocketTicketV10Rows(SessionProjectionV10RowsBase):
    __tablename__ = "websocket_tickets_v10"

    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    ticket_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    ticket_digest: Mapped[str] = mapped_column(String(64))
    principal_type: Mapped[str] = mapped_column(Text)
    principal_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    refresh_session_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    session_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    observer_scope: Mapped[list[str]] = mapped_column(JSONB)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retention_until: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SessionProjectionV11RowsBase(DeclarativeBase):
    """Read-only ORM bindings for validating flattened revision-11 tables."""


class SessionProjectionV11Rows(SessionProjectionV11RowsBase):
    __tablename__ = "sessions"

    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    session_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    session_key: Mapped[str] = mapped_column(Text)
    workspace_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    agent_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    profile: Mapped[str] = mapped_column(String(128))
    title: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(Text)
    revision: Mapped[int] = mapped_column(BigInteger)
    lineage_tip_message_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    lineage_tip_sequence: Mapped[int] = mapped_column(BigInteger)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retention_until: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class WebSocketTicketV11Rows(SessionProjectionV11RowsBase):
    __tablename__ = "websocket_tickets"

    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    ticket_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    ticket_digest: Mapped[str] = mapped_column(String(64))
    principal_type: Mapped[str] = mapped_column(Text)
    principal_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    refresh_session_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    session_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=True,
    )
    observer_scope: Mapped[list[str]] = mapped_column(JSONB)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retention_until: Mapped[datetime] = mapped_column(DateTime(timezone=True))
