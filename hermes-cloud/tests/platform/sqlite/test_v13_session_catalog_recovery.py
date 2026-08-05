from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, select

from hermes_cloud.platform.sqlite.migrations import PUBLISHED_SQLITE_MIGRATIONS
from hermes_cloud.platform.sqlite.schema import (
    build_sqlite_metadata,
    build_sqlite_v12_metadata,
)

NOW = datetime(2026, 8, 3, 10, 0, tzinfo=UTC)
TENANT_ID = UUID("10000000-0000-4000-8000-000000000001")
WORKSPACE_ID = UUID("30000000-0000-4000-8000-000000000001")
AGENT_ID = UUID("50000000-0000-4000-8000-000000000001")
DEVICE_ID = UUID("60000000-0000-4000-8000-000000000001")


def test_sqlite_v13_upgrades_v12_rows_and_preserves_replay_windows() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    v12_metadata = build_sqlite_v12_metadata()
    authority = v12_metadata.tables["session_catalog_authorities"]
    inbox = v12_metadata.tables["session_catalog_inbox"]
    pages = v12_metadata.tables["session_catalog_snapshot_pages"]
    snapshot_id = UUID("71000000-0000-4000-8000-000000000001")
    message_id = UUID("74000000-0000-4000-8000-000000000001")

    with engine.begin() as connection:
        operations = Operations(MigrationContext.configure(connection))
        for migration in PUBLISHED_SQLITE_MIGRATIONS[:12]:
            migration.upgrade(operations)
        connection.execute(
            authority.insert().values(
                tenant_id=TENANT_ID,
                agent_id=AGENT_ID,
                profile="default",
                workspace_id=WORKSPACE_ID,
                writer_id=DEVICE_ID,
                writer_fence=1,
                runtime_generation=None,
                catalog_revision=0,
                catalog_sequence=0,
                staging_snapshot_id=snapshot_id,
                staging_runtime_generation="runtime-a",
                staging_catalog_revision=1,
                expected_page_index=1,
                updated_at=NOW,
            )
        )
        connection.execute(
            pages.insert().values(
                tenant_id=TENANT_ID,
                agent_id=AGENT_ID,
                profile="default",
                snapshot_id=snapshot_id,
                page_index=0,
                runtime_generation="runtime-a",
                catalog_revision=1,
                is_last=False,
                sessions=[],
                payload_digest="a" * 64,
                created_at=NOW,
            )
        )
        connection.execute(
            inbox.insert().values(
                tenant_id=TENANT_ID,
                message_id=message_id,
                workspace_id=WORKSPACE_ID,
                agent_id=AGENT_ID,
                device_id=DEVICE_ID,
                connector_instance_id=UUID(
                    "73000000-0000-4000-8000-000000000001"
                ),
                runtime_generation="runtime-a",
                connector_sequence=1,
                message_type="session.catalog.snapshot.page",
                payload_digest="b" * 64,
                receipt_type="session.catalog.ack",
                receipt_payload={"acked_message_id": str(message_id)},
                received_at=NOW,
            )
        )
        PUBLISHED_SQLITE_MIGRATIONS[12].upgrade(operations)

    current = build_sqlite_metadata()
    with engine.connect() as connection:
        authority_row = connection.execute(
            select(current.tables["session_catalog_authorities"])
        ).mappings().one()
        inbox_row = connection.execute(
            select(current.tables["session_catalog_inbox"])
        ).mappings().one()
    inspection = inspect(engine)
    authority_columns = {
        column["name"]
        for column in inspection.get_columns("session_catalog_authorities")
    }
    inbox_indexes = {
        index["name"]
        for index in inspection.get_indexes("session_catalog_inbox")
    }
    page_indexes = {
        index["name"]
        for index in inspection.get_indexes("session_catalog_snapshot_pages")
    }

    assert {"staging_deadline", "require_full_snapshot"} <= authority_columns
    assert authority_row["require_full_snapshot"] is False
    assert authority_row["staging_deadline"].replace(tzinfo=UTC) == (
        NOW + timedelta(minutes=10)
    )
    assert inbox_row["retention_until"].replace(tzinfo=UTC) == (
        NOW + timedelta(days=7)
    )
    assert inbox_row["receipt_state"] == "settled"
    assert inbox_row["dispatch_connection_id"] is None
    assert inbox_row["dispatch_message_id"] is None
    assert inbox_row["dispatch_sequence"] is None
    assert inbox_row["dispatch_attempts"] == 0
    assert inbox_row["updated_at"].replace(tzinfo=UTC) == NOW
    assert inbox_row["receipt_sent_at"].replace(tzinfo=UTC) == NOW
    assert inbox_row["receipt_settled_at"].replace(tzinfo=UTC) == NOW
    assert inbox_row["receipt_retired_at"] is None
    assert inbox_row["receipt_retirement_reason"] is None
    assert "session_catalog_inbox_retention_idx" in inbox_indexes
    assert "session_catalog_inbox_pending_receipt_idx" in inbox_indexes
    assert "session_catalog_snapshot_page_retention_idx" in page_indexes
    engine.dispose()
