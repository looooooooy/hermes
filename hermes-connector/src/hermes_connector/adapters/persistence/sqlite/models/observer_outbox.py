from __future__ import annotations

from sqlalchemy import CheckConstraint, Index, LargeBinary, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from hermes_connector.adapters.sqlite_models import (
    MAX_DURABLE_PAYLOAD_BYTES,
    Base,
)


class ObserverOutboxRow(Base):
    __tablename__ = "observer_outbox"

    message_id: Mapped[str] = mapped_column(Text, primary_key=True)
    payload_digest: Mapped[str] = mapped_column(Text, nullable=False)
    connector_sequence: Mapped[int] = mapped_column(nullable=False)
    transport_epoch_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    message_type: Mapped[str] = mapped_column(Text, nullable=False)
    profile: Mapped[str] = mapped_column(Text, nullable=False)
    session_key: Mapped[str] = mapped_column(Text, nullable=False)
    runtime_generation: Mapped[str] = mapped_column(Text, nullable=False)
    runtime_session_id: Mapped[str] = mapped_column(Text, nullable=False)
    event_sequence: Mapped[int] = mapped_column(nullable=False)
    payload: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    frame: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    settled_at: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint(
            func.length(payload_digest) == 64,
            name="ck_observer_outbox_digest_length",
        ),
        CheckConstraint(
            connector_sequence >= 0,
            name="ck_observer_outbox_connector_sequence_nonnegative",
        ),
        CheckConstraint(
            (transport_epoch_id.is_(None)) | (func.length(transport_epoch_id) == 36),
            name="ck_observer_outbox_epoch_length",
        ),
        CheckConstraint(
            func.length(message_id) == 36,
            name="ck_observer_outbox_message_id_length",
        ),
        CheckConstraint(
            func.length(runtime_generation).between(1, 128),
            name="ck_observer_outbox_runtime_generation_length",
        ),
        CheckConstraint(
            func.length(profile).between(1, 128),
            name="ck_observer_outbox_profile_length",
        ),
        CheckConstraint(
            func.length(session_key).between(1, 256),
            name="ck_observer_outbox_session_key_length",
        ),
        CheckConstraint(
            func.length(runtime_session_id).between(1, 256),
            name="ck_observer_outbox_runtime_session_id_length",
        ),
        CheckConstraint(
            event_sequence >= 0,
            name="ck_observer_outbox_event_sequence_nonnegative",
        ),
        CheckConstraint(
            message_type.in_(
                (
                    "session.snapshot",
                    "session.event",
                    "session.snapshot.v2",
                    "session.event.v2",
                )
            ),
            name="ck_observer_outbox_message_type",
        ),
        CheckConstraint(
            state.in_(("pending", "acked", "rejected", "retired")),
            name="ck_observer_outbox_state",
        ),
        CheckConstraint(
            func.length(payload) <= MAX_DURABLE_PAYLOAD_BYTES,
            name="ck_observer_outbox_payload_size",
        ),
        CheckConstraint(
            func.length(frame) <= MAX_DURABLE_PAYLOAD_BYTES,
            name="ck_observer_outbox_frame_size",
        ),
        Index(
            "idx_observer_outbox_fact_attempt",
            message_type,
            profile,
            session_key,
            runtime_generation,
            runtime_session_id,
            event_sequence,
            connector_sequence,
        ),
        UniqueConstraint(
            transport_epoch_id,
            connector_sequence,
            name="uq_observer_outbox_epoch_sequence",
        ),
        Index(
            "idx_observer_outbox_pending_sequence",
            state,
            connector_sequence,
        ),
    )


__all__ = ["ObserverOutboxRow"]
