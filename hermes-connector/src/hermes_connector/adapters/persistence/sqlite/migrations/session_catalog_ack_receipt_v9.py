from __future__ import annotations

import sqlalchemy as sa
from alembic.operations import Operations

SESSION_CATALOG_ACK_RECEIPT_V9_AUDIT_SIGNATURE = (
    "schema:v9",
    "table:session_catalog_ack_receipts",
    "identity:profile-runtime-generation",
    "receipt:last-terminal-snapshot-ack",
    "conflict:message-digest-sequence-snapshot-revision-page",
    "retention:bounded-and-generation-rollover",
    "catalog:host-session-key-no-cloud-session-id",
    "orm:sqlalchemy2-alembic-operations-only",
)
SESSION_CATALOG_ACK_RECEIPT_V9_CHECKSUM = (
    "f1735bc34fec62051daacd080ace0713af1cebd81b82b0c839157002b7656dc3"
)


def upgrade_session_catalog_ack_receipt_v9(operations: Operations) -> None:
    profile = sa.Column("profile", sa.Text(), primary_key=True)
    runtime_generation = sa.Column(
        "runtime_generation",
        sa.Text(),
        primary_key=True,
    )
    acked_message_id = sa.Column(
        "acked_message_id",
        sa.Text(),
        nullable=False,
    )
    acked_payload_digest = sa.Column(
        "acked_payload_digest",
        sa.Text(),
        nullable=False,
    )
    acked_connector_sequence = sa.Column(
        "acked_connector_sequence",
        sa.Integer(),
        nullable=False,
    )
    snapshot_id = sa.Column("snapshot_id", sa.Text(), nullable=False)
    catalog_revision = sa.Column(
        "catalog_revision",
        sa.Integer(),
        nullable=False,
    )
    page_index = sa.Column("page_index", sa.Integer(), nullable=False)
    is_last = sa.Column("is_last", sa.Boolean(), nullable=False)
    operations.create_table(
        "session_catalog_ack_receipts",
        profile,
        runtime_generation,
        acked_message_id,
        acked_payload_digest,
        acked_connector_sequence,
        snapshot_id,
        catalog_revision,
        page_index,
        is_last,
        sa.Column("acked_at", sa.Text(), nullable=False),
        sa.CheckConstraint(
            sa.func.length(profile).between(1, 128),
            name="ck_session_catalog_ack_receipt_profile_length",
        ),
        sa.CheckConstraint(
            sa.func.length(runtime_generation).between(1, 128),
            name="ck_session_catalog_ack_receipt_generation_length",
        ),
        sa.CheckConstraint(
            sa.func.length(acked_message_id) == 36,
            name="ck_session_catalog_ack_receipt_message_id_length",
        ),
        sa.CheckConstraint(
            sa.func.length(acked_payload_digest) == 64,
            name="ck_session_catalog_ack_receipt_digest_length",
        ),
        sa.CheckConstraint(
            acked_connector_sequence >= 0,
            name="ck_session_catalog_ack_receipt_sequence_nonnegative",
        ),
        sa.CheckConstraint(
            sa.func.length(snapshot_id) == 36,
            name="ck_session_catalog_ack_receipt_snapshot_id_length",
        ),
        sa.CheckConstraint(
            catalog_revision >= 0,
            name="ck_session_catalog_ack_receipt_revision_nonnegative",
        ),
        sa.CheckConstraint(
            page_index >= 0,
            name="ck_session_catalog_ack_receipt_page_nonnegative",
        ),
        sa.CheckConstraint(
            is_last == sa.true(),
            name="ck_session_catalog_ack_receipt_terminal",
        ),
    )
    operations.create_index(
        "idx_session_catalog_ack_receipt_retention",
        "session_catalog_ack_receipts",
        ("acked_at", "profile", "runtime_generation"),
    )


__all__ = [
    "SESSION_CATALOG_ACK_RECEIPT_V9_AUDIT_SIGNATURE",
    "SESSION_CATALOG_ACK_RECEIPT_V9_CHECKSUM",
    "upgrade_session_catalog_ack_receipt_v9",
]
