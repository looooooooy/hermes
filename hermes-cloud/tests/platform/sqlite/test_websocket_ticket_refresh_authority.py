from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from hermes_cloud.modules.cloud_api.application.service import (
    AuthenticationFailed,
    CloudApiService,
)
from hermes_cloud.modules.cloud_api.domain import CloudApiSettings
from hermes_cloud.modules.identity.domain import (
    Argon2PasswordHasher,
    PasswordCredential,
    RefreshSession,
    WebSocketTicket,
    WebSocketTicketClaim,
    WebSocketTicketUnavailable,
)
from hermes_cloud.platform.postgres.models import (
    TenantModel,
    UserModel,
    WebSocketTicketModel,
)
from hermes_cloud.platform.sqlite.engine import build_sqlite_engine
from hermes_cloud.platform.sqlite.runtime import (
    SQLiteLoginTenantResolver,
    SQLiteOperationScopedIdentityRepository,
)
from hermes_cloud.platform.sqlite.schema import build_sqlite_metadata

NOW = datetime.now(UTC).replace(microsecond=0)
TENANT_ID = UUID("11111111-1111-4111-8111-111111111111")
USER_ID = UUID("22222222-2222-4222-8222-222222222222")
OTHER_USER_ID = UUID("33333333-3333-4333-8333-333333333333")
SIGNING_KEY = b"ticket-refresh-authority-signing-key-32-bytes"


class _SecretResolver:
    def resolve(self, reference: str) -> bytes:
        assert reference == "secret-manager/test/ticket-refresh-authority"
        return SIGNING_KEY


def _runtime(
    tmp_path: Path,
    *,
    name: str,
) -> tuple[object, sessionmaker[Session], SQLiteOperationScopedIdentityRepository]:
    database = tmp_path / f"{name}.sqlite3"
    engine = build_sqlite_engine(
        f"sqlite+pysqlite:///{database}",
        allow_missing=True,
    )
    build_sqlite_metadata().create_all(engine)
    database.chmod(0o660)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory.begin() as session:
        session.add(
            TenantModel(
                tenant_id=TENANT_ID,
                slug="ticket-authority",
                display_name="Ticket Authority",
                status="active",
                created_at=NOW,
            )
        )
        session.add_all(
            (
                UserModel(
                    tenant_id=TENANT_ID,
                    user_id=USER_ID,
                    subject="user@example.test",
                    display_name="Ticket User",
                    email=None,
                    status="active",
                    created_at=NOW,
                ),
                UserModel(
                    tenant_id=TENANT_ID,
                    user_id=OTHER_USER_ID,
                    subject="other@example.test",
                    display_name="Other User",
                    email=None,
                    status="active",
                    created_at=NOW,
                ),
            )
        )
    return engine, factory, SQLiteOperationScopedIdentityRepository(factory)


def _refresh_session(*, user_id: UUID = USER_ID) -> RefreshSession:
    return RefreshSession(
        tenant_id=TENANT_ID,
        refresh_session_id=uuid4(),
        user_id=user_id,
        token_digest="a" * 64,
        rotation=0,
        created_at=NOW,
        rotated_at=None,
        revoked_at=None,
        expires_at=NOW + timedelta(hours=1),
        retention_until=NOW + timedelta(days=31),
    )


def _ticket(
    refresh: RefreshSession,
    *,
    principal_id: UUID = USER_ID,
    digest: str = "b" * 64,
) -> WebSocketTicket:
    return WebSocketTicket(
        tenant_id=TENANT_ID,
        ticket_id=uuid4(),
        ticket_digest=digest,
        principal_type="user",
        principal_id=principal_id,
        refresh_session_id=refresh.refresh_session_id,
        session_id=None,
        observer_scope=("session.observe",),
        issued_at=NOW,
        expires_at=NOW + timedelta(hours=3),
        consumed_at=None,
        retention_until=NOW + timedelta(days=31),
    )


def _claim(ticket: WebSocketTicket) -> WebSocketTicketClaim:
    return WebSocketTicketClaim(
        tenant_id=ticket.tenant_id,
        ticket_digest=ticket.ticket_digest,
        principal_type=ticket.principal_type,
        principal_id=ticket.principal_id,
        refresh_session_id=ticket.refresh_session_id,
        session_id=ticket.session_id,
    )


def test_issued_ticket_cannot_be_consumed_after_logout(tmp_path: Path) -> None:
    engine, factory, repository = _runtime(tmp_path, name="logout")
    try:
        repository.store_password_credential(
            PasswordCredential(
                tenant_id=TENANT_ID,
                credential_id=uuid4(),
                user_id=USER_ID,
                subject="user@example.test",
                password_hash=Argon2PasswordHasher().hash("correct-password"),
                status="active",
                created_at=NOW,
                updated_at=NOW,
            )
        )
        service = CloudApiService(
            identity_repository=repository,
            tenant_resolver=SQLiteLoginTenantResolver(factory),
            secret_resolver=_SecretResolver(),
            settings=CloudApiSettings(
                signing_secret_ref="secret-manager/test/ticket-refresh-authority",
                access_ttl_seconds=300,
                refresh_ttl_seconds=3600,
                ticket_ttl_seconds=60,
            ),
            now=lambda: NOW,
        )
        issued = service.issue_password_login(
            provider="basic",
            subject="user@example.test",
            password="correct-password",
            next_path="",
        )
        principal = service.authenticate_access(issued.access_token.reveal())
        ticket = service.mint_observer_ticket(principal).ticket.reveal()

        service.logout_browser_session(
            access_token=issued.access_token.reveal(),
            refresh_token=issued.refresh_token.reveal(),
        )

        with pytest.raises(AuthenticationFailed):
            service.consume_websocket_ticket(ticket)
        with factory.begin() as session:
            stored = session.scalar(select(WebSocketTicketModel))
            assert stored is not None
            assert stored.consumed_at is None
    finally:
        engine.dispose()


@pytest.mark.parametrize("authority", ("revoked", "expired", "principal"))
def test_ticket_consumption_requires_current_refresh_authority(
    tmp_path: Path,
    authority: str,
) -> None:
    engine, factory, repository = _runtime(tmp_path, name=authority)
    try:
        refresh = _refresh_session()
        consume_at = NOW + timedelta(minutes=1)
        if authority == "revoked":
            refresh = replace(refresh, revoked_at=NOW + timedelta(seconds=30))
        if authority == "expired":
            refresh = replace(
                refresh,
                expires_at=NOW + timedelta(seconds=30),
                retention_until=NOW + timedelta(days=31),
            )
        principal_id = OTHER_USER_ID if authority == "principal" else USER_ID
        ticket = _ticket(refresh, principal_id=principal_id)
        repository.create_refresh_session(refresh)
        repository.issue_websocket_ticket(ticket)

        with pytest.raises(WebSocketTicketUnavailable):
            repository.consume_websocket_ticket(_claim(ticket), now=consume_at)

        with factory.begin() as session:
            stored = session.scalar(
                select(WebSocketTicketModel).where(
                    WebSocketTicketModel.ticket_id == ticket.ticket_id
                )
            )
            assert stored is not None
            assert stored.consumed_at is None
    finally:
        engine.dispose()


def test_concurrent_ticket_consumption_commits_exactly_one_winner(
    tmp_path: Path,
) -> None:
    engine, _factory, repository = _runtime(tmp_path, name="concurrent")
    try:
        refresh = _refresh_session()
        ticket = _ticket(refresh)
        repository.create_refresh_session(refresh)
        repository.issue_websocket_ticket(ticket)
        barrier = Barrier(2)

        def consume() -> str:
            barrier.wait(timeout=2)
            try:
                repository.consume_websocket_ticket(
                    _claim(ticket),
                    now=NOW + timedelta(minutes=1),
                )
            except WebSocketTicketUnavailable:
                return "rejected"
            return "consumed"

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = tuple(executor.map(lambda _index: consume(), range(2)))

        assert sorted(outcomes) == ["consumed", "rejected"]
    finally:
        engine.dispose()
