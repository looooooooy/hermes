"""SQLAlchemy ORM mappings for durable Observer subscription routing."""

from __future__ import annotations

from datetime import datetime
from typing import Any, ClassVar
from uuid import UUID

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class ObserverSubscriptionBase(DeclarativeBase):
    """Metadata isolated for the published Observer subscription migration."""


class ObserverSubscriptionTargetModel(ObserverSubscriptionBase):
    __tablename__ = "observer_subscription_targets"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "target_subscription_id"),
        UniqueConstraint("tenant_id", "agent_id", "profile", "session_key"),
        CheckConstraint("active_ref_count >= 0"),
        CheckConstraint("next_intent_sequence >= 0"),
        CheckConstraint("revision > 0"),
        CheckConstraint("state IN ('active', 'closing', 'closed')"),
        Index(
            "observer_subscription_targets_route_idx",
            "tenant_id",
            "device_id",
            "agent_id",
            "state",
        ),
    )

    tenant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True))
    target_subscription_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True))
    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True))
    agent_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True))
    device_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True))
    profile: Mapped[str] = mapped_column(String(128))
    session_key: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String(16))
    active_ref_count: Mapped[int] = mapped_column(BigInteger)
    next_intent_sequence: Mapped[int] = mapped_column(BigInteger)
    revision: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __mapper_args__: ClassVar[dict[str, object]] = {"version_id_col": revision}


class ObserverSubscriptionLeaseModel(ObserverSubscriptionBase):
    __tablename__ = "observer_subscription_leases"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "client_subscription_id"),
        ForeignKeyConstraint(
            ["tenant_id", "target_subscription_id"],
            [
                "observer_subscription_targets.tenant_id",
                "observer_subscription_targets.target_subscription_id",
            ],
        ),
        CheckConstraint("state IN ('active', 'closed', 'expired')"),
        CheckConstraint("expires_at > created_at"),
        Index(
            "observer_subscription_leases_expiry_idx",
            "tenant_id",
            "state",
            "expires_at",
        ),
    )

    tenant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True))
    client_subscription_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True))
    target_subscription_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True))
    user_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True))
    state: Mapped[str] = mapped_column(String(16))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ObserverSubscriptionIntentModel(ObserverSubscriptionBase):
    __tablename__ = "observer_subscription_intents"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "request_id"),
        ForeignKeyConstraint(
            ["tenant_id", "target_subscription_id"],
            [
                "observer_subscription_targets.tenant_id",
                "observer_subscription_targets.target_subscription_id",
            ],
        ),
        CheckConstraint(
            "message_type IN ('session.observe.open', 'session.observe.close')"
        ),
        CheckConstraint("state IN ('pending', 'dispatching', 'settled', 'cancelled')"),
        CheckConstraint("dispatch_sequence IS NULL OR dispatch_sequence >= 0"),
        CheckConstraint("dispatch_attempts >= 0"),
        CheckConstraint("intent_sequence >= 0"),
        UniqueConstraint("tenant_id", "target_subscription_id", "intent_sequence"),
        Index(
            "observer_subscription_intents_route_idx",
            "tenant_id",
            "device_id",
            "agent_id",
            "state",
            "created_at",
        ),
    )

    tenant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True))
    request_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True))
    supersedes_request_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    target_subscription_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True))
    intent_sequence: Mapped[int] = mapped_column(BigInteger)
    workspace_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True))
    agent_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True))
    device_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True))
    message_type: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    state: Mapped[str] = mapped_column(String(16))
    dispatch_connection_id: Mapped[str | None] = mapped_column(String(36))
    dispatch_sequence: Mapped[int | None] = mapped_column(BigInteger)
    dispatch_attempts: Mapped[int] = mapped_column(BigInteger)
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    observer_contract: Mapped[int | None] = mapped_column()
    wire_message_type: Mapped[str | None] = mapped_column(String(35))
    wire_payload_digest: Mapped[str | None] = mapped_column(String(64))


class ObserverConnectorRouteModel(ObserverSubscriptionBase):
    __tablename__ = "observer_connector_routes"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "device_id"),
        UniqueConstraint("tenant_id", "connection_id"),
        CheckConstraint("state IN ('active', 'offline')"),
        CheckConstraint("next_connector_sequence >= 0"),
        CheckConstraint("next_cloud_sequence >= 0"),
        CheckConstraint("revision > 0"),
        Index(
            "observer_connector_routes_active_idx",
            "tenant_id",
            "state",
            "updated_at",
        ),
    )

    tenant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True))
    device_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True))
    agent_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True))
    connector_instance_id: Mapped[str] = mapped_column(String(36))
    connection_id: Mapped[str] = mapped_column(String(36))
    runtime_generation: Mapped[str] = mapped_column(String(128))
    state: Mapped[str] = mapped_column(String(16))
    next_connector_sequence: Mapped[int] = mapped_column(BigInteger)
    next_cloud_sequence: Mapped[int] = mapped_column(BigInteger)
    revision: Mapped[int] = mapped_column(BigInteger)
    connected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __mapper_args__: ClassVar[dict[str, object]] = {"version_id_col": revision}
