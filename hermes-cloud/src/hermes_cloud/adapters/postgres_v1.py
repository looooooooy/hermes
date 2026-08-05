"""Compatibility imports for the PostgreSQL platform migration catalog."""

from hermes_cloud.platform.postgres.catalog import (
    POSTGRES_V1_MIGRATIONS,
    Migration,
    MigrationOperation,
    MigrationPhase,
    MigrationPlan,
    verify_migration_catalog,
)

__all__ = (
    "POSTGRES_V1_MIGRATIONS",
    "Migration",
    "MigrationOperation",
    "MigrationPhase",
    "MigrationPlan",
    "verify_migration_catalog",
)
