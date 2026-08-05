from __future__ import annotations

from dataclasses import fields, replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import ClassVar
from uuid import UUID

import jwt
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from hermes_cloud.contracts.mobile_control import CONTROL_ERROR_CODES
from hermes_cloud.entrypoints.business_api import create_app
from hermes_cloud.modules.cloud_api.application.service import (
    AuthenticationFailed,
    CloudApiService,
)
from hermes_cloud.modules.cloud_api.domain import CloudApiSettings
from hermes_cloud.modules.identity.domain import (
    Argon2PasswordHasher,
    PasswordCredential,
    RefreshSession,
    RefreshSessionUnavailable,
    WebSocketTicket,
    WebSocketTicketClaim,
    WebSocketTicketUnavailable,
)
from hermes_cloud.modules.identity.ports import IdentityRepositoryFailure
from hermes_cloud.modules.projection.domain import (
    AgentProjection,
    CatalogSessionProjection,
    ProjectionScopeAmbiguous,
    SessionMessageProjection,
    SessionProjection,
)

TENANT_ID = UUID("11111111-1111-4111-8111-111111111111")
USER_ID = UUID("22222222-2222-4222-8222-222222222222")
NOW = datetime.now(UTC).replace(microsecond=0)
SIGNING_KEY = b"unit-test-signing-key-with-at-least-32-bytes"
SESSION_ID = UUID("44444444-4444-4444-8444-444444444444")
WORKSPACE_ID = UUID("55555555-5555-4555-8555-555555555555")
MESSAGE_ID = UUID("66666666-6666-4666-8666-666666666666")
SESSION_KEY = "session-root-1"
AGENT_ID = UUID("77777777-7777-4777-8777-777777777777")


class _TenantResolver:
    def tenant_for_subject(self, subject: str) -> UUID | None:
        return TENANT_ID if subject == "user@example.test" else None


class _SecretResolver:
    def resolve(self, reference: str) -> bytes:
        if reference != "secret-manager/unit/cloud-p0-signing":
            raise KeyError(reference)
        return SIGNING_KEY

    def __repr__(self) -> str:
        return "_SecretResolver(<redacted>)"


class _IdentityRepository:
    def __init__(self) -> None:
        password_hash = Argon2PasswordHasher().hash("correct-password")
        self.credential = PasswordCredential(
            tenant_id=TENANT_ID,
            credential_id=UUID("33333333-3333-4333-8333-333333333333"),
            user_id=USER_ID,
            subject="user@example.test",
            password_hash=password_hash,
            status="active",
            created_at=NOW,
            updated_at=NOW,
        )
        self.refresh_sessions: dict[UUID, RefreshSession] = {}
        self.websocket_tickets: dict[str, WebSocketTicket] = {}

    def credential_by_subject(
        self,
        *,
        tenant_id: UUID,
        subject: str,
    ) -> PasswordCredential | None:
        if tenant_id == TENANT_ID and subject == self.credential.subject:
            return self.credential
        return None

    def create_refresh_session(
        self,
        refresh_session: RefreshSession,
    ) -> RefreshSession:
        self.refresh_sessions[refresh_session.refresh_session_id] = refresh_session
        return refresh_session

    def refresh_session_by_id(
        self,
        *,
        tenant_id: UUID,
        refresh_session_id: UUID,
    ) -> RefreshSession | None:
        value = self.refresh_sessions.get(refresh_session_id)
        return value if value is not None and value.tenant_id == tenant_id else None

    def rotate_refresh_session(
        self,
        *,
        tenant_id: UUID,
        refresh_session_id: UUID,
        expected_digest: str,
        replacement_digest: str,
        now: datetime,
    ) -> RefreshSession:
        current = self.refresh_sessions.get(refresh_session_id)
        if (
            current is None
            or current.tenant_id != tenant_id
            or current.token_digest != expected_digest
            or current.revoked_at is not None
            or current.expires_at <= now
        ):
            raise RefreshSessionUnavailable
        rotated = replace(
            current,
            token_digest=replacement_digest,
            rotation=current.rotation + 1,
            rotated_at=now,
        )
        self.refresh_sessions[refresh_session_id] = rotated
        return rotated

    def revoke_refresh_session(
        self,
        *,
        tenant_id: UUID,
        refresh_session_id: UUID,
        now: datetime,
    ) -> RefreshSession:
        current = self.refresh_session_by_id(
            tenant_id=tenant_id,
            refresh_session_id=refresh_session_id,
        )
        if current is None or current.revoked_at is not None:
            raise RefreshSessionUnavailable
        revoked = replace(current, revoked_at=now)
        self.refresh_sessions[refresh_session_id] = revoked
        return revoked

    def issue_websocket_ticket(
        self,
        ticket: WebSocketTicket,
    ) -> WebSocketTicket:
        self.websocket_tickets[ticket.ticket_digest] = ticket
        return ticket

    def consume_websocket_ticket(
        self,
        claim: WebSocketTicketClaim,
        *,
        now: datetime,
    ) -> WebSocketTicket:
        current = self.websocket_tickets.get(claim.ticket_digest)
        if (
            current is None
            or current.tenant_id != claim.tenant_id
            or current.principal_type != claim.principal_type
            or current.principal_id != claim.principal_id
            or current.refresh_session_id != claim.refresh_session_id
            or current.session_id != claim.session_id
            or current.consumed_at is not None
            or current.expires_at <= now
        ):
            raise WebSocketTicketUnavailable
        consumed = replace(current, consumed_at=now)
        self.websocket_tickets[claim.ticket_digest] = consumed
        return consumed


class _ProjectionRepository:
    def __init__(self) -> None:
        self.session = SessionProjection(
            tenant_id=TENANT_ID,
            session_id=SESSION_ID,
            session_key=SESSION_KEY,
            workspace_id=WORKSPACE_ID,
            agent_id=AGENT_ID,
            profile="default",
            title="Contract session",
            state="active",
            revision=2,
            lineage_tip_message_id=MESSAGE_ID,
            lineage_tip_sequence=1,
            started_at=NOW - timedelta(minutes=5),
            updated_at=NOW,
            closed_at=None,
            retention_until=NOW + timedelta(days=30),
        )
        self.message = SessionMessageProjection(
            tenant_id=TENANT_ID,
            session_id=SESSION_ID,
            message_id=MESSAGE_ID,
            sequence=1,
            role="assistant",
            content={"text": "Projected response"},
            parent_message_id=None,
            created_at=NOW,
            retention_until=NOW + timedelta(days=30),
        )
        self.calls: list[tuple[str, UUID, UUID]] = []
        self.detail_agent_ids: list[UUID | None] = []

    def list_sessions(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        limit: int,
        offset: int,
        min_messages: int,
        agent_id: UUID | None = None,
        profile: str | None = None,
    ) -> tuple[tuple[SessionProjection, ...], int]:
        self.calls.append(("list", tenant_id, user_id))
        sessions = (
            (self.session,)
            if (tenant_id, user_id) == (TENANT_ID, USER_ID)
            and offset == 0
            and min_messages <= 1
            and limit >= 1
            and agent_id in {None, AGENT_ID}
            and profile in {None, "default"}
            else ()
        )
        return sessions, len(sessions)

    def session_detail(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        session_key: str,
        agent_id: UUID | None = None,
        profile: str | None = None,
    ) -> SessionProjection | None:
        self.calls.append(("detail", tenant_id, user_id))
        self.detail_agent_ids.append(agent_id)
        if (
            (tenant_id, user_id, session_key) == (TENANT_ID, USER_ID, SESSION_KEY)
            and agent_id in {None, AGENT_ID}
            and profile in {None, "default"}
        ):
            return self.session
        return None

    def session_messages(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        session_key: str,
        after_sequence: int,
        limit: int,
        offset: int,
        agent_id: UUID | None = None,
        profile: str | None = None,
    ) -> tuple[SessionMessageProjection, ...]:
        self.calls.append(("messages", tenant_id, user_id))
        if (
            (
                tenant_id,
                user_id,
                session_key,
                after_sequence,
            )
            == (TENANT_ID, USER_ID, SESSION_KEY, 0)
            and offset == 0
            and limit >= 1
            and agent_id in {None, AGENT_ID}
            and profile in {None, "default"}
        ):
            return (self.message,)
        return ()

    def session_event_head(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        session_key: str,
        agent_id: UUID | None = None,
        profile: str | None = None,
    ) -> int:
        assert (tenant_id, user_id, session_key) == (
            TENANT_ID,
            USER_ID,
            SESSION_KEY,
        )
        return 1

    def session_transcript(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        session_key: str,
        after_sequence: int,
        limit: int,
        offset: int,
        agent_id: UUID | None = None,
        profile: str | None = None,
    ) -> tuple[SessionProjection, tuple[SessionMessageProjection, ...], int] | None:
        detail = self.session_detail(
            tenant_id=tenant_id,
            user_id=user_id,
            session_key=session_key,
            agent_id=agent_id,
            profile=profile,
        )
        if detail is None:
            return None
        messages = self.session_messages(
            tenant_id=tenant_id,
            user_id=user_id,
            session_key=session_key,
            after_sequence=after_sequence,
            limit=limit,
            offset=offset,
            agent_id=agent_id,
            profile=profile,
        )
        return detail, messages, 1


class _AgentProjectionRepository(_ProjectionRepository):
    def list_agents(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        workspace_id: UUID | None = None,
    ) -> tuple[AgentProjection, ...]:
        if (tenant_id, user_id) != (TENANT_ID, USER_ID):
            return ()
        agents = (
            AgentProjection(
                tenant_id=TENANT_ID,
                agent_id=AGENT_ID,
                workspace_id=WORKSPACE_ID,
                agent_key="agent-a",
                status="active",
                last_seen_at=NOW,
            ),
            AgentProjection(
                tenant_id=TENANT_ID,
                agent_id=UUID("88888888-8888-4888-8888-888888888888"),
                workspace_id=UUID("99999999-9999-4999-8999-999999999999"),
                agent_key="agent-b",
                status="offline",
                last_seen_at=None,
            ),
        )
        return tuple(
            agent
            for agent in agents
            if workspace_id is None or agent.workspace_id == workspace_id
        )

    def list_agent_sessions(self, **scope: object):
        if (
            scope["tenant_id"],
            scope["user_id"],
            scope["agent_id"],
        ) != (TENANT_ID, USER_ID, AGENT_ID):
            return (), 0
        return (
            (
                CatalogSessionProjection(
                    session_id=SESSION_ID,
                    agent_id=AGENT_ID,
                    workspace_id=WORKSPACE_ID,
                    profile="default",
                    session_key=SESSION_KEY,
                    runtime_generation="runtime-catalog",
                    surface="hermes-cli",
                    authority_revision=1,
                    available_actions=("prompt.submit",),
                    active=True,
                ),
            ),
            1,
        )


class _MalformedAgentProjectionRepository(_ProjectionRepository):
    """Repository double that violates its domain return contract."""

    def __init__(self, resolved_agent: object) -> None:
        super().__init__()
        self._resolved_agent = resolved_agent

    def session_detail(self, **scope: object):
        projection = super().session_detail(**scope)  # type: ignore[arg-type]
        if projection is None:
            return None
        values = {
            field.name: getattr(projection, field.name)
            for field in fields(SessionProjection)
        }
        values["agent_id"] = self._resolved_agent
        return SimpleNamespace(**values)


class _ProjectionEventSource:
    def __init__(self, events: tuple[dict[str, object], ...] = ()) -> None:
        self._events = events
        self.calls: list[tuple[UUID, UUID, str, str | None, int]] = []
        self.agent_ids: list[UUID | None] = []

    async def events(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        session_key: str,
        profile: str | None,
        after_sequence: int,
        agent_id: UUID | None = None,
    ):
        self.calls.append((tenant_id, user_id, session_key, profile, after_sequence))
        self.agent_ids.append(agent_id)
        for event in self._events:
            yield event


class _DeferredObserverProjection:
    def __init__(self) -> None:
        self.ready = False

    def observer_snapshot(self, **_scope: object):
        if not self.ready:
            return None
        return {
            "session_key": SESSION_KEY,
            "runtime_session_id": "runtime-session-1",
            "running": True,
            "status": "working",
            "event_sequence": 0,
            "snapshot_event_sequence": 0,
            "messages": [],
            "inflight": {
                "user": None,
                "assistant": None,
                "streaming": False,
                "error": None,
            },
            "replay_events": [],
        }


class _ObserverV2Projection:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def observer_snapshot(self, **scope: object):
        self.calls.append(scope)
        return {
            "observer_contract": 2,
            "profile": "default",
            "runtime_generation": "runtime-generation-v2",
            "session_id": str(SESSION_ID),
            "running": True,
            "status": "running",
            "event_sequence": 4,
            "snapshot_event_sequence": 4,
            "messages": [],
            "inflight": {
                "user": None,
                "assistant": None,
                "streaming": False,
                "error": None,
            },
            "todo_sections": [
                {
                    "turn_id": "turn-1",
                    "section_id": "todo-1",
                    "revision": 1,
                    "first_event_sequence": 1,
                    "status": "in_progress",
                    "items": [
                        {"id": "item-1", "label": "Run tests", "status": "in_progress"}
                    ],
                }
            ],
            "subagents": [],
            "tools": [],
            "terminals": [],
            "replay_events": [],
        }


class _CatalogRepository:
    def __init__(self) -> None:
        self.active = True

    def list_agent_sessions(self, **scope: object):
        projection = self.resolve_visible_session(
            **scope,
            session_id=SESSION_ID,
        )
        return ((projection,), 1) if projection is not None else ((), 0)

    def resolve_visible_session(self, **scope: object):
        if not self.active or scope["session_id"] != SESSION_ID:
            return None
        requested_agent = scope.get("agent_id")
        if requested_agent not in {None, AGENT_ID}:
            return None
        return CatalogSessionProjection(
            session_id=SESSION_ID,
            agent_id=AGENT_ID,
            workspace_id=WORKSPACE_ID,
            profile="default",
            session_key=SESSION_KEY,
            runtime_generation="runtime-generation-v2",
            surface="hermes-cli",
            authority_revision=1,
            available_actions=("prompt.submit",),
            active=self.active,
        )


class _ObserverSubscriptions:
    def __init__(self, projection: _DeferredObserverProjection) -> None:
        self.projection = projection
        self.opened: list[dict[str, object]] = []
        self.closed: list[dict[str, object]] = []
        self.ready_checks = 0

    def open_subscription(self, **call: object):
        self.opened.append(dict(call))
        return SimpleNamespace(
            subscription_id=UUID("82000000-0000-4000-8000-000000000001"),
            target_subscription_id=UUID("83000000-0000-4000-8000-000000000001"),
            session_key=SESSION_KEY,
            profile="default",
            requires_initial_snapshot=True,
        )

    def snapshot_ready(self, **_call: object) -> bool:
        self.ready_checks += 1
        if self.ready_checks < 2:
            return False
        self.projection.ready = True
        return True

    def renew_subscription(self, **_call: object) -> None:
        return None

    def close_subscription(self, **call: object) -> None:
        self.closed.append(dict(call))


def test_default_business_api_is_live_but_not_ready_without_runtime_config() -> None:
    application = create_app()

    with TestClient(application) as client:
        assert client.get("/live").status_code == 200
        assert client.get("/ready").status_code == 503
        response = client.get("/api/status")

    assert response.status_code == 200
    assert response.json() == {
        "gateway_running": True,
        "gateway_state": "degraded",
        "auth_required": False,
        "auth_providers": [],
        "auth_flows": [],
        "overall": "degraded",
    }
    assert application.snapshot()["component"] == "business-api"
    assert application.snapshot()["ready"] is False


def test_password_login_uses_argon2_and_sets_only_secure_contract_cookies() -> None:
    repository = _IdentityRepository()
    application = create_app(
        identity_repository=repository,
        tenant_resolver=_TenantResolver(),
        secret_resolver=_SecretResolver(),
        settings={
            "signing_secret_ref": "secret-manager/unit/cloud-p0-signing",
            "access_ttl_seconds": 300,
            "refresh_ttl_seconds": 3600,
            "ticket_ttl_seconds": 60,
        },
        now=lambda: NOW,
    )

    with TestClient(application, base_url="https://testserver") as client:
        response = client.post(
            "/auth/password-login",
            json={
                "provider": "basic",
                "username": "user@example.test",
                "password": "correct-password",
                "next": "",
            },
        )

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    cookies = response.headers.get_list("set-cookie")
    assert len(cookies) == 3
    assert all("Secure" in cookie for cookie in cookies)
    assert all("HttpOnly" in cookie for cookie in cookies)
    assert all("SameSite=strict" in cookie for cookie in cookies)
    assert any("hermes_session_at=" in cookie for cookie in cookies)
    assert any("hermes_session_rt=" in cookie for cookie in cookies)
    assert any("hermes_session_provider=basic" in cookie for cookie in cookies)
    access_token = response.cookies["hermes_session_at"]
    claims = jwt.decode(
        access_token,
        SIGNING_KEY,
        algorithms=["HS256"],
        options={"verify_exp": False},
    )
    assert claims["tenant_id"] == str(TENANT_ID)
    assert claims["user_id"] == str(USER_ID)
    assert claims["provider"] == "basic"
    assert claims["iat"] == int(NOW.timestamp())
    assert claims["nbf"] == int(NOW.timestamp())
    assert claims["exp"] == int(NOW.timestamp()) + 300
    assert len(repository.refresh_sessions) == 1
    assert SIGNING_KEY.decode() not in repr(application)


def test_owner_access_expiry_is_enforced_by_jwt_runtime() -> None:
    repository = _IdentityRepository()
    service = CloudApiService(
        identity_repository=repository,
        tenant_resolver=_TenantResolver(),
        secret_resolver=_SecretResolver(),
        settings=CloudApiSettings(
            signing_secret_ref="secret-manager/unit/cloud-p0-signing",
            access_ttl_seconds=300,
            refresh_ttl_seconds=3600,
            ticket_ttl_seconds=60,
        ),
        now=lambda: datetime(2020, 1, 1, tzinfo=UTC),
    )
    expired = jwt.encode(
        {
            "tenant_id": str(TENANT_ID),
            "user_id": str(USER_ID),
            "provider": "basic",
            "refresh_session_id": "12121212-1212-4212-8212-121212121212",
            "exp": 1_577_837_100,
        },
        SIGNING_KEY,
        algorithm="HS256",
    )

    with pytest.raises(AuthenticationFailed):
        service.authenticate_access(expired)


def test_refresh_rotates_opaque_digest_and_rejects_old_token_replay() -> None:
    repository = _IdentityRepository()
    application = create_app(
        identity_repository=repository,
        tenant_resolver=_TenantResolver(),
        secret_resolver=_SecretResolver(),
        settings={
            "signing_secret_ref": "secret-manager/unit/cloud-p0-signing",
            "access_ttl_seconds": 300,
            "refresh_ttl_seconds": 3600,
            "ticket_ttl_seconds": 60,
        },
        now=lambda: NOW,
    )

    with TestClient(application, base_url="https://testserver") as client:
        login = client.post(
            "/auth/password-login",
            json={
                "provider": "basic",
                "username": "user@example.test",
                "password": "correct-password",
                "next": "",
            },
        )
        old_refresh = login.cookies["hermes_session_rt"]
        rotated = client.post(
            "/auth/native/refresh",
            json={"refresh_token": old_refresh, "provider": "basic"},
        )
        replay = client.post(
            "/auth/native/refresh",
            json={"refresh_token": old_refresh, "provider": "basic"},
        )

    assert rotated.status_code == 200
    body = rotated.json()
    assert set(body) == {
        "access_token",
        "refresh_token",
        "token_type",
        "expires_at",
        "provider",
        "user_id",
    }
    assert body["refresh_token"] != old_refresh
    assert body["token_type"] == "Bearer"
    assert body["provider"] == "basic"
    assert body["user_id"] == str(USER_ID)
    assert body["expires_at"] == int(NOW.timestamp()) + 300
    claims = jwt.decode(
        body["access_token"],
        SIGNING_KEY,
        algorithms=["HS256"],
        options={"verify_exp": False},
    )
    assert claims["tenant_id"] == str(TENANT_ID)
    assert claims["user_id"] == str(USER_ID)
    assert next(iter(repository.refresh_sessions.values())).rotation == 1
    assert old_refresh not in repr(repository.refresh_sessions)
    assert body["refresh_token"] not in repr(repository.refresh_sessions)
    assert replay.status_code == 401
    assert old_refresh not in replay.text


def test_bearer_acl_serves_exact_session_list_detail_and_transcript_shapes() -> None:
    identity = _IdentityRepository()
    projections = _ProjectionRepository()
    application = create_app(
        identity_repository=identity,
        projection_repository=projections,
        session_catalog_repository=_CatalogRepository(),
        tenant_resolver=_TenantResolver(),
        secret_resolver=_SecretResolver(),
        settings={
            "signing_secret_ref": "secret-manager/unit/cloud-p0-signing",
            "access_ttl_seconds": 300,
            "refresh_ttl_seconds": 3600,
            "ticket_ttl_seconds": 60,
        },
        now=lambda: NOW,
    )

    with TestClient(application, base_url="https://testserver") as client:
        unauthenticated = client.get("/api/sessions")
        login = client.post(
            "/auth/password-login",
            json={
                "provider": "basic",
                "username": "user@example.test",
                "password": "correct-password",
                "next": "",
            },
        )
        authorization = {
            "Authorization": f"Bearer {login.cookies['hermes_session_at']}"
        }
        page = client.get(
            "/api/sessions",
            params={
                "limit": 20,
                "offset": 0,
                "min_messages": 1,
                "archived": "exclude",
                "order": "recent",
                "profile": "default",
            },
            headers=authorization,
        )
        detail = client.get(
            f"/api/v1/agents/{AGENT_ID}/sessions/{SESSION_ID}",
            params={"profile": "default"},
            headers=authorization,
        )
        transcript = client.get(
            f"/api/v1/agents/{AGENT_ID}/sessions/{SESSION_ID}/messages",
            params={"limit": 200, "offset": 0, "profile": "default"},
            headers=authorization,
        )

    assert unauthenticated.status_code == 401
    expected_session = {
        "id": str(SESSION_ID),
        "agent_id": str(AGENT_ID),
        "workspace_id": str(WORKSPACE_ID),
        "_lineage_root_id": str(SESSION_ID),
        "parent_session_id": None,
        "title": "Contract session",
        "preview": None,
        "source": None,
        "model": None,
        "profile": "default",
        "cwd": None,
        "git_branch": None,
        "started_at": (NOW - timedelta(minutes=5)).timestamp(),
        "ended_at": None,
        "last_active": NOW.timestamp(),
        "message_count": 1,
        "tool_call_count": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "is_active": True,
        "archived": False,
    }
    assert page.status_code == 200
    assert page.json() == {
        "sessions": [expected_session],
        "total": 1,
        "limit": 20,
        "offset": 0,
    }
    assert detail.status_code == 200
    assert detail.json() == {
        **expected_session,
        "workspace_id": None,
        "title": None,
        "started_at": None,
        "last_active": None,
        "message_count": 0,
        "directory_source": "host_catalog",
        "availability": "live",
        "runtime_generation": "runtime-generation-v2",
        "surface": "hermes-cli",
        "authority_revision": 1,
        "available_actions": ["prompt.submit"],
        "transcript_available": False,
    }
    assert transcript.status_code == 200
    assert transcript.json() == {
        "session_id": str(SESSION_ID),
        "agent_id": str(AGENT_ID),
        "workspace_id": str(WORKSPACE_ID),
        "messages": [
            {
                "id": 1,
                "role": "assistant",
                "content": {"text": "Projected response"},
                "timestamp": NOW.timestamp(),
                "reasoning": None,
                "reasoning_content": None,
                "reasoning_details": None,
                "tool_call_id": None,
                "tool_calls": None,
                "tool_name": None,
                "display_kind": None,
                "display_metadata": None,
            }
        ],
        "pagination": {"limit": 200, "offset": 0, "returned": 1},
    }
    assert projections.calls == [
        ("list", TENANT_ID, USER_ID),
        ("detail", TENANT_ID, USER_ID),
        ("messages", TENANT_ID, USER_ID),
    ]


def test_public_session_detail_rejects_host_identity_and_enumeration_scopes() -> None:
    identity = _IdentityRepository()
    application = create_app(
        identity_repository=identity,
        projection_repository=_ProjectionRepository(),
        session_catalog_repository=_CatalogRepository(),
        tenant_resolver=_TenantResolver(),
        secret_resolver=_SecretResolver(),
        settings={
            "signing_secret_ref": "secret-manager/unit/cloud-p0-signing",
            "access_ttl_seconds": 300,
            "refresh_ttl_seconds": 3600,
            "ticket_ttl_seconds": 60,
        },
        now=lambda: NOW,
    )
    with TestClient(application, base_url="https://testserver") as client:
        login = client.post(
            "/auth/password-login",
            json={
                "provider": "basic",
                "username": "user@example.test",
                "password": "correct-password",
                "next": "",
            },
        )
        headers = {
            "Authorization": f"Bearer {login.cookies['hermes_session_at']}"
        }
        host_identity = client.get(
            f"/api/sessions/{SESSION_KEY}",
            params={"agent_id": str(AGENT_ID)},
            headers=headers,
        )
        compatible_stable = client.get(
            f"/api/sessions/{SESSION_ID}",
            params={"agent_id": str(AGENT_ID)},
            headers=headers,
        )
        unknown_stable = client.get(
            "/api/sessions/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            params={"agent_id": str(AGENT_ID)},
            headers=headers,
        )
        wrong_agent = client.get(
            "/api/v1/agents/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa/"
            f"sessions/{SESSION_ID}",
            headers=headers,
        )
        duplicate_scope = client.get(
            f"/api/v1/agents/{AGENT_ID}/sessions/{SESSION_ID}",
            params={"agent_id": str(AGENT_ID)},
            headers=headers,
        )

    assert host_identity.status_code == 400
    assert compatible_stable.status_code == 200
    assert compatible_stable.json()["id"] == str(SESSION_ID)
    assert unknown_stable.status_code == 404
    assert unknown_stable.json() == wrong_agent.json() == {
        "code": "SESSION_NOT_FOUND",
        "reason": "session not found",
    }
    assert wrong_agent.status_code == 404
    assert duplicate_scope.status_code == 400


def test_session_list_uses_stable_ambiguous_scope_contract() -> None:
    class AmbiguousProjectionRepository(_ProjectionRepository):
        def list_sessions(self, **scope: object):
            if scope.get("agent_id") == AGENT_ID and scope.get("profile") == "default":
                return (self.session,), 1
            raise ProjectionScopeAmbiguous

    identity = _IdentityRepository()
    application = create_app(
        identity_repository=identity,
        projection_repository=AmbiguousProjectionRepository(),
        tenant_resolver=_TenantResolver(),
        secret_resolver=_SecretResolver(),
        settings={
            "signing_secret_ref": "secret-manager/unit/cloud-p0-signing",
            "access_ttl_seconds": 300,
            "refresh_ttl_seconds": 3600,
            "ticket_ttl_seconds": 60,
        },
        now=lambda: NOW,
    )

    with TestClient(application, base_url="https://testserver") as client:
        login = client.post(
            "/auth/password-login",
            json={
                "provider": "basic",
                "username": "user@example.test",
                "password": "correct-password",
                "next": "",
            },
        )
        authorization = {
            "Authorization": f"Bearer {login.cookies['hermes_session_at']}"
        }
        ambiguous = client.get("/api/sessions", headers=authorization)
        exact = client.get(
            "/api/sessions",
            params={"agent_id": str(AGENT_ID), "profile": "default"},
            headers=authorization,
        )

    assert ambiguous.status_code == 409
    assert ambiguous.json() == {
        "code": "SESSION_SCOPE_AMBIGUOUS",
        "reason": "session scope is ambiguous",
    }
    assert exact.status_code == 200
    assert exact.json()["total"] == 1


def test_agent_list_is_authenticated_and_workspace_filtered() -> None:
    identity = _IdentityRepository()
    application = create_app(
        identity_repository=identity,
        projection_repository=_AgentProjectionRepository(),
        tenant_resolver=_TenantResolver(),
        secret_resolver=_SecretResolver(),
        settings={
            "signing_secret_ref": "secret-manager/unit/cloud-p0-signing",
            "access_ttl_seconds": 300,
            "refresh_ttl_seconds": 3600,
            "ticket_ttl_seconds": 60,
        },
        now=lambda: NOW,
    )

    with TestClient(application, base_url="https://testserver") as client:
        assert client.get("/api/agents").status_code == 401
        login = client.post(
            "/auth/password-login",
            json={
                "provider": "basic",
                "username": "user@example.test",
                "password": "correct-password",
                "next": "",
            },
        )
        response = client.get(
            "/api/agents",
            params={"workspace_id": str(WORKSPACE_ID)},
            headers={"Authorization": f"Bearer {login.cookies['hermes_session_at']}"},
        )

    assert response.status_code == 200
    assert response.json() == {
        "agents": [
            {
                "agent_id": str(AGENT_ID),
                "workspace_id": str(WORKSPACE_ID),
                "agent_key": "agent-a",
                "status": "active",
                "last_seen_at": NOW.isoformat(),
            }
        ]
    }


def test_browser_cookie_reads_agent_and_session_catalogs_with_fetch_metadata() -> None:
    identity = _IdentityRepository()
    projections = _AgentProjectionRepository()
    application = create_app(
        identity_repository=identity,
        projection_repository=projections,
        session_catalog_repository=projections,
        tenant_resolver=_TenantResolver(),
        secret_resolver=_SecretResolver(),
        settings={
            "signing_secret_ref": "secret-manager/unit/cloud-p0-signing",
            "access_ttl_seconds": 300,
            "refresh_ttl_seconds": 3600,
            "ticket_ttl_seconds": 60,
        },
        now=lambda: NOW,
    )
    browser_headers = {
        "Accept": "application/json",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
    }

    with TestClient(application, base_url="https://testserver") as client:
        assert client.post(
            "/auth/password-login",
            json={
                "provider": "basic",
                "username": "user@example.test",
                "password": "correct-password",
                "next": "",
            },
        ).status_code == 200
        agents = client.get("/api/v1/agents", headers=browser_headers)
        sessions = client.get(
            f"/api/v1/agents/{AGENT_ID}/sessions",
            params={
                "profile": "default",
                    "min_messages": 0,
                "archived": "exclude",
                "order": "recent",
                "limit": 20,
                "offset": 0,
            },
            headers=browser_headers,
        )
        compatible_agents = client.get("/api/agents", headers=browser_headers)
        compatible_sessions = client.get(
            "/api/sessions",
            params={
                "agent_id": str(AGENT_ID),
                "profile": "default",
                "min_messages": 1,
                "archived": "exclude",
                "order": "recent",
                "limit": 20,
                "offset": 0,
            },
            headers=browser_headers,
        )
        invalid_path_scope = client.get(
            "/api/v1/agents/not-a-uuid/sessions",
            headers=browser_headers,
        )
        duplicate_scope = client.get(
            f"/api/v1/agents/{AGENT_ID}/sessions",
            params={"agent_id": str(AGENT_ID)},
            headers=browser_headers,
        )

    assert agents.status_code == 200
    assert set(agents.json()) == {"agents"}
    assert sessions.status_code == 200
    assert sessions.json()["total"] == 1
    assert compatible_agents.json() == agents.json()
    assert compatible_sessions.json()["total"] == 1
    assert sessions.json()["sessions"][0]["directory_source"] == "host_catalog"
    assert sessions.json()["sessions"][0]["transcript_available"] is False
    assert invalid_path_scope.status_code == 400
    assert duplicate_scope.status_code == 400


@pytest.mark.parametrize(
    ("path", "params"),
    [
        (f"/api/v1/agents/{AGENT_ID}/sessions", [("unknown", "1")]),
        (f"/api/v1/agents/{AGENT_ID}/sessions", [("agent_id", str(AGENT_ID))]),
        *[
            (
                f"/api/v1/agents/{AGENT_ID}/sessions",
                [(name, value), (name, value)],
            )
            for name, value in (
                ("limit", "20"),
                ("offset", "0"),
                ("min_messages", "1"),
                ("archived", "exclude"),
                ("order", "recent"),
                ("profile", "default"),
            )
        ],
        ("/api/sessions", [("unknown", "1")]),
        ("/api/sessions", [("agent_id", str(AGENT_ID)), ("agent_id", str(AGENT_ID))]),
        ("/api/sessions", [("agent_id", "not-a-uuid")]),
        (
            "/api/sessions",
            [("agent_id", "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA")],
        ),
        *[
            ("/api/sessions", [(name, value), (name, value)])
            for name, value in (
                ("limit", "20"),
                ("offset", "0"),
                ("min_messages", "1"),
                ("archived", "exclude"),
                ("order", "recent"),
                ("profile", "default"),
            )
        ],
    ],
)
def test_session_list_routes_reject_unknown_duplicate_and_noncanonical_query(
    path: str,
    params: list[tuple[str, str]],
) -> None:
    identity = _IdentityRepository()
    client, authorization = _authenticated_control_client(
        identity,
        _AgentProjectionRepository(),
    )
    try:
        response = client.get(path, params=params, headers=authorization)
    finally:
        client.close()

    assert response.status_code == 400


def test_browser_catalog_cookie_accepts_only_configured_loopback_https_forwarding() -> None:
    identity = _IdentityRepository()
    settings = {
        "signing_secret_ref": "secret-manager/unit/cloud-p0-signing",
        "access_ttl_seconds": 300,
        "refresh_ttl_seconds": 3600,
        "ticket_ttl_seconds": 60,
        "trusted_forwarded_proxy_hosts": ("127.0.0.1", "::1"),
    }
    application = create_app(
        identity_repository=identity,
        projection_repository=_AgentProjectionRepository(),
        tenant_resolver=_TenantResolver(),
        secret_resolver=_SecretResolver(),
        settings=settings,
        now=lambda: NOW,
    )
    remote_application = create_app(
        identity_repository=identity,
        projection_repository=_AgentProjectionRepository(),
        tenant_resolver=_TenantResolver(),
        secret_resolver=_SecretResolver(),
        settings=settings,
        now=lambda: NOW,
    )
    origin_headers = {
        "Accept": "application/json",
        "Origin": "https://testserver",
        "X-Forwarded-Proto": "https",
    }

    with TestClient(
        remote_application,
        base_url="http://testserver",
        client=("127.0.0.1", 50000),
    ) as loopback:
        login = loopback.post(
            "/auth/password-login",
            json={
                "provider": "basic",
                "username": "user@example.test",
                "password": "correct-password",
                "next": "",
            },
        )
        cookie = f"hermes_session_at={login.cookies['hermes_session_at']}"
        accepted = loopback.get(
            "/api/agents",
            headers={**origin_headers, "Cookie": cookie},
        )
    with TestClient(
        application,
        base_url="http://testserver",
        client=("203.0.113.9", 50000),
    ) as remote:
        rejected = remote.get(
            "/api/agents",
            headers={**origin_headers, "Cookie": cookie},
        )

    assert accepted.status_code == 200
    assert rejected.status_code == 403


def test_browser_ticket_cookie_accepts_https_forwarding_only_from_trusted_proxy() -> None:
    identity = _IdentityRepository()
    settings = {
        "signing_secret_ref": "secret-manager/unit/cloud-p0-signing",
        "access_ttl_seconds": 300,
        "refresh_ttl_seconds": 3600,
        "ticket_ttl_seconds": 60,
        "trusted_forwarded_proxy_hosts": ("127.0.0.1", "::1"),
    }
    applications = [
        create_app(
            identity_repository=identity,
            projection_repository=_AgentProjectionRepository(),
            tenant_resolver=_TenantResolver(),
            secret_resolver=_SecretResolver(),
            settings=settings,
            now=lambda: NOW,
        )
        for _ in range(2)
    ]
    headers = {
        "Origin": "https://testserver",
        "X-Forwarded-Proto": "https",
    }
    body = {
        "connection_role": "observer",
        "client_instance_id": "88888888-8888-4888-8888-888888888888",
        "observer_contract": 2,
        "agent_id": str(AGENT_ID),
    }

    with TestClient(
        applications[0],
        base_url="http://testserver",
        client=("127.0.0.1", 50000),
    ) as loopback:
        login = loopback.post(
            "/auth/password-login",
            json={
                "provider": "basic",
                "username": "user@example.test",
                "password": "correct-password",
                "next": "",
            },
        )
        cookie = f"hermes_session_at={login.cookies['hermes_session_at']}"
        accepted = loopback.post(
            "/api/auth/ws-ticket",
            json=body,
            headers={**headers, "Cookie": cookie},
        )
    with TestClient(
        applications[1],
        base_url="http://testserver",
        client=("203.0.113.9", 50000),
    ) as remote:
        rejected = remote.post(
            "/api/auth/ws-ticket",
            json=body,
            headers={**headers, "Cookie": cookie},
        )

    assert accepted.status_code == 200
    assert accepted.json()["observer_contract"] == 2
    assert rejected.status_code == 403


def test_revoked_server_session_invalidates_access_catalog_ticket_and_refresh() -> None:
    identity = _IdentityRepository()
    application = create_app(
        identity_repository=identity,
        projection_repository=_AgentProjectionRepository(),
        tenant_resolver=_TenantResolver(),
        secret_resolver=_SecretResolver(),
        settings={
            "signing_secret_ref": "secret-manager/unit/cloud-p0-signing",
            "access_ttl_seconds": 300,
            "refresh_ttl_seconds": 3600,
            "ticket_ttl_seconds": 60,
        },
        now=lambda: NOW,
    )

    with TestClient(application, base_url="https://testserver") as client:
        login = client.post(
            "/auth/password-login",
            json={
                "provider": "basic",
                "username": "user@example.test",
                "password": "correct-password",
                "next": "",
            },
        )
        access = login.cookies["hermes_session_at"]
        refresh = login.cookies["hermes_session_rt"]
        refresh_session = next(iter(identity.refresh_sessions.values()))
        identity.revoke_refresh_session(
            tenant_id=TENANT_ID,
            refresh_session_id=refresh_session.refresh_session_id,
            now=NOW,
        )
        catalog = client.get(
            "/api/agents",
            headers={
                "Sec-Fetch-Site": "same-origin",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Dest": "empty",
            },
        )
        ticket = client.post(
            "/api/auth/ws-ticket",
            json={},
            headers={"Origin": "https://testserver"},
        )
        native = client.get(
            "/api/agents",
            headers={"Authorization": f"Bearer {access}", "Cookie": ""},
        )
        rotated = client.post(
            "/auth/native/refresh",
            json={"refresh_token": refresh, "provider": "basic"},
        )

    assert catalog.status_code == 401
    assert ticket.status_code == 401
    assert native.status_code == 401
    assert rotated.status_code == 401


def test_browser_logout_revokes_session_clears_cookies_and_is_idempotent() -> None:
    identity = _IdentityRepository()
    application = create_app(
        identity_repository=identity,
        projection_repository=_AgentProjectionRepository(),
        tenant_resolver=_TenantResolver(),
        secret_resolver=_SecretResolver(),
        settings={
            "signing_secret_ref": "secret-manager/unit/cloud-p0-signing",
            "access_ttl_seconds": 300,
            "refresh_ttl_seconds": 3600,
            "ticket_ttl_seconds": 60,
        },
        now=lambda: NOW,
    )
    same_origin = {
        "Accept": "application/json",
        "Origin": "https://testserver",
    }

    with TestClient(application, base_url="https://testserver") as client:
        login = client.post(
            "/auth/password-login",
            json={
                "provider": "basic",
                "username": "user@example.test",
                "password": "correct-password",
                "next": "",
            },
        )
        access = login.cookies["hermes_session_at"]
        refresh = login.cookies["hermes_session_rt"]
        old_cookie = (
            f"hermes_session_at={access}; "
            f"hermes_session_rt={refresh}; hermes_session_provider=basic"
        )
        logout = client.post("/auth/logout", headers=same_origin)
        repeated = client.post(
            "/auth/logout",
            headers={**same_origin, "Cookie": old_cookie},
        )
        already_absent = client.post(
            "/auth/logout",
            headers={**same_origin, "Cookie": ""},
        )
        catalog = client.get(
            "/api/agents",
            headers={
                "Cookie": f"hermes_session_at={access}",
                "Sec-Fetch-Site": "same-origin",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Dest": "empty",
            },
        )
        ticket = client.post(
            "/api/auth/ws-ticket",
            json={},
            headers={
                "Cookie": f"hermes_session_at={access}",
                "Origin": "https://testserver",
            },
        )
        rotated = client.post(
            "/auth/native/refresh",
            json={"refresh_token": refresh, "provider": "basic"},
        )

    assert logout.status_code == 200
    assert logout.json() == {"ok": True}
    cleared = logout.headers.get_list("set-cookie")
    assert len(cleared) == 3
    assert all("Max-Age=0" in cookie for cookie in cleared)
    assert all("expires=" in cookie.casefold() for cookie in cleared)
    assert all("Secure" in cookie for cookie in cleared)
    assert all("HttpOnly" in cookie for cookie in cleared)
    assert all("SameSite=strict" in cookie for cookie in cleared)
    assert all("Path=/" in cookie for cookie in cleared)
    assert any("hermes_session_at=" in cookie for cookie in cleared)
    assert any("hermes_session_rt=" in cookie for cookie in cleared)
    assert any("hermes_session_provider=" in cookie for cookie in cleared)
    stored = next(iter(identity.refresh_sessions.values()))
    assert stored.revoked_at == NOW
    assert repeated.status_code == 200
    assert repeated.json() == {"ok": True}
    assert already_absent.status_code == 200
    assert already_absent.json() == {"ok": True}
    assert catalog.status_code == 401
    assert ticket.status_code == 401
    assert rotated.status_code == 401


def test_browser_logout_rejects_untrusted_or_nonempty_requests_without_revocation() -> None:
    identity = _IdentityRepository()
    application = create_app(
        identity_repository=identity,
        projection_repository=_AgentProjectionRepository(),
        tenant_resolver=_TenantResolver(),
        secret_resolver=_SecretResolver(),
        settings={
            "signing_secret_ref": "secret-manager/unit/cloud-p0-signing",
            "access_ttl_seconds": 300,
            "refresh_ttl_seconds": 3600,
            "ticket_ttl_seconds": 60,
        },
        now=lambda: NOW,
    )

    with TestClient(application, base_url="https://testserver") as client:
        login = client.post(
            "/auth/password-login",
            json={
                "provider": "basic",
                "username": "user@example.test",
                "password": "correct-password",
                "next": "",
            },
        )
        without_origin = client.post("/auth/logout")
        fetch_metadata_only = client.post(
            "/auth/logout",
            headers={
                "Sec-Fetch-Site": "same-origin",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Dest": "empty",
            },
        )
        cross_origin = client.post(
            "/auth/logout",
            headers={"Origin": "https://attacker.example"},
        )
        nonempty = client.post(
            "/auth/logout",
            content=b"{}",
            headers={"Origin": "https://testserver"},
        )

    assert login.status_code == 200
    assert without_origin.status_code == 403
    assert fetch_metadata_only.status_code == 403
    assert cross_origin.status_code == 403
    assert nonempty.status_code == 400
    assert nonempty.json() == {
        "code": "INVALID_REQUEST",
        "reason": "empty request body required",
    }
    assert next(iter(identity.refresh_sessions.values())).revoked_at is None


def test_browser_logout_rejects_mismatched_credentials_without_revocation() -> None:
    identity = _IdentityRepository()
    application = create_app(
        identity_repository=identity,
        tenant_resolver=_TenantResolver(),
        secret_resolver=_SecretResolver(),
        settings={
            "signing_secret_ref": "secret-manager/unit/cloud-p0-signing",
            "access_ttl_seconds": 300,
            "refresh_ttl_seconds": 3600,
            "ticket_ttl_seconds": 60,
        },
        now=lambda: NOW,
    )
    login_body = {
        "provider": "basic",
        "username": "user@example.test",
        "password": "correct-password",
        "next": "",
    }

    with TestClient(application, base_url="https://testserver") as client:
        first = client.post("/auth/password-login", json=login_body)
        second = client.post("/auth/password-login", json=login_body)
        response = client.post(
            "/auth/logout",
            headers={
                "Origin": "https://testserver",
                "Cookie": (
                    f"hermes_session_at={first.cookies['hermes_session_at']}; "
                    f"hermes_session_rt={second.cookies['hermes_session_rt']}"
                ),
            },
        )

    assert response.status_code == 401
    assert response.json() == {
        "code": "AUTHENTICATION_FAILED",
        "reason": "authentication failed",
    }
    assert response.headers.get_list("set-cookie") == []
    assert len(identity.refresh_sessions) == 2
    assert all(value.revoked_at is None for value in identity.refresh_sessions.values())


def test_browser_logout_database_failure_keeps_session_and_cookies_active() -> None:
    class FailingRevokeRepository(_IdentityRepository):
        def revoke_refresh_session(self, **_values: object) -> RefreshSession:
            raise IdentityRepositoryFailure("database unavailable")

    identity = FailingRevokeRepository()
    application = create_app(
        identity_repository=identity,
        projection_repository=_AgentProjectionRepository(),
        tenant_resolver=_TenantResolver(),
        secret_resolver=_SecretResolver(),
        settings={
            "signing_secret_ref": "secret-manager/unit/cloud-p0-signing",
            "access_ttl_seconds": 300,
            "refresh_ttl_seconds": 3600,
            "ticket_ttl_seconds": 60,
        },
        now=lambda: NOW,
    )

    with TestClient(application, base_url="https://testserver") as client:
        login = client.post(
            "/auth/password-login",
            json={
                "provider": "basic",
                "username": "user@example.test",
                "password": "correct-password",
                "next": "",
            },
        )
        access = login.cookies["hermes_session_at"]
        response = client.post(
            "/auth/logout",
            headers={"Origin": "https://testserver"},
        )
        catalog = client.get(
            "/api/agents",
            headers={
                "Cookie": f"hermes_session_at={access}",
                "Sec-Fetch-Site": "same-origin",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Dest": "empty",
            },
        )

    assert response.status_code == 503
    assert response.json() == {"code": "LOGOUT_FAILED", "reason": "logout failed"}
    assert response.headers.get_list("set-cookie") == []
    assert next(iter(identity.refresh_sessions.values())).revoked_at is None
    assert catalog.status_code == 200


def test_browser_catalog_cookie_reads_stable_detail_and_transcript_routes() -> None:
    identity = _IdentityRepository()
    application = create_app(
        identity_repository=identity,
        projection_repository=_ProjectionRepository(),
        session_catalog_repository=_CatalogRepository(),
        tenant_resolver=_TenantResolver(),
        secret_resolver=_SecretResolver(),
        settings={
            "signing_secret_ref": "secret-manager/unit/cloud-p0-signing",
            "access_ttl_seconds": 300,
            "refresh_ttl_seconds": 3600,
            "ticket_ttl_seconds": 60,
        },
        now=lambda: NOW,
    )
    browser_headers = {
        "Accept": "application/json",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
    }

    with TestClient(application, base_url="https://testserver") as client:
        assert client.post(
            "/auth/password-login",
            json={
                "provider": "basic",
                "username": "user@example.test",
                "password": "correct-password",
                "next": "",
            },
        ).status_code == 200
        detail = client.get(
            f"/api/v1/agents/{AGENT_ID}/sessions/{SESSION_ID}",
            headers=browser_headers,
        )
        messages = client.get(
            f"/api/v1/agents/{AGENT_ID}/sessions/{SESSION_ID}/messages",
            headers=browser_headers,
        )

    assert detail.status_code == 200
    assert detail.json()["id"] == str(SESSION_ID)
    assert messages.status_code == 200
    assert messages.json()["session_id"] == str(SESSION_ID)


def test_browser_catalog_rejects_untrusted_metadata_and_dual_credential_mismatch() -> None:
    identity = _IdentityRepository()
    application = create_app(
        identity_repository=identity,
        projection_repository=_AgentProjectionRepository(),
        tenant_resolver=_TenantResolver(),
        secret_resolver=_SecretResolver(),
        settings={
            "signing_secret_ref": "secret-manager/unit/cloud-p0-signing",
            "access_ttl_seconds": 300,
            "refresh_ttl_seconds": 3600,
            "ticket_ttl_seconds": 60,
        },
        now=lambda: NOW,
    )
    login_body = {
        "provider": "basic",
        "username": "user@example.test",
        "password": "correct-password",
        "next": "",
    }

    with TestClient(application, base_url="https://testserver") as client:
        first = client.post("/auth/password-login", json=login_body)
        second = client.post("/auth/password-login", json=login_body)
        first_access = first.cookies["hermes_session_at"]
        second_access = second.cookies["hermes_session_at"]
        missing_metadata = client.get(
            "/api/agents",
            headers={"Cookie": f"hermes_session_at={second_access}"},
        )
        cross_site = client.get(
            "/api/agents",
            headers={
                "Cookie": f"hermes_session_at={second_access}",
                "Sec-Fetch-Site": "cross-site",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Dest": "empty",
            },
        )
        malformed_origin = client.get(
            "/api/agents",
            headers={
                "Cookie": f"hermes_session_at={second_access}",
                "Origin": "https://user@testserver",
            },
        )
        mismatched = client.get(
            "/api/agents",
            headers={
                "Authorization": f"Bearer {first_access}",
                "Cookie": f"hermes_session_at={second_access}",
            },
        )
        matched = client.get(
            "/api/agents",
            headers={
                "Authorization": f"Bearer {first_access}",
                "Cookie": f"hermes_session_at={first_access}",
            },
        )

    assert missing_metadata.status_code == 403
    assert cross_site.status_code == 403
    assert malformed_origin.status_code == 403
    assert mismatched.status_code == 401
    assert matched.status_code == 200


def test_browser_logout_honors_https_forwarding_only_from_configured_loopback() -> None:
    identity = _IdentityRepository()
    settings = {
        "signing_secret_ref": "secret-manager/unit/cloud-p0-signing",
        "access_ttl_seconds": 300,
        "refresh_ttl_seconds": 3600,
        "ticket_ttl_seconds": 60,
        "trusted_forwarded_proxy_hosts": ("127.0.0.1", "::1"),
    }
    applications = [
        create_app(
            identity_repository=identity,
            tenant_resolver=_TenantResolver(),
            secret_resolver=_SecretResolver(),
            settings=settings,
            now=lambda: NOW,
        )
        for _ in range(3)
    ]
    forwarded_headers = {
        "Origin": "https://testserver",
        "X-Forwarded-Proto": "https",
    }

    with TestClient(
        applications[0],
        base_url="http://testserver",
        client=("127.0.0.1", 50000),
    ) as login_client:
        login = login_client.post(
            "/auth/password-login",
            json={
                "provider": "basic",
                "username": "user@example.test",
                "password": "correct-password",
                "next": "",
            },
        )
        cookie = (
            f"hermes_session_at={login.cookies['hermes_session_at']}; "
            f"hermes_session_rt={login.cookies['hermes_session_rt']}"
        )
    with TestClient(
        applications[1],
        base_url="http://testserver",
        client=("203.0.113.9", 50000),
    ) as remote:
        spoofed = remote.post(
            "/auth/logout",
            headers={**forwarded_headers, "Cookie": cookie},
        )
    with TestClient(
        applications[2],
        base_url="http://testserver",
        client=("127.0.0.1", 50000),
    ) as loopback:
        duplicated = loopback.post(
            "/auth/logout",
            headers=[
                ("Origin", "https://testserver"),
                ("X-Forwarded-Proto", "https"),
                ("X-Forwarded-Proto", "https"),
                ("Cookie", cookie),
            ],
        )
        accepted = loopback.post(
            "/auth/logout",
            headers={**forwarded_headers, "Cookie": cookie},
        )

    assert spoofed.status_code == 403
    assert duplicated.status_code == 403
    assert accepted.status_code == 200
    assert next(iter(identity.refresh_sessions.values())).revoked_at == NOW


@pytest.mark.parametrize(
    ("path", "params"),
    [
        ("/api/sessions", {"profile": ""}),
        ("/api/sessions", {"profile": "p" * 129}),
        (f"/api/sessions/{'s' * 257}", {}),
        (f"/api/sessions/{'s' * 257}/messages", {}),
        (f"/api/sessions/{SESSION_KEY}", {"profile": ""}),
        (f"/api/sessions/{SESSION_KEY}/messages", {"profile": "p" * 129}),
        (
            "/api/sessions",
            {"agent_id": "00000000-0000-0000-0000-000000000000"},
        ),
        (
            f"/api/sessions/{SESSION_KEY}",
            {"agent_id": "77777777-7777-7777-8777-777777777777"},
        ),
    ],
)
def test_session_routes_reject_profile_and_path_bounds(
    path: str,
    params: dict[str, str],
) -> None:
    identity = _IdentityRepository()
    projections = _ProjectionRepository()
    application = create_app(
        identity_repository=identity,
        projection_repository=projections,
        tenant_resolver=_TenantResolver(),
        secret_resolver=_SecretResolver(),
        settings={
            "signing_secret_ref": "secret-manager/unit/cloud-p0-signing",
            "access_ttl_seconds": 300,
            "refresh_ttl_seconds": 3600,
            "ticket_ttl_seconds": 60,
        },
        now=lambda: NOW,
    )

    with TestClient(application, base_url="https://testserver") as client:
        login = client.post(
            "/auth/password-login",
            json={
                "provider": "basic",
                "username": "user@example.test",
                "password": "correct-password",
                "next": "",
            },
        )
        response = client.get(
            path,
            params=params,
            headers={"Authorization": f"Bearer {login.cookies['hermes_session_at']}"},
        )

    assert response.status_code == 400
    assert response.json() == {
        "code": "INVALID_REQUEST",
        "reason": "request parameters are invalid",
    }


def test_ws_ticket_is_observer_only_opaque_and_never_stored_in_plaintext() -> None:
    identity = _IdentityRepository()
    application = create_app(
        identity_repository=identity,
        tenant_resolver=_TenantResolver(),
        secret_resolver=_SecretResolver(),
        settings={
            "signing_secret_ref": "secret-manager/unit/cloud-p0-signing",
            "access_ttl_seconds": 300,
            "refresh_ttl_seconds": 3600,
            "ticket_ttl_seconds": 60,
        },
        now=lambda: NOW,
    )

    with TestClient(application, base_url="https://testserver") as client:
        login = client.post(
            "/auth/password-login",
            json={
                "provider": "basic",
                "username": "user@example.test",
                "password": "correct-password",
                "next": "",
            },
        )
        response = client.post(
            "/api/auth/ws-ticket",
            json={},
            headers={"Authorization": f"Bearer {login.cookies['hermes_session_at']}"},
        )

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"ticket", "ttl_seconds", "connection_role"}
    assert 32 <= len(body["ticket"]) <= 4096
    assert body["ttl_seconds"] == 60
    assert body["connection_role"] == "observer"
    assert len(identity.websocket_tickets) == 1
    stored = next(iter(identity.websocket_tickets.values()))
    assert stored.principal_type == "user"
    assert stored.principal_id == USER_ID
    assert stored.session_id is None
    assert stored.observer_scope == ("session.observe",)
    assert stored.expires_at == NOW + timedelta(seconds=60)
    assert body["ticket"] not in repr(identity.websocket_tickets)


def test_scoped_observer_ticket_accepts_role_and_client_without_session_target() -> (
    None
):
    identity = _IdentityRepository()
    application = create_app(
        identity_repository=identity,
        tenant_resolver=_TenantResolver(),
        secret_resolver=_SecretResolver(),
        settings={
            "signing_secret_ref": "secret-manager/unit/cloud-p0-signing",
            "access_ttl_seconds": 300,
            "refresh_ttl_seconds": 3600,
            "ticket_ttl_seconds": 60,
        },
        now=lambda: NOW,
    )

    with TestClient(application, base_url="https://testserver") as client:
        login = client.post(
            "/auth/password-login",
            json={
                "provider": "basic",
                "username": "user@example.test",
                "password": "correct-password",
                "next": "",
            },
        )
        response = client.post(
            "/api/auth/ws-ticket",
            json={
                "connection_role": "observer",
                "client_instance_id": "33333333-3333-4333-8333-333333333333",
            },
            headers={"Authorization": f"Bearer {login.cookies['hermes_session_at']}"},
        )

    assert response.status_code == 200
    assert response.json()["connection_role"] == "observer"
    stored = next(iter(identity.websocket_tickets.values()))
    assert stored.session_id is None
    assert stored.observer_scope == (
        "session.observe",
        "client_instance_id=33333333-3333-4333-8333-333333333333",
    )


def test_observer_ticket_preserves_optional_agent_claim_through_consumption() -> None:
    identity = _IdentityRepository()
    application = create_app(
        identity_repository=identity,
        projection_repository=_ProjectionRepository(),
        tenant_resolver=_TenantResolver(),
        secret_resolver=_SecretResolver(),
        settings={
            "signing_secret_ref": "secret-manager/unit/cloud-p0-signing",
            "access_ttl_seconds": 300,
            "refresh_ttl_seconds": 3600,
            "ticket_ttl_seconds": 60,
        },
        now=lambda: NOW,
    )

    with TestClient(application, base_url="https://testserver") as client:
        login = client.post(
            "/auth/password-login",
            json={
                "provider": "basic",
                "username": "user@example.test",
                "password": "correct-password",
                "next": "",
            },
        )
        response = client.post(
            "/api/auth/ws-ticket",
            json={
                "connection_role": "observer",
                "client_instance_id": "33333333-3333-4333-8333-333333333333",
                "observer_contract": 2,
                "agent_id": str(AGENT_ID),
            },
            headers={"Authorization": f"Bearer {login.cookies['hermes_session_at']}"},
        )

    assert response.status_code == 200
    stored = next(iter(identity.websocket_tickets.values()))
    assert stored.observer_scope[-1] == f"agent_id={AGENT_ID}"
    service = application._cloud_api_service
    assert service is not None
    authenticated = service.consume_websocket_ticket(response.json()["ticket"])
    assert authenticated.connection_role == "observer"
    assert authenticated.agent_id == AGENT_ID


def test_observer_v2_ticket_request_response_and_single_use_claim_bind_contract() -> (
    None
):
    identity = _IdentityRepository()
    application = create_app(
        identity_repository=identity,
        projection_repository=_ProjectionRepository(),
        tenant_resolver=_TenantResolver(),
        secret_resolver=_SecretResolver(),
        settings={
            "signing_secret_ref": "secret-manager/unit/cloud-p0-signing",
            "access_ttl_seconds": 300,
            "refresh_ttl_seconds": 3600,
            "ticket_ttl_seconds": 60,
        },
        now=lambda: NOW,
    )

    with TestClient(application, base_url="https://testserver") as client:
        login = client.post(
            "/auth/password-login",
            json={
                "provider": "basic",
                "username": "user@example.test",
                "password": "correct-password",
                "next": "",
            },
        )
        response = client.post(
            "/api/auth/ws-ticket",
            json={
                "connection_role": "observer",
                "client_instance_id": "33333333-3333-4333-8333-333333333333",
                "observer_contract": 2,
            },
            headers={"Authorization": f"Bearer {login.cookies['hermes_session_at']}"},
        )

    assert response.status_code == 200
    assert set(response.json()) == {
        "ticket",
        "ttl_seconds",
        "connection_role",
        "observer_contract",
    }
    assert response.json()["observer_contract"] == 2
    stored = next(iter(identity.websocket_tickets.values()))
    assert stored.observer_scope == (
        "session.observe",
        "client_instance_id=33333333-3333-4333-8333-333333333333",
        "observer_contract=2",
    )

    service = application._cloud_api_service
    assert service is not None
    authenticated = service.consume_websocket_ticket(response.json()["ticket"])
    assert authenticated.connection_role == "observer"
    assert authenticated.observer_contract == 2
    with pytest.raises(AuthenticationFailed):
        service.consume_websocket_ticket(response.json()["ticket"])


def test_observer_v2_socket_requires_v2_subprotocol_and_echoes_exact_contract() -> None:
    identity = _IdentityRepository()
    application = create_app(
        identity_repository=identity,
        projection_repository=_ProjectionRepository(),
        session_catalog_repository=_CatalogRepository(),
        observer_projection_repository=_ObserverV2Projection(),
        projection_event_source=_ProjectionEventSource(),
        tenant_resolver=_TenantResolver(),
        secret_resolver=_SecretResolver(),
        settings={
            "signing_secret_ref": "secret-manager/unit/cloud-p0-signing",
            "access_ttl_seconds": 300,
            "refresh_ttl_seconds": 3600,
            "ticket_ttl_seconds": 60,
        },
        now=lambda: NOW,
    )

    with TestClient(application, base_url="https://testserver") as client:
        login = client.post(
            "/auth/password-login",
            json={
                "provider": "basic",
                "username": "user@example.test",
                "password": "correct-password",
                "next": "",
            },
        )
        ticket = client.post(
            "/api/auth/ws-ticket",
            json={
                "connection_role": "observer",
                "client_instance_id": "33333333-3333-4333-8333-333333333333",
                "observer_contract": 2,
            },
            headers={"Authorization": f"Bearer {login.cookies['hermes_session_at']}"},
        ).json()["ticket"]

        with client.websocket_connect(
            f"/api/ws?ticket={ticket}",
            subprotocols=["hermes.tui.v2"],
        ) as websocket:
            assert websocket.accepted_subprotocol == "hermes.tui.v2"
            assert websocket.receive_json() == {
                "jsonrpc": "2.0",
                "method": "event",
                "params": {
                    "type": "gateway.ready",
                    "payload": {
                        "observer_contract": 2,
                        "connection_role": "observer",
                    },
                },
            }
            websocket.send_json(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "session.observe.subscribe",
                    "params": {
                        "observer_contract": 2,
                        "session_id": str(SESSION_ID),
                        "profile": "default",
                    },
                }
            )
            result = websocket.receive_json()
            websocket.send_json(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "session.observe.unsubscribe",
                    "params": {
                        "observer_contract": 2,
                        "subscription_id": result["result"]["subscription_id"],
                    },
                }
            )
            unsubscribe = websocket.receive_json()

    assert result == {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "observer_contract": 2,
            "subscription_id": result["result"]["subscription_id"],
            **_ObserverV2Projection().observer_snapshot(),
        },
    }
    assert unsubscribe == {
        "jsonrpc": "2.0",
        "id": 2,
        "result": {"observer_contract": 2},
    }


def test_observer_websocket_ready_snapshot_events_gap_and_ticket_replay() -> None:
    identity = _IdentityRepository()
    projections = _ProjectionRepository()
    events = _ProjectionEventSource(
        (
            {
                "type": "unknown.internal.event",
                "session_id": str(SESSION_ID),
                "session_key": SESSION_KEY,
                "event_sequence": 2,
                "payload": {},
            },
            {
                "type": "message.delta",
                "session_id": str(SESSION_ID),
                "session_key": SESSION_KEY,
                "event_sequence": 2,
                "payload": {"text": "Live response"},
            },
            {
                "type": "status.update",
                "session_id": str(SESSION_ID),
                "session_key": SESSION_KEY,
                "event_sequence": 4,
                "payload": {"status": "working", "running": True},
            },
        )
    )
    application = create_app(
        identity_repository=identity,
        projection_repository=projections,
        projection_event_source=events,
        session_catalog_repository=_CatalogRepository(),
        tenant_resolver=_TenantResolver(),
        secret_resolver=_SecretResolver(),
        settings={
            "signing_secret_ref": "secret-manager/unit/cloud-p0-signing",
            "access_ttl_seconds": 300,
            "refresh_ttl_seconds": 3600,
            "ticket_ttl_seconds": 60,
        },
        now=lambda: NOW,
    )

    with TestClient(application, base_url="https://testserver") as client:
        login = client.post(
            "/auth/password-login",
            json={
                "provider": "basic",
                "username": "user@example.test",
                "password": "correct-password",
                "next": "",
            },
        )
        authorization = {
            "Authorization": f"Bearer {login.cookies['hermes_session_at']}"
        }
        ticket = client.post(
            "/api/auth/ws-ticket",
            json={},
            headers=authorization,
        ).json()["ticket"]

        with client.websocket_connect(
            f"/api/ws?ticket={ticket}",
            subprotocols=["hermes.tui.v1"],
        ) as websocket:
            assert websocket.accepted_subprotocol == "hermes.tui.v1"
            assert websocket.receive_json() == {
                "jsonrpc": "2.0",
                "method": "event",
                "params": {
                    "type": "gateway.ready",
                    "payload": {
                        "observer_contract": 1,
                        "connection_role": "observer",
                    },
                },
            }
            websocket.send_json(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "session.observe.subscribe",
                    "params": {
                        "session_key": SESSION_KEY,
                        "profile": "default",
                    },
                }
            )
            snapshot = websocket.receive_json()
            assert snapshot == {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "subscription_id": snapshot["result"]["subscription_id"],
                    "session_key": SESSION_KEY,
                    "runtime_session_id": str(SESSION_ID),
                    "running": False,
                    "status": "active",
                    "event_sequence": 1,
                    "snapshot_event_sequence": 1,
                    "messages": [
                        {"role": "assistant", "content": "Projected response"}
                    ],
                    "inflight": {
                        "user": None,
                        "assistant": None,
                        "streaming": False,
                        "error": None,
                    },
                    "replay_events": [],
                },
            }
            assert websocket.receive_json() == {
                "jsonrpc": "2.0",
                "method": "event",
                "params": {
                    "type": "message.delta",
                    "session_id": str(SESSION_ID),
                    "session_key": SESSION_KEY,
                    "event_sequence": 2,
                    "payload": {"text": "Live response"},
                },
            }
            assert websocket.receive_json() == {
                "jsonrpc": "2.0",
                "id": None,
                "error": {
                    "code": 4091,
                    "message": "projection replay is unavailable",
                },
            }
            websocket.send_json(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "session.observe.unsubscribe",
                    "params": {
                        "subscription_id": snapshot["result"]["subscription_id"]
                    },
                }
            )
            assert websocket.receive_json() == {
                "jsonrpc": "2.0",
                "id": 2,
                "result": {},
            }

        with (
            pytest.raises(WebSocketDisconnect),
            client.websocket_connect(
                f"/api/ws?ticket={ticket}",
                subprotocols=["hermes.tui.v1"],
            ),
        ):
            pass

    assert events.calls == [(TENANT_ID, USER_ID, SESSION_KEY, "default", 1)]
    assert events.agent_ids == [AGENT_ID]


def test_agent_bound_observer_ticket_inherits_scope_and_rejects_mismatch() -> None:
    identity = _IdentityRepository()
    projections = _ProjectionRepository()
    observer_projection = _ObserverV2Projection()
    application = create_app(
        identity_repository=identity,
        projection_repository=projections,
        session_catalog_repository=_CatalogRepository(),
        observer_projection_repository=observer_projection,
        projection_event_source=_ProjectionEventSource(),
        tenant_resolver=_TenantResolver(),
        secret_resolver=_SecretResolver(),
        settings={
            "signing_secret_ref": "secret-manager/unit/cloud-p0-signing",
            "access_ttl_seconds": 300,
            "refresh_ttl_seconds": 3600,
            "ticket_ttl_seconds": 60,
        },
        now=lambda: NOW,
    )

    with TestClient(application, base_url="https://testserver") as client:
        login = client.post(
            "/auth/password-login",
            json={
                "provider": "basic",
                "username": "user@example.test",
                "password": "correct-password",
                "next": "",
            },
        )
        authorization = {
            "Authorization": f"Bearer {login.cookies['hermes_session_at']}"
        }

        def ticket() -> str:
            response = client.post(
                "/api/auth/ws-ticket",
                json={
                    "connection_role": "observer",
                    "client_instance_id": "33333333-3333-4333-8333-333333333333",
                    "observer_contract": 2,
                    "agent_id": str(AGENT_ID),
                },
                headers=authorization,
            )
            assert response.status_code == 200
            return str(response.json()["ticket"])

        with client.websocket_connect(
            f"/api/ws?ticket={ticket()}",
            subprotocols=["hermes.tui.v2"],
        ) as websocket:
            websocket.receive_json()
            websocket.send_json(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "session.observe.subscribe",
                    "params": {
                        "observer_contract": 2,
                        "session_id": str(SESSION_ID),
                        "profile": "default",
                    },
                }
            )
            assert websocket.receive_json()["result"]["observer_contract"] == 2

        assert observer_projection.calls[-1]["agent_id"] == AGENT_ID

        with client.websocket_connect(
            f"/api/ws?ticket={ticket()}",
            subprotocols=["hermes.tui.v2"],
        ) as websocket:
            websocket.receive_json()
            websocket.send_json(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "session.observe.subscribe",
                        "params": {
                            "observer_contract": 2,
                            "session_id": str(SESSION_ID),
                            "profile": "default",
                            "agent_id": "88888888-8888-4888-8888-888888888888",
                    },
                }
            )
            assert websocket.receive_json() == {
                "jsonrpc": "2.0",
                "id": 2,
                "error": {"code": 4001, "message": "session not found"},
            }

def test_observer_subscribe_enqueues_open_before_waiting_for_first_snapshot() -> None:
    identity = _IdentityRepository()
    projections = _ProjectionRepository()
    observer_projection = _DeferredObserverProjection()
    subscriptions = _ObserverSubscriptions(observer_projection)
    application = create_app(
        identity_repository=identity,
        projection_repository=projections,
        observer_projection_repository=observer_projection,
        observer_subscription_manager=subscriptions,
        projection_event_source=_ProjectionEventSource(),
        tenant_resolver=_TenantResolver(),
        secret_resolver=_SecretResolver(),
        settings={
            "signing_secret_ref": "secret-manager/unit/cloud-p0-signing",
            "access_ttl_seconds": 300,
            "refresh_ttl_seconds": 3600,
            "ticket_ttl_seconds": 60,
        },
        now=lambda: NOW,
    )

    with TestClient(application, base_url="https://testserver") as client:
        login = client.post(
            "/auth/password-login",
            json={
                "provider": "basic",
                "username": "user@example.test",
                "password": "correct-password",
                "next": "",
            },
        )
        ticket = client.post(
            "/api/auth/ws-ticket",
            json={},
            headers={"Authorization": f"Bearer {login.cookies['hermes_session_at']}"},
        ).json()["ticket"]
        with client.websocket_connect(
            f"/api/ws?ticket={ticket}", subprotocols=["hermes.tui.v1"]
        ) as websocket:
            websocket.receive_json()
            websocket.send_json(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "session.observe.subscribe",
                    "params": {"session_key": SESSION_KEY, "profile": "default"},
                }
            )
            snapshot = websocket.receive_json()
            assert snapshot["result"]["subscription_id"] == (
                "82000000-0000-4000-8000-000000000001"
            )
            assert snapshot["result"]["runtime_session_id"] == "runtime-session-1"
            websocket.send_json(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "session.observe.unsubscribe",
                    "params": {
                        "subscription_id": snapshot["result"]["subscription_id"]
                    },
                }
            )
            assert websocket.receive_json() == {
                "jsonrpc": "2.0",
                "id": 2,
                "result": {},
            }

    assert subscriptions.opened[0]["profile"] == "default"
    assert subscriptions.ready_checks >= 2
    assert len(subscriptions.closed) == 1
    assert subscriptions.closed[0]["reason"] == "client_unsubscribe"


@pytest.mark.parametrize(
    ("invalid_frame", "expected_close"),
    [
        (b"binary", 1002),
        ("{}{}", 1002),
        ("x" * 262145, 1009),
        ('"' + ("界" * 43691) + '"', 1009),
        ("[" * 33 + "0" + "]" * 33, 1009),
        ("[" + ",".join("0" for _ in range(1025)) + "]", 1009),
        (
            "{" + ",".join(f'"field{index}":0' for index in range(1025)) + "}",
            1009,
        ),
    ],
)
def test_observer_websocket_enforces_strict_frame_limits(
    invalid_frame: str | bytes,
    expected_close: int,
) -> None:
    identity = _IdentityRepository()
    application = create_app(
        identity_repository=identity,
        tenant_resolver=_TenantResolver(),
        secret_resolver=_SecretResolver(),
        settings={
            "signing_secret_ref": "secret-manager/unit/cloud-p0-signing",
            "access_ttl_seconds": 300,
            "refresh_ttl_seconds": 3600,
            "ticket_ttl_seconds": 60,
        },
        now=lambda: NOW,
    )

    with TestClient(application, base_url="https://testserver") as client:
        login = client.post(
            "/auth/password-login",
            json={
                "provider": "basic",
                "username": "user@example.test",
                "password": "correct-password",
                "next": "",
            },
        )
        ticket = client.post(
            "/api/auth/ws-ticket",
            json={},
            headers={"Authorization": f"Bearer {login.cookies['hermes_session_at']}"},
        ).json()["ticket"]
        with client.websocket_connect(
            f"/api/ws?ticket={ticket}",
            subprotocols=["hermes.tui.v1"],
        ) as websocket:
            websocket.receive_json()
            if isinstance(invalid_frame, bytes):
                websocket.send_bytes(invalid_frame)
            else:
                websocket.send_text(invalid_frame)
            with pytest.raises(WebSocketDisconnect) as disconnected:
                websocket.receive_json()
            assert disconnected.value.code == expected_close


_CONTROL_ERROR_CODES = dict(CONTROL_ERROR_CODES)


def _control_ticket_request(
    *,
    client_instance_id: str = "33333333-3333-4333-8333-333333333333",
    session_id: UUID = SESSION_ID,
    agent_id: UUID = AGENT_ID,
) -> dict[str, str]:
    return {
        "connection_role": "control",
        "client_instance_id": client_instance_id,
        "session_id": str(session_id),
        "agent_id": str(agent_id),
    }


def _authenticated_control_client(
    identity: _IdentityRepository,
    projections: _ProjectionRepository,
    *,
    control_runtime: object | None = None,
    catalog: object | None = None,
) -> tuple[TestClient, dict[str, str]]:
    application = create_app(
        identity_repository=identity,
        projection_repository=projections,
        session_catalog_repository=catalog or _CatalogRepository(),
        tenant_resolver=_TenantResolver(),
        secret_resolver=_SecretResolver(),
        settings={
            "signing_secret_ref": "secret-manager/unit/cloud-p0-signing",
            "access_ttl_seconds": 300,
            "refresh_ttl_seconds": 3600,
            "ticket_ttl_seconds": 60,
        },
        now=lambda: NOW,
        control_runtime=control_runtime,
    )
    client = TestClient(application, base_url="https://testserver")
    login = client.post(
        "/auth/password-login",
        json={
            "provider": "basic",
            "username": "user@example.test",
            "password": "correct-password",
            "next": "",
        },
    )
    return client, {"Authorization": f"Bearer {login.cookies['hermes_session_at']}"}


class _ControlRuntime:
    available_methods = (
        "prompt.submit",
        "session.command.status",
        "session.control.acquire",
        "session.control.release",
        "session.control.renew",
        "session.control.status",
        "session.interrupt",
    )
    error_codes: ClassVar[dict[str, int]] = {
        "live_runtime_unavailable": 4202,
        "controller_conflict": 4203,
        "lease_required": 4204,
        "lease_expired": 4205,
        "lease_mismatch": 4206,
        "request_id_payload_conflict": 4207,
        "method_not_allowed": 4209,
        "command_unknown": 4210,
        "session_binding_mismatch": 4212,
        "owner_adapter_unavailable": 4214,
        "relay_overloaded": 4215,
    }

    def __init__(self) -> None:
        self.calls: list[tuple[object, str, dict[str, object]]] = []
        self.opened: list[object] = []
        self.closed: list[tuple[object, str]] = []

    async def open(self, *, context: object) -> None:
        self.opened.append(context)

    async def execute(
        self,
        *,
        context: object,
        method: str,
        params: dict[str, object],
    ) -> dict[str, object]:
        self.calls.append((context, method, params))
        return {
            "lease_id": "opaque-test-lease",
            "expires_at_epoch_ms": 1_785_460_830_000,
            "control_revision": 1,
            "controller_kind": "mobile",
            "controller_label": "Hermes Mobile",
            "pending_input": None,
        }

    async def close(self, *, context: object, reason: str) -> None:
        self.closed.append((context, reason))


def test_control_ticket_opens_role_aware_socket_and_fails_unrouted_methods_closed() -> (
    None
):
    identity = _IdentityRepository()
    projections = _ProjectionRepository()
    client, authorization = _authenticated_control_client(identity, projections)
    request = {
        "connection_role": "control",
        "client_instance_id": "33333333-3333-4333-8333-333333333333",
        "session_id": str(SESSION_ID),
        "agent_id": str(AGENT_ID),
    }

    with client:
        response = client.post(
            "/api/auth/ws-ticket",
            json=request,
            headers=authorization,
        )

        assert response.status_code == 200
        body = response.json()
        assert set(body) == {"ticket", "ttl_seconds", "connection_role"}
        assert body["ttl_seconds"] == 60
        assert body["connection_role"] == "control"
        stored = next(iter(identity.websocket_tickets.values()))
        assert stored.tenant_id == TENANT_ID
        assert stored.principal_id == USER_ID
        assert stored.session_id == SESSION_ID
        assert stored.observer_scope == (
            "session.control",
            "provider=basic",
            "client_instance_id=33333333-3333-4333-8333-333333333333",
            "profile=default",
            f"agent_id={AGENT_ID}",
        )
        assert stored.expires_at == NOW + timedelta(seconds=60)
        assert body["ticket"] not in repr(identity.websocket_tickets)
        service = client.app._cloud_api_service  # type: ignore[attr-defined]
        assert service is not None
        authenticated = service.consume_websocket_ticket(body["ticket"])
        assert authenticated.agent_id == AGENT_ID
        assert authenticated.session_id == SESSION_ID
        assert authenticated.session_key is None

        replacement = client.post(
            "/api/auth/ws-ticket",
            json=request,
            headers=authorization,
        ).json()["ticket"]

        with client.websocket_connect(
            f"/api/ws?ticket={replacement}",
            subprotocols=["hermes.tui.v1"],
        ) as websocket:
            assert websocket.receive_json() == {
                "jsonrpc": "2.0",
                "method": "event",
                "params": {
                    "type": "gateway.ready",
                    "payload": {
                        "observer_contract": 1,
                        "control_contract": 1,
                        "connection_role": "control",
                        "control_available_methods": [],
                        "control_error_codes": _CONTROL_ERROR_CODES,
                    },
                },
            }
            websocket.send_json(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "session.control.status",
                    "params": {
                        "session_id": str(SESSION_ID),
                    },
                }
            )
            assert websocket.receive_json() == {
                "jsonrpc": "2.0",
                "id": 1,
                "error": {
                    "code": 4202,
                    "message": "authoritative live runtime unavailable",
                },
            }
            websocket.send_json(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "prompt.submit",
                    "params": {
                        "session_id": str(SESSION_ID),
                        "lease_id": "not-authorized",
                        "client_request_id": "request-1",
                        "client_turn_id": "turn-1",
                        "text": "Do not execute this unrouted prompt",
                    },
                }
            )
            assert websocket.receive_json() == {
                "jsonrpc": "2.0",
                "id": 2,
                "error": {
                    "code": 4209,
                    "message": "method not allowed for this control slice",
                },
            }

        with (
            pytest.raises(WebSocketDisconnect),
            client.websocket_connect(
                f"/api/ws?ticket={body['ticket']}",
                subprotocols=["hermes.tui.v1"],
            ),
        ):
            pass


def test_control_socket_rejects_catalog_session_that_became_inactive() -> None:
    identity = _IdentityRepository()
    projections = _ProjectionRepository()
    catalog = _CatalogRepository()
    client, authorization = _authenticated_control_client(
        identity,
        projections,
        catalog=catalog,
    )

    with client:
        issued = client.post(
            "/api/auth/ws-ticket",
            json=_control_ticket_request(),
            headers=authorization,
        )
        assert issued.status_code == 200
        catalog.active = False

        with pytest.raises(WebSocketDisconnect), client.websocket_connect(
            f"/api/ws?ticket={issued.json()['ticket']}",
            subprotocols=["hermes.tui.v1"],
        ):
            pass


def test_control_ticket_freezes_agent_scope_through_consumption() -> None:
    identity = _IdentityRepository()
    projections = _ProjectionRepository()
    client, authorization = _authenticated_control_client(identity, projections)

    with client:
        response = client.post(
            "/api/auth/ws-ticket",
            json=_control_ticket_request(),
            headers=authorization,
        )

    assert response.status_code == 200
    stored = next(iter(identity.websocket_tickets.values()))
    assert stored.observer_scope[-1] == f"agent_id={AGENT_ID}"
    service = client.app._cloud_api_service  # type: ignore[attr-defined]
    assert service is not None
    consumed = service.consume_websocket_ticket(response.json()["ticket"])
    assert consumed.agent_id == AGENT_ID


@pytest.mark.parametrize(
    ("session_id", "agent_id"),
    [
        (UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"), AGENT_ID),
        (SESSION_ID, UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")),
    ],
)
def test_control_ticket_fails_closed_for_unknown_stable_scope(
    session_id: UUID,
    agent_id: UUID,
) -> None:
    identity = _IdentityRepository()
    projections = _ProjectionRepository()
    client, authorization = _authenticated_control_client(identity, projections)

    with client:
        response = client.post(
            "/api/auth/ws-ticket",
            json=_control_ticket_request(session_id=session_id, agent_id=agent_id),
            headers=authorization,
        )

    assert response.status_code == 404
    assert identity.websocket_tickets == {}


def test_control_runtime_advertises_and_executes_only_injected_methods() -> None:
    identity = _IdentityRepository()
    projections = _ProjectionRepository()
    runtime = _ControlRuntime()
    client, authorization = _authenticated_control_client(
        identity,
        projections,
        control_runtime=runtime,
    )

    with client:
        ticket = client.post(
            "/api/auth/ws-ticket",
            json=_control_ticket_request(),
            headers=authorization,
        ).json()["ticket"]
        with client.websocket_connect(
            f"/api/ws?ticket={ticket}",
            subprotocols=["hermes.tui.v1"],
        ) as websocket:
            ready = websocket.receive_json()
            assert ready["params"]["payload"] == {
                "observer_contract": 1,
                "control_contract": 1,
                "connection_role": "control",
                "control_available_methods": list(runtime.available_methods),
                "control_error_codes": runtime.error_codes,
            }
            params = {
                "session_id": str(SESSION_ID),
            }
            websocket.send_json(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "session.control.acquire",
                    "params": params,
                }
            )
            response = websocket.receive_json()

    assert response == {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "lease_id": "opaque-test-lease",
            "expires_at_epoch_ms": 1_785_460_830_000,
            "control_revision": 1,
            "controller_kind": "mobile",
            "controller_label": "Hermes Mobile",
            "pending_input": None,
        },
    }
    assert len(runtime.calls) == 1
    context, method, called_params = runtime.calls[0]
    assert method == "session.control.acquire"
    assert called_params == params
    assert context.authentication.connection_role == "control"
    assert str(UUID(context.connection_id)) == context.connection_id
    assert runtime.opened == [context]
    assert runtime.closed == [(context, "client_disconnected")]


@pytest.mark.parametrize(
    ("method", "params", "expected_code", "expected_message"),
    [
        (
            "session.control.status",
            {"session_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"},
            4202,
            "authoritative live runtime unavailable",
        ),
        (
            "session.control.status",
            {"session_id": str(SESSION_ID), "extra": "forbidden"},
            4202,
            "authoritative live runtime unavailable",
        ),
        (
            "session.control.acquire",
            {"session_id": str(SESSION_ID)},
            4202,
            "authoritative live runtime unavailable",
        ),
        (
            "session.control.future",
            {"session_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"},
            4202,
            "authoritative live runtime unavailable",
        ),
        (
            "prompt.submit",
            {"session_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"},
            4209,
            "method not allowed for this control slice",
        ),
    ],
)
def test_control_first_slice_classifies_method_before_unadvertised_binding_errors(
    method: str,
    params: dict[str, object],
    expected_code: int,
    expected_message: str,
) -> None:
    identity = _IdentityRepository()
    projections = _ProjectionRepository()
    client, authorization = _authenticated_control_client(identity, projections)
    request = _control_ticket_request()

    with client:
        ticket = client.post(
            "/api/auth/ws-ticket",
            json=request,
            headers=authorization,
        ).json()["ticket"]
        with client.websocket_connect(
            f"/api/ws?ticket={ticket}",
            subprotocols=["hermes.tui.v1"],
        ) as websocket:
            websocket.receive_json()
            websocket.send_json(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": method,
                    "params": params,
                }
            )

            assert websocket.receive_json() == {
                "jsonrpc": "2.0",
                "id": 1,
                "error": {
                    "code": expected_code,
                    "message": expected_message,
                },
            }


@pytest.mark.parametrize(
    "body",
    [
        {
            "connection_role": "control",
            "client_instance_id": "33333333-3333-4333-8333-333333333333",
            "session_id": str(SESSION_ID),
        },
        {
            "connection_role": "control",
            "client_instance_id": "33333333-3333-4333-8333-333333333333",
            "session_id": str(SESSION_ID),
            "agent_id": str(AGENT_ID),
            "extra": True,
        },
        {
            "connection_role": "observer",
            "client_instance_id": "33333333-3333-4333-8333-333333333333",
            "session_id": str(SESSION_ID),
            "agent_id": str(AGENT_ID),
        },
        {
            "connection_role": "admin",
            "client_instance_id": "33333333-3333-4333-8333-333333333333",
            "session_id": str(SESSION_ID),
            "agent_id": str(AGENT_ID),
        },
        {
            "connection_role": "control",
            "client_instance_id": "not-a-uuid",
            "session_id": str(SESSION_ID),
            "agent_id": str(AGENT_ID),
        },
        {
            "connection_role": "control",
            "client_instance_id": "00000000-0000-0000-0000-000000000000",
            "session_id": str(SESSION_ID),
            "agent_id": str(AGENT_ID),
        },
        {
            "connection_role": "control",
            "client_instance_id": "01890f47-6c22-7c00-98c4-dc0c0c07398f",
            "session_id": str(SESSION_ID),
            "agent_id": str(AGENT_ID),
        },
        {
            "connection_role": "control",
            "client_instance_id": "33333333-3333-4333-7333-333333333333",
            "session_id": str(SESSION_ID),
            "agent_id": str(AGENT_ID),
        },
        {
            "connection_role": "control",
            "client_instance_id": "33333333-3333-4333-8333-333333333333",
            "session_id": SESSION_KEY,
            "agent_id": str(AGENT_ID),
        },
        {
            "connection_role": "control",
            "client_instance_id": "33333333-3333-4333-8333-333333333333",
            "session_id": str(SESSION_ID),
            "agent_id": "not-an-agent-uuid",
        },
    ],
)
def test_control_ticket_request_rejects_non_exact_scope(
    body: dict[str, object],
) -> None:
    identity = _IdentityRepository()
    projections = _ProjectionRepository()
    client, authorization = _authenticated_control_client(identity, projections)

    with client:
        response = client.post(
            "/api/auth/ws-ticket",
            json=body,
            headers=authorization,
        )

    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_REQUEST"
    assert identity.websocket_tickets == {}


@pytest.mark.parametrize(
    "client_instance_id",
    [
        "11111111-1111-1111-8111-111111111111",
        "22222222-2222-2222-8222-222222222222",
        "33333333-3333-3333-8333-333333333333",
        "44444444-4444-4444-8444-444444444444",
        "55555555-5555-5555-8555-555555555555",
    ],
)
def test_control_ticket_accepts_canonical_rfc4122_uuid_versions_one_to_five(
    client_instance_id: str,
) -> None:
    identity = _IdentityRepository()
    projections = _ProjectionRepository()
    client, authorization = _authenticated_control_client(identity, projections)

    with client:
        response = client.post(
            "/api/auth/ws-ticket",
            json=_control_ticket_request(client_instance_id=client_instance_id),
            headers=authorization,
        )

    assert response.status_code == 200
    assert response.json()["connection_role"] == "control"


def test_control_ticket_rejects_session_outside_authenticated_authority() -> None:
    identity = _IdentityRepository()
    projections = _ProjectionRepository()
    client, authorization = _authenticated_control_client(identity, projections)

    with client:
        response = client.post(
            "/api/auth/ws-ticket",
            json=_control_ticket_request(
                session_id=UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
            ),
            headers=authorization,
        )

    assert response.status_code == 404
    assert response.json() == {
        "code": "SESSION_NOT_FOUND",
        "reason": "session not found",
    }
    assert identity.websocket_tickets == {}


def test_browser_cookie_jar_mints_observer_and_control_tickets_without_token_body(
    caplog: pytest.LogCaptureFixture,
) -> None:
    identity = _IdentityRepository()
    projections = _ProjectionRepository()
    application = create_app(
        identity_repository=identity,
        projection_repository=projections,
        session_catalog_repository=_CatalogRepository(),
        tenant_resolver=_TenantResolver(),
        secret_resolver=_SecretResolver(),
        settings={
            "signing_secret_ref": "secret-manager/unit/cloud-p0-signing",
            "access_ttl_seconds": 300,
            "refresh_ttl_seconds": 3600,
            "ticket_ttl_seconds": 60,
        },
        now=lambda: NOW,
    )

    with TestClient(application, base_url="https://testserver") as client:
        login = client.post(
            "/auth/password-login",
            json={
                "provider": "basic",
                "username": "user@example.test",
                "password": "correct-password",
                "next": "",
            },
        )
        access_token = login.cookies["hermes_session_at"]
        observer = client.post(
            "/api/auth/ws-ticket",
            json={},
            headers={"Origin": "https://testserver"},
        )
        control = client.post(
            "/api/auth/ws-ticket",
            json=_control_ticket_request(),
            headers={"Origin": "https://testserver"},
        )

    assert login.json() == {"ok": True}
    assert access_token not in login.text
    assert observer.status_code == 200
    assert observer.json()["connection_role"] == "observer"
    assert control.status_code == 200
    assert control.json()["connection_role"] == "control"
    assert access_token not in caplog.text


@pytest.mark.parametrize(
    ("base_url", "headers"),
    [
        ("https://testserver", {}),
        ("https://testserver", {"Origin": "https://attacker.test"}),
        ("http://testserver", {"Origin": "https://testserver"}),
        (
            "https://testserver",
            {
                "Origin": "https://attacker.test",
                "X-Forwarded-Host": "attacker.test",
                "X-Forwarded-Proto": "https",
            },
        ),
    ],
)
def test_cookie_ticket_rejects_non_https_or_non_same_origin_mutation(
    base_url: str,
    headers: dict[str, str],
) -> None:
    identity = _IdentityRepository()
    application = create_app(
        identity_repository=identity,
        tenant_resolver=_TenantResolver(),
        secret_resolver=_SecretResolver(),
        settings={
            "signing_secret_ref": "secret-manager/unit/cloud-p0-signing",
            "access_ttl_seconds": 300,
            "refresh_ttl_seconds": 3600,
            "ticket_ttl_seconds": 60,
        },
        now=lambda: NOW,
    )

    with TestClient(application, base_url=base_url) as client:
        login = client.post(
            "/auth/password-login",
            json={
                "provider": "basic",
                "username": "user@example.test",
                "password": "correct-password",
                "next": "",
            },
        )
        assert login.status_code == 200
        request_headers = dict(headers)
        if base_url.startswith("http://"):
            request_headers["Cookie"] = (
                f"hermes_session_at={login.cookies['hermes_session_at']}"
            )
        response = client.post(
            "/api/auth/ws-ticket",
            json={},
            headers=request_headers,
        )

    assert response.status_code == 403
    assert response.json() == {
        "code": "FORBIDDEN",
        "reason": "trusted same-origin request required",
    }
    assert identity.websocket_tickets == {}


def test_ticket_rejects_invalid_or_conflicting_bearer_when_cookie_is_present() -> None:
    identity = _IdentityRepository()
    application = create_app(
        identity_repository=identity,
        tenant_resolver=_TenantResolver(),
        secret_resolver=_SecretResolver(),
        settings={
            "signing_secret_ref": "secret-manager/unit/cloud-p0-signing",
            "access_ttl_seconds": 300,
            "refresh_ttl_seconds": 3600,
            "ticket_ttl_seconds": 60,
        },
        now=lambda: NOW,
    )
    conflicting = jwt.encode(
        {
            "tenant_id": str(TENANT_ID),
            "user_id": "99999999-9999-4999-8999-999999999999",
            "provider": "basic",
            "refresh_session_id": "88888888-8888-4888-8888-888888888888",
            "iat": int(NOW.timestamp()),
            "nbf": int(NOW.timestamp()),
            "exp": int(NOW.timestamp()) + 300,
        },
        SIGNING_KEY,
        algorithm="HS256",
    )

    with TestClient(application, base_url="https://testserver") as client:
        login = client.post(
            "/auth/password-login",
            json={
                "provider": "basic",
                "username": "user@example.test",
                "password": "correct-password",
                "next": "",
            },
        )
        assert login.status_code == 200
        invalid = client.post(
            "/api/auth/ws-ticket",
            json={},
            headers={"Authorization": "Bearer invalid"},
        )
        mismatch = client.post(
            "/api/auth/ws-ticket",
            json={},
            headers={"Authorization": f"Bearer {conflicting}"},
        )

    assert invalid.status_code == 401
    assert mismatch.status_code == 401
    assert identity.websocket_tickets == {}
