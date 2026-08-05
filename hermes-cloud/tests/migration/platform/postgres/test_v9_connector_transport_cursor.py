from __future__ import annotations

from sqlalchemy import CheckConstraint, Index, PrimaryKeyConstraint, UniqueConstraint

from hermes_cloud.platform.postgres.catalog import POSTGRES_V1_MIGRATIONS
from hermes_cloud.platform.postgres.models import ConnectorTransportCursorModel


def test_v9_adds_one_gateway_transport_cursor_authority_expand_only() -> None:
    migration = POSTGRES_V1_MIGRATIONS[8]
    table = ConnectorTransportCursorModel.__table__
    qualified = f"{table.schema}.{table.name}"
    keys = {operation.key for operation in migration.plan.operations}

    assert migration.version == 9
    assert migration.name == "0009_connector_transport_cursor"
    assert migration.variables == ()
    assert f"table:{qualified}" in keys
    assert "index:connector_transport_cursors_active_idx" in keys
    assert f"rls-enable:{qualified}" in keys
    assert f"rls-force:{qualified}" in keys
    assert f"rls-policy:{qualified}" in keys
    assert all("alter" not in operation.key for operation in migration.plan.operations)


def test_gateway_cursor_model_has_exact_identity_ownership_and_cas_fields() -> None:
    table = ConnectorTransportCursorModel.__table__
    columns = set(table.columns.keys())

    assert table.schema == "platform"
    assert columns == {
        "tenant_id",
        "device_id",
        "connector_instance_id",
        "runtime_generation",
        "connection_id",
        "state",
        "next_connector_sequence",
        "next_cloud_sequence",
        "revision",
        "connected_at",
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
    assert "next_connector_sequence >= 0" in checks
    assert "next_cloud_sequence >= 0" in checks
    assert any(
        isinstance(index, Index)
        and index.name == "connector_transport_cursors_active_idx"
        for index in table.indexes
    )
