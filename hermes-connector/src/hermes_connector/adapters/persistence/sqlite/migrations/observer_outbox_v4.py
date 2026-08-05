from __future__ import annotations

import sqlalchemy as sa
from alembic.operations import Operations

from hermes_connector.adapters.sqlite_models import MAX_DURABLE_PAYLOAD_BYTES

OBSERVER_OUTBOX_V4_CHECKSUM = (
    "ac2ed4fed22b209906f2f8b35dbe4a1997caeea65f1cdf1035242ab44aa623b6"
)
OBSERVER_OUTBOX_V4_AUDIT_SIGNATURE = (
    "schema:v4",
    "table:observer_outbox",
    "index:idx_observer_outbox_pending_sequence",
    "unique:observer_fact_identity",
    "field:payload_digest",
    "state:pending-acked-rejected",
    "limit:payload:262144",
)


def upgrade_observer_outbox_v4(operations: Operations) -> None:
    payload_digest = sa.Column("payload_digest", sa.Text(), nullable=False)
    connector_sequence = sa.Column(
        "connector_sequence",
        sa.Integer(),
        nullable=False,
        unique=True,
    )
    message_type = sa.Column("message_type", sa.Text(), nullable=False)
    event_sequence = sa.Column("event_sequence", sa.Integer(), nullable=False)
    payload = sa.Column("payload", sa.LargeBinary(), nullable=False)
    frame = sa.Column("frame", sa.LargeBinary(), nullable=False)
    state = sa.Column("state", sa.Text(), nullable=False)
    operations.create_table(
        "observer_outbox",
        sa.Column("message_id", sa.Text(), primary_key=True),
        payload_digest,
        connector_sequence,
        message_type,
        sa.Column("profile", sa.Text(), nullable=False),
        sa.Column("session_key", sa.Text(), nullable=False),
        sa.Column("runtime_generation", sa.Text(), nullable=False),
        sa.Column("runtime_session_id", sa.Text(), nullable=False),
        event_sequence,
        payload,
        frame,
        state,
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("settled_at", sa.Text(), nullable=True),
        sa.CheckConstraint(
            sa.func.length(payload_digest) == 64,
            name="ck_observer_outbox_digest_length",
        ),
        sa.CheckConstraint(
            connector_sequence >= 0,
            name="ck_observer_outbox_connector_sequence_nonnegative",
        ),
        sa.CheckConstraint(
            event_sequence >= 0,
            name="ck_observer_outbox_event_sequence_nonnegative",
        ),
        sa.CheckConstraint(
            message_type.in_(("session.snapshot", "session.event")),
            name="ck_observer_outbox_message_type",
        ),
        sa.CheckConstraint(
            state.in_(("pending", "acked", "rejected")),
            name="ck_observer_outbox_state",
        ),
        sa.CheckConstraint(
            sa.func.length(payload) <= MAX_DURABLE_PAYLOAD_BYTES,
            name="ck_observer_outbox_payload_size",
        ),
        sa.CheckConstraint(
            sa.func.length(frame) <= MAX_DURABLE_PAYLOAD_BYTES,
            name="ck_observer_outbox_frame_size",
        ),
        sa.UniqueConstraint(
            "message_type",
            "profile",
            "session_key",
            "runtime_generation",
            "runtime_session_id",
            "event_sequence",
            name="uq_observer_outbox_fact_identity",
        ),
    )
    operations.create_index(
        "idx_observer_outbox_pending_sequence",
        "observer_outbox",
        ("state", "connector_sequence"),
    )


__all__ = [
    "OBSERVER_OUTBOX_V4_AUDIT_SIGNATURE",
    "OBSERVER_OUTBOX_V4_CHECKSUM",
    "upgrade_observer_outbox_v4",
]
