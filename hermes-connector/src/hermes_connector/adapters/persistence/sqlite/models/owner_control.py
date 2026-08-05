from __future__ import annotations

from sqlalchemy import Boolean, CheckConstraint, Index, LargeBinary, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from hermes_connector.adapters.sqlite_models import MAX_DURABLE_PAYLOAD_BYTES, Base


class OwnerControlResultRow(Base):
    __tablename__ = "owner_control_results"

    request_id: Mapped[str] = mapped_column(Text, primary_key=True)
    request_digest: Mapped[str] = mapped_column(Text, nullable=False)
    control_transport_id: Mapped[str] = mapped_column(Text, nullable=False)
    operation: Mapped[str] = mapped_column(Text, nullable=False)
    request_payload: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    scope_payload: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    response_payload: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    response_revision: Mapped[int] = mapped_column(nullable=False)
    transport_received: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)
    completed_at: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint(
            func.length(request_digest) == 64,
            name="ck_owner_control_digest_length",
        ),
        CheckConstraint(
            func.length(request_id) == 36,
            name="ck_owner_control_request_id_length",
        ),
        CheckConstraint(
            func.length(control_transport_id) == 36,
            name="ck_owner_control_transport_id_length",
        ),
        CheckConstraint(
            operation.in_(
                (
                    "control.transport.open",
                    "session.control.acquire",
                    "session.control.renew",
                    "session.control.release",
                    "session.control.status",
                    "control.transport.close",
                )
            ),
            name="ck_owner_control_operation",
        ),
        CheckConstraint(
            state.in_(("received", "executing", "completed", "effect_unknown")),
            name="ck_owner_control_state",
        ),
        CheckConstraint(
            response_revision >= 1,
            name="ck_owner_control_revision_positive",
        ),
        CheckConstraint(
            func.length(request_payload) <= MAX_DURABLE_PAYLOAD_BYTES,
            name="ck_owner_control_request_size",
        ),
        CheckConstraint(
            func.length(scope_payload) <= MAX_DURABLE_PAYLOAD_BYTES,
            name="ck_owner_control_scope_size",
        ),
        CheckConstraint(
            response_payload.is_(None)
            | (func.length(response_payload) <= MAX_DURABLE_PAYLOAD_BYTES),
            name="ck_owner_control_response_size",
        ),
        Index(
            "idx_owner_control_state_updated",
            state,
            updated_at,
            request_id,
        ),
    )


__all__ = ["OwnerControlResultRow"]
