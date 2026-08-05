from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy.exc import OperationalError

from hermes_cloud.modules.identity.domain import RefreshSession, WebSocketTicket
from hermes_cloud.modules.identity.ports import IdentityRepositoryFailure
from hermes_cloud.platform.postgres.models import RefreshSessionModel
from hermes_cloud.platform.postgres.runtime import (
    OperationScopedIdentityRepository,
    OperationScopedSessionProjectionRepository,
    SqlAlchemyLoginTenantResolver,
)

NOW = datetime(2026, 7, 31, 8, 0, tzinfo=UTC)
TENANT_ID = UUID("11111111-1111-4111-8111-111111111111")
OTHER_TENANT_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
USER_ID = UUID("22222222-2222-4222-8222-222222222222")
REFRESH_SESSION_ID = UUID("33333333-3333-4333-8333-333333333333")
TICKET_ID = UUID("44444444-4444-4444-8444-444444444444")


class _ScalarRows:
    def __init__(self, values: list[object]) -> None:
        self._values = values

    def all(self) -> list[object]:
        return self._values


class _ExecuteResult:
    def __init__(self, value: object | None) -> None:
        self._value = value

    def scalar_one_or_none(self) -> object | None:
        return self._value

    def scalar_one(self) -> object:
        assert self._value is not None
        return self._value

    def scalars(self) -> _ScalarRows:
        values = [] if self._value is None else list(self._value)
        return _ScalarRows(values)

    def all(self) -> list[object]:
        return [] if self._value is None else list(self._value)


class _Session:
    def __init__(
        self,
        *,
        scalar_values: list[object] | None = None,
        execute_values: list[object | None] | None = None,
        fail_flush: bool = False,
        fail_execute: bool = False,
    ) -> None:
        self.scalar_values = scalar_values or []
        self.execute_values = execute_values or []
        self.fail_flush = fail_flush
        self.fail_execute = fail_execute
        self.added: list[object] = []
        self.statements: list[object] = []

    def add(self, value: object) -> None:
        self.added.append(value)

    def flush(self) -> None:
        if self.fail_flush:
            raise RuntimeError("database write failed")

    def scalars(self, statement: object) -> _ScalarRows:
        self.statements.append(statement)
        return _ScalarRows(self.scalar_values)

    def execute(self, statement: object) -> _ExecuteResult:
        self.statements.append(statement)
        if self.fail_execute:
            raise OperationalError("database write failed", {}, None)
        value = self.execute_values.pop(0) if self.execute_values else None
        return _ExecuteResult(value)


class _Begin:
    def __init__(self, factory: _SessionFactory, session: _Session) -> None:
        self._factory = factory
        self._session = session

    def __enter__(self) -> _Session:
        return self._session

    def __exit__(
        self,
        error_type: type[BaseException] | None,
        _error: BaseException | None,
        _traceback: object,
    ) -> bool:
        if error_type is None:
            self._factory.commits += 1
        else:
            self._factory.rollbacks += 1
        self._factory.closes += 1
        return False


class _SessionFactory:
    def __init__(self, sessions: list[_Session]) -> None:
        self._sessions = sessions
        self.begin_calls = 0
        self.commits = 0
        self.rollbacks = 0
        self.closes = 0

    def begin(self) -> _Begin:
        self.begin_calls += 1
        return _Begin(self, self._sessions.pop(0))


def _refresh_session() -> RefreshSession:
    return RefreshSession(
        tenant_id=TENANT_ID,
        refresh_session_id=REFRESH_SESSION_ID,
        user_id=USER_ID,
        token_digest="a" * 64,
        rotation=0,
        created_at=NOW,
        rotated_at=None,
        revoked_at=None,
        expires_at=NOW + timedelta(hours=1),
        retention_until=NOW + timedelta(days=31),
    )


def _ticket() -> WebSocketTicket:
    return WebSocketTicket(
        tenant_id=TENANT_ID,
        ticket_id=TICKET_ID,
        ticket_digest="b" * 64,
        principal_type="user",
        principal_id=USER_ID,
        refresh_session_id=REFRESH_SESSION_ID,
        session_id=None,
        observer_scope=("session.observe",),
        issued_at=NOW,
        expires_at=NOW + timedelta(seconds=60),
        consumed_at=None,
        retention_until=NOW + timedelta(days=30),
    )


def test_identity_write_commits_and_closes_one_operation_scoped_session() -> None:
    factory = _SessionFactory([_Session()])
    repository = OperationScopedIdentityRepository(factory)

    stored = repository.create_refresh_session(_refresh_session())

    assert stored == _refresh_session()
    assert factory.begin_calls == 1
    assert factory.commits == 1
    assert factory.rollbacks == 0
    assert factory.closes == 1


def test_identity_rotate_and_ticket_writes_each_commit_and_close() -> None:
    rotated_model = RefreshSessionModel(
        tenant_id=TENANT_ID,
        refresh_session_id=REFRESH_SESSION_ID,
        user_id=USER_ID,
        token_digest="c" * 64,
        rotation=1,
        created_at=NOW,
        rotated_at=NOW,
        revoked_at=None,
        expires_at=NOW + timedelta(hours=1),
        retention_until=NOW + timedelta(days=31),
    )
    factory = _SessionFactory(
        [
            _Session(execute_values=[rotated_model]),
            _Session(),
        ]
    )
    repository = OperationScopedIdentityRepository(factory)

    rotated = repository.rotate_refresh_session(
        tenant_id=TENANT_ID,
        refresh_session_id=REFRESH_SESSION_ID,
        expected_digest="a" * 64,
        replacement_digest="c" * 64,
        now=NOW,
    )
    issued = repository.issue_websocket_ticket(_ticket())

    assert rotated.rotation == 1
    assert issued == _ticket()
    assert factory.begin_calls == 2
    assert factory.commits == 2
    assert factory.rollbacks == 0
    assert factory.closes == 2


def test_identity_write_rolls_back_and_closes_on_failure() -> None:
    factory = _SessionFactory([_Session(fail_flush=True)])
    repository = OperationScopedIdentityRepository(factory)

    with pytest.raises(RuntimeError, match="database write failed"):
        repository.create_refresh_session(_refresh_session())

    assert factory.begin_calls == 1
    assert factory.commits == 0
    assert factory.rollbacks == 1
    assert factory.closes == 1


def test_identity_revoke_rolls_back_and_closes_on_database_failure() -> None:
    factory = _SessionFactory([_Session(fail_execute=True)])
    repository = OperationScopedIdentityRepository(factory)

    with pytest.raises(IdentityRepositoryFailure):
        repository.revoke_refresh_session(
            tenant_id=TENANT_ID,
            refresh_session_id=REFRESH_SESSION_ID,
            now=NOW,
        )

    assert factory.begin_calls == 1
    assert factory.commits == 0
    assert factory.rollbacks == 1
    assert factory.closes == 1


def test_projection_repository_is_operation_scoped_not_session_singleton() -> None:
    factory = _SessionFactory(
        [
            _Session(execute_values=[[], 0, []]),
            _Session(execute_values=[None]),
        ]
    )
    repository = OperationScopedSessionProjectionRepository(factory)

    sessions = repository.list_sessions(
        tenant_id=TENANT_ID,
        user_id=USER_ID,
        limit=20,
        offset=0,
        min_messages=1,
    )
    event_head = repository.session_event_head(
        tenant_id=TENANT_ID,
        user_id=USER_ID,
        session_key="session-key",
    )

    assert sessions == ((), 0)
    assert event_head == 0
    assert factory.begin_calls == 2
    assert factory.commits == 2
    assert factory.closes == 2


def test_login_tenant_resolution_requires_one_unique_active_match() -> None:
    unique_session = _Session(scalar_values=[TENANT_ID])
    ambiguous_session = _Session(
        scalar_values=[
            TENANT_ID,
            OTHER_TENANT_ID,
        ]
    )
    factory = _SessionFactory([unique_session, ambiguous_session])
    resolver = SqlAlchemyLoginTenantResolver(factory)

    assert resolver.tenant_for_subject("user@example.test") == TENANT_ID
    assert resolver.tenant_for_subject("user@example.test") is None
    assert factory.commits == 2
    assert factory.closes == 2

    statement = unique_session.statements[0]
    rendered = str(statement)
    assert "password_credentials" in rendered
    assert "users" in rendered
    assert "tenants" in rendered
    assert rendered.count("status") >= 3
