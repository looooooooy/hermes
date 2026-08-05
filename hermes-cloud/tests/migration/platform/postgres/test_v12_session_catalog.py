from __future__ import annotations

from hermes_cloud.platform.postgres.catalog import POSTGRES_V1_MIGRATIONS
from hermes_cloud.platform.sqlalchemy.session_catalog_models import SessionCatalogBase


def test_postgres_v12_publishes_typed_tenant_isolated_session_catalog() -> None:
    migration = POSTGRES_V1_MIGRATIONS[11]
    keys = tuple(operation.key for operation in migration.plan.operations)
    table_names = tuple(sorted(SessionCatalogBase.metadata.tables))

    assert migration.version == 12
    assert migration.name == "0012_session_catalog_v1"
    assert table_names == (
        "projection.session_catalog_authorities",
        "projection.session_catalog_entries",
        "projection.session_catalog_generations",
        "projection.session_catalog_inbox",
        "projection.session_catalog_snapshot_pages",
    )
    for table_name in table_names:
        assert f"table:{table_name}" in keys
        assert f"rls-enable:{table_name}" in keys
        assert f"rls-force:{table_name}" in keys
        assert f"rls-policy:{table_name}" in keys
    assert all(
        not hasattr(operation.statement({}), "text")
        for operation in migration.plan.operations
    )
