from __future__ import annotations

from sqlalchemy import Boolean, CheckConstraint, Index, Text, func, true
from sqlalchemy.orm import Mapped, mapped_column

from hermes_connector.adapters.sqlite_models import Base


class SessionCatalogAckReceiptRow(Base):
    __tablename__ = "session_catalog_ack_receipts"

    profile: Mapped[str] = mapped_column(Text, primary_key=True)
    runtime_generation: Mapped[str] = mapped_column(Text, primary_key=True)
    acked_message_id: Mapped[str] = mapped_column(Text, nullable=False)
    acked_payload_digest: Mapped[str] = mapped_column(Text, nullable=False)
    acked_connector_sequence: Mapped[int] = mapped_column(nullable=False)
    snapshot_id: Mapped[str] = mapped_column(Text, nullable=False)
    catalog_revision: Mapped[int] = mapped_column(nullable=False)
    page_index: Mapped[int] = mapped_column(nullable=False)
    is_last: Mapped[bool] = mapped_column(Boolean, nullable=False)
    acked_at: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        CheckConstraint(
            func.length(profile).between(1, 128),
            name="ck_session_catalog_ack_receipt_profile_length",
        ),
        CheckConstraint(
            func.length(runtime_generation).between(1, 128),
            name="ck_session_catalog_ack_receipt_generation_length",
        ),
        CheckConstraint(
            func.length(acked_message_id) == 36,
            name="ck_session_catalog_ack_receipt_message_id_length",
        ),
        CheckConstraint(
            func.length(acked_payload_digest) == 64,
            name="ck_session_catalog_ack_receipt_digest_length",
        ),
        CheckConstraint(
            acked_connector_sequence >= 0,
            name="ck_session_catalog_ack_receipt_sequence_nonnegative",
        ),
        CheckConstraint(
            func.length(snapshot_id) == 36,
            name="ck_session_catalog_ack_receipt_snapshot_id_length",
        ),
        CheckConstraint(
            catalog_revision >= 0,
            name="ck_session_catalog_ack_receipt_revision_nonnegative",
        ),
        CheckConstraint(
            page_index >= 0,
            name="ck_session_catalog_ack_receipt_page_nonnegative",
        ),
        CheckConstraint(
            is_last == true(),
            name="ck_session_catalog_ack_receipt_terminal",
        ),
        Index(
            "idx_session_catalog_ack_receipt_retention",
            acked_at,
            profile,
            runtime_generation,
        ),
    )


__all__ = ["SessionCatalogAckReceiptRow"]
