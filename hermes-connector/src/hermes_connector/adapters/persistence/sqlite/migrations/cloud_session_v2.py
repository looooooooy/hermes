from __future__ import annotations

import sqlalchemy as sa
from alembic.operations import Operations

CLOUD_SESSION_V2_CHECKSUM = (
    "b97b0a46d912be2cd5f0fd5267316b1c1be2df8b962ffea842f9c83f6f980fe6"
)
CLOUD_SESSION_V2_AUDIT_SIGNATURE = (
    "schema:v2",
    "table:cloud_session_checkpoint",
    "field:previous_connection_id",
    "field:next_outbound_sequence",
    "field:next_inbound_sequence",
    "field:reconciliation_required",
)


def upgrade_cloud_session_v2(operations: Operations) -> None:
    checkpoint_id = sa.Column("id", sa.Integer(), primary_key=True)
    outbound_sequence = sa.Column(
        "next_outbound_sequence",
        sa.Integer(),
        nullable=False,
    )
    inbound_sequence = sa.Column(
        "next_inbound_sequence",
        sa.Integer(),
        nullable=False,
    )
    operations.create_table(
        "cloud_session_checkpoint",
        checkpoint_id,
        sa.Column("previous_connection_id", sa.Text(), nullable=True),
        outbound_sequence,
        inbound_sequence,
        sa.Column("reconciliation_required", sa.Boolean(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.CheckConstraint(
            checkpoint_id == 1,
            name="ck_cloud_session_singleton",
        ),
        sa.CheckConstraint(
            outbound_sequence >= 0,
            name="ck_cloud_outbound_sequence_nonnegative",
        ),
        sa.CheckConstraint(
            inbound_sequence >= 0,
            name="ck_cloud_inbound_sequence_nonnegative",
        ),
    )
