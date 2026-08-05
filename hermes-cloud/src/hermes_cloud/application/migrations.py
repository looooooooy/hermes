"""Fail-closed orchestration for the PostgreSQL v1 migration catalog."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Final

from hermes_cloud.domain.migrations import (
    PUBLISHED_POSTGRES_MIGRATIONS,
    AppliedMigration,
    MigrationHistoryConflict,
    MigrationLockUnavailable,
    PublishedMigration,
    UnsafeMigrationIdentifier,
    verify_published_migration_registry,
)
from hermes_cloud.ports.migration_session import MigrationSession

MIGRATION_ADVISORY_LOCK_KEY: Final = 0x4845524D4553434C
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]{0,62}$")


class PostgresMigrationRunner:
    """Apply an immutable migration catalog through a driver-neutral session."""

    def __init__(
        self,
        *,
        catalog: Sequence[PublishedMigration] = PUBLISHED_POSTGRES_MIGRATIONS,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._catalog = tuple(catalog)
        self._clock = clock or (lambda: datetime.now(UTC))

    def apply_all(
        self,
        session: MigrationSession,
        *,
        identifiers: Mapping[str, str],
        deadline: datetime,
    ) -> tuple[AppliedMigration, ...]:
        """Apply every pending migration and return newly recorded rows.

        Runner state::

            unlocked --> locked --> history_validated --> applying --> complete
                 |          |               |                 |
                 +------ lock_failed        +-------------> failed

        Per-migration transaction::

            begin --> execute typed plan --> record ledger --> commit
               |             |
               +----------> rollback

        Lock contention, unknown history, checksum drift, missing versions, and
        unsafe identifiers all fail closed. Role identifiers remain separate
        until typed PostgreSQL constructs validate and quote them.
        """

        verify_published_migration_registry(self._catalog)
        self._require_deadline(deadline)
        safe_identifiers = self._validated_identifiers(identifiers)

        acquired = session.try_advisory_lock(
            MIGRATION_ADVISORY_LOCK_KEY,
            deadline=deadline,
        )
        if not acquired:
            raise MigrationLockUnavailable("migration advisory lock unavailable")

        try:
            with session.transaction(deadline=deadline):
                session.bootstrap_ledger(deadline=deadline)

            history = tuple(session.read_applied_migrations(deadline=deadline))
            self._validate_history(history)
            newly_applied: list[AppliedMigration] = []
            for migration in self._catalog[len(history) :]:
                bound_identifiers = MappingProxyType(
                    {
                        variable: safe_identifiers[variable]
                        for variable in migration.variables
                    }
                )
                applied = AppliedMigration(
                    version=migration.version,
                    name=migration.name,
                    checksum=migration.checksum,
                    applied_at=self._now(),
                )
                with session.transaction(deadline=deadline):
                    session.apply_migration(
                        migration,
                        identifiers=bound_identifiers,
                        deadline=deadline,
                    )
                    session.record_applied_migration(
                        applied,
                        deadline=deadline,
                    )
                newly_applied.append(applied)
            result = tuple(newly_applied)
        except BaseException as primary_error:
            try:
                session.release_advisory_lock(
                    MIGRATION_ADVISORY_LOCK_KEY,
                    deadline=deadline,
                )
            except BaseException as cleanup_error:  # noqa: BLE001
                raise BaseExceptionGroup(
                    "migration failed and advisory lock cleanup failed",
                    (primary_error, cleanup_error),
                ) from primary_error
            raise
        else:
            session.release_advisory_lock(
                MIGRATION_ADVISORY_LOCK_KEY,
                deadline=deadline,
            )
            return result

    def _validated_identifiers(
        self,
        identifiers: Mapping[str, str],
    ) -> Mapping[str, str]:
        required = {
            variable for migration in self._catalog for variable in migration.variables
        }
        if set(identifiers) != required:
            raise UnsafeMigrationIdentifier(
                "migration identifier bindings are incomplete or unexpected"
            )
        for name in required:
            value = identifiers[name]
            if (
                _SAFE_IDENTIFIER.fullmatch(value) is None
                or len(value.encode("utf-8")) > 63
            ):
                raise UnsafeMigrationIdentifier(
                    f"migration identifier is unsafe: {name}"
                )
        if (
            safe_migration_role := identifiers.get("migration_role")
        ) is not None and safe_migration_role == identifiers.get("runtime_role"):
            raise UnsafeMigrationIdentifier(
                "migration and runtime roles must be different"
            )
        return MappingProxyType(dict(identifiers))

    def _validate_history(
        self,
        history: tuple[AppliedMigration, ...],
    ) -> None:
        if any(item.version > len(self._catalog) for item in history):
            raise MigrationHistoryConflict(
                "database has a migration newer than this binary"
            )
        expected_versions = tuple(range(1, len(history) + 1))
        actual_versions = tuple(item.version for item in history)
        if actual_versions != expected_versions:
            raise MigrationHistoryConflict(
                "migration history must be a contiguous ordered prefix"
            )
        for item in history:
            expected = self._catalog[item.version - 1]
            if item.name != expected.name or item.checksum != expected.checksum:
                raise MigrationHistoryConflict(
                    "migration name or checksum does not match the catalog"
                )

    def _now(self) -> datetime:
        applied_at = self._clock()
        if applied_at.utcoffset() is None:
            raise ValueError("migration clock must return a timezone-aware time")
        return applied_at

    @staticmethod
    def _require_deadline(deadline: datetime) -> None:
        if deadline.utcoffset() is None:
            raise ValueError("migration deadline must include a timezone")
