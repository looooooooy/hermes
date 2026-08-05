from __future__ import annotations

from sqlalchemy import CheckConstraint, Index, PrimaryKeyConstraint, UniqueConstraint

from hermes_cloud.platform.postgres.catalog import POSTGRES_V1_MIGRATIONS
from hermes_cloud.platform.postgres.models import HermesCloudBase


def test_v10_adds_expiring_connector_handshake_ownership_expand_only() -> None:
    migration = POSTGRES_V1_MIGRATIONS[9]
    table = HermesCloudBase.metadata.tables[
        "platform.connector_transport_handshake_ownership"
    ]
    qualified = f"{table.schema}.{table.name}"
    keys = {operation.key for operation in migration.plan.operations}

    assert migration.version == 10
    assert migration.name == "0010_connector_handshake_ownership"
    assert migration.variables == ()
    assert f"table:{qualified}" in keys
    assert "index:connector_transport_handshake_lease_idx" in keys
    assert f"rls-enable:{qualified}" in keys
    assert f"rls-force:{qualified}" in keys
    assert f"rls-policy:{qualified}" in keys
    receipt_qualified = "platform.connector_observer_receipts"
    assert f"table:{receipt_qualified}" in keys
    assert "index:connector_observer_receipts_pending_idx" in keys
    assert "index:connector_observer_receipts_settled_idx" in keys
    assert f"rls-enable:{receipt_qualified}" in keys
    assert f"rls-force:{receipt_qualified}" in keys
    assert f"rls-policy:{receipt_qualified}" in keys


def test_handshake_ownership_model_has_bounded_state_and_cursor_proof() -> None:
    table = HermesCloudBase.metadata.tables[
        "platform.connector_transport_handshake_ownership"
    ]

    assert set(table.columns.keys()) == {
        "tenant_id",
        "device_id",
        "connector_instance_id",
        "runtime_generation",
        "connection_id",
        "previous_connection_id",
        "resume_decision",
        "handshake_disposition",
        "state",
        "expected_next_connector_sequence",
        "expected_next_cloud_sequence",
        "next_connector_sequence",
        "next_cloud_sequence",
        "revision",
        "lease_expires_at",
        "prepared_at",
        "updated_at",
    }
    assert any(
        isinstance(constraint, PrimaryKeyConstraint)
        and {column.name for column in constraint.columns} == {"tenant_id", "device_id"}
        for constraint in table.constraints
    )
    assert any(
        isinstance(constraint, UniqueConstraint)
        and {column.name for column in constraint.columns}
        == {"tenant_id", "connection_id"}
        for constraint in table.constraints
    )
    checks = {
        str(constraint.sqltext)
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint)
    }
    assert "state IN ('activating', 'active')" in checks
    assert "resume_decision IN ('fresh', 'resumed', 'reset_required')" in checks
    assert "handshake_disposition IN ('advance', 'preserve')" in checks
    assert "expected_next_connector_sequence >= 0" in checks
    assert "expected_next_cloud_sequence >= 0" in checks
    assert "next_connector_sequence >= 0" in checks
    assert "next_cloud_sequence >= 0" in checks
    assert any(
        isinstance(index, Index)
        and index.name == "connector_transport_handshake_lease_idx"
        for index in table.indexes
    )


def test_observer_receipt_model_stores_only_bounded_delivery_metadata() -> None:
    table = HermesCloudBase.metadata.tables["platform.connector_observer_receipts"]

    assert set(table.columns.keys()) == {
        "tenant_id",
        "device_id",
        "observer_message_id",
        "receipt_type",
        "payload",
        "payload_digest",
        "state",
        "dispatch_connection_id",
        "dispatch_message_id",
        "dispatch_sequence",
        "dispatch_attempts",
        "created_at",
        "updated_at",
        "sent_at",
        "settled_at",
    }
    checks = {
        str(constraint.sqltext)
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint)
    }
    assert "receipt_type IN ('stream.ack', 'stream.nack')" in checks
    assert "state IN ('pending', 'settled')" in checks
    assert "dispatch_sequence IS NULL OR dispatch_sequence >= 0" in checks
    assert "dispatch_attempts >= 0" in checks
    assert {index.name for index in table.indexes} >= {
        "connector_observer_receipts_pending_idx",
        "connector_observer_receipts_settled_idx",
    }
