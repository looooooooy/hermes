from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from sqlalchemy import CheckConstraint, inspect, select
from sqlalchemy.orm import Session

from hermes_cloud.platform.postgres.models import (
    HermesCloudBase,
    RoleModel,
    TenantModel,
)
from hermes_cloud.platform.sqlalchemy.session_catalog_models import SessionCatalogBase
from hermes_cloud.platform.sqlite.engine import build_sqlite_engine
from hermes_cloud.platform.sqlite.schema import (
    SQLITE_SCHEMA_TRANSLATE_MAP,
    build_sqlite_metadata,
)


def _database_url(path: Path) -> str:
    return f"sqlite+pysqlite:///{path}"


def test_sqlite_metadata_flattens_schemas_and_filters_only_unsupported_checks() -> None:
    metadata = build_sqlite_metadata()

    assert len(metadata.tables) == (
        len(HermesCloudBase.metadata.tables)
        + len(SessionCatalogBase.metadata.tables)
    )
    assert all(table.schema is None for table in metadata.tables.values())
    assert set(SQLITE_SCHEMA_TRANSLATE_MAP) == {
        str(table.schema) for table in HermesCloudBase.metadata.tables.values()
    }
    check_expressions = {
        str(constraint.sqltext).lower()
        for table in metadata.tables.values()
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint)
    }
    assert check_expressions
    assert all(" ~ " not in expression for expression in check_expressions)
    assert all("jsonb_typeof" not in expression for expression in check_expressions)
    assert all("octet_length" not in expression for expression in check_expressions)
    assert "status in ('active', 'suspended', 'closed')" in check_expressions


def test_sqlite_metadata_creates_real_flat_tables_and_adapts_uuid_jsonb(
    tmp_path: Path,
) -> None:
    database = tmp_path / "database.sqlite3"
    engine = build_sqlite_engine(
        _database_url(database),
        allow_missing=True,
    )
    metadata = build_sqlite_metadata()
    try:
        metadata.create_all(engine)
        database.chmod(0o660)
        tenant_id = uuid4()
        role_id = uuid4()
        with Session(engine) as session, session.begin():
            session.add(
                TenantModel(
                    tenant_id=tenant_id,
                    slug="sqlite-test",
                    display_name="SQLite Test",
                    status="active",
                )
            )
            session.flush()
            session.add(
                RoleModel(
                    tenant_id=tenant_id,
                    role_id=role_id,
                    role_key="reader",
                    display_name="Reader",
                    scope_type="tenant",
                    permissions=["session.read"],
                    status="active",
                    version=1,
                )
            )

        with Session(engine) as session:
            role = session.scalar(select(RoleModel).where(RoleModel.role_id == role_id))
            assert role is not None
            assert role.tenant_id == tenant_id
            assert role.permissions == ["session.read"]
            assert role.created_at.utcoffset() == timedelta(0)

        table_names = set(inspect(engine).get_table_names())
        assert table_names == {table.name for table in metadata.tables.values()}
        assert "tenants" in table_names
        assert "roles" in table_names
    finally:
        engine.dispose()
