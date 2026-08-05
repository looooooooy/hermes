from __future__ import annotations

from datetime import timedelta

from sqlalchemy.dialects import postgresql

from hermes_cloud.platform.postgres.catalog import POSTGRES_V1_MIGRATIONS
from hermes_cloud.platform.sqlalchemy.session_catalog_models import (
    SessionCatalogAuthorityModel,
    SessionCatalogInboxModel,
)
from hermes_cloud.platform.sqlite.migrations import (
    CURRENT_SQLITE_SCHEMA_VERSION,
    PUBLISHED_SQLITE_MIGRATIONS,
)


def test_postgres_v13_publishes_typed_catalog_recovery_and_retention() -> None:
    assert len(POSTGRES_V1_MIGRATIONS) >= 13
    migration = POSTGRES_V1_MIGRATIONS[12]
    keys = tuple(operation.key for operation in migration.plan.operations)

    assert migration.version == 13
    assert migration.name == "0013_session_catalog_recovery"
    assert {
        "column:projection.session_catalog_authorities.staging_deadline:add",
        "column:projection.session_catalog_authorities.require_full_snapshot:add",
        "data:projection.session_catalog_authorities.staging_deadline:backfill",
        "column:projection.session_catalog_inbox.retention_until:add",
        "column:projection.session_catalog_inbox.receipt_state:add",
        "column:projection.session_catalog_inbox.dispatch_connection_id:add",
        "column:projection.session_catalog_inbox.dispatch_message_id:add",
        "column:projection.session_catalog_inbox.dispatch_sequence:add",
        "column:projection.session_catalog_inbox.dispatch_attempts:add",
        "column:projection.session_catalog_inbox.updated_at:add",
        "column:projection.session_catalog_inbox.receipt_sent_at:add",
        "column:projection.session_catalog_inbox.receipt_settled_at:add",
        "column:projection.session_catalog_inbox.receipt_retired_at:add",
        "column:projection.session_catalog_inbox.receipt_retirement_reason:add",
        "data:projection.session_catalog_inbox.receipt_dispatch:backfill",
        "column:projection.session_catalog_inbox.dispatch_attempts:not-null",
        "column:projection.session_catalog_inbox.updated_at:not-null",
        "constraint:projection.session_catalog_inbox.receipt_state:add",
        "constraint:projection.session_catalog_inbox.dispatch_sequence:add",
        "constraint:projection.session_catalog_inbox.dispatch_attempts:add",
        "index:session_catalog_authority_recovery_idx",
        "index:session_catalog_inbox_retention_idx",
        "index:session_catalog_inbox_pending_receipt_idx",
        "index:session_catalog_snapshot_page_retention_idx",
    } <= set(keys)
    assert all(
        not hasattr(operation.statement({}), "text")
        for operation in migration.plan.operations
    )


def test_postgres_v13_backfills_preexisting_staging_authority_deadlines() -> None:
    migration = POSTGRES_V1_MIGRATIONS[12]
    operation = next(
        operation
        for operation in migration.plan.migrate
        if operation.key
        == "data:projection.session_catalog_authorities.staging_deadline:backfill"
    )

    compiled = operation.statement({}).compile(dialect=postgresql.dialect())
    sql = str(compiled)

    assert "session_catalog_authorities.staging_snapshot_id IS NOT NULL" in sql
    assert "session_catalog_authorities.staging_deadline IS NULL" in sql
    assert timedelta(minutes=10) in compiled.params.values()


def test_postgres_v13_retention_backfill_is_deterministic_from_received_time() -> (
    None
):
    migration = POSTGRES_V1_MIGRATIONS[12]
    operation = next(
        operation
        for operation in migration.plan.migrate
        if operation.key
        == "data:projection.session_catalog_inbox.retention_until:backfill"
    )

    compiled = operation.statement({}).compile(dialect=postgresql.dialect())
    sql = str(compiled)

    assert "session_catalog_inbox.received_at +" in sql
    assert "now()" not in sql
    assert timedelta(days=7) in compiled.params.values()


def test_current_catalog_orm_exposes_v13_recovery_fields() -> None:
    assert {
        "staging_deadline",
        "require_full_snapshot",
    } <= set(SessionCatalogAuthorityModel.__table__.columns.keys())
    assert {
        "retention_until",
        "receipt_state",
        "dispatch_connection_id",
        "dispatch_message_id",
        "dispatch_sequence",
        "dispatch_attempts",
        "updated_at",
        "receipt_sent_at",
        "receipt_settled_at",
        "receipt_retired_at",
        "receipt_retirement_reason",
    } <= set(SessionCatalogInboxModel.__table__.columns.keys())


def test_sqlite_v13_is_appended_without_rewriting_v12() -> None:
    assert CURRENT_SQLITE_SCHEMA_VERSION == 13
    assert PUBLISHED_SQLITE_MIGRATIONS[11].name == "0012_session_catalog_v1"
    assert PUBLISHED_SQLITE_MIGRATIONS[12].name == (
        "0013_session_catalog_recovery"
    )


def test_postgres_v12_plan_remains_frozen_at_catalog_v1_shape() -> None:
    keys = {operation.key for operation in POSTGRES_V1_MIGRATIONS[11].plan.operations}
    assert not any("staging_deadline" in key for key in keys)
    assert not any("require_full_snapshot" in key for key in keys)
    assert not any("retention_until" in key for key in keys)
    assert not any("receipt_state" in key for key in keys)
    assert not any("dispatch_connection_id" in key for key in keys)
    assert not any("receipt_settled_at" in key for key in keys)
