from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import update
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql.dml import Update

from hermes_cloud.modules.identity.domain import (
    RefreshSessionUnavailable,
    WebSocketTicketClaim,
    WebSocketTicketUnavailable,
)
from hermes_cloud.platform.postgres.models import (
    PasswordCredentialModel,
    RefreshSessionModel,
    WebSocketTicketModel,
)
from hermes_cloud.platform.postgres.repositories.identity import (
    SqlAlchemyIdentityRepository,
)
from hermes_cloud.platform.sqlalchemy.repositories.identity import (
    ticket_consumption_scope,
)

NOW = datetime(2026, 7, 30, 10, 0, tzinfo=UTC)
TENANT_ID = UUID("11111111-1111-4111-8111-111111111111")
USER_ID = UUID("22222222-2222-4222-8222-222222222222")
SESSION_ID = UUID("33333333-3333-4333-8333-333333333333")
TICKET_ID = UUID("44444444-4444-4444-8444-444444444444")
DIGEST_A = "a" * 64
DIGEST_B = "b" * 64
OBSERVER_REQUEST_FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "fixtures/repository_contracts"
    / "fixtures"
    / "valid"
    / "cloud-api-observer-ticket-request.json"
)


class _ScalarResult:
    def __init__(self, value: object | None) -> None:
        self._value = value

    def scalar_one_or_none(self) -> object | None:
        return self._value


class _Session:
    def __init__(self, values: list[object | None]) -> None:
        self.values = values
        self.statements: list[object] = []

    def execute(self, statement: object) -> _ScalarResult:
        self.statements.append(statement)
        return _ScalarResult(self.values.pop(0))


def _compiled(statement: object) -> str:
    return str(
        statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": False},
        )
    )


def _refresh_model() -> RefreshSessionModel:
    return RefreshSessionModel(
        tenant_id=TENANT_ID,
        refresh_session_id=SESSION_ID,
        user_id=USER_ID,
        token_digest=DIGEST_B,
        rotation=2,
        created_at=NOW - timedelta(days=1),
        rotated_at=NOW,
        revoked_at=None,
        expires_at=NOW + timedelta(days=1),
        retention_until=NOW + timedelta(days=31),
    )


def _ticket_model(
    *,
    session_id: UUID | None = SESSION_ID,
) -> WebSocketTicketModel:
    return WebSocketTicketModel(
        tenant_id=TENANT_ID,
        ticket_id=TICKET_ID,
        ticket_digest=DIGEST_A,
        principal_type="user",
        principal_id=USER_ID,
        refresh_session_id=SESSION_ID,
        session_id=session_id,
        observer_scope=["session.observe"],
        issued_at=NOW - timedelta(seconds=10),
        expires_at=NOW + timedelta(seconds=20),
        consumed_at=NOW,
        retention_until=NOW + timedelta(days=1),
    )


def test_credential_lookup_is_tenant_subject_and_status_scoped() -> None:
    model = PasswordCredentialModel(
        tenant_id=TENANT_ID,
        credential_id=UUID("55555555-5555-4555-8555-555555555555"),
        user_id=USER_ID,
        subject="user@example.test",
        password_hash="$argon2id$v=19$m=65536,t=3,p=4$c2FsdA$aGFzaA",
        status="active",
        created_at=NOW,
        updated_at=NOW,
    )
    session = _Session([model])
    repository = SqlAlchemyIdentityRepository(session)  # type: ignore[arg-type]

    credential = repository.credential_by_subject(
        tenant_id=TENANT_ID,
        subject="user@example.test",
    )

    assert credential is not None
    assert credential.password_hash.startswith("$argon2id$")
    compiled = _compiled(session.statements[0])
    assert "identity.password_credentials.tenant_id" in compiled
    assert "identity.password_credentials.subject" in compiled
    assert "identity.password_credentials.status" in compiled
    assert "JOIN identity.users" in compiled
    assert "JOIN identity.tenants" in compiled
    assert "identity.users.status" in compiled
    assert "identity.tenants.status" in compiled


def test_refresh_session_lookup_is_tenant_and_session_scoped() -> None:
    session = _Session([_refresh_model(), None])
    repository = SqlAlchemyIdentityRepository(session)  # type: ignore[arg-type]

    current = repository.refresh_session_by_id(
        tenant_id=TENANT_ID,
        refresh_session_id=SESSION_ID,
    )
    missing = repository.refresh_session_by_id(
        tenant_id=TENANT_ID,
        refresh_session_id=UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
    )

    assert current is not None
    assert current.tenant_id == TENANT_ID
    assert current.refresh_session_id == SESSION_ID
    assert current.user_id == USER_ID
    assert missing is None
    compiled = _compiled(session.statements[0])
    assert "FROM identity.refresh_sessions" in compiled
    assert "identity.refresh_sessions.tenant_id" in compiled
    assert "identity.refresh_sessions.refresh_session_id" in compiled


def test_refresh_rotate_is_one_atomic_tenant_digest_expiry_guarded_update() -> None:
    session = _Session([_refresh_model(), None])
    repository = SqlAlchemyIdentityRepository(session)  # type: ignore[arg-type]

    rotated = repository.rotate_refresh_session(
        tenant_id=TENANT_ID,
        refresh_session_id=SESSION_ID,
        expected_digest=DIGEST_A,
        replacement_digest=DIGEST_B,
        now=NOW,
    )

    assert rotated.rotation == 2
    statement = session.statements[0]
    assert isinstance(statement, Update)
    compiled = _compiled(statement)
    assert "UPDATE identity.refresh_sessions" in compiled
    assert "token_digest" in compiled
    assert "rotation=(identity.refresh_sessions.rotation +" in compiled
    assert "revoked_at IS NULL" in compiled
    assert "expires_at >" in compiled
    assert "RETURNING" in compiled

    with pytest.raises(RefreshSessionUnavailable):
        repository.rotate_refresh_session(
            tenant_id=TENANT_ID,
            refresh_session_id=SESSION_ID,
            expected_digest=DIGEST_A,
            replacement_digest=DIGEST_B,
            now=NOW,
        )


def test_refresh_rotate_rejects_non_digest_values_before_database_execution() -> None:
    session = _Session([])
    repository = SqlAlchemyIdentityRepository(session)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="digest"):
        repository.rotate_refresh_session(
            tenant_id=TENANT_ID,
            refresh_session_id=SESSION_ID,
            expected_digest=DIGEST_A,
            replacement_digest="plaintext-token",
            now=NOW,
        )

    assert session.statements == []


def test_refresh_revoke_is_idempotent_tenant_scoped_atomic_update() -> None:
    model = _refresh_model()
    model.revoked_at = NOW
    session = _Session([model, None])
    repository = SqlAlchemyIdentityRepository(session)  # type: ignore[arg-type]

    revoked = repository.revoke_refresh_session(
        tenant_id=TENANT_ID,
        refresh_session_id=SESSION_ID,
        now=NOW,
    )

    assert revoked.revoked_at == NOW
    statement = session.statements[0]
    assert isinstance(statement, Update)
    compiled = _compiled(statement)
    assert "UPDATE identity.refresh_sessions" in compiled
    assert "tenant_id" in compiled
    assert "refresh_session_id" in compiled
    assert "revoked_at IS NULL" in compiled
    assert "RETURNING" in compiled

    with pytest.raises(RefreshSessionUnavailable):
        repository.revoke_refresh_session(
            tenant_id=TENANT_ID,
            refresh_session_id=SESSION_ID,
            now=NOW,
        )


def test_ticket_consume_is_single_atomic_bound_and_expiry_guarded_update() -> None:
    session = _Session([_ticket_model(), None])
    repository = SqlAlchemyIdentityRepository(session)  # type: ignore[arg-type]
    claim = WebSocketTicketClaim(
        tenant_id=TENANT_ID,
        ticket_digest=DIGEST_A,
        principal_type="user",
        principal_id=USER_ID,
        refresh_session_id=SESSION_ID,
        session_id=SESSION_ID,
    )

    consumed = repository.consume_websocket_ticket(claim, now=NOW)

    assert consumed.consumed_at == NOW
    statement = session.statements[0]
    assert isinstance(statement, Update)
    compiled = _compiled(statement)
    assert "UPDATE identity.websocket_tickets" in compiled
    assert "consumed_at IS NULL" in compiled
    assert "expires_at >" in compiled
    assert "principal_id" in compiled
    assert "refresh_session_id" in compiled
    assert "session_id" in compiled
    assert "EXISTS (SELECT" in compiled
    assert "identity.refresh_sessions.tenant_id" in compiled
    assert "identity.refresh_sessions.user_id" in compiled
    assert "identity.refresh_sessions.revoked_at IS NULL" in compiled
    assert "identity.refresh_sessions.expires_at >" in compiled
    assert "RETURNING" in compiled

    with pytest.raises(WebSocketTicketUnavailable):
        repository.consume_websocket_ticket(claim, now=NOW)


def test_shared_ticket_consumption_scope_freezes_postgresql_mapped_orm_contract() -> None:
    claim = WebSocketTicketClaim(
        tenant_id=TENANT_ID,
        ticket_digest=DIGEST_A,
        principal_type="user",
        principal_id=USER_ID,
        refresh_session_id=SESSION_ID,
        session_id=SESSION_ID,
    )
    statement = (
        update(WebSocketTicketModel)
        .where(ticket_consumption_scope(claim, now=NOW))
        .values(consumed_at=NOW)
    )

    compiled = _compiled(statement)

    assert "UPDATE identity.websocket_tickets" in compiled
    assert "ticket_digest" in compiled
    assert "principal_type" in compiled
    assert "principal_id" in compiled
    assert "refresh_session_id" in compiled
    assert "session_id" in compiled
    assert "consumed_at IS NULL" in compiled
    assert "identity.websocket_tickets.expires_at >" in compiled
    assert "EXISTS (SELECT" in compiled
    assert "identity.refresh_sessions.user_id" in compiled
    assert "identity.refresh_sessions.revoked_at IS NULL" in compiled
    assert "identity.refresh_sessions.expires_at >" in compiled


def test_legacy_empty_observer_request_consumes_only_unscoped_ticket() -> None:
    request = json.loads(OBSERVER_REQUEST_FIXTURE.read_text(encoding="utf-8"))
    assert request == {}
    claim = WebSocketTicketClaim(
        tenant_id=TENANT_ID,
        ticket_digest=DIGEST_A,
        principal_type="user",
        principal_id=USER_ID,
        refresh_session_id=SESSION_ID,
        session_id=request.get("session_id"),
    )
    session = _Session([_ticket_model(session_id=None)])
    repository = SqlAlchemyIdentityRepository(session)  # type: ignore[arg-type]

    consumed = repository.consume_websocket_ticket(claim, now=NOW)

    assert consumed.session_id is None
    unscoped_sql = _compiled(session.statements[0])
    assert "websocket_tickets.session_id IS NULL" in unscoped_sql

    scoped_claim = WebSocketTicketClaim(
        tenant_id=TENANT_ID,
        ticket_digest=DIGEST_A,
        principal_type="user",
        principal_id=USER_ID,
        refresh_session_id=SESSION_ID,
        session_id=SESSION_ID,
    )
    scoped_session = _Session([None])
    scoped_repository = SqlAlchemyIdentityRepository(
        scoped_session  # type: ignore[arg-type]
    )

    with pytest.raises(WebSocketTicketUnavailable):
        scoped_repository.consume_websocket_ticket(scoped_claim, now=NOW)

    scoped_sql = _compiled(scoped_session.statements[0])
    assert "websocket_tickets.session_id IS NULL" not in scoped_sql
    assert "websocket_tickets.session_id =" in scoped_sql
