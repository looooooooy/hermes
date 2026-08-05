"""Explicit, bounded PostgreSQL migration process for the test server."""

from __future__ import annotations

import os
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.engine import URL, make_url
from sqlalchemy.pool import NullPool

from hermes_cloud.application.migrations import PostgresMigrationRunner
from hermes_cloud.configuration import DsnFileReference
from hermes_cloud.platform.postgres.session import SqlAlchemyMigrationSession

_SUPPORTED_POSTGRESQL_DRIVERS = frozenset(
    {
        "postgresql",
        "postgresql+psycopg",
    }
)


def _migration_configuration(
    database_url: str,
    environment: Mapping[str, str],
) -> tuple[URL, dict[str, str]]:
    try:
        parsed_url = make_url(database_url)
    except Exception:  # noqa: BLE001 - configuration errors must not expose the DSN.
        raise SystemExit("migration database identity is invalid") from None
    if (
        parsed_url.drivername not in _SUPPORTED_POSTGRESQL_DRIVERS
        or not parsed_url.database
    ):
        raise SystemExit("migration database identity is invalid")
    if parsed_url.drivername == "postgresql":
        parsed_url = parsed_url.set(drivername="postgresql+psycopg")
    return (
        parsed_url,
        {
            "database_name": parsed_url.database,
            "migration_role": environment.get(
                "HERMES_MIGRATION_ROLE",
                "hermes_cloud_migrate",
            ),
            "runtime_role": environment.get(
                "HERMES_RUNTIME_ROLE",
                "hermes_cloud_runtime",
            ),
        },
    )


def _migration_identifiers(
    database_url: str,
    environment: Mapping[str, str],
) -> dict[str, str]:
    return _migration_configuration(database_url, environment)[1]


def _positive_seconds(name: str, default: int) -> int:
    raw = os.environ.get(name, str(default))
    try:
        value = int(raw)
    except ValueError:
        raise SystemExit(f"{name} must be an integer") from None
    if not 1 <= value <= 3_600:
        raise SystemExit(f"{name} must be between 1 and 3600")
    return value


def main() -> None:
    reference_path = os.environ.get("HERMES_MIGRATION_DSN_FILE")
    if not reference_path:
        raise SystemExit("HERMES_MIGRATION_DSN_FILE is required")
    database_url = DsnFileReference(reference_path).read()
    deadline = datetime.now(UTC) + timedelta(
        seconds=_positive_seconds("HERMES_MIGRATION_DEADLINE_SECONDS", 240)
    )
    normalized_url, identifiers = _migration_configuration(database_url, os.environ)
    engine = create_engine(normalized_url, poolclass=NullPool)
    try:
        with engine.connect() as connection:
            applied = PostgresMigrationRunner().apply_all(
                SqlAlchemyMigrationSession(connection),
                identifiers=identifiers,
                deadline=deadline,
            )
    finally:
        engine.dispose()
    print(f"migration_count={len(applied)}")


if __name__ == "__main__":
    main()
