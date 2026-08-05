"""Driver-neutral PostgreSQL migration session boundary."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import AbstractContextManager
from datetime import datetime
from typing import Protocol

from hermes_cloud.domain.migrations import AppliedMigration, PublishedMigration


class MigrationSession(Protocol):
    """Provide one dedicated PostgreSQL migration connection.

    The session owns a connection-level advisory lock and transaction
    boundaries. Implementations must honor each deadline, must not retry or
    partially commit a migration, and must keep all side effects inside the
    supplied transaction except the session-level advisory lock.

    Migration plans expose only typed SQLAlchemy executable objects. Concrete
    sessions must not accept raw SQL strings or driver escape hatches.
    """

    def try_advisory_lock(
        self,
        key: int,
        *,
        deadline: datetime,
    ) -> bool:
        """Attempt a session-level PostgreSQL advisory lock."""
        ...

    def release_advisory_lock(
        self,
        key: int,
        *,
        deadline: datetime,
    ) -> None:
        """Release a lock previously acquired by this session."""
        ...

    def transaction(
        self,
        *,
        deadline: datetime,
    ) -> AbstractContextManager[None]:
        """Commit on success and roll back on every exception."""
        ...

    def bootstrap_ledger(
        self,
        *,
        deadline: datetime,
    ) -> None:
        """Ensure the ORM-backed ledger table exists."""
        ...

    def apply_migration(
        self,
        migration: PublishedMigration,
        *,
        identifiers: Mapping[str, str],
        deadline: datetime,
    ) -> None:
        """Execute one typed expand, migrate, contract plan in order."""
        ...

    def read_applied_migrations(
        self,
        *,
        deadline: datetime,
    ) -> Sequence[AppliedMigration]:
        """Read migration ledger rows in ascending version order."""
        ...

    def record_applied_migration(
        self,
        migration: AppliedMigration,
        *,
        deadline: datetime,
    ) -> None:
        """Persist the ledger row in the migration's ambient transaction."""
        ...
