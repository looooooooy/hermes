from __future__ import annotations

from sqlalchemy import Boolean, CheckConstraint, Index, LargeBinary, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from hermes_connector.adapters.sqlite_models import (
    MAX_DURABLE_PAYLOAD_BYTES,
    Base,
)


class ControlCommandRow(Base):
    __tablename__ = "control_commands"

    command_id: Mapped[str] = mapped_column(Text, primary_key=True)
    message_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    digest: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    delivery_payload: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    receipt_payload: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    result_payload: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    expires_at: Mapped[str] = mapped_column(Text, nullable=False)
    receipt_revision: Mapped[int] = mapped_column(nullable=False)
    revision: Mapped[int] = mapped_column(nullable=False)
    receipt_acknowledged: Mapped[bool] = mapped_column(Boolean, nullable=False)
    result_acknowledged: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)
    completed_at: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint(
            state.in_(("delivered", "executing", "succeeded", "failed", "unknown")),
            name="ck_control_command_state",
        ),
        CheckConstraint(revision >= 1, name="ck_control_command_revision_positive"),
        CheckConstraint(
            receipt_revision >= 1,
            name="ck_control_command_receipt_revision_positive",
        ),
        CheckConstraint(
            func.length(delivery_payload) <= MAX_DURABLE_PAYLOAD_BYTES,
            name="ck_control_command_delivery_size",
        ),
        CheckConstraint(
            func.length(receipt_payload) <= MAX_DURABLE_PAYLOAD_BYTES,
            name="ck_control_command_receipt_size",
        ),
        CheckConstraint(
            (result_payload.is_(None))
            | (func.length(result_payload) <= MAX_DURABLE_PAYLOAD_BYTES),
            name="ck_control_command_result_size",
        ),
        Index(
            "idx_control_commands_state_updated",
            state,
            updated_at,
            command_id,
        ),
    )
