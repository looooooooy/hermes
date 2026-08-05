from __future__ import annotations

import sqlite3
from typing import Any

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError, SQLAlchemyError

from hermes_connector.domain.storage import (
    SQLiteDiagnostics,
    StorageCorrupt,
    StorageFatalError,
    StorageFull,
    StorageReadOnly,
    StorageUnavailable,
)

SQLITE_FAILURES = (SQLAlchemyError, sqlite3.DatabaseError)


class SQLiteConnectionPolicy:
    """Install the SQLite-only connection contract in one infrastructure seam.

    SQLAlchemy intentionally has no portable abstraction for these settings.
    This is the only production module allowed to issue DBAPI PRAGMA commands;
    repositories and migrations remain SQLAlchemy ORM/schema operations.
    """

    def __init__(self, *, busy_timeout_ms: int) -> None:
        if type(busy_timeout_ms) is not int or busy_timeout_ms <= 0:
            raise ValueError("busy_timeout_ms must be a positive integer")
        self._busy_timeout_ms = busy_timeout_ms

    def install(self, engine: Engine) -> None:
        event.listen(engine, "connect", self._configure_connection)

    def diagnostics(self, connection: Any) -> SQLiteDiagnostics:
        cursor = connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode")
            journal_mode = str(cursor.fetchone()[0]).lower()
            cursor.execute("PRAGMA foreign_keys")
            foreign_keys = bool(cursor.fetchone()[0])
            cursor.execute("PRAGMA synchronous")
            synchronous = int(cursor.fetchone()[0])
            cursor.execute("PRAGMA busy_timeout")
            busy_timeout_ms = int(cursor.fetchone()[0])
        finally:
            cursor.close()
        return SQLiteDiagnostics(
            journal_mode=journal_mode,
            foreign_keys=foreign_keys,
            synchronous=synchronous,
            busy_timeout_ms=busy_timeout_ms,
        )

    def _configure_connection(self, connection: Any, _: object) -> None:
        cursor = connection.cursor()
        try:
            cursor.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms}")
            cursor.execute("PRAGMA foreign_keys = ON")
            cursor.execute("PRAGMA synchronous = FULL")
            cursor.execute("PRAGMA journal_mode = WAL")
            journal_mode = str(cursor.fetchone()[0]).lower()
            if journal_mode != "wal":
                raise sqlite3.OperationalError("WAL mode unavailable")
        finally:
            cursor.close()


def map_sqlite_error(error: BaseException) -> StorageFatalError:
    original = error.orig if isinstance(error, DBAPIError) else error
    code = getattr(original, "sqlite_errorcode", None)
    primary_code = code & 0xFF if isinstance(code, int) else None
    message = str(original).lower()
    if primary_code == sqlite3.SQLITE_FULL or "database or disk is full" in message:
        return StorageFull()
    if primary_code == sqlite3.SQLITE_READONLY or "readonly" in message:
        return StorageReadOnly()
    if primary_code in {sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB} or any(
        marker in message
        for marker in ("malformed", "not a database", "database disk image")
    ):
        return StorageCorrupt()
    return StorageUnavailable()
