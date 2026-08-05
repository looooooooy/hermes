"""Explicit, dry-run-first SQLite ORM schema creation for the test server."""

from __future__ import annotations

import argparse
import os
import stat
import sys
from collections.abc import Mapping

from sqlalchemy.engine import Engine

from hermes_cloud.configuration import DsnFileReference
from hermes_cloud.platform.sqlalchemy.observer_encryption import (
    AesGcmTenantEnvelopeCipher,
    read_tenant_kek_registry,
)
from hermes_cloud.platform.sqlite.engine import (
    build_sqlite_engine,
    sqlite_database_path,
)
from hermes_cloud.platform.sqlite.migrations import (
    CURRENT_SQLITE_SCHEMA_VERSION,
    VERIFIED_SQLITE_UPGRADE_SOURCE_VERSIONS,
    SQLiteUpgradeResult,
    plan_sqlite_schema,
    sqlite_upgrade_coverage,
    upgrade_sqlite_schema,
    verify_published_sqlite_catalog,
)
from hermes_cloud.platform.sqlite.schema import build_sqlite_metadata

_SENSITIVE_ARGUMENT_PREFIXES = (
    "--credential",
    "--dsn",
    "--password",
    "--secret",
    "--token",
)


class _RedactingArgumentParser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        self.print_usage(sys.stderr)
        self.exit(2, f"{self.prog}: error: invalid arguments\n")


def _arguments(argv: list[str] | None) -> argparse.Namespace:
    parser = _RedactingArgumentParser(
        prog="migrate_sqlite.py",
        usage="%(prog)s [-h] [--apply]",
        description="Plan or explicitly create the SQLite ORM schema",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="create missing SQLite ORM tables",
    )
    arguments = list(sys.argv[1:] if argv is None else argv)
    if any(
        argument.startswith(prefix)
        for argument in arguments
        for prefix in _SENSITIVE_ARGUMENT_PREFIXES
    ):
        parser.error("sensitive command-line arguments are forbidden")
    return parser.parse_args(arguments)


def _secure_database_file(path: os.PathLike[str] | str) -> None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise RuntimeError("SQLite database is not a regular file")
        if stat.S_IMODE(metadata.st_mode) != 0o660:
            os.fchmod(descriptor, 0o660)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _dispose(engine: Engine | None) -> None:
    if engine is not None:
        engine.dispose()


def main(
    argv: list[str] | None = None,
    *,
    environment: Mapping[str, str] = os.environ,
) -> None:
    arguments = _arguments(argv)
    engine: Engine | None = None
    previous_umask: int | None = None
    try:
        observer_cipher = AesGcmTenantEnvelopeCipher(
            read_tenant_kek_registry(environment["HERMES_OBSERVER_KEYRING_FILE"])
        )
        reference_path = environment["HERMES_MIGRATION_DSN_FILE"]
        database_url = DsnFileReference(reference_path).read()
        database_path = sqlite_database_path(database_url, allow_missing=True)
        metadata = build_sqlite_metadata()
        table_count = len(metadata.tables)
        verify_published_sqlite_catalog()
        coverage = sqlite_upgrade_coverage()
        if not arguments.apply:
            if database_path.exists():
                engine = build_sqlite_engine(database_url)
                result = plan_sqlite_schema(engine)
                _dispose(engine)
                engine = None
            else:
                result = SQLiteUpgradeResult(
                    schema_version=CURRENT_SQLITE_SCHEMA_VERSION,
                    source="empty",
                )
            print(
                "sqlite_migration_mode=plan "
                f"table_count={table_count} "
                f"schema_version={result.schema_version} "
                "historical_source_count="
                f"{len(VERIFIED_SQLITE_UPGRADE_SOURCE_VERSIONS)} "
                f"source={result.source} "
                "recent_two_covered="
                f"{str(coverage.recent_two_covered).lower()}"
            )
            return

        database_existing = database_path.exists()
        previous_umask = os.umask(0o007)
        engine = build_sqlite_engine(database_url, allow_missing=True)
        result = upgrade_sqlite_schema(engine, observer_cipher=observer_cipher)
        _dispose(engine)
        engine = None
        _secure_database_file(database_path)
    except Exception:  # noqa: BLE001 - CLI boundary redacts storage configuration.
        _dispose(engine)
        raise SystemExit("SQLite migration failed") from None
    except BaseException:
        _dispose(engine)
        raise
    finally:
        if previous_umask is not None:
            os.umask(previous_umask)

    print(
        "sqlite_migration_mode=apply "
        f"table_count={table_count} "
        f"database_existing={str(database_existing).lower()} "
        f"schema_version={result.schema_version} "
        f"source={result.source} "
        "recent_two_covered="
        f"{str(coverage.recent_two_covered).lower()}"
    )


if __name__ == "__main__":
    main()
