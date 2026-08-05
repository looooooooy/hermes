from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID

import httpx2 as httpx
import jwt
import pytest

from hermes_cloud.entrypoints.business_api import create_app
from hermes_cloud.modules.cloud_api.application.service import (
    AuthenticationFailed,
    CloudApiService,
)
from hermes_cloud.modules.cloud_api.domain import CloudApiSettings

NOW = datetime.now(UTC).replace(microsecond=0)
TENANT_ID = UUID("11111111-1111-4111-8111-111111111111")
USER_ID = UUID("22222222-2222-4222-8222-222222222222")
REFRESH_SESSION_ID = UUID("33333333-3333-4333-8333-333333333333")
SIGNING_KEY = b"unit-test-signing-key-with-at-least-32-bytes"


class _ThreadTenantResolver:
    def __init__(self) -> None:
        self.thread_ids: list[int] = []

    def tenant_for_subject(self, _subject: str):
        self.thread_ids.append(threading.get_ident())


class _ThreadSecretResolver:
    def __init__(self) -> None:
        self.thread_ids: list[int] = []

    def resolve(self, _reference: str) -> bytes:
        self.thread_ids.append(threading.get_ident())
        return SIGNING_KEY


class _ThreadProjectionRepository:
    def __init__(self) -> None:
        self.thread_ids: list[int] = []

    def list_sessions(self, **_kwargs: object):
        self.thread_ids.append(threading.get_ident())
        return (), 0


class _ThreadIdentityRepository:
    def __init__(self) -> None:
        self.thread_ids: list[int] = []

    def refresh_session_by_id(self, **_scope: object):
        self.thread_ids.append(threading.get_ident())
        return SimpleNamespace(
            user_id=USER_ID,
            revoked_at=None,
            expires_at=NOW + timedelta(hours=1),
        )


def _settings() -> dict[str, object]:
    return {
        "signing_secret_ref": "secret-manager/unit/cloud-api",
        "access_ttl_seconds": 300,
        "refresh_ttl_seconds": 3600,
        "ticket_ttl_seconds": 60,
    }


@pytest.mark.asyncio
async def test_password_login_service_is_offloaded_from_event_loop() -> None:
    event_loop_thread = threading.get_ident()
    tenant_resolver = _ThreadTenantResolver()
    application = create_app(
        identity_repository=object(),
        tenant_resolver=tenant_resolver,
        secret_resolver=_ThreadSecretResolver(),
        settings=_settings(),
        now=lambda: NOW,
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application),
        base_url="https://testserver",
    ) as client:
        response = await client.post(
            "/auth/password-login",
            json={
                "provider": "basic",
                "username": "user@example.test",
                "password": "password",
                "next": "",
            },
        )

    assert response.status_code == 401
    assert tenant_resolver.thread_ids
    assert set(tenant_resolver.thread_ids) == {
        thread_id
        for thread_id in tenant_resolver.thread_ids
        if thread_id != event_loop_thread
    }


@pytest.mark.asyncio
async def test_bearer_and_session_query_are_offloaded_from_event_loop() -> None:
    event_loop_thread = threading.get_ident()
    secret_resolver = _ThreadSecretResolver()
    projections = _ThreadProjectionRepository()
    identity = _ThreadIdentityRepository()
    application = create_app(
        identity_repository=identity,
        projection_repository=projections,
        tenant_resolver=_ThreadTenantResolver(),
        secret_resolver=secret_resolver,
        settings=_settings(),
        now=lambda: NOW,
    )
    access_token = jwt.encode(
        {
            "tenant_id": str(TENANT_ID),
            "user_id": str(USER_ID),
            "provider": "basic",
            "refresh_session_id": str(REFRESH_SESSION_ID),
            "iat": int(NOW.timestamp()),
            "nbf": int(NOW.timestamp()),
            "exp": int((NOW + timedelta(minutes=5)).timestamp()),
        },
        SIGNING_KEY,
        algorithm="HS256",
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application),
        base_url="https://testserver",
    ) as client:
        response = await client.get(
            "/api/sessions",
            headers={"Authorization": f"Bearer {access_token}"},
        )

    assert response.status_code == 200
    assert secret_resolver.thread_ids
    assert identity.thread_ids
    assert projections.thread_ids
    assert event_loop_thread not in secret_resolver.thread_ids
    assert event_loop_thread not in identity.thread_ids
    assert event_loop_thread not in projections.thread_ids


class _FailingSecretResolver:
    def __init__(self) -> None:
        self.calls = 0
        self.fail_on: int | None = None

    def resolve(self, _reference: str) -> bytes:
        self.calls += 1
        if self.calls == self.fail_on:
            raise AuthenticationFailed
        return SIGNING_KEY


class _RotateRepository:
    def __init__(self) -> None:
        self.rotate_calls = 0

    def rotate_refresh_session(self, **_kwargs: object):
        self.rotate_calls += 1
        return SimpleNamespace(user_id=USER_ID)


def test_refresh_prepares_access_token_before_atomic_rotation() -> None:
    secret_resolver = _FailingSecretResolver()
    repository = _RotateRepository()
    service = CloudApiService(
        identity_repository=repository,  # type: ignore[arg-type]
        tenant_resolver=_ThreadTenantResolver(),
        secret_resolver=secret_resolver,
        settings=CloudApiSettings.from_mapping(_settings()),
        now=lambda: NOW,
    )
    refresh_token = service._encode_opaque_refresh(
        tenant_id=str(TENANT_ID),
        user_id=str(USER_ID),
        refresh_session_id=str(REFRESH_SESSION_ID),
    ).reveal()
    secret_resolver.calls = 0
    secret_resolver.fail_on = 3

    with pytest.raises(AuthenticationFailed):
        service.rotate_refresh(
            provider="basic",
            refresh_token=refresh_token,
        )

    assert repository.rotate_calls == 0
