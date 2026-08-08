from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import URL, create_engine, event
from sqlalchemy.orm import sessionmaker

from hermes_connector.adapters.persistence.sqlite.repositories import (
    owner_control as owner_control_repository,
)
from hermes_connector.adapters.sqlite_migrations import apply_migrations
from hermes_connector.adapters.sqlite_storage import SQLiteStorageComponent
from hermes_connector.domain.storage import StorageFatalError

from .private_file import ensure_private_empty_file
from .private_state import private_file_exists, validate_private_file


class WindowsPrivateSQLiteStorageComponent(SQLiteStorageComponent):
    """SQLite storage whose DB/WAL/SHM files are private before every connect."""

    def _open(self) -> None:
        engine = create_engine(
            URL.create("sqlite+pysqlite", database=str(self._path)),
            pool_size=1,
            max_overflow=0,
        )
        event.listen(engine, "do_connect", self._prepare_before_connect)
        self._engine = engine
        self._policy.install(engine)
        apply_migrations(engine)
        self._session_factory = sessionmaker(
            bind=engine,
            autoflush=False,
            expire_on_commit=False,
        )
        with self._session_factory() as session, session.begin():
            owner_control_repository.recover_executing(
                session,
                now=datetime.now(UTC),
            )
        diagnostics = self._diagnostics()
        if (
            diagnostics.journal_mode != "wal"
            or not diagnostics.foreign_keys
            or diagnostics.synchronous != 2
            or diagnostics.busy_timeout_ms != self._config.storage_busy_timeout_ms
        ):
            raise StorageFatalError("SQLite connection policy was not applied")
        self.validate_private_files()

    def _prepare_before_connect(
        self,
        _dialect: Any,
        _connection_record: Any,
        _connection_arguments: list[Any],
        _connection_parameters: dict[str, Any],
    ) -> None:
        for path in self.private_file_family:
            ensure_private_empty_file(path)

    @property
    def private_file_family(self) -> tuple[Path, Path, Path]:
        database = Path(self._path)
        return (
            database,
            Path(f"{database}-wal"),
            Path(f"{database}-shm"),
        )

    def validate_private_files(self) -> None:
        for path in self.private_file_family:
            if private_file_exists(path):
                validate_private_file(path)


__all__ = ["WindowsPrivateSQLiteStorageComponent"]
