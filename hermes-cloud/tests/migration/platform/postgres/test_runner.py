from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest

from hermes_cloud.application.migrations import (
    MIGRATION_ADVISORY_LOCK_KEY,
    PostgresMigrationRunner,
)
from hermes_cloud.domain.migrations import (
    AppliedMigration,
    MigrationHistoryConflict,
    MigrationLockUnavailable,
    UnsafeMigrationIdentifier,
)
from hermes_cloud.platform.postgres.catalog import (
    POSTGRES_V1_MIGRATIONS,
    Migration,
)

NOW = datetime(2026, 7, 30, 8, 0, tzinfo=UTC)
DEADLINE = NOW + timedelta(minutes=5)
IDENTIFIERS = {
    "database_name": "hermes_cloud",
    "migration_role": "hermes_migration",
    "runtime_role": "hermes_runtime",
}


class _FakeMigrationSession:
    def __init__(
        self,
        *,
        history: tuple[AppliedMigration, ...] = (),
        lock_available: bool = True,
        fail_version: int | None = None,
        fail_unlock: bool = False,
    ) -> None:
        self.history = list(history)
        self.lock_available = lock_available
        self.fail_version = fail_version
        self.fail_unlock = fail_unlock
        self.calls: list[object] = []

    def try_advisory_lock(self, key: int, *, deadline: datetime) -> bool:
        self.calls.append(("lock", key, deadline))
        return self.lock_available

    def release_advisory_lock(self, key: int, *, deadline: datetime) -> None:
        self.calls.append(("unlock", key, deadline))
        if self.fail_unlock:
            raise RuntimeError("migration unlock failed")

    @contextmanager
    def transaction(self, *, deadline: datetime) -> Iterator[None]:
        self.calls.append(("begin", deadline))
        try:
            yield
        except BaseException:
            self.calls.append("rollback")
            raise
        self.calls.append("commit")

    def bootstrap_ledger(self, *, deadline: datetime) -> None:
        self.calls.append(("ledger", deadline))

    def apply_migration(
        self,
        migration: Migration,
        *,
        identifiers: Mapping[str, str],
        deadline: datetime,
    ) -> None:
        self.calls.append(("apply", migration.version, dict(identifiers), deadline))
        if migration.version == self.fail_version:
            raise RuntimeError("migration execution failed")

    def read_applied_migrations(
        self,
        *,
        deadline: datetime,
    ) -> tuple[AppliedMigration, ...]:
        self.calls.append(("read_history", deadline))
        return tuple(self.history)

    def record_applied_migration(
        self,
        migration: AppliedMigration,
        *,
        deadline: datetime,
    ) -> None:
        self.calls.append(("record", migration, deadline))
        self.history.append(migration)


def _runner() -> PostgresMigrationRunner:
    return PostgresMigrationRunner(clock=lambda: NOW)


def _call_names(calls: list[object]) -> list[str]:
    return [call if isinstance(call, str) else str(call[0]) for call in calls]


def _history_prefix(count: int) -> tuple[AppliedMigration, ...]:
    return tuple(
        AppliedMigration(
            version=migration.version,
            name=migration.name,
            checksum=migration.checksum,
            applied_at=NOW,
        )
        for migration in POSTGRES_V1_MIGRATIONS[:count]
    )


def test_applies_each_pending_migration_in_its_own_transaction_and_records_it() -> None:
    session = _FakeMigrationSession()

    applied = _runner().apply_all(
        session,
        identifiers=IDENTIFIERS,
        deadline=DEADLINE,
    )

    assert tuple(item.version for item in applied) == tuple(
        migration.version for migration in POSTGRES_V1_MIGRATIONS
    )
    assert session.history == list(applied)
    assert all(item.applied_at == NOW for item in applied)
    assert [(item.version, item.name, item.checksum) for item in applied] == [
        (migration.version, migration.name, migration.checksum)
        for migration in POSTGRES_V1_MIGRATIONS
    ]
    names = _call_names(session.calls)
    assert names.count("begin") == len(POSTGRES_V1_MIGRATIONS) + 1
    assert names.count("commit") == len(POSTGRES_V1_MIGRATIONS) + 1
    assert names.count("rollback") == 0
    assert names[0] == "lock"
    assert names[-1] == "unlock"

    apply_calls = [
        call
        for call in session.calls
        if not isinstance(call, str) and call[0] == "apply"
    ]
    assert apply_calls[0][2] == {}
    assert apply_calls[1][2] == {}
    assert apply_calls[2][2] == IDENTIFIERS
    assert apply_calls[3][2] == {}
    assert apply_calls[4][2] == {"database_name": IDENTIFIERS["database_name"]}
    assert apply_calls[5][2] == {
        "migration_role": IDENTIFIERS["migration_role"],
        "runtime_role": IDENTIFIERS["runtime_role"],
    }
    assert apply_calls[6][2] == {
        "migration_role": IDENTIFIERS["migration_role"],
        "runtime_role": IDENTIFIERS["runtime_role"],
    }


def test_complete_history_rerun_is_idempotent_without_apply_or_record() -> None:
    session = _FakeMigrationSession(
        history=_history_prefix(len(POSTGRES_V1_MIGRATIONS))
    )

    applied = _runner().apply_all(
        session,
        identifiers=IDENTIFIERS,
        deadline=DEADLINE,
    )

    assert applied == ()
    names = _call_names(session.calls)
    assert names[0] == "lock"
    assert names[-1] == "unlock"
    assert names.count("ledger") == 1
    assert names.count("read_history") == 1
    assert "apply" not in names
    assert "record" not in names


def test_valid_history_prefix_applies_and_records_only_missing_suffix() -> None:
    prefix_length = 2
    session = _FakeMigrationSession(history=_history_prefix(prefix_length))

    applied = _runner().apply_all(
        session,
        identifiers=IDENTIFIERS,
        deadline=DEADLINE,
    )

    expected_suffix = POSTGRES_V1_MIGRATIONS[prefix_length:]
    assert tuple(item.version for item in applied) == tuple(
        item.version for item in expected_suffix
    )
    apply_versions = [
        call[1]
        for call in session.calls
        if not isinstance(call, str) and call[0] == "apply"
    ]
    record_versions = [
        call[1].version
        for call in session.calls
        if not isinstance(call, str) and call[0] == "record"
    ]
    assert apply_versions == [item.version for item in expected_suffix]
    assert record_versions == [item.version for item in expected_suffix]
    assert _call_names(session.calls)[-1] == "unlock"


def test_advisory_lock_contention_fails_before_bootstrap_or_history_read() -> None:
    session = _FakeMigrationSession(lock_available=False)

    with pytest.raises(MigrationLockUnavailable):
        _runner().apply_all(
            session,
            identifiers=IDENTIFIERS,
            deadline=DEADLINE,
        )

    assert session.calls == [("lock", MIGRATION_ADVISORY_LOCK_KEY, DEADLINE)]


@pytest.mark.parametrize(
    "history",
    (
        (
            AppliedMigration(
                version=1,
                name="0001_foundation",
                checksum="0" * 64,
                applied_at=NOW,
            ),
        ),
        (
            AppliedMigration(
                version=5,
                name="0005_unknown",
                checksum="0" * 64,
                applied_at=NOW,
            ),
        ),
        (
            AppliedMigration(
                version=2,
                name="0002_tenant_storage",
                checksum=POSTGRES_V1_MIGRATIONS[1].checksum,
                applied_at=NOW,
            ),
        ),
    ),
)
def test_checksum_unknown_version_and_noncontiguous_history_fail_closed(
    history: tuple[AppliedMigration, ...],
) -> None:
    session = _FakeMigrationSession(history=history)

    with pytest.raises(MigrationHistoryConflict):
        _runner().apply_all(
            session,
            identifiers=IDENTIFIERS,
            deadline=DEADLINE,
        )

    names = _call_names(session.calls)
    assert "apply" not in names
    assert names[-1] == "unlock"


def test_failed_migration_rolls_back_without_ledger_record_and_releases_lock() -> None:
    session = _FakeMigrationSession(fail_version=2)

    with pytest.raises(RuntimeError, match="migration execution failed"):
        _runner().apply_all(
            session,
            identifiers=IDENTIFIERS,
            deadline=DEADLINE,
        )

    assert [item.version for item in session.history] == [1]
    assert _call_names(session.calls)[-3:] == ["apply", "rollback", "unlock"]


@pytest.mark.parametrize(
    "identifiers",
    (
        {
            "database_name": "hermes_cloud",
            "migration_role": 'owner"; DROP TABLE identity.users; --',
            "runtime_role": "hermes_runtime",
        },
        {
            "database_name": "hermes_cloud",
            "migration_role": "hermes_migration",
        },
        {
            "database_name": "hermes_cloud",
            "migration_role": "same_role",
            "runtime_role": "same_role",
        },
    ),
)
def test_role_variables_must_be_complete_safe_identifiers(
    identifiers: dict[str, str],
) -> None:
    session = _FakeMigrationSession()

    with pytest.raises(UnsafeMigrationIdentifier):
        _runner().apply_all(
            session,
            identifiers=identifiers,
            deadline=DEADLINE,
        )

    assert session.calls == []


def test_primary_migration_failure_and_unlock_failure_are_both_preserved() -> None:
    session = _FakeMigrationSession(fail_version=2, fail_unlock=True)

    with pytest.raises(BaseExceptionGroup) as captured:
        _runner().apply_all(
            session,
            identifiers=IDENTIFIERS,
            deadline=DEADLINE,
        )

    assert [str(error) for error in captured.value.exceptions] == [
        "migration execution failed",
        "migration unlock failed",
    ]
    assert [item.version for item in session.history] == [1]
