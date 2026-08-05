from __future__ import annotations

from contextlib import AbstractContextManager
from datetime import UTC, datetime, timedelta
from types import TracebackType

import pytest
from sqlalchemy.schema import CreateSchema, CreateTable

from hermes_cloud.domain.migrations import PublishedMigration
from hermes_cloud.platform.postgres.catalog import (
    MigrationOperation,
    MigrationPhase,
    MigrationPlan,
)
from hermes_cloud.platform.postgres.ddl import (
    ReleaseAdvisoryLock,
    SetLocalLockTimeout,
    SetLocalStatementTimeout,
    TryAdvisoryLock,
)
from hermes_cloud.platform.postgres.models import MigrationLedgerModel
from hermes_cloud.platform.postgres.session import SqlAlchemyMigrationSession

NOW = datetime(2026, 7, 30, 8, 0, tzinfo=UTC)
DEADLINE = NOW + timedelta(minutes=5)


class _Result:
    def __init__(self, value: object) -> None:
        self._value = value

    def scalar_one(self) -> object:
        return self._value


class _Transaction(AbstractContextManager[None]):
    def __init__(self, calls: list[object]) -> None:
        self._calls = calls

    def __enter__(self) -> None:
        self._calls.append("begin")

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._calls.append("rollback" if exc_type else "commit")


class _Connection:
    def __init__(
        self,
        *,
        fail_execute_type: type[object] | None = None,
        fail_commit: bool = False,
    ) -> None:
        self.calls: list[object] = []
        self.fail_execute_type = fail_execute_type
        self.fail_commit = fail_commit

    def execute(self, statement: object) -> _Result:
        self.calls.append(statement)
        if self.fail_execute_type is not None and isinstance(
            statement, self.fail_execute_type
        ):
            raise RuntimeError("lock execution outcome is ambiguous")
        return _Result(True)

    def commit(self) -> None:
        self.calls.append("autonomous-commit")
        if self.fail_commit:
            raise RuntimeError("lock commit outcome is ambiguous")

    def begin(self) -> _Transaction:
        return _Transaction(self.calls)

    def rollback(self) -> None:
        self.calls.append("rollback-connection")

    def invalidate(self) -> None:
        self.calls.append("invalidate-connection")

    def close(self) -> None:
        self.calls.append("close-connection")


def _operation(
    phase: MigrationPhase,
    name: str,
) -> MigrationOperation:
    return MigrationOperation(
        phase=phase,
        key=f"schema:{name}",
        variables=(),
        _factory=lambda _: CreateSchema(name),
    )


def test_advisory_lock_uses_typed_statements_and_releases_on_same_connection() -> None:
    connection = _Connection()
    session = SqlAlchemyMigrationSession(connection, clock=lambda: NOW)

    assert session.try_advisory_lock(42, deadline=DEADLINE) is True
    session.release_advisory_lock(42, deadline=DEADLINE)

    assert isinstance(connection.calls[0], SetLocalStatementTimeout)
    assert isinstance(connection.calls[1], SetLocalLockTimeout)
    assert isinstance(connection.calls[2], TryAdvisoryLock)
    assert connection.calls[3] == "autonomous-commit"
    assert isinstance(connection.calls[4], SetLocalStatementTimeout)
    assert isinstance(connection.calls[5], SetLocalLockTimeout)
    assert isinstance(connection.calls[6], ReleaseAdvisoryLock)
    assert connection.calls[7] == "autonomous-commit"
    assert connection.calls[0].milliseconds == 300_000
    assert connection.calls[1].milliseconds == 300_000


def test_expired_deadline_cannot_prevent_owned_lock_release() -> None:
    connection = _Connection()
    session = SqlAlchemyMigrationSession(
        connection,
        clock=lambda: DEADLINE + timedelta(seconds=1),
    )

    session.release_advisory_lock(42, deadline=DEADLINE)

    assert isinstance(connection.calls[0], SetLocalStatementTimeout)
    assert isinstance(connection.calls[1], SetLocalLockTimeout)
    assert isinstance(connection.calls[2], ReleaseAdvisoryLock)
    assert connection.calls[3] == "autonomous-commit"


def test_ledger_and_plan_execute_only_typed_statements_in_phase_order() -> None:
    migration = PublishedMigration(
        version=7,
        name="0007_phase_order",
        checksum="a" * 64,
    )
    plan = MigrationPlan(
        checksum=migration.checksum,
        expand=(_operation(MigrationPhase.EXPAND, "expand_marker"),),
        migrate=(_operation(MigrationPhase.MIGRATE, "migrate_marker"),),
        contract=(_operation(MigrationPhase.CONTRACT, "contract_marker"),),
    )
    connection = _Connection()
    session = SqlAlchemyMigrationSession(
        connection,
        clock=lambda: NOW,
        plan_resolver=lambda _: plan,
    )

    with session.transaction(deadline=DEADLINE):
        session.bootstrap_ledger(deadline=DEADLINE)
        session.apply_migration(
            migration,
            identifiers={},
            deadline=DEADLINE,
        )

    statements = [item for item in connection.calls if not isinstance(item, str)]
    actual_statements = [
        item
        for item in statements
        if not isinstance(
            item,
            (SetLocalStatementTimeout, SetLocalLockTimeout),
        )
    ]
    assert isinstance(actual_statements[0], CreateTable)
    assert actual_statements[0].element is MigrationLedgerModel.__table__
    assert actual_statements[0].if_not_exists is True
    assert [item.element for item in actual_statements[1:]] == [
        "expand_marker",
        "migrate_marker",
        "contract_marker",
    ]
    assert len(statements) == len(actual_statements) * 3
    for offset in range(0, len(statements), 3):
        assert isinstance(statements[offset], SetLocalStatementTimeout)
        assert isinstance(statements[offset + 1], SetLocalLockTimeout)


def test_each_plan_operation_uses_the_shrinking_remaining_budget() -> None:
    migration = PublishedMigration(
        version=7,
        name="0007_budget",
        checksum="b" * 64,
    )
    plan = MigrationPlan(
        checksum=migration.checksum,
        expand=(
            _operation(MigrationPhase.EXPAND, "one"),
            _operation(MigrationPhase.EXPAND, "two"),
            _operation(MigrationPhase.EXPAND, "three"),
        ),
    )
    clock_values = iter(
        (
            NOW,
            NOW + timedelta(seconds=1),
            NOW + timedelta(seconds=2),
            NOW + timedelta(seconds=3),
        )
    )
    connection = _Connection()
    session = SqlAlchemyMigrationSession(
        connection,
        clock=lambda: next(clock_values),
        plan_resolver=lambda _: plan,
    )

    with session.transaction(deadline=DEADLINE):
        session.apply_migration(migration, identifiers={}, deadline=DEADLINE)

    statement_timeouts = [
        item.milliseconds
        for item in connection.calls
        if isinstance(item, SetLocalStatementTimeout)
    ]
    assert statement_timeouts == [299_000, 298_000, 297_000]


def test_expired_deadline_fails_before_any_database_execution() -> None:
    connection = _Connection()
    session = SqlAlchemyMigrationSession(connection, clock=lambda: DEADLINE)

    with pytest.raises(TimeoutError, match="expired"):
        session.try_advisory_lock(42, deadline=DEADLINE)

    assert connection.calls == []


@pytest.mark.parametrize("failure", ["execute", "commit"])
def test_ambiguous_advisory_lock_failure_abandons_dedicated_connection(
    failure: str,
) -> None:
    connection = _Connection(
        fail_execute_type=TryAdvisoryLock if failure == "execute" else None,
        fail_commit=failure == "commit",
    )
    session = SqlAlchemyMigrationSession(connection, clock=lambda: NOW)

    with pytest.raises(RuntimeError, match="outcome is ambiguous"):
        session.try_advisory_lock(42, deadline=DEADLINE)

    assert connection.calls[-3:] == [
        "rollback-connection",
        "invalidate-connection",
        "close-connection",
    ]
