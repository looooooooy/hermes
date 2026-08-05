from __future__ import annotations

from alembic.operations import Operations

OBSERVER_ATTEMPT_V5_CHECKSUM = (
    "7876fecb20a3084874237a75f81652e785945a55f5da8bb91d6207ad571fe9da"
)
OBSERVER_ATTEMPT_V5_AUDIT_SIGNATURE = (
    "schema:v5",
    "table:observer_outbox",
    "drop-unique:observer_fact_identity",
    "index:idx_observer_outbox_fact_attempt",
    "rule:rejected-attempt-terminal",
)


def upgrade_observer_attempt_v5(operations: Operations) -> None:
    with operations.batch_alter_table(
        "observer_outbox",
        recreate="always",
    ) as batch:
        batch.drop_constraint(
            "uq_observer_outbox_fact_identity",
            type_="unique",
        )
    operations.create_index(
        "idx_observer_outbox_fact_attempt",
        "observer_outbox",
        (
            "message_type",
            "profile",
            "session_key",
            "runtime_generation",
            "runtime_session_id",
            "event_sequence",
            "connector_sequence",
        ),
    )


__all__ = [
    "OBSERVER_ATTEMPT_V5_AUDIT_SIGNATURE",
    "OBSERVER_ATTEMPT_V5_CHECKSUM",
    "upgrade_observer_attempt_v5",
]
