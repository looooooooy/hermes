from __future__ import annotations

from sqlalchemy import Boolean, CheckConstraint, Integer, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from hermes_connector.adapters.sqlite_models import Base


class CloudSessionCheckpointRow(Base):
    """Singleton durable checkpoint for Connector Protocol resume."""

    __tablename__ = "cloud_session_checkpoint"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    previous_connection_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_outbound_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    next_inbound_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    reconciliation_required: Mapped[bool] = mapped_column(Boolean, nullable=False)
    transport_epoch_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    runtime_generation: Mapped[str | None] = mapped_column(Text, nullable=True)
    fresh_epoch_required: Mapped[bool] = mapped_column(Boolean, nullable=False)
    transport_recovery_floor: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        CheckConstraint(id == 1, name="ck_cloud_session_singleton"),
        CheckConstraint(
            next_outbound_sequence >= 0,
            name="ck_cloud_outbound_sequence_nonnegative",
        ),
        CheckConstraint(
            next_inbound_sequence >= 0,
            name="ck_cloud_inbound_sequence_nonnegative",
        ),
        CheckConstraint(
            transport_recovery_floor >= 0,
            name="ck_cloud_transport_recovery_floor_nonnegative",
        ),
        CheckConstraint(
            (previous_connection_id.is_(None))
            | (func.length(previous_connection_id) == 36),
            name="ck_cloud_previous_connection_id_length",
        ),
        CheckConstraint(
            (transport_epoch_id.is_(None)) | (func.length(transport_epoch_id) == 36),
            name="ck_cloud_transport_epoch_id_length",
        ),
        CheckConstraint(
            (runtime_generation.is_(None))
            | func.length(runtime_generation).between(1, 128),
            name="ck_cloud_runtime_generation_length",
        ),
    )
