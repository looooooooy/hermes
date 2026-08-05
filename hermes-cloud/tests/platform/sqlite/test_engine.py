from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from hermes_cloud.platform.postgres.models import UserModel
from hermes_cloud.platform.sqlite.engine import (
    SQLiteConfigurationError,
    build_sqlite_engine,
    require_sqlite_version,
    sqlite_database_path,
)
from hermes_cloud.platform.sqlite.schema import build_sqlite_metadata


def _database_url(path: Path) -> str:
    return f"sqlite+pysqlite:///{path}"


def test_sqlite_database_path_requires_safe_absolute_file(
    tmp_path: Path,
) -> None:
    database = tmp_path / "hermes-cloud.sqlite3"
    database.touch(mode=0o660)

    assert sqlite_database_path(_database_url(database)) == database

    database.chmod(0o664)
    with pytest.raises(SQLiteConfigurationError, match="database file is unsafe"):
        sqlite_database_path(_database_url(database))


@pytest.mark.parametrize(
    "database_url",
    (
        "sqlite+pysqlite:///:memory:",
        "sqlite+pysqlite:///relative.sqlite3",
        "sqlite://",
        "postgresql+psycopg://database.invalid/hermes",
    ),
)
def test_sqlite_database_path_rejects_memory_relative_and_other_providers(
    database_url: str,
) -> None:
    with pytest.raises(SQLiteConfigurationError):
        sqlite_database_path(database_url, allow_missing=True)


def test_sqlite_database_path_rejects_symlinks_and_unsafe_parent(
    tmp_path: Path,
) -> None:
    database = tmp_path / "database.sqlite3"
    database.touch(mode=0o660)
    database_link = tmp_path / "database-link.sqlite3"
    database_link.symlink_to(database)

    with pytest.raises(SQLiteConfigurationError, match="database file is unsafe"):
        sqlite_database_path(_database_url(database_link))

    safe_parent = tmp_path / "safe"
    safe_parent.mkdir(mode=0o770)
    parent_link = tmp_path / "parent-link"
    parent_link.symlink_to(safe_parent, target_is_directory=True)
    with pytest.raises(SQLiteConfigurationError, match="parent directory is unsafe"):
        sqlite_database_path(
            _database_url(parent_link / "database.sqlite3"),
            allow_missing=True,
        )

    unsafe_parent = tmp_path / "unsafe"
    unsafe_parent.mkdir(mode=0o777)
    os.chmod(unsafe_parent, 0o777)
    with pytest.raises(SQLiteConfigurationError, match="parent directory is unsafe"):
        sqlite_database_path(
            _database_url(unsafe_parent / "database.sqlite3"),
            allow_missing=True,
        )


def test_sqlite_engine_uses_bounded_file_concurrency_options(
    tmp_path: Path,
) -> None:
    database = tmp_path / "database.sqlite3"
    database.touch(mode=0o660)
    captured: dict[str, object] = {}
    sentinel = object()

    def engine_factory(database_url: str, **options: object) -> object:
        captured["database_url"] = database_url
        captured["options"] = options
        return sentinel

    engine = build_sqlite_engine(
        _database_url(database),
        engine_factory=engine_factory,
    )

    assert engine is sentinel
    assert captured == {
        "database_url": _database_url(database),
        "options": {
            "connect_args": {
                "check_same_thread": False,
                "timeout": 5.0,
            },
            "execution_options": {
                "schema_translate_map": {
                    "audit": None,
                    "authorization": None,
                    "command": None,
                    "device": None,
                    "identity": None,
                    "platform": None,
                    "projection": None,
                    "public": None,
                    "workspace": None,
                }
            },
            "pool_pre_ping": True,
        },
    }


def test_sqlite_runtime_requires_on_conflict_capable_library() -> None:
    assert require_sqlite_version((3, 24, 0)) == (3, 24, 0)

    with pytest.raises(
        SQLiteConfigurationError,
        match="SQLite 3.24 or newer is required",
    ):
        require_sqlite_version((3, 23, 1))


def _foreign_keys_enabled(engine: object) -> int:
    with engine.connect() as connection:  # type: ignore[attr-defined]
        return int(connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one())


def test_sqlite_engine_enables_foreign_keys_on_every_pooled_connection(
    tmp_path: Path,
) -> None:
    database = tmp_path / "database.sqlite3"
    engine = build_sqlite_engine(
        _database_url(database),
        allow_missing=True,
    )
    try:
        assert _foreign_keys_enabled(engine) == 1
        with engine.connect() as first, engine.connect() as second:
            assert first.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
            assert second.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
        engine.dispose()
        assert _foreign_keys_enabled(engine) == 1
    finally:
        engine.dispose()


def test_sqlite_engine_rejects_orphan_orm_rows_and_rolls_back(
    tmp_path: Path,
) -> None:
    database = tmp_path / "database.sqlite3"
    engine = build_sqlite_engine(
        _database_url(database),
        allow_missing=True,
    )
    try:
        build_sqlite_metadata().create_all(engine)
        with Session(engine) as session:
            with pytest.raises(IntegrityError), session.begin():
                session.add(
                    UserModel(
                        tenant_id=uuid4(),
                        user_id=uuid4(),
                        subject="orphan",
                        display_name="Orphan",
                        email=None,
                        status="active",
                    )
                )
            assert session.scalar(select(func.count()).select_from(UserModel)) == 0
    finally:
        engine.dispose()
