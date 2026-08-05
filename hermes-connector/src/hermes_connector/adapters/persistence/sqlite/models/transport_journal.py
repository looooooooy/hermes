from __future__ import annotations

from sqlalchemy import CheckConstraint, Index, LargeBinary, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from hermes_connector.adapters.sqlite_models import MAX_DURABLE_PAYLOAD_BYTES, Base


class TransportFrameJournalRow(Base):
    __tablename__ = "transport_frame_journal"

    message_id: Mapped[str] = mapped_column(Text, primary_key=True)
    epoch_id: Mapped[str] = mapped_column(Text, nullable=False)
    sequence: Mapped[int] = mapped_column(nullable=False)
    message_type: Mapped[str] = mapped_column(Text, nullable=False)
    business_kind: Mapped[str] = mapped_column(Text, nullable=False)
    business_key: Mapped[str] = mapped_column(Text, nullable=False)
    business_revision: Mapped[int] = mapped_column(nullable=False)
    runtime_generation: Mapped[str | None] = mapped_column(Text, nullable=True)
    frame: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)
    settled_at: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint(
            sequence >= 0, name="ck_transport_journal_sequence_nonnegative"
        ),
        CheckConstraint(
            func.length(message_id) == 36,
            name="ck_transport_journal_message_id_length",
        ),
        CheckConstraint(
            func.length(epoch_id) == 36,
            name="ck_transport_journal_epoch_id_length",
        ),
        CheckConstraint(
            func.length(message_type).between(1, 64),
            name="ck_transport_journal_message_type_length",
        ),
        CheckConstraint(
            func.length(business_kind).between(1, 64),
            name="ck_transport_journal_business_kind_length",
        ),
        CheckConstraint(
            func.length(business_key).between(1, 512),
            name="ck_transport_journal_business_key_length",
        ),
        CheckConstraint(
            (runtime_generation.is_(None))
            | func.length(runtime_generation).between(1, 128),
            name="ck_transport_journal_runtime_generation_length",
        ),
        CheckConstraint(
            message_type.in_(
                (
                    "connector.heartbeat",
                    "command.receipt",
                    "command.result",
                    "control.response",
                    "session.snapshot",
                    "session.event",
                    "session.snapshot.v2",
                    "session.event.v2",
                    "session.catalog.snapshot.page",
                    "session.catalog.event",
                )
            ),
            name="ck_transport_journal_message_type",
        ),
        CheckConstraint(
            business_kind.in_(
                (
                    "heartbeat",
                    "command.receipt",
                    "command.result",
                    "control.response",
                    "observer",
                    "session_catalog",
                )
            ),
            name="ck_transport_journal_business_kind",
        ),
        CheckConstraint(
            ((message_type == "connector.heartbeat") & (business_kind == "heartbeat"))
            | (
                (message_type == "command.receipt")
                & (business_kind == "command.receipt")
            )
            | ((message_type == "command.result") & (business_kind == "command.result"))
            | (
                (message_type == "control.response")
                & (business_kind == "control.response")
            )
            | (
                message_type.in_(
                    (
                        "session.snapshot",
                        "session.event",
                        "session.snapshot.v2",
                        "session.event.v2",
                    )
                )
                & (business_kind == "observer")
            )
            | (
                message_type.in_(
                    (
                        "session.catalog.snapshot.page",
                        "session.catalog.event",
                    )
                )
                & (business_kind == "session_catalog")
            ),
            name="ck_transport_journal_business_pair",
        ),
        CheckConstraint(
            (business_kind == "heartbeat") | (func.length(business_key) == 36),
            name="ck_transport_journal_business_key_identity",
        ),
        CheckConstraint(
            business_revision >= 0,
            name="ck_transport_journal_revision_nonnegative",
        ),
        CheckConstraint(
            state.in_(("staged", "sent", "settled", "retired")),
            name="ck_transport_journal_state",
        ),
        CheckConstraint(
            func.length(frame) <= MAX_DURABLE_PAYLOAD_BYTES,
            name="ck_transport_journal_frame_size",
        ),
        UniqueConstraint(
            epoch_id,
            sequence,
            name="uq_transport_journal_epoch_sequence",
        ),
        UniqueConstraint(
            epoch_id,
            business_kind,
            business_key,
            business_revision,
            name="uq_transport_journal_business_attempt",
        ),
        Index(
            "idx_transport_journal_epoch_state_sequence",
            epoch_id,
            state,
            sequence,
        ),
        Index(
            "idx_transport_journal_retention",
            state,
            updated_at,
            message_id,
        ),
    )


__all__ = ["TransportFrameJournalRow"]
