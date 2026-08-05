"""SQLite-local compilation and flattened metadata for shared ORM mappings."""

from __future__ import annotations

from sqlalchemy import CheckConstraint, MetaData
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.schema import BLANK_SCHEMA

from hermes_cloud.platform.postgres.models import HermesCloudBase
from hermes_cloud.platform.sqlalchemy.session_catalog_migration_models import (
    SessionCatalogV12Base,
)
from hermes_cloud.platform.sqlalchemy.session_catalog_models import SessionCatalogBase
from hermes_cloud.platform.sqlalchemy.session_projection_migration_models import (
    SESSION_PROJECTION_V10_MODELS,
)

SQLITE_SCHEMA_TRANSLATE_MAP = {
    schema: None
    for schema in sorted(
        {
            str(table.schema)
            for table in HermesCloudBase.metadata.tables.values()
            if table.schema is not None
        }
    )
}
_UNSUPPORTED_CHECK_MARKERS = (
    " ~ ",
    "jsonb_typeof",
    "octet_length",
)


@compiles(PG_UUID, "sqlite")
def _compile_postgres_uuid_for_sqlite(
    _type: PG_UUID,
    _compiler: object,
    **_kwargs: object,
) -> str:
    return "CHAR(32)"


@compiles(JSONB, "sqlite")
def _compile_postgres_jsonb_for_sqlite(
    _type: JSONB,
    _compiler: object,
    **_kwargs: object,
) -> str:
    return "JSON"


def build_sqlite_metadata() -> MetaData:
    """Clone published mappings without schemas or PostgreSQL-only checks."""

    metadata = MetaData()
    for source in HermesCloudBase.metadata.sorted_tables:
        copied = source.to_metadata(
            metadata,
            schema=None,
            referred_schema_fn=lambda *_arguments: BLANK_SCHEMA,
        )
        for constraint in tuple(copied.constraints):
            if not isinstance(constraint, CheckConstraint):
                continue
            expression = str(constraint.sqltext).lower()
            if any(marker in expression for marker in _UNSUPPORTED_CHECK_MARKERS):
                copied.constraints.remove(constraint)
    for source in SessionCatalogBase.metadata.sorted_tables:
        source.to_metadata(
            metadata,
            schema=None,
            referred_schema_fn=lambda *_arguments: BLANK_SCHEMA,
        )
    return metadata


def build_sqlite_v10_metadata() -> MetaData:
    """Clone the published revision-10 baseline independently of current models."""

    metadata = build_sqlite_metadata()
    frozen_names = {
        model.__table__.name for model in SESSION_PROJECTION_V10_MODELS
    }
    for name in frozen_names:
        metadata.remove(metadata.tables[name])
    for model in SESSION_PROJECTION_V10_MODELS:
        copied = model.__table__.to_metadata(
            metadata,
            schema=None,
            referred_schema_fn=lambda *_arguments: BLANK_SCHEMA,
        )
        for constraint in tuple(copied.constraints):
            if not isinstance(constraint, CheckConstraint):
                continue
            expression = str(constraint.sqltext).lower()
            if any(marker in expression for marker in _UNSUPPORTED_CHECK_MARKERS):
                copied.constraints.remove(constraint)
    return metadata


def build_sqlite_v12_metadata() -> MetaData:
    """Clone the published revision-12 catalog independently of current models."""

    metadata = build_sqlite_metadata()
    current_catalog_names = {
        table.name for table in SessionCatalogBase.metadata.sorted_tables
    }
    for name in current_catalog_names:
        metadata.remove(metadata.tables[name])
    for source in SessionCatalogV12Base.metadata.sorted_tables:
        source.to_metadata(
            metadata,
            schema=None,
            referred_schema_fn=lambda *_arguments: BLANK_SCHEMA,
        )
    return metadata
