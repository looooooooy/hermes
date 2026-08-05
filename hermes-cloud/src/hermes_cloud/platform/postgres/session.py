"""Dedicated SQLAlchemy session boundary for PostgreSQL migrations."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session
from sqlalchemy.schema import CreateTable

from hermes_cloud.domain.migrations import AppliedMigration, PublishedMigration
from hermes_cloud.platform.postgres.catalog import MigrationPlan, migration_plan_for
from hermes_cloud.platform.postgres.ddl import (
    ReleaseAdvisoryLock,
    SetLocalLockTimeout,
    SetLocalStatementTimeout,
    TryAdvisoryLock,
)
from hermes_cloud.platform.postgres.models import MigrationLedgerModel

_POSTGRESQL_TIMEOUT_MAX = 2**31 - 1
_LOCK_RELEASE_TIMEOUT_MS = 5_000


class SqlAlchemyMigrationSession:
    """Keep lock ownership and typed migration work on one connection."""

    def __init__(
        self,
        connection: Connection,
        *,
        clock: Callable[[], datetime] | None = None,
        plan_resolver: Callable[[PublishedMigration], MigrationPlan] | None = None,
    ) -> None:
        self._connection = connection
        self._clock = clock or (lambda: datetime.now(UTC))
        self._plan_resolver = plan_resolver or migration_plan_for
        self._in_transaction = False

    def try_advisory_lock(
        self,
        key: int,
        *,
        deadline: datetime,
    ) -> bool:
        self._require_outside_transaction()
        milliseconds = self._remaining_timeout_ms(deadline)
        try:
            self._set_timeouts(milliseconds)
            result = self._connection.execute(TryAdvisoryLock(key))
            acquired = bool(result.scalar_one())
            self._connection.commit()
        except BaseException:
            self._abandon_connection()
            raise
        return acquired

    def release_advisory_lock(
        self,
        key: int,
        *,
        deadline: datetime,
    ) -> None:
        self._require_deadline(deadline)
        self._require_outside_transaction()
        try:
            self._configure_cleanup_timeouts()
            result = self._connection.execute(ReleaseAdvisoryLock(key))
            released = bool(result.scalar_one())
            self._connection.commit()
        except BaseException:
            self._abandon_connection()
            raise
        if not released:
            raise RuntimeError("migration advisory lock was not owned")

    @contextmanager
    def transaction(
        self,
        *,
        deadline: datetime,
    ) -> Iterator[None]:
        self._remaining_timeout_ms(deadline)
        self._require_outside_transaction()
        with self._connection.begin():
            self._in_transaction = True
            try:
                yield
            finally:
                self._in_transaction = False

    def bootstrap_ledger(
        self,
        *,
        deadline: datetime,
    ) -> None:
        self._require_transaction()
        self._configure_timeouts(deadline)
        self._connection.execute(
            CreateTable(
                MigrationLedgerModel.__table__,
                if_not_exists=True,
            )
        )

    def apply_migration(
        self,
        migration: PublishedMigration,
        *,
        identifiers: Mapping[str, str],
        deadline: datetime,
    ) -> None:
        self._require_transaction()
        for operation in self._plan_resolver(migration).operations:
            self._configure_timeouts(deadline)
            self._connection.execute(operation.statement(identifiers))

    def read_applied_migrations(
        self,
        *,
        deadline: datetime,
    ) -> Sequence[AppliedMigration]:
        self._require_outside_transaction()
        with self._connection.begin():
            self._configure_timeouts(deadline)
            with Session(
                bind=self._connection,
                join_transaction_mode="rollback_only",
            ) as orm_session:
                rows = orm_session.scalars(
                    select(MigrationLedgerModel).order_by(MigrationLedgerModel.version)
                ).all()
        return tuple(
            AppliedMigration(
                version=row.version,
                name=row.name,
                checksum=row.checksum,
                applied_at=row.applied_at,
            )
            for row in rows
        )

    def record_applied_migration(
        self,
        migration: AppliedMigration,
        *,
        deadline: datetime,
    ) -> None:
        self._require_transaction()
        self._configure_timeouts(deadline)
        with Session(
            bind=self._connection,
            join_transaction_mode="rollback_only",
        ) as orm_session:
            orm_session.add(
                MigrationLedgerModel(
                    version=migration.version,
                    name=migration.name,
                    checksum=migration.checksum,
                    applied_at=migration.applied_at,
                )
            )
            orm_session.flush()

    def _remaining_timeout_ms(self, deadline: datetime) -> int:
        self._require_deadline(deadline)
        now = self._clock()
        if now.utcoffset() is None:
            raise ValueError("migration clock must return a timezone-aware time")
        remaining_seconds = (deadline - now).total_seconds()
        if remaining_seconds <= 0:
            raise TimeoutError("migration deadline has expired")
        milliseconds = max(1, int(remaining_seconds * 1000))
        return min(milliseconds, _POSTGRESQL_TIMEOUT_MAX)

    def _configure_timeouts(self, deadline: datetime) -> None:
        milliseconds = self._remaining_timeout_ms(deadline)
        self._set_timeouts(milliseconds)

    def _set_timeouts(self, milliseconds: int) -> None:
        self._connection.execute(SetLocalStatementTimeout(milliseconds))
        self._connection.execute(SetLocalLockTimeout(milliseconds))

    def _configure_cleanup_timeouts(self) -> None:
        self._connection.execute(SetLocalStatementTimeout(_LOCK_RELEASE_TIMEOUT_MS))
        self._connection.execute(SetLocalLockTimeout(_LOCK_RELEASE_TIMEOUT_MS))

    def _abandon_connection(self) -> None:
        for action in (
            self._connection.rollback,
            self._connection.invalidate,
            self._connection.close,
        ):
            with suppress(Exception):
                action()

    @staticmethod
    def _require_deadline(deadline: datetime) -> None:
        if deadline.utcoffset() is None:
            raise ValueError("migration deadline must include a timezone")

    def _require_outside_transaction(self) -> None:
        if self._in_transaction:
            raise RuntimeError("operation requires no ambient migration transaction")

    def _require_transaction(self) -> None:
        if not self._in_transaction:
            raise RuntimeError("operation requires an ambient migration transaction")
