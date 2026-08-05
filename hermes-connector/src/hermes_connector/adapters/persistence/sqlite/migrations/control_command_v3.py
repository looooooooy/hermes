from __future__ import annotations

import sqlalchemy as sa
from alembic.operations import Operations

from hermes_connector.adapters.sqlite_models import MAX_DURABLE_PAYLOAD_BYTES

CONTROL_COMMAND_V3_CHECKSUM = (
    "5883fe087445068b84fd23bd257093cc42f2ee8565d6b726079e420e55d1afb2"
)
CONTROL_COMMAND_V3_AUDIT_SIGNATURE = (
    "schema:v3",
    "table:control_commands",
    "index:idx_control_commands_state_updated",
    "field:receipt_revision",
    "field:receipt_acknowledged",
    "field:result_acknowledged",
    "limit:payload:262144",
)


def upgrade_control_command_v3(operations: Operations) -> None:
    state = sa.Column("state", sa.Text(), nullable=False)
    delivery_payload = sa.Column("delivery_payload", sa.LargeBinary(), nullable=False)
    receipt_payload = sa.Column("receipt_payload", sa.LargeBinary(), nullable=False)
    result_payload = sa.Column("result_payload", sa.LargeBinary(), nullable=True)
    revision = sa.Column("revision", sa.Integer(), nullable=False)
    receipt_revision = sa.Column("receipt_revision", sa.Integer(), nullable=False)
    operations.create_table(
        "control_commands",
        sa.Column("command_id", sa.Text(), primary_key=True),
        sa.Column("message_id", sa.Text(), nullable=False, unique=True),
        sa.Column("digest", sa.Text(), nullable=False),
        state,
        delivery_payload,
        receipt_payload,
        result_payload,
        sa.Column("expires_at", sa.Text(), nullable=False),
        receipt_revision,
        revision,
        sa.Column("receipt_acknowledged", sa.Boolean(), nullable=False),
        sa.Column("result_acknowledged", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.Column("completed_at", sa.Text(), nullable=True),
        sa.CheckConstraint(
            state.in_(("delivered", "executing", "succeeded", "failed", "unknown")),
            name="ck_control_command_state",
        ),
        sa.CheckConstraint(
            receipt_revision >= 1,
            name="ck_control_command_receipt_revision_positive",
        ),
        sa.CheckConstraint(
            revision >= 1,
            name="ck_control_command_revision_positive",
        ),
        sa.CheckConstraint(
            sa.func.length(delivery_payload) <= MAX_DURABLE_PAYLOAD_BYTES,
            name="ck_control_command_delivery_size",
        ),
        sa.CheckConstraint(
            sa.func.length(receipt_payload) <= MAX_DURABLE_PAYLOAD_BYTES,
            name="ck_control_command_receipt_size",
        ),
        sa.CheckConstraint(
            (result_payload.is_(None))
            | (sa.func.length(result_payload) <= MAX_DURABLE_PAYLOAD_BYTES),
            name="ck_control_command_result_size",
        ),
    )
    operations.create_index(
        "idx_control_commands_state_updated",
        "control_commands",
        ("state", "updated_at", "command_id"),
    )
