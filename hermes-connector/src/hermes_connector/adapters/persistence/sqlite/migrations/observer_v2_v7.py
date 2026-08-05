from __future__ import annotations

import sqlalchemy as sa
from alembic.operations import Operations

OBSERVER_V2_V7_AUDIT_SIGNATURE = (
    "schema:v7",
    "observer-outbox:versioned-v1-v2-message-types",
    "transport-journal:versioned-v1-v2-observer-message-types",
    "orm:sqlalchemy2-alembic-operations-only",
)
OBSERVER_V2_V7_CHECKSUM = (
    "fffd2dffc625149015b9901b15cddd38eecfe90927a84be05480457a776bce24"
)

_OBSERVER_MESSAGE_TYPES = (
    "session.snapshot",
    "session.event",
    "session.snapshot.v2",
    "session.event.v2",
)


def upgrade_observer_v2_v7(operations: Operations) -> None:
    with operations.batch_alter_table("observer_outbox", recreate="always") as batch:
        batch.drop_constraint("ck_observer_outbox_message_type", type_="check")
        batch.create_check_constraint(
            "ck_observer_outbox_message_type",
            sa.column("message_type").in_(_OBSERVER_MESSAGE_TYPES),
        )

    with operations.batch_alter_table(
        "transport_frame_journal", recreate="always"
    ) as batch:
        batch.drop_constraint("ck_transport_journal_message_type", type_="check")
        batch.drop_constraint("ck_transport_journal_business_pair", type_="check")
        batch.create_check_constraint(
            "ck_transport_journal_message_type",
            sa.column("message_type").in_(
                (
                    "connector.heartbeat",
                    "command.receipt",
                    "command.result",
                    "control.response",
                    *_OBSERVER_MESSAGE_TYPES,
                )
            ),
        )
        batch.create_check_constraint(
            "ck_transport_journal_business_pair",
            (
                (sa.column("message_type") == "connector.heartbeat")
                & (sa.column("business_kind") == "heartbeat")
            )
            | (
                (sa.column("message_type") == "command.receipt")
                & (sa.column("business_kind") == "command.receipt")
            )
            | (
                (sa.column("message_type") == "command.result")
                & (sa.column("business_kind") == "command.result")
            )
            | (
                (sa.column("message_type") == "control.response")
                & (sa.column("business_kind") == "control.response")
            )
            | (
                sa.column("message_type").in_(_OBSERVER_MESSAGE_TYPES)
                & (sa.column("business_kind") == "observer")
            ),
        )


__all__ = [
    "OBSERVER_V2_V7_AUDIT_SIGNATURE",
    "OBSERVER_V2_V7_CHECKSUM",
    "upgrade_observer_v2_v7",
]
