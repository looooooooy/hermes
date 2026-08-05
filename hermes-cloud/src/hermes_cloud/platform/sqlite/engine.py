"""Safe SQLAlchemy engine composition for a real SQLite database file."""

from __future__ import annotations

import os
import sqlite3
import stat
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from sqlalchemy import DateTime, TypeDecorator, create_engine, event
from sqlalchemy.dialects.sqlite import DATETIME
from sqlalchemy.engine import Engine, make_url

from hermes_cloud.platform.sqlite.schema import SQLITE_SCHEMA_TRANSLATE_MAP

_SQLITE_DRIVERS = frozenset({"sqlite", "sqlite+pysqlite"})
_MINIMUM_SQLITE_VERSION = (3, 24, 0)
_SQLITE_FOREIGN_KEYS_PRAGMA = "PRAGMA foreign_keys=ON"


class _SQLiteCursor(Protocol):
    def execute(self, statement: str) -> object: ...

    def close(self) -> None: ...


class _SQLiteConnection(Protocol):
    def cursor(self) -> _SQLiteCursor: ...


class SQLiteConfigurationError(ValueError):
    """Raised when the SQLite provider cannot be composed safely."""


def _configure_sqlite_pragma_policy(
    dbapi_connection: _SQLiteConnection,
    _connection_record: object,
) -> None:
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute(_SQLITE_FOREIGN_KEYS_PRAGMA)
    finally:
        cursor.close()


def require_sqlite_version(
    version_info: tuple[int, ...] | None = None,
) -> tuple[int, ...]:
    """Require SQLite's first generally available ON CONFLICT implementation."""

    version = tuple(
        sqlite3.sqlite_version_info if version_info is None else version_info
    )
    if version < _MINIMUM_SQLITE_VERSION:
        raise SQLiteConfigurationError("SQLite 3.24 or newer is required")
    return version


class _SQLiteUtcDateTime(TypeDecorator[datetime]):
    impl = DATETIME
    cache_ok = True

    def process_bind_param(
        self,
        value: datetime | None,
        _dialect: object,
    ) -> datetime | None:
        if value is None:
            return None
        if value.utcoffset() is None:
            raise ValueError("SQLite datetime must include a timezone")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(
        self,
        value: datetime | None,
        _dialect: object,
    ) -> datetime | None:
        if value is None or value.utcoffset() is not None:
            return value
        return value.replace(tzinfo=UTC)


def sqlite_database_path(
    database_url: str,
    *,
    allow_missing: bool = False,
) -> Path:
    """Return one validated absolute, non-symlink SQLite database path."""

    try:
        url = make_url(database_url)
    except Exception:  # noqa: BLE001 - configuration errors are redacted.
        raise SQLiteConfigurationError("SQLite database URL is invalid") from None
    if (
        url.drivername not in _SQLITE_DRIVERS
        or url.username is not None
        or url.password is not None
        or url.host is not None
        or url.port is not None
        or url.query
        or not url.database
    ):
        raise SQLiteConfigurationError("SQLite database URL is invalid")
    database = Path(url.database)
    if not database.is_absolute() or url.database == ":memory:":
        raise SQLiteConfigurationError("SQLite database path must be absolute")

    _validate_parent(database.parent)
    try:
        metadata = os.lstat(database)
    except FileNotFoundError:
        if allow_missing:
            return database
        raise SQLiteConfigurationError("SQLite database file is unavailable") from None
    except OSError:
        raise SQLiteConfigurationError("SQLite database file is unavailable") from None
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) & ~0o660
    ):
        raise SQLiteConfigurationError("SQLite database file is unsafe")
    return database


def _validate_parent(parent: Path) -> None:
    try:
        metadata = os.lstat(parent)
    except OSError:
        raise SQLiteConfigurationError(
            "SQLite database parent directory is unavailable"
        ) from None
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) & ~0o770
    ):
        raise SQLiteConfigurationError("SQLite database parent directory is unsafe")


def build_sqlite_engine(
    database_url: str,
    *,
    allow_missing: bool = False,
    engine_factory: Callable[..., Engine] = create_engine,
) -> Engine:
    """Build a bounded file-backed engine after validating its storage path."""

    require_sqlite_version()
    sqlite_database_path(database_url, allow_missing=allow_missing)
    engine = engine_factory(
        database_url,
        connect_args={
            "check_same_thread": False,
            "timeout": 5.0,
        },
        execution_options={
            "schema_translate_map": SQLITE_SCHEMA_TRANSLATE_MAP,
        },
        pool_pre_ping=True,
    )
    if isinstance(engine, Engine):
        event.listen(engine, "connect", _configure_sqlite_pragma_policy)
        engine.dialect.colspecs[DateTime] = _SQLiteUtcDateTime
    return engine
