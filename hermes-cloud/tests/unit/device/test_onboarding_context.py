from __future__ import annotations

from uuid import UUID

from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cloud.modules.cloud_api.application.service import AuthenticationFailed
from hermes_cloud.modules.cloud_api.domain import Principal
from hermes_cloud.modules.device.onboarding_context import PairingTarget
from hermes_cloud.modules.device.onboarding_http import register_pairing_context_route


_PRINCIPAL = Principal(
    tenant_id=UUID("a495873f-cc49-5e21-b9fd-a581e3159ec8"),
    user_id=UUID("5f4da0f1-0e21-53bc-9fd6-5a7c35831b08"),
    provider="basic",
    refresh_session_id=UUID("04283a6d-f513-4a16-8d29-3a1b6c11a401"),
)


class _Authentication:
    def authenticate_access(self, token: str) -> Principal:
        if token != "valid-access-token":
            raise AuthenticationFailed("invalid")
        return _PRINCIPAL


class _Resolver:
    def __init__(self) -> None:
        self.principals: list[Principal] = []

    def targets_for(self, principal: Principal) -> tuple[PairingTarget, ...]:
        self.principals.append(principal)
        return (
            PairingTarget(
                workspace_id="4051c194-5536-5d29-b230-2ce731ead101",
                workspace_key="android",
                workspace_display_name="Android",
                agent_id="c4871c80-6ca0-5d89-8ce3-1d1d8aa93dd0",
                agent_key="android-agent",
            ),
        )


def _application(resolver: _Resolver) -> FastAPI:
    app = FastAPI()
    register_pairing_context_route(
        app,
        authentication=_Authentication(),
        resolver=resolver,
    )
    return app


def test_pairing_context_requires_bearer_authentication() -> None:
    resolver = _Resolver()
    with TestClient(_application(resolver)) as client:
        response = client.get("/api/onboarding/pairing-context")
    assert response.status_code == 401
    assert response.headers["cache-control"] == "no-store"
    assert resolver.principals == []


def test_pairing_context_returns_only_resolver_authorized_targets() -> None:
    resolver = _Resolver()
    with TestClient(_application(resolver)) as client:
        response = client.get(
            "/api/onboarding/pairing-context",
            headers={"Authorization": "Bearer valid-access-token"},
        )
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "targets": [
            {
                "workspace_id": "4051c194-5536-5d29-b230-2ce731ead101",
                "workspace_key": "android",
                "workspace_display_name": "Android",
                "agent_id": "c4871c80-6ca0-5d89-8ce3-1d1d8aa93dd0",
                "agent_key": "android-agent",
            }
        ]
    }
    assert resolver.principals == [_PRINCIPAL]
