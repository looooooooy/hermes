"""SQLAlchemy 2.x declarative mappings for Hermes Cloud PostgreSQL storage.

The mappings are the single source of truth for table shape.  Application and
repository code work with these mapped types and operation-scoped ``Session``
instances; schema migrations consume the same table metadata through Alembic.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class HermesCloudBase(DeclarativeBase):
    """Base for all PostgreSQL ORM models."""


class MigrationLedgerModel(HermesCloudBase):
    __tablename__ = "hermes_cloud_migrations"
    __table_args__ = (
        PrimaryKeyConstraint("version"),
        UniqueConstraint("name"),
        CheckConstraint("version > 0"),
        CheckConstraint("checksum ~ '^[0-9a-f]{64}$'"),
        {"schema": "public"},
    )

    version: Mapped[int] = mapped_column(BigInteger)
    name: Mapped[str] = mapped_column(Text)
    checksum: Mapped[str] = mapped_column(String(64))
    applied_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class TenantModel(HermesCloudBase):
    __tablename__ = "tenants"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id"),
        UniqueConstraint("slug"),
        CheckConstraint("slug = lower(slug) AND slug ~ '^[a-z0-9][a-z0-9-]{0,62}$'"),
        CheckConstraint("status IN ('active', 'suspended', 'closed')"),
        {"schema": "identity"},
    )

    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    slug: Mapped[str] = mapped_column(Text)
    display_name: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default="active", server_default="active")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.current_timestamp()
    )


class UserModel(HermesCloudBase):
    __tablename__ = "users"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "user_id"),
        ForeignKeyConstraint(["tenant_id"], ["identity.tenants.tenant_id"]),
        UniqueConstraint("tenant_id", "subject"),
        UniqueConstraint("tenant_id", "email"),
        CheckConstraint("status IN ('active', 'disabled')"),
        {"schema": "identity"},
    )

    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    user_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    subject: Mapped[str] = mapped_column(Text)
    display_name: Mapped[str] = mapped_column(Text)
    email: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default="active", server_default="active")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.current_timestamp()
    )


class MembershipModel(HermesCloudBase):
    __tablename__ = "memberships"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "membership_id"),
        ForeignKeyConstraint(["tenant_id"], ["identity.tenants.tenant_id"]),
        ForeignKeyConstraint(
            ["tenant_id", "user_id"],
            ["identity.users.tenant_id", "identity.users.user_id"],
        ),
        UniqueConstraint("tenant_id", "user_id", "workspace_id", "role_key"),
        CheckConstraint("status IN ('active', 'revoked')"),
        Index("memberships_user_idx", "tenant_id", "user_id"),
        {"schema": "identity"},
    )

    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    membership_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    user_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    workspace_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    role_key: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default="active", server_default="active")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.current_timestamp()
    )


class AgentModel(HermesCloudBase):
    __tablename__ = "agents"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "agent_id"),
        ForeignKeyConstraint(["tenant_id"], ["identity.tenants.tenant_id"]),
        UniqueConstraint("tenant_id", "agent_key"),
        CheckConstraint("status IN ('active', 'disabled', 'offline')"),
        Index("agents_status_idx", "tenant_id", "status", "last_seen_at"),
        {"schema": "device"},
    )

    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    agent_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    workspace_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    agent_key: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default="active", server_default="active")
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.current_timestamp()
    )


class DeviceModel(HermesCloudBase):
    __tablename__ = "devices"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "device_id"),
        ForeignKeyConstraint(["tenant_id"], ["identity.tenants.tenant_id"]),
        ForeignKeyConstraint(
            ["tenant_id", "agent_id"],
            ["device.agents.tenant_id", "device.agents.agent_id"],
        ),
        UniqueConstraint("tenant_id", "device_key"),
        CheckConstraint("status IN ('active', 'disabled', 'offline')"),
        Index("devices_agent_idx", "tenant_id", "agent_id"),
        {"schema": "device"},
    )

    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    device_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    agent_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    workspace_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    device_key: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default="active", server_default="active")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.current_timestamp()
    )


class CommandModel(HermesCloudBase):
    __tablename__ = "commands"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "command_id"),
        ForeignKeyConstraint(["tenant_id"], ["identity.tenants.tenant_id"]),
        ForeignKeyConstraint(
            ["tenant_id", "agent_id"],
            ["device.agents.tenant_id", "device.agents.agent_id"],
        ),
        ForeignKeyConstraint(
            ["tenant_id", "device_id"],
            ["device.devices.tenant_id", "device.devices.device_id"],
        ),
        UniqueConstraint("tenant_id", "idempotency_key"),
        CheckConstraint(
            "state IN ('queued', 'dispatched', 'running', 'succeeded', "
            "'failed', 'cancelled')"
        ),
        CheckConstraint("jsonb_typeof(payload) = 'object'"),
        CheckConstraint("deadline_at >= created_at"),
        Index("commands_dispatch_idx", "tenant_id", "state", "deadline_at"),
        {"schema": "command"},
    )

    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    command_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    workspace_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    agent_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    device_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    idempotency_key: Mapped[str] = mapped_column(Text)
    command_type: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(Text, default="queued", server_default="queued")
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.current_timestamp()
    )


class CommandAttemptModel(HermesCloudBase):
    __tablename__ = "attempts"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "attempt_id"),
        ForeignKeyConstraint(["tenant_id"], ["identity.tenants.tenant_id"]),
        ForeignKeyConstraint(
            ["tenant_id", "command_id"],
            ["command.commands.tenant_id", "command.commands.command_id"],
        ),
        UniqueConstraint("tenant_id", "command_id", "attempt_number"),
        CheckConstraint("attempt_number > 0"),
        CheckConstraint("state IN ('started', 'succeeded', 'failed', 'timed_out')"),
        CheckConstraint("finished_at IS NULL OR finished_at >= started_at"),
        Index(
            "attempts_command_idx",
            "tenant_id",
            "command_id",
            "attempt_number",
        ),
        {"schema": "command"},
    )

    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    attempt_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    command_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    attempt_number: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(
        Text, default="started", server_default="started"
    )
    error_code: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.current_timestamp()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CommandTransitionModel(HermesCloudBase):
    __tablename__ = "transitions"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "transition_id"),
        ForeignKeyConstraint(["tenant_id"], ["identity.tenants.tenant_id"]),
        ForeignKeyConstraint(
            ["tenant_id", "command_id"],
            ["command.commands.tenant_id", "command.commands.command_id"],
        ),
        UniqueConstraint("tenant_id", "command_id", "sequence"),
        CheckConstraint("sequence > 0"),
        CheckConstraint(
            "(from_state = 'queued' AND to_state IN ('dispatched', 'cancelled')) "
            "OR (from_state = 'dispatched' AND to_state IN "
            "('running', 'failed', 'cancelled')) "
            "OR (from_state = 'running' AND to_state IN "
            "('succeeded', 'failed', 'cancelled'))"
        ),
        Index(
            "transitions_command_idx",
            "tenant_id",
            "command_id",
            "sequence",
        ),
        {"schema": "command"},
    )

    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    transition_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    command_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    sequence: Mapped[int] = mapped_column(BigInteger)
    from_state: Mapped[str] = mapped_column(Text)
    to_state: Mapped[str] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text)
    transitioned_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.current_timestamp()
    )


class ConnectorBindingModel(HermesCloudBase):
    __tablename__ = "connector_bindings"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "device_id"),
        UniqueConstraint("tenant_id", "connection_id"),
        CheckConstraint("state IN ('active', 'offline')"),
        Index("connector_bindings_active_idx", "tenant_id", "state", "updated_at"),
        {"schema": "command"},
    )

    tenant_id: Mapped[str] = mapped_column(Text)
    device_id: Mapped[str] = mapped_column(Text)
    connector_instance_id: Mapped[str] = mapped_column(String(36))
    connection_id: Mapped[str] = mapped_column(String(36))
    runtime_generation: Mapped[str] = mapped_column(Text)
    accepted_control: Mapped[bool] = mapped_column(Boolean)
    state: Mapped[str] = mapped_column(Text)
    next_connector_sequence: Mapped[int] = mapped_column(BigInteger)
    next_cloud_sequence: Mapped[int] = mapped_column(BigInteger)
    revision: Mapped[int] = mapped_column(BigInteger)
    connected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ConnectorTransportCursorModel(HermesCloudBase):
    """Sole durable transport position for one Connector device."""

    __tablename__ = "connector_transport_cursors"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "device_id"),
        ForeignKeyConstraint(
            ["tenant_id", "device_id"],
            ["device.devices.tenant_id", "device.devices.device_id"],
        ),
        UniqueConstraint("tenant_id", "connection_id"),
        CheckConstraint("state IN ('active', 'offline')"),
        CheckConstraint("next_connector_sequence >= 0"),
        CheckConstraint("next_cloud_sequence >= 0"),
        CheckConstraint("revision > 0"),
        Index(
            "connector_transport_cursors_active_idx",
            "tenant_id",
            "state",
            "updated_at",
        ),
        {"schema": "platform"},
    )

    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    device_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    connector_instance_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    runtime_generation: Mapped[str] = mapped_column(String(128))
    connection_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    state: Mapped[str] = mapped_column(String(16))
    next_connector_sequence: Mapped[int] = mapped_column(BigInteger)
    next_cloud_sequence: Mapped[int] = mapped_column(BigInteger)
    revision: Mapped[int] = mapped_column(BigInteger)
    connected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ConnectorTransportHandshakeOwnershipModel(HermesCloudBase):
    """Expiring two-phase ownership for one Connector welcome handshake."""

    __tablename__ = "connector_transport_handshake_ownership"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "device_id"),
        ForeignKeyConstraint(
            ["tenant_id", "device_id"],
            ["device.devices.tenant_id", "device.devices.device_id"],
        ),
        UniqueConstraint("tenant_id", "connection_id"),
        CheckConstraint("state IN ('activating', 'active')"),
        CheckConstraint("resume_decision IN ('fresh', 'resumed', 'reset_required')"),
        CheckConstraint("handshake_disposition IN ('advance', 'preserve')"),
        CheckConstraint("expected_next_connector_sequence >= 0"),
        CheckConstraint("expected_next_cloud_sequence >= 0"),
        CheckConstraint("next_connector_sequence >= 0"),
        CheckConstraint("next_cloud_sequence >= 0"),
        CheckConstraint("revision > 0"),
        Index(
            "connector_transport_handshake_lease_idx",
            "tenant_id",
            "state",
            "lease_expires_at",
        ),
        {"schema": "platform"},
    )

    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    device_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    connector_instance_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    runtime_generation: Mapped[str] = mapped_column(String(128))
    connection_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    previous_connection_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    resume_decision: Mapped[str] = mapped_column(String(24))
    handshake_disposition: Mapped[str] = mapped_column(String(16))
    state: Mapped[str] = mapped_column(String(16))
    expected_next_connector_sequence: Mapped[int] = mapped_column(BigInteger)
    expected_next_cloud_sequence: Mapped[int] = mapped_column(BigInteger)
    next_connector_sequence: Mapped[int] = mapped_column(BigInteger)
    next_cloud_sequence: Mapped[int] = mapped_column(BigInteger)
    revision: Mapped[int] = mapped_column(BigInteger)
    lease_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    prepared_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ConnectorObserverReceiptModel(HermesCloudBase):
    """Durable ACK/NACK business receipt awaiting Connector confirmation."""

    __tablename__ = "connector_observer_receipts"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "device_id", "observer_message_id"),
        ForeignKeyConstraint(
            ["tenant_id", "device_id"],
            ["device.devices.tenant_id", "device.devices.device_id"],
        ),
        UniqueConstraint("tenant_id", "device_id", "dispatch_message_id"),
        CheckConstraint("receipt_type IN ('stream.ack', 'stream.nack')"),
        CheckConstraint("state IN ('pending', 'settled')"),
        CheckConstraint("dispatch_sequence IS NULL OR dispatch_sequence >= 0"),
        CheckConstraint("dispatch_attempts >= 0"),
        Index(
            "connector_observer_receipts_pending_idx",
            "tenant_id",
            "device_id",
            "state",
            "updated_at",
            "observer_message_id",
        ),
        Index(
            "connector_observer_receipts_settled_idx",
            "tenant_id",
            "device_id",
            "state",
            "settled_at",
            "observer_message_id",
        ),
        {"schema": "platform"},
    )

    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    device_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    observer_message_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    receipt_type: Mapped[str] = mapped_column(String(16))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    payload_digest: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(16))
    dispatch_connection_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    dispatch_message_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    dispatch_sequence: Mapped[int | None] = mapped_column(BigInteger)
    dispatch_attempts: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ControlCommandModel(HermesCloudBase):
    __tablename__ = "control_commands"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "command_id"),
        UniqueConstraint("tenant_id", "delivery_message_id"),
        UniqueConstraint(
            "tenant_id",
            "provider",
            "principal_id",
            "client_instance_id",
            "session_key",
            "profile",
            "method",
            "client_request_id",
        ),
        UniqueConstraint("tenant_id", "receipt_message_id"),
        UniqueConstraint("tenant_id", "result_message_id"),
        CheckConstraint(
            "state IN ('queued', 'dispatched', 'delivered', 'succeeded', "
            "'failed', 'unknown', 'expired')"
        ),
        CheckConstraint("revision > 0"),
        CheckConstraint("expires_at > issued_at"),
        CheckConstraint("jsonb_typeof(params) = 'object'"),
        Index(
            "control_commands_delivery_idx",
            "tenant_id",
            "device_id",
            "connector_instance_id",
            "state",
            "issued_at",
        ),
        Index(
            "control_commands_status_idx",
            "tenant_id",
            "principal_id",
            "session_key",
            "client_request_id",
        ),
        {"schema": "command"},
    )

    tenant_id: Mapped[str] = mapped_column(Text)
    command_id: Mapped[str] = mapped_column(String(36))
    delivery_message_id: Mapped[str] = mapped_column(String(36))
    device_id: Mapped[str] = mapped_column(Text)
    connector_instance_id: Mapped[str] = mapped_column(String(36))
    client_instance_id: Mapped[str] = mapped_column(String(36))
    provider: Mapped[str] = mapped_column(Text)
    principal_id: Mapped[str] = mapped_column(String(36))
    session_key: Mapped[str] = mapped_column(Text)
    profile: Mapped[str] = mapped_column(Text)
    runtime_session_id: Mapped[str] = mapped_column(Text)
    runtime_generation: Mapped[str] = mapped_column(Text)
    client_request_id: Mapped[str] = mapped_column(Text)
    client_turn_id: Mapped[str | None] = mapped_column(Text)
    method: Mapped[str] = mapped_column(Text)
    params: Mapped[dict[str, Any]] = mapped_column(JSONB)
    payload_digest: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(Text)
    revision: Mapped[int] = mapped_column(BigInteger)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    dispatch_connection_id: Mapped[str | None] = mapped_column(String(36))
    dispatch_sequence: Mapped[int | None] = mapped_column(BigInteger)
    delivery_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    receipt_message_id: Mapped[str | None] = mapped_column(String(36))
    receipt_digest: Mapped[str | None] = mapped_column(String(64))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result_message_id: Mapped[str | None] = mapped_column(String(36))
    result_digest: Mapped[str | None] = mapped_column(String(64))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PolicyModel(HermesCloudBase):
    __tablename__ = "policies"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "policy_id"),
        ForeignKeyConstraint(["tenant_id"], ["identity.tenants.tenant_id"]),
        UniqueConstraint("tenant_id", "policy_key", "version"),
        CheckConstraint("effect IN ('allow', 'deny')"),
        CheckConstraint("version > 0"),
        CheckConstraint("jsonb_typeof(conditions) = 'object'"),
        Index("policies_action_idx", "tenant_id", "action_pattern"),
        {"schema": "authorization"},
    )

    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    policy_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    workspace_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    policy_key: Mapped[str] = mapped_column(Text)
    effect: Mapped[str] = mapped_column(Text)
    action_pattern: Mapped[str] = mapped_column(Text)
    resource_pattern: Mapped[str] = mapped_column(Text)
    conditions: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    version: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.current_timestamp()
    )


class AccessGrantModel(HermesCloudBase):
    __tablename__ = "access_grants"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "grant_id"),
        ForeignKeyConstraint(["tenant_id"], ["identity.tenants.tenant_id"]),
        ForeignKeyConstraint(
            ["tenant_id", "policy_id"],
            ["authorization.policies.tenant_id", "authorization.policies.policy_id"],
        ),
        UniqueConstraint("tenant_id", "policy_id", "principal_type", "principal_id"),
        CheckConstraint("principal_type IN ('user', 'agent', 'service')"),
        CheckConstraint("valid_until IS NULL OR valid_until > valid_from"),
        Index(
            "access_grants_principal_idx",
            "tenant_id",
            "principal_type",
            "principal_id",
        ),
        {"schema": "authorization"},
    )

    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    grant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    workspace_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    policy_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    principal_type: Mapped[str] = mapped_column(Text)
    principal_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    valid_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.current_timestamp()
    )
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.current_timestamp()
    )


class OutboxEventModel(HermesCloudBase):
    __tablename__ = "outbox_events"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "event_id"),
        ForeignKeyConstraint(["tenant_id"], ["identity.tenants.tenant_id"]),
        CheckConstraint("jsonb_typeof(payload) = 'object'"),
        CheckConstraint("state IN ('pending', 'publishing', 'published', 'dead')"),
        CheckConstraint("publish_attempts >= 0"),
        CheckConstraint("published_at IS NULL OR published_at >= created_at"),
        Index("outbox_publish_idx", "tenant_id", "state", "available_at"),
        {"schema": "platform"},
    )

    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    event_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    workspace_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    aggregate_type: Mapped[str] = mapped_column(Text)
    aggregate_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    event_type: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    state: Mapped[str] = mapped_column(
        Text, default="pending", server_default="pending"
    )
    publish_attempts: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0"
    )
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.current_timestamp()
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.current_timestamp()
    )


class InboxMessageModel(HermesCloudBase):
    __tablename__ = "inbox_messages"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "message_id"),
        ForeignKeyConstraint(["tenant_id"], ["identity.tenants.tenant_id"]),
        CheckConstraint("digest ~ '^[0-9a-f]{64}$'"),
        CheckConstraint("processed_at IS NULL OR processed_at >= received_at"),
        Index("inbox_received_idx", "tenant_id", "received_at"),
        {"schema": "platform"},
    )

    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    message_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    workspace_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    digest: Mapped[str] = mapped_column(String(64))
    message_type: Mapped[str] = mapped_column(Text)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.current_timestamp()
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuditEventModel(HermesCloudBase):
    __tablename__ = "audit_events"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "audit_event_id"),
        ForeignKeyConstraint(["tenant_id"], ["identity.tenants.tenant_id"]),
        CheckConstraint("actor_type IN ('user', 'agent', 'service', 'system')"),
        CheckConstraint("outcome IN ('accepted', 'rejected', 'failed')"),
        CheckConstraint("octet_length(purpose) BETWEEN 1 AND 128"),
        CheckConstraint("jsonb_typeof(details) = 'object'"),
        Index(
            "audit_subject_idx",
            "tenant_id",
            "subject_type",
            "subject_id",
            "occurred_at",
        ),
        {"schema": "audit"},
    )

    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    audit_event_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    workspace_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    actor_type: Mapped[str] = mapped_column(Text)
    actor_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    purpose: Mapped[str] = mapped_column(Text)
    action: Mapped[str] = mapped_column(Text)
    subject_type: Mapped[str] = mapped_column(Text)
    subject_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    outcome: Mapped[str] = mapped_column(Text)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.current_timestamp()
    )


class RoleModel(HermesCloudBase):
    __tablename__ = "roles"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "role_id"),
        ForeignKeyConstraint(["tenant_id"], ["identity.tenants.tenant_id"]),
        UniqueConstraint("tenant_id", "role_key", "version"),
        CheckConstraint("scope_type IN ('tenant', 'workspace')"),
        CheckConstraint("status IN ('active', 'disabled')"),
        CheckConstraint("version > 0"),
        CheckConstraint("jsonb_typeof(permissions) = 'array'"),
        Index("roles_lookup_idx", "tenant_id", "role_key", "status", "version"),
        {"schema": "identity"},
    )

    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    role_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    role_key: Mapped[str] = mapped_column(Text)
    display_name: Mapped[str] = mapped_column(Text)
    scope_type: Mapped[str] = mapped_column(Text)
    permissions: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    status: Mapped[str] = mapped_column(Text, default="active", server_default="active")
    version: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.current_timestamp()
    )


class WorkspaceModel(HermesCloudBase):
    __tablename__ = "workspaces"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "workspace_id"),
        ForeignKeyConstraint(["tenant_id"], ["identity.tenants.tenant_id"]),
        ForeignKeyConstraint(
            ["tenant_id", "created_by"],
            ["identity.users.tenant_id", "identity.users.user_id"],
        ),
        UniqueConstraint("tenant_id", "workspace_key"),
        CheckConstraint(
            "workspace_key = lower(workspace_key) "
            "AND workspace_key ~ '^[a-z0-9][a-z0-9-]{0,62}$'"
        ),
        CheckConstraint("status IN ('active', 'suspended', 'archived')"),
        Index("workspaces_status_idx", "tenant_id", "status"),
        {"schema": "workspace"},
    )

    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    workspace_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    workspace_key: Mapped[str] = mapped_column(Text)
    display_name: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default="active", server_default="active")
    created_by: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.current_timestamp()
    )


class WorkspaceMembershipModel(HermesCloudBase):
    __tablename__ = "workspace_memberships"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "workspace_membership_id"),
        ForeignKeyConstraint(["tenant_id"], ["identity.tenants.tenant_id"]),
        ForeignKeyConstraint(
            ["tenant_id", "workspace_id"],
            ["workspace.workspaces.tenant_id", "workspace.workspaces.workspace_id"],
        ),
        ForeignKeyConstraint(
            ["tenant_id", "user_id"],
            ["identity.users.tenant_id", "identity.users.user_id"],
        ),
        ForeignKeyConstraint(
            ["tenant_id", "role_id"],
            ["identity.roles.tenant_id", "identity.roles.role_id"],
        ),
        UniqueConstraint("tenant_id", "workspace_id", "user_id", "role_id"),
        CheckConstraint("status IN ('active', 'suspended', 'revoked')"),
        CheckConstraint("revoked_at IS NULL OR revoked_at >= joined_at"),
        Index(
            "workspace_memberships_user_idx",
            "tenant_id",
            "user_id",
            "workspace_id",
            "status",
        ),
        {"schema": "workspace"},
    )

    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    workspace_membership_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    workspace_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    user_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    role_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    status: Mapped[str] = mapped_column(Text, default="active", server_default="active")
    joined_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.current_timestamp()
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DeviceCredentialModel(HermesCloudBase):
    __tablename__ = "device_credentials"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "credential_id"),
        ForeignKeyConstraint(["tenant_id"], ["identity.tenants.tenant_id"]),
        ForeignKeyConstraint(
            ["tenant_id", "device_id"],
            ["device.devices.tenant_id", "device.devices.device_id"],
        ),
        UniqueConstraint("tenant_id", "key_id"),
        CheckConstraint("credential_type IN ('public_key', 'certificate')"),
        CheckConstraint("credential_fingerprint ~ '^[0-9a-f]{64}$'"),
        CheckConstraint("status IN ('active', 'revoked', 'expired')"),
        CheckConstraint("expires_at IS NULL OR expires_at > issued_at"),
        CheckConstraint("revoked_at IS NULL OR revoked_at >= issued_at"),
        Index(
            "device_credentials_device_idx",
            "tenant_id",
            "device_id",
            "status",
        ),
        {"schema": "device"},
    )

    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    credential_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    device_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    credential_type: Mapped[str] = mapped_column(Text)
    key_id: Mapped[str] = mapped_column(Text)
    credential_fingerprint: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(Text, default="active", server_default="active")
    issued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.current_timestamp()
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PairingSessionModel(HermesCloudBase):
    __tablename__ = "pairing_sessions"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "pairing_session_id"),
        ForeignKeyConstraint(["tenant_id"], ["identity.tenants.tenant_id"]),
        ForeignKeyConstraint(
            ["tenant_id", "workspace_id"],
            ["workspace.workspaces.tenant_id", "workspace.workspaces.workspace_id"],
        ),
        ForeignKeyConstraint(
            ["tenant_id", "agent_id"],
            ["device.agents.tenant_id", "device.agents.agent_id"],
        ),
        ForeignKeyConstraint(
            ["tenant_id", "device_id"],
            ["device.devices.tenant_id", "device.devices.device_id"],
        ),
        UniqueConstraint("tenant_id", "pairing_code_digest"),
        CheckConstraint("pairing_code_digest ~ '^[0-9a-f]{64}$'"),
        CheckConstraint(
            "state IN ('pending', 'claimed', 'confirmed', 'expired', 'cancelled')"
        ),
        CheckConstraint("failed_attempts >= 0"),
        CheckConstraint("expires_at > created_at"),
        CheckConstraint("claimed_at IS NULL OR claimed_at >= created_at"),
        CheckConstraint("confirmed_at IS NULL OR confirmed_at >= claimed_at"),
        Index(
            "pairing_sessions_expiry_idx",
            "tenant_id",
            "state",
            "expires_at",
        ),
        Index(
            "pairing_sessions_agent_idx",
            "tenant_id",
            "agent_id",
            "created_at",
        ),
        {"schema": "device"},
    )

    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    pairing_session_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    workspace_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    agent_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    device_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    pairing_code_digest: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(
        Text, default="pending", server_default="pending"
    )
    failed_attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.current_timestamp()
    )


class PairingOfferModel(HermesCloudBase):
    """Tenant-neutral bootstrap offer created before authenticated owner claim."""

    __tablename__ = "pairing_offers"
    __table_args__ = (
        PrimaryKeyConstraint("pairing_offer_id"),
        UniqueConstraint("pairing_code_digest"),
        UniqueConstraint("bootstrap_secret_digest"),
        CheckConstraint("pairing_code_digest ~ '^[0-9a-f]{64}$'"),
        CheckConstraint("bootstrap_secret_digest ~ '^[0-9a-f]{64}$'"),
        CheckConstraint("pairing_code_digest <> bootstrap_secret_digest"),
        CheckConstraint("public_key_algorithm = 'ed25519'"),
        CheckConstraint("octet_length(public_key) = 32"),
        CheckConstraint("credential_fingerprint ~ '^[0-9a-f]{64}$'"),
        CheckConstraint("state IN ('pending', 'claimed', 'expired', 'cancelled')"),
        CheckConstraint("revision BETWEEN 0 AND 1"),
        CheckConstraint("expires_at > created_at"),
        CheckConstraint("claimed_at IS NULL OR claimed_at >= created_at"),
        Index("pairing_offers_expiry_idx", "state", "expires_at"),
        Index("pairing_offers_fingerprint_idx", "credential_fingerprint", "state"),
        {"schema": "device"},
    )

    pairing_offer_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    pairing_code_digest: Mapped[str] = mapped_column(String(64))
    bootstrap_secret_digest: Mapped[str] = mapped_column(String(64))
    public_key_algorithm: Mapped[str] = mapped_column(Text)
    public_key: Mapped[bytes] = mapped_column(LargeBinary(32))
    credential_fingerprint: Mapped[str] = mapped_column(String(64))
    key_id: Mapped[str] = mapped_column(Text)
    device_key: Mapped[str] = mapped_column(Text)
    device_name: Mapped[str] = mapped_column(Text)
    platform: Mapped[str] = mapped_column(Text)
    connector_version: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(
        Text, default="pending", server_default="pending"
    )
    revision: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    enrollment_proof: Mapped[PairingEnrollmentProofModel | None] = relationship(
        back_populates="offer",
        uselist=False,
    )


class DeviceLifecycleModel(HermesCloudBase):
    """Authorization lifecycle kept separate from live/offline connectivity."""

    __tablename__ = "device_lifecycles"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "device_id"),
        ForeignKeyConstraint(["tenant_id"], ["identity.tenants.tenant_id"]),
        ForeignKeyConstraint(
            ["tenant_id", "device_id"],
            ["device.devices.tenant_id", "device.devices.device_id"],
        ),
        ForeignKeyConstraint(
            ["tenant_id", "workspace_id"],
            ["workspace.workspaces.tenant_id", "workspace.workspaces.workspace_id"],
        ),
        ForeignKeyConstraint(
            ["tenant_id", "agent_id"],
            ["device.agents.tenant_id", "device.agents.agent_id"],
        ),
        CheckConstraint(
            "state IN ('pending', 'active', 'suspended', 'revoked', 'retired')"
        ),
        CheckConstraint("revision >= 0"),
        Index(
            "device_lifecycles_scope_idx",
            "tenant_id",
            "workspace_id",
            "agent_id",
            "state",
        ),
        {"schema": "device"},
    )

    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    device_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    workspace_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    agent_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    state: Mapped[str] = mapped_column(Text)
    revision: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PairingEnrollmentProofModel(HermesCloudBase):
    """Owner claim and challenge digest bound to a tenant-scoped session."""

    __tablename__ = "pairing_enrollment_proofs"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "pairing_session_id"),
        ForeignKeyConstraint(["tenant_id"], ["identity.tenants.tenant_id"]),
        ForeignKeyConstraint(
            ["tenant_id", "pairing_session_id"],
            [
                "device.pairing_sessions.tenant_id",
                "device.pairing_sessions.pairing_session_id",
            ],
        ),
        ForeignKeyConstraint(
            ["pairing_offer_id"],
            ["device.pairing_offers.pairing_offer_id"],
        ),
        ForeignKeyConstraint(
            ["tenant_id", "owner_user_id"],
            ["identity.users.tenant_id", "identity.users.user_id"],
        ),
        UniqueConstraint("tenant_id", "pairing_offer_id"),
        UniqueConstraint("tenant_id", "claim_id"),
        CheckConstraint("jsonb_typeof(scopes) = 'array'"),
        CheckConstraint(
            "challenge_digest IS NULL OR challenge_digest ~ '^[0-9a-f]{64}$'"
        ),
        CheckConstraint(
            "confirmation_digest IS NULL OR confirmation_digest ~ '^[0-9a-f]{64}$'"
        ),
        CheckConstraint("revision BETWEEN 1 AND 4"),
        CheckConstraint(
            "(challenge_id IS NULL AND challenge_digest IS NULL "
            "AND challenge_expires_at IS NULL AND owner_confirmed_at IS NULL) "
            "OR (challenge_id IS NOT NULL AND challenge_digest IS NOT NULL "
            "AND challenge_expires_at IS NOT NULL "
            "AND owner_confirmed_at IS NOT NULL)"
        ),
        CheckConstraint(
            "challenge_expires_at IS NULL OR challenge_expires_at > owner_confirmed_at"
        ),
        CheckConstraint("confirmation_digest IS NULL OR challenge_digest IS NOT NULL"),
        CheckConstraint("updated_at >= created_at"),
        Index(
            "pairing_enrollment_owner_idx",
            "tenant_id",
            "owner_user_id",
            "created_at",
        ),
        Index(
            "pairing_enrollment_challenge_idx",
            "tenant_id",
            "challenge_digest",
            "challenge_expires_at",
        ),
        {"schema": "device"},
    )

    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    pairing_session_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    pairing_offer_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    owner_user_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    device_display_name: Mapped[str] = mapped_column(Text)
    claim_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    scopes: Mapped[list[str]] = mapped_column(JSONB)
    challenge_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    challenge_digest: Mapped[str | None] = mapped_column(String(64))
    challenge_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    owner_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confirmation_digest: Mapped[str | None] = mapped_column(String(64))
    revision: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    offer: Mapped[PairingOfferModel] = relationship(
        back_populates="enrollment_proof",
    )


class PairingClaimLimitModel(HermesCloudBase):
    """Tenant-scoped failed-code window for one authenticated owner."""

    __tablename__ = "pairing_claim_limits"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "owner_user_id"),
        ForeignKeyConstraint(["tenant_id"], ["identity.tenants.tenant_id"]),
        ForeignKeyConstraint(
            ["tenant_id", "owner_user_id"],
            ["identity.users.tenant_id", "identity.users.user_id"],
        ),
        CheckConstraint("failed_attempts BETWEEN 0 AND 5"),
        CheckConstraint("window_expires_at > window_started_at"),
        CheckConstraint("updated_at >= window_started_at"),
        Index("pairing_claim_limits_expiry_idx", "tenant_id", "window_expires_at"),
        {"schema": "device"},
    )

    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    owner_user_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    failed_attempts: Mapped[int] = mapped_column(Integer)
    revision: Mapped[int] = mapped_column(Integer)
    window_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    window_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PairingIdempotencyModel(HermesCloudBase):
    """Digest-only mutation ledger shared by bootstrap and owner operations."""

    __tablename__ = "pairing_idempotency_records"
    __table_args__ = (
        PrimaryKeyConstraint("pairing_mutation_id"),
        ForeignKeyConstraint(
            ["pairing_offer_id"],
            ["device.pairing_offers.pairing_offer_id"],
        ),
        UniqueConstraint(
            "operation",
            "idempotency_key_digest",
            "principal_digest",
        ),
        CheckConstraint(
            "operation IN "
            "('create', 'claim', 'confirm', 'proof', 'cancel', 'revoke', "
            "'device_challenge', 'device_token')"
        ),
        CheckConstraint("idempotency_key_digest ~ '^[0-9a-f]{64}$'"),
        CheckConstraint("principal_digest ~ '^[0-9a-f]{64}$'"),
        CheckConstraint("request_digest ~ '^[0-9a-f]{64}$'"),
        CheckConstraint("expected_revision >= 0"),
        CheckConstraint("result_revision >= 0"),
        CheckConstraint(
            "result_state IN "
            "('pending', 'claimed', 'confirmed', 'expired', 'cancelled', "
            "'active', 'suspended', 'revoked', 'retired')"
        ),
        CheckConstraint(
            "result_code IN "
            "('OK', 'PAIRING_INVALID_REQUEST', 'UNAUTHORIZED', 'FORBIDDEN', "
            "'PAIRING_NOT_FOUND', 'PAIRING_STATE_CONFLICT', 'PAIRING_EXPIRED', "
            "'CHALLENGE_EXPIRED', 'CHALLENGE_INVALID', 'CHALLENGE_REPLAYED', "
            "'DEVICE_AUTH_UNAVAILABLE', "
            "'RATE_LIMITED', 'PAIRING_CLAIM_UNAVAILABLE', "
            "'PAIRING_CLAIM_RATE_LIMITED')"
        ),
        CheckConstraint(
            "retry_after_seconds IS NULL OR retry_after_seconds BETWEEN 1 AND 300"
        ),
        CheckConstraint(
            "(result_code = 'PAIRING_CLAIM_RATE_LIMITED' "
            "AND retry_after_seconds IS NOT NULL) "
            "OR (result_code <> 'PAIRING_CLAIM_RATE_LIMITED' "
            "AND retry_after_seconds IS NULL)"
        ),
        CheckConstraint("expires_at > created_at"),
        Index(
            "pairing_idempotency_expiry_idx",
            "expires_at",
        ),
        Index(
            "pairing_idempotency_offer_idx",
            "pairing_offer_id",
            "operation",
        ),
        {"schema": "device"},
    )

    pairing_mutation_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    pairing_offer_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    operation: Mapped[str] = mapped_column(Text)
    idempotency_key_digest: Mapped[str] = mapped_column(String(64))
    principal_digest: Mapped[str] = mapped_column(String(64))
    request_digest: Mapped[str] = mapped_column(String(64))
    expected_revision: Mapped[int] = mapped_column(Integer)
    result_revision: Mapped[int] = mapped_column(Integer)
    result_state: Mapped[str] = mapped_column(Text)
    result_code: Mapped[str] = mapped_column(Text)
    retry_after_seconds: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class DeviceCredentialPublicKeyModel(HermesCloudBase):
    """Verifiable public key material for an activated device credential."""

    __tablename__ = "device_credential_public_keys"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "credential_id"),
        ForeignKeyConstraint(["tenant_id"], ["identity.tenants.tenant_id"]),
        ForeignKeyConstraint(
            ["tenant_id", "credential_id"],
            [
                "device.device_credentials.tenant_id",
                "device.device_credentials.credential_id",
            ],
        ),
        UniqueConstraint("tenant_id", "credential_fingerprint"),
        CheckConstraint("algorithm = 'ed25519'"),
        CheckConstraint("octet_length(public_key) = 32"),
        CheckConstraint("credential_fingerprint ~ '^[0-9a-f]{64}$'"),
        Index(
            "device_credential_public_keys_lookup_idx",
            "tenant_id",
            "credential_fingerprint",
        ),
        {"schema": "device"},
    )

    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    credential_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    algorithm: Mapped[str] = mapped_column(Text)
    public_key: Mapped[bytes] = mapped_column(LargeBinary(32))
    credential_fingerprint: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class DeviceAuthenticationChallengeModel(HermesCloudBase):
    """Digest-only later-authentication challenge with atomic consumption."""

    __tablename__ = "device_authentication_challenges"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "challenge_id"),
        ForeignKeyConstraint(["tenant_id"], ["identity.tenants.tenant_id"]),
        ForeignKeyConstraint(
            ["tenant_id", "device_id"],
            ["device.devices.tenant_id", "device.devices.device_id"],
        ),
        ForeignKeyConstraint(
            ["tenant_id", "credential_id"],
            [
                "device.device_credentials.tenant_id",
                "device.device_credentials.credential_id",
            ],
        ),
        ForeignKeyConstraint(
            ["pairing_mutation_id"],
            ["device.pairing_idempotency_records.pairing_mutation_id"],
        ),
        UniqueConstraint("pairing_mutation_id"),
        CheckConstraint("challenge_digest ~ '^[0-9a-f]{64}$'"),
        CheckConstraint("proof_digest IS NULL OR proof_digest ~ '^[0-9a-f]{64}$'"),
        CheckConstraint("expires_at > issued_at"),
        CheckConstraint("consumed_at IS NULL OR consumed_at >= issued_at"),
        CheckConstraint("revision BETWEEN 0 AND 1"),
        CheckConstraint(
            "(proof_digest IS NULL AND consumed_at IS NULL AND revision = 0) "
            "OR (proof_digest IS NOT NULL AND consumed_at IS NOT NULL "
            "AND revision = 1)"
        ),
        Index(
            "device_authentication_challenges_lookup_idx",
            "tenant_id",
            "device_id",
            "credential_id",
            "expires_at",
        ),
        {"schema": "device"},
    )

    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    challenge_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    device_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    credential_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    pairing_mutation_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    challenge_digest: Mapped[str] = mapped_column(String(64))
    proof_digest: Mapped[str | None] = mapped_column(String(64))
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revision: Mapped[int] = mapped_column(Integer)


class PasswordCredentialModel(HermesCloudBase):
    __tablename__ = "password_credentials"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "credential_id"),
        ForeignKeyConstraint(["tenant_id"], ["identity.tenants.tenant_id"]),
        ForeignKeyConstraint(
            ["tenant_id", "user_id"],
            ["identity.users.tenant_id", "identity.users.user_id"],
        ),
        UniqueConstraint("tenant_id", "subject"),
        UniqueConstraint("tenant_id", "user_id"),
        CheckConstraint("password_hash LIKE '$argon2id$%'"),
        CheckConstraint("status IN ('active', 'disabled')"),
        Index(
            "password_credentials_subject_idx",
            "tenant_id",
            "subject",
            "status",
        ),
        {"schema": "identity"},
    )

    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    credential_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    user_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    subject: Mapped[str] = mapped_column(Text)
    password_hash: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        Text,
        default="active",
        server_default="active",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.current_timestamp(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.current_timestamp(),
    )


class RefreshSessionModel(HermesCloudBase):
    __tablename__ = "refresh_sessions"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "refresh_session_id"),
        ForeignKeyConstraint(["tenant_id"], ["identity.tenants.tenant_id"]),
        ForeignKeyConstraint(
            ["tenant_id", "user_id"],
            ["identity.users.tenant_id", "identity.users.user_id"],
        ),
        UniqueConstraint("tenant_id", "token_digest"),
        CheckConstraint("token_digest ~ '^[0-9a-f]{64}$'"),
        CheckConstraint("rotation >= 0"),
        CheckConstraint("expires_at > created_at"),
        CheckConstraint("rotated_at IS NULL OR rotated_at >= created_at"),
        CheckConstraint("revoked_at IS NULL OR revoked_at >= created_at"),
        CheckConstraint("retention_until >= expires_at"),
        Index(
            "refresh_sessions_user_expiry_idx",
            "tenant_id",
            "user_id",
            "revoked_at",
            "expires_at",
        ),
        Index(
            "refresh_sessions_retention_idx",
            "tenant_id",
            "retention_until",
        ),
        {"schema": "identity"},
    )

    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    refresh_session_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    user_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    token_digest: Mapped[str] = mapped_column(String(64))
    rotation: Mapped[int] = mapped_column(
        BigInteger,
        default=0,
        server_default="0",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.current_timestamp(),
    )
    rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    retention_until: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SessionProjectionModel(HermesCloudBase):
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
        UniqueConstraint("tenant_id", "agent_id", "profile", "session_key"),
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
            "session_projection_identity_idx",
            "tenant_id",
            "agent_id",
            "profile",
            "session_key",
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
    profile: Mapped[str] = mapped_column(String(128))
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


class SessionMessageProjectionModel(HermesCloudBase):
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


class SessionEventProjectionModel(HermesCloudBase):
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


class SessionProjectionCursorModel(HermesCloudBase):
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


class WebSocketTicketModel(HermesCloudBase):
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
            ["tenant_id", "session_id"],
            ["projection.sessions.tenant_id", "projection.sessions.session_id"],
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
    session_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=True,
    )
    observer_scope: Mapped[list[str]] = mapped_column(JSONB)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retention_until: Mapped[datetime] = mapped_column(DateTime(timezone=True))


Index(
    "session_projection_legacy_identity_uq",
    SessionProjectionModel.tenant_id,
    SessionProjectionModel.profile,
    SessionProjectionModel.session_key,
    unique=True,
    sqlite_where=SessionProjectionModel.agent_id.is_(None),
    postgresql_where=SessionProjectionModel.agent_id.is_(None),
)


V7_TENANT_MODELS = (
    SessionProjectionModel,
    SessionMessageProjectionModel,
    SessionEventProjectionModel,
    SessionProjectionCursorModel,
    PasswordCredentialModel,
    RefreshSessionModel,
    WebSocketTicketModel,
)

PAIRING_V8_MODELS = (
    PairingOfferModel,
    DeviceLifecycleModel,
    PairingEnrollmentProofModel,
    PairingClaimLimitModel,
    PairingIdempotencyModel,
    DeviceCredentialPublicKeyModel,
    DeviceAuthenticationChallengeModel,
)

PAIRING_V8_TENANT_MODELS = (
    DeviceLifecycleModel,
    PairingEnrollmentProofModel,
    PairingClaimLimitModel,
    DeviceCredentialPublicKeyModel,
    DeviceAuthenticationChallengeModel,
)


ALL_TENANT_MODELS = (
    TenantModel,
    UserModel,
    MembershipModel,
    RoleModel,
    WorkspaceModel,
    WorkspaceMembershipModel,
    AgentModel,
    DeviceModel,
    DeviceCredentialModel,
    PairingSessionModel,
    CommandModel,
    CommandAttemptModel,
    CommandTransitionModel,
    PolicyModel,
    AccessGrantModel,
    OutboxEventModel,
    InboxMessageModel,
    AuditEventModel,
    ConnectorTransportCursorModel,
    ConnectorTransportHandshakeOwnershipModel,
    ConnectorObserverReceiptModel,
    *V7_TENANT_MODELS,
    *PAIRING_V8_TENANT_MODELS,
)
