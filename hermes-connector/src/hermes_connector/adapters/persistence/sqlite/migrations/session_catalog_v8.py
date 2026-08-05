from __future__ import annotations

import sqlalchemy as sa
from alembic.operations import Operations

from hermes_connector.adapters.sqlite_models import MAX_DURABLE_PAYLOAD_BYTES

SESSION_CATALOG_V8_AUDIT_SIGNATURE = (
    "schema:v8",
    "table:session_catalog_outbox",
    "catalog:host-session-key-no-cloud-session-id",
    "identity:profile-runtime-generation-position-message-attempt",
    "state:pending-acked-rejected-retired",
    "nack-audit:exact-reason-position-tuple",
    "transport:session-catalog-snapshot-page-event",
    "transaction:catalog-attempt-and-transport-frame",
    "retention:terminal-only-active-fail-closed",
    "orm:sqlalchemy2-alembic-operations-only",
    "limit:payload:262144",
)
SESSION_CATALOG_V8_CHECKSUM = (
    "2598d264d7920471214e8c07147fcbd941c626ca69337743db18ba9999afb3ca"
)

_CATALOG_MESSAGE_TYPES = (
    "session.catalog.snapshot.page",
    "session.catalog.event",
)
_OBSERVER_MESSAGE_TYPES = (
    "session.snapshot",
    "session.event",
    "session.snapshot.v2",
    "session.event.v2",
)


def upgrade_session_catalog_v8(operations: Operations) -> None:
    message_id = sa.Column("message_id", sa.Text(), primary_key=True)
    digest = sa.Column("payload_digest", sa.Text(), nullable=False)
    connector_sequence = sa.Column(
        "connector_sequence", sa.Integer(), nullable=False
    )
    epoch_id = sa.Column("transport_epoch_id", sa.Text(), nullable=True)
    message_type = sa.Column("message_type", sa.Text(), nullable=False)
    profile = sa.Column("profile", sa.Text(), nullable=False)
    runtime_generation = sa.Column(
        "runtime_generation", sa.Text(), nullable=False
    )
    snapshot_id = sa.Column("snapshot_id", sa.Text(), nullable=True)
    catalog_revision = sa.Column("catalog_revision", sa.Integer(), nullable=True)
    page_index = sa.Column("page_index", sa.Integer(), nullable=True)
    is_last = sa.Column("is_last", sa.Boolean(), nullable=True)
    catalog_sequence = sa.Column("catalog_sequence", sa.Integer(), nullable=True)
    payload = sa.Column("payload", sa.LargeBinary(), nullable=False)
    frame = sa.Column("frame", sa.LargeBinary(), nullable=False)
    state = sa.Column("state", sa.Text(), nullable=False)
    rejection_reason = sa.Column("rejection_reason", sa.Text(), nullable=True)
    rejection_snapshot_id = sa.Column(
        "rejection_snapshot_id", sa.Text(), nullable=True
    )
    rejection_expected_page_index = sa.Column(
        "rejection_expected_page_index", sa.Integer(), nullable=True
    )
    rejection_expected_catalog_sequence = sa.Column(
        "rejection_expected_catalog_sequence", sa.Integer(), nullable=True
    )
    operations.create_table(
        "session_catalog_outbox",
        message_id,
        digest,
        connector_sequence,
        epoch_id,
        message_type,
        profile,
        runtime_generation,
        snapshot_id,
        catalog_revision,
        page_index,
        is_last,
        catalog_sequence,
        payload,
        frame,
        state,
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("settled_at", sa.Text(), nullable=True),
        rejection_reason,
        rejection_snapshot_id,
        rejection_expected_page_index,
        rejection_expected_catalog_sequence,
        sa.CheckConstraint(
            sa.func.length(message_id) == 36,
            name="ck_session_catalog_outbox_message_id_length",
        ),
        sa.CheckConstraint(
            sa.func.length(digest) == 64,
            name="ck_session_catalog_outbox_digest_length",
        ),
        sa.CheckConstraint(
            connector_sequence >= 0,
            name="ck_session_catalog_outbox_connector_sequence_nonnegative",
        ),
        sa.CheckConstraint(
            epoch_id.is_(None) | (sa.func.length(epoch_id) == 36),
            name="ck_session_catalog_outbox_epoch_length",
        ),
        sa.CheckConstraint(
            sa.func.length(profile).between(1, 128),
            name="ck_session_catalog_outbox_profile_length",
        ),
        sa.CheckConstraint(
            sa.func.length(runtime_generation).between(1, 128),
            name="ck_session_catalog_outbox_runtime_generation_length",
        ),
        sa.CheckConstraint(
            message_type.in_(_CATALOG_MESSAGE_TYPES),
            name="ck_session_catalog_outbox_message_type",
        ),
        sa.CheckConstraint(
            state.in_(("pending", "acked", "rejected", "retired")),
            name="ck_session_catalog_outbox_state",
        ),
        sa.CheckConstraint(
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
                        & (sa.func.length(rejection_snapshot_id) == 36)
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
        sa.CheckConstraint(
            (
                (message_type == "session.catalog.snapshot.page")
                & snapshot_id.is_not(None)
                & (sa.func.length(snapshot_id) == 36)
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
        sa.CheckConstraint(
            sa.func.length(payload) <= MAX_DURABLE_PAYLOAD_BYTES,
            name="ck_session_catalog_outbox_payload_size",
        ),
        sa.CheckConstraint(
            sa.func.length(frame) <= MAX_DURABLE_PAYLOAD_BYTES,
            name="ck_session_catalog_outbox_frame_size",
        ),
        sa.UniqueConstraint(
            "transport_epoch_id",
            "connector_sequence",
            name="uq_session_catalog_outbox_epoch_sequence",
        ),
    )
    operations.create_index(
        "idx_session_catalog_outbox_fact_attempt",
        "session_catalog_outbox",
        (
            "message_type",
            "profile",
            "runtime_generation",
            "snapshot_id",
            "catalog_revision",
            "page_index",
            "catalog_sequence",
            "connector_sequence",
        ),
    )
    operations.create_index(
        "idx_session_catalog_outbox_pending_sequence",
        "session_catalog_outbox",
        ("state", "connector_sequence"),
    )

    with operations.batch_alter_table(
        "transport_frame_journal", recreate="always"
    ) as batch:
        batch.drop_constraint("ck_transport_journal_message_type", type_="check")
        batch.drop_constraint("ck_transport_journal_business_kind", type_="check")
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
                    *_CATALOG_MESSAGE_TYPES,
                )
            ),
        )
        batch.create_check_constraint(
            "ck_transport_journal_business_kind",
            sa.column("business_kind").in_(
                (
                    "heartbeat",
                    "command.receipt",
                    "command.result",
                    "control.response",
                    "observer",
                    "session_catalog",
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
            )
            | (
                sa.column("message_type").in_(_CATALOG_MESSAGE_TYPES)
                & (sa.column("business_kind") == "session_catalog")
            ),
        )


__all__ = [
    "SESSION_CATALOG_V8_AUDIT_SIGNATURE",
    "SESSION_CATALOG_V8_CHECKSUM",
    "upgrade_session_catalog_v8",
]
