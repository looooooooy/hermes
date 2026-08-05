from __future__ import annotations

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Index,
    LargeBinary,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from hermes_connector.adapters.sqlite_models import MAX_DURABLE_PAYLOAD_BYTES, Base


class SessionCatalogOutboxRow(Base):
    __tablename__ = "session_catalog_outbox"

    message_id: Mapped[str] = mapped_column(Text, primary_key=True)
    payload_digest: Mapped[str] = mapped_column(Text, nullable=False)
    connector_sequence: Mapped[int] = mapped_column(nullable=False)
    transport_epoch_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    message_type: Mapped[str] = mapped_column(Text, nullable=False)
    profile: Mapped[str] = mapped_column(Text, nullable=False)
    runtime_generation: Mapped[str] = mapped_column(Text, nullable=False)
    snapshot_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    catalog_revision: Mapped[int | None] = mapped_column(nullable=True)
    page_index: Mapped[int | None] = mapped_column(nullable=True)
    is_last: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    catalog_sequence: Mapped[int | None] = mapped_column(nullable=True)
    payload: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    frame: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    settled_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    rejection_snapshot_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    rejection_expected_page_index: Mapped[int | None] = mapped_column(nullable=True)
    rejection_expected_catalog_sequence: Mapped[int | None] = mapped_column(
        nullable=True
    )

    __table_args__ = (
        CheckConstraint(
            func.length(message_id) == 36,
            name="ck_session_catalog_outbox_message_id_length",
        ),
        CheckConstraint(
            func.length(payload_digest) == 64,
            name="ck_session_catalog_outbox_digest_length",
        ),
        CheckConstraint(
            connector_sequence >= 0,
            name="ck_session_catalog_outbox_connector_sequence_nonnegative",
        ),
        CheckConstraint(
            transport_epoch_id.is_(None) | (func.length(transport_epoch_id) == 36),
            name="ck_session_catalog_outbox_epoch_length",
        ),
        CheckConstraint(
            func.length(profile).between(1, 128),
            name="ck_session_catalog_outbox_profile_length",
        ),
        CheckConstraint(
            func.length(runtime_generation).between(1, 128),
            name="ck_session_catalog_outbox_runtime_generation_length",
        ),
        CheckConstraint(
            message_type.in_(
                ("session.catalog.snapshot.page", "session.catalog.event")
            ),
            name="ck_session_catalog_outbox_message_type",
        ),
        CheckConstraint(
            state.in_(("pending", "acked", "rejected", "retired")),
            name="ck_session_catalog_outbox_state",
        ),
        CheckConstraint(
            (
                (state != "rejected")
                & rejection_reason.is_(None)
                & rejection_snapshot_id.is_(None)
                & rejection_expected_page_index.is_(None)
                & rejection_expected_catalog_sequence.is_(None)
            )
            | (
                (state == "rejected")
                & (
                    (
                        rejection_reason.in_(("page_gap", "revision_conflict"))
                        & rejection_snapshot_id.is_not(None)
                        & (func.length(rejection_snapshot_id) == 36)
                        & rejection_expected_page_index.is_not(None)
                        & (rejection_expected_page_index >= 0)
                        & rejection_expected_catalog_sequence.is_(None)
                    )
                    | (
                        (rejection_reason == "event_gap")
                        & rejection_snapshot_id.is_(None)
                        & rejection_expected_page_index.is_(None)
                        & rejection_expected_catalog_sequence.is_not(None)
                        & (rejection_expected_catalog_sequence >= 1)
                    )
                    | (
                        rejection_reason.in_(
                            (
                                "runtime_mismatch",
                                "stale_writer",
                                "contract_mismatch",
                            )
                        )
                        & rejection_snapshot_id.is_(None)
                        & rejection_expected_page_index.is_(None)
                        & rejection_expected_catalog_sequence.is_(None)
                    )
                )
            ),
            name="ck_session_catalog_outbox_rejection_audit",
        ),
        CheckConstraint(
            (
                (message_type == "session.catalog.snapshot.page")
                & snapshot_id.is_not(None)
                & (func.length(snapshot_id) == 36)
                & catalog_revision.is_not(None)
                & (catalog_revision >= 0)
                & page_index.is_not(None)
                & (page_index >= 0)
                & is_last.is_not(None)
                & catalog_sequence.is_(None)
            )
            | (
                (message_type == "session.catalog.event")
                & snapshot_id.is_(None)
                & catalog_revision.is_(None)
                & page_index.is_(None)
                & is_last.is_(None)
                & catalog_sequence.is_not(None)
                & (catalog_sequence >= 0)
            ),
            name="ck_session_catalog_outbox_position",
        ),
        CheckConstraint(
            func.length(payload) <= MAX_DURABLE_PAYLOAD_BYTES,
            name="ck_session_catalog_outbox_payload_size",
        ),
        CheckConstraint(
            func.length(frame) <= MAX_DURABLE_PAYLOAD_BYTES,
            name="ck_session_catalog_outbox_frame_size",
        ),
        UniqueConstraint(
            transport_epoch_id,
            connector_sequence,
            name="uq_session_catalog_outbox_epoch_sequence",
        ),
        Index(
            "idx_session_catalog_outbox_fact_attempt",
            message_type,
            profile,
            runtime_generation,
            snapshot_id,
            catalog_revision,
            page_index,
            catalog_sequence,
            connector_sequence,
        ),
        Index(
            "idx_session_catalog_outbox_pending_sequence",
            state,
            connector_sequence,
        ),
    )


__all__ = ["SessionCatalogOutboxRow"]
