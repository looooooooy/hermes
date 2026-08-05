from __future__ import annotations

import sqlalchemy as sa
from alembic.operations import Operations
from sqlalchemy import select
from sqlalchemy.orm import Session

from hermes_connector.adapters.persistence.sqlite.models.observer_outbox import (
    ObserverOutboxRow,
)
from hermes_connector.adapters.sqlite_models import (
    MAX_DURABLE_PAYLOAD_BYTES,
    OutboxMessage,
)

DURABLE_TRANSPORT_V6_CHECKSUM = (
    "b469fffd1bf069cadd8b2fb686d21f923454e9f3b4639eb3af44769e3796ecd3"
)
DURABLE_TRANSPORT_V6_AUDIT_SIGNATURE = (
    "schema:v6",
    "table:transport_frame_journal",
    "journal:epoch-sequence-message-business-revision-generation-frame-state",
    "unique:transport-epoch-sequence",
    "unique:transport-epoch-business-attempt",
    "state:transport-staged-sent-settled-retired",
    "table:owner_control_results",
    "owner:request-digest-transport-operation-scope-request-response-revision",
    "state:owner-received-executing-completed-effect-unknown",
    "checkpoint:transport_epoch-runtime-generation-fresh-required",
    "checkpoint:transport-recovery-floor",
    "legacy:retire-unknown-epoch-force-fresh",
    "observer:epoch-sequence-identity",
    "observer:profile128-session256-runtime-session256",
    "retention:terminal-only-active-fail-closed",
    "bounds:uuid36-generation128-key512-enums",
    "constraint:transport-message-business-pair-key",
    "limit:payload:262144",
)


def upgrade_durable_transport_v6(operations: Operations) -> None:
    operations.add_column(
        "cloud_session_checkpoint",
        sa.Column("transport_epoch_id", sa.Text(), nullable=True),
    )
    operations.add_column(
        "cloud_session_checkpoint",
        sa.Column("runtime_generation", sa.Text(), nullable=True),
    )
    operations.add_column(
        "cloud_session_checkpoint",
        sa.Column(
            "transport_recovery_floor",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    with operations.batch_alter_table(
        "cloud_session_checkpoint",
        recreate="always",
    ) as batch:
        batch.create_check_constraint(
            "ck_cloud_previous_connection_id_length",
            sa.column("previous_connection_id").is_(None)
            | (sa.func.length(sa.column("previous_connection_id")) == 36),
        )
        batch.create_check_constraint(
            "ck_cloud_transport_epoch_id_length",
            sa.column("transport_epoch_id").is_(None)
            | (sa.func.length(sa.column("transport_epoch_id")) == 36),
        )
        batch.create_check_constraint(
            "ck_cloud_runtime_generation_length",
            sa.column("runtime_generation").is_(None)
            | sa.func.length(sa.column("runtime_generation")).between(1, 128),
        )
        batch.create_check_constraint(
            "ck_cloud_transport_recovery_floor_nonnegative",
            sa.column("transport_recovery_floor") >= 0,
        )
    operations.add_column(
        "cloud_session_checkpoint",
        sa.Column(
            "fresh_epoch_required",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
    )

    with operations.batch_alter_table(
        "outbox_messages",
        recreate="always",
    ) as batch:
        batch.drop_constraint("ck_outbox_state", type_="check")
        batch.create_check_constraint(
            "ck_outbox_state",
            sa.column("state").in_(("pending", "acked", "retired")),
        )

    naming_convention = {
        "uq": "uq_%(table_name)s_%(column_0_name)s",
    }
    with operations.batch_alter_table(
        "observer_outbox",
        recreate="always",
        naming_convention=naming_convention,
    ) as batch:
        batch.drop_constraint(
            "uq_observer_outbox_connector_sequence",
            type_="unique",
        )
        batch.drop_constraint("ck_observer_outbox_state", type_="check")
        batch.add_column(sa.Column("transport_epoch_id", sa.Text(), nullable=True))
        batch.create_check_constraint(
            "ck_observer_outbox_state",
            sa.column("state").in_(("pending", "acked", "rejected", "retired")),
        )
        batch.create_check_constraint(
            "ck_observer_outbox_epoch_length",
            sa.column("transport_epoch_id").is_(None)
            | (sa.func.length(sa.column("transport_epoch_id")) == 36),
        )
        batch.create_check_constraint(
            "ck_observer_outbox_message_id_length",
            sa.func.length(sa.column("message_id")) == 36,
        )
        batch.create_check_constraint(
            "ck_observer_outbox_runtime_generation_length",
            sa.func.length(sa.column("runtime_generation")).between(1, 128),
        )
        batch.create_check_constraint(
            "ck_observer_outbox_profile_length",
            sa.func.length(sa.column("profile")).between(1, 128),
        )
        batch.create_check_constraint(
            "ck_observer_outbox_session_key_length",
            sa.func.length(sa.column("session_key")).between(1, 256),
        )
        batch.create_check_constraint(
            "ck_observer_outbox_runtime_session_id_length",
            sa.func.length(sa.column("runtime_session_id")).between(1, 256),
        )
        batch.create_unique_constraint(
            "uq_observer_outbox_epoch_sequence",
            ("transport_epoch_id", "connector_sequence"),
        )
    with Session(bind=operations.get_bind()) as session:
        legacy_outbox = session.scalars(
            select(OutboxMessage).where(OutboxMessage.state == "pending")
        ).all()
        for row in legacy_outbox:
            row.state = "retired"
        legacy_observer = session.scalars(
            select(ObserverOutboxRow).where(ObserverOutboxRow.state == "pending")
        ).all()
        for row in legacy_observer:
            row.state = "retired"
        session.commit()

    journal_state = sa.Column("state", sa.Text(), nullable=False)
    journal_frame = sa.Column("frame", sa.LargeBinary(), nullable=False)
    operations.create_table(
        "transport_frame_journal",
        sa.Column("message_id", sa.Text(), primary_key=True),
        sa.Column("epoch_id", sa.Text(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("message_type", sa.Text(), nullable=False),
        sa.Column("business_kind", sa.Text(), nullable=False),
        sa.Column("business_key", sa.Text(), nullable=False),
        sa.Column("business_revision", sa.Integer(), nullable=False),
        sa.Column("runtime_generation", sa.Text(), nullable=True),
        journal_frame,
        journal_state,
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.Column("settled_at", sa.Text(), nullable=True),
        sa.CheckConstraint(
            sa.column("sequence") >= 0,
            name="ck_transport_journal_sequence_nonnegative",
        ),
        sa.CheckConstraint(
            sa.func.length(sa.column("message_id")) == 36,
            name="ck_transport_journal_message_id_length",
        ),
        sa.CheckConstraint(
            sa.func.length(sa.column("epoch_id")) == 36,
            name="ck_transport_journal_epoch_id_length",
        ),
        sa.CheckConstraint(
            sa.func.length(sa.column("message_type")).between(1, 64),
            name="ck_transport_journal_message_type_length",
        ),
        sa.CheckConstraint(
            sa.func.length(sa.column("business_kind")).between(1, 64),
            name="ck_transport_journal_business_kind_length",
        ),
        sa.CheckConstraint(
            sa.func.length(sa.column("business_key")).between(1, 512),
            name="ck_transport_journal_business_key_length",
        ),
        sa.CheckConstraint(
            sa.column("runtime_generation").is_(None)
            | sa.func.length(sa.column("runtime_generation")).between(1, 128),
            name="ck_transport_journal_runtime_generation_length",
        ),
        sa.CheckConstraint(
            sa.column("message_type").in_(
                (
                    "connector.heartbeat",
                    "command.receipt",
                    "command.result",
                    "control.response",
                    "session.snapshot",
                    "session.event",
                )
            ),
            name="ck_transport_journal_message_type",
        ),
        sa.CheckConstraint(
            sa.column("business_kind").in_(
                (
                    "heartbeat",
                    "command.receipt",
                    "command.result",
                    "control.response",
                    "observer",
                )
            ),
            name="ck_transport_journal_business_kind",
        ),
        sa.CheckConstraint(
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
                sa.column("message_type").in_(("session.snapshot", "session.event"))
                & (sa.column("business_kind") == "observer")
            ),
            name="ck_transport_journal_business_pair",
        ),
        sa.CheckConstraint(
            (sa.column("business_kind") == "heartbeat")
            | (sa.func.length(sa.column("business_key")) == 36),
            name="ck_transport_journal_business_key_identity",
        ),
        sa.CheckConstraint(
            sa.column("business_revision") >= 0,
            name="ck_transport_journal_revision_nonnegative",
        ),
        sa.CheckConstraint(
            journal_state.in_(("staged", "sent", "settled", "retired")),
            name="ck_transport_journal_state",
        ),
        sa.CheckConstraint(
            sa.func.length(journal_frame) <= MAX_DURABLE_PAYLOAD_BYTES,
            name="ck_transport_journal_frame_size",
        ),
        sa.UniqueConstraint(
            "epoch_id",
            "sequence",
            name="uq_transport_journal_epoch_sequence",
        ),
        sa.UniqueConstraint(
            "epoch_id",
            "business_kind",
            "business_key",
            "business_revision",
            name="uq_transport_journal_business_attempt",
        ),
    )
    operations.create_index(
        "idx_transport_journal_epoch_state_sequence",
        "transport_frame_journal",
        ("epoch_id", "state", "sequence"),
    )
    operations.create_index(
        "idx_transport_journal_retention",
        "transport_frame_journal",
        ("state", "updated_at", "message_id"),
    )

    owner_state = sa.Column("state", sa.Text(), nullable=False)
    request_payload = sa.Column("request_payload", sa.LargeBinary(), nullable=False)
    scope_payload = sa.Column("scope_payload", sa.LargeBinary(), nullable=False)
    response_payload = sa.Column("response_payload", sa.LargeBinary(), nullable=True)
    operations.create_table(
        "owner_control_results",
        sa.Column("request_id", sa.Text(), primary_key=True),
        sa.Column("request_digest", sa.Text(), nullable=False),
        sa.Column("control_transport_id", sa.Text(), nullable=False),
        sa.Column("operation", sa.Text(), nullable=False),
        request_payload,
        scope_payload,
        response_payload,
        owner_state,
        sa.Column("response_revision", sa.Integer(), nullable=False),
        sa.Column("transport_received", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.Column("completed_at", sa.Text(), nullable=True),
        sa.CheckConstraint(
            sa.func.length(sa.column("request_digest")) == 64,
            name="ck_owner_control_digest_length",
        ),
        sa.CheckConstraint(
            sa.func.length(sa.column("request_id")) == 36,
            name="ck_owner_control_request_id_length",
        ),
        sa.CheckConstraint(
            sa.func.length(sa.column("control_transport_id")) == 36,
            name="ck_owner_control_transport_id_length",
        ),
        sa.CheckConstraint(
            sa.column("operation").in_(
                (
                    "control.transport.open",
                    "session.control.acquire",
                    "session.control.renew",
                    "session.control.release",
                    "session.control.status",
                    "control.transport.close",
                )
            ),
            name="ck_owner_control_operation",
        ),
        sa.CheckConstraint(
            owner_state.in_(("received", "executing", "completed", "effect_unknown")),
            name="ck_owner_control_state",
        ),
        sa.CheckConstraint(
            sa.column("response_revision") >= 1,
            name="ck_owner_control_revision_positive",
        ),
        sa.CheckConstraint(
            sa.func.length(request_payload) <= MAX_DURABLE_PAYLOAD_BYTES,
            name="ck_owner_control_request_size",
        ),
        sa.CheckConstraint(
            sa.func.length(scope_payload) <= MAX_DURABLE_PAYLOAD_BYTES,
            name="ck_owner_control_scope_size",
        ),
        sa.CheckConstraint(
            response_payload.is_(None)
            | (sa.func.length(response_payload) <= MAX_DURABLE_PAYLOAD_BYTES),
            name="ck_owner_control_response_size",
        ),
    )
    operations.create_index(
        "idx_owner_control_state_updated",
        "owner_control_results",
        ("state", "updated_at", "request_id"),
    )


__all__ = [
    "DURABLE_TRANSPORT_V6_AUDIT_SIGNATURE",
    "DURABLE_TRANSPORT_V6_CHECKSUM",
    "upgrade_durable_transport_v6",
]
