from __future__ import annotations

import base64
import hashlib
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cloud.modules.cloud_api.adapters.native_auth import (
    register_native_auth_routes,
)
from hermes_cloud.modules.cloud_api.application.service import AuthenticationFailed
from hermes_cloud.modules.cloud_api.domain import IssuedAuthentication, SensitiveToken

NOW = datetime(2026, 8, 9, 3, 0, tzinfo=UTC)
USER_ID = UUID("22222222-2222-4222-8222-222222222222")
VERIFIER = "v" * 43
CHALLENGE = base64.urlsafe_b64encode(
    hashlib.sha256(VERIFIER.encode("ascii")).digest()
).rstrip(b"=").decode("ascii")
STATE = "state-0123456789abcdef"
REDIRECT = "http://127.0.0.1:55407/oauth/callback"


class _AuthService:
    def issue_password_login(
        self,
        *,
        provider: str,
        subject: str,
        password: str,
        next_path: str,
    ) -> IssuedAuthentication:
        if (
            provider != "basic"
            or subject != "user@example.test"
            or password != "correct-password"
            or next_path != ""
        ):
            raise AuthenticationFailed
        return IssuedAuthentication(
            access_token=SensitiveToken("access-token"),
            refresh_token=SensitiveToken("refresh-token"),
            access_expires_at=NOW + timedelta(minutes=5),
            user_id=USER_ID,
        )


def _application() -> FastAPI:
    app = FastAPI()
    register_native_auth_routes(app, authentication=_AuthService())
    return app


def _params(**overrides: str) -> dict[str, str]:
    values = {
        "code_challenge": CHALLENGE,
        "code_challenge_method": "S256",
        "redirect_uri": REDIRECT,
        "state": STATE,
        "provider": "basic",
    }
    values.update(overrides)
    return values


def test_native_authorize_renders_password_form_only_for_loopback_pkce() -> None:
    with TestClient(_application(), base_url="https://api.example.test") as client:
        response = client.get("/auth/native/authorize", params=_params())
        rejected = client.get(
            "/auth/native/authorize",
            params=_params(redirect_uri="https://evil.example/oauth/callback"),
        )

    assert response.status_code == 200
    assert "Connect Hermes" in response.text
    assert 'name="username"' in response.text
    assert 'name="password"' in response.text
    assert "Cache-Control" in response.headers
    assert response.headers["x-frame-options"] == "DENY"
    assert rejected.status_code == 400


def test_native_pkce_code_is_loopback_bound_one_time_and_exchanges_tokens() -> None:
    with TestClient(_application(), base_url="https://api.example.test") as client:
        authorization = client.post(
            "/auth/native/authorize",
            data={
                **_params(),
                "username": "user@example.test",
                "password": "correct-password",
            },
            follow_redirects=False,
        )
        assert authorization.status_code == 303
        location = authorization.headers["location"]
        parsed = urlsplit(location)
        query = parse_qs(parsed.query)
        code = query["code"][0]
        assert f"{parsed.scheme}://{parsed.hostname}:{parsed.port}{parsed.path}" == REDIRECT
        assert query["state"] == [STATE]

        exchanged = client.post(
            "/auth/native/token",
            json={"code": code, "code_verifier": VERIFIER},
        )
        replay = client.post(
            "/auth/native/token",
            json={"code": code, "code_verifier": VERIFIER},
        )

    assert exchanged.status_code == 200
    assert exchanged.json() == {
        "access_token": "access-token",
        "refresh_token": "refresh-token",
        "token_type": "Bearer",
        "expires_at": int((NOW + timedelta(minutes=5)).timestamp()),
        "provider": "basic",
        "user_id": str(USER_ID),
    }
    assert replay.status_code == 401
    assert "access-token" not in replay.text
    assert "refresh-token" not in replay.text


def test_native_authorization_rejects_bad_password_without_issuing_code() -> None:
    with TestClient(_application(), base_url="https://api.example.test") as client:
        response = client.post(
            "/auth/native/authorize",
            data={
                **_params(),
                "username": "user@example.test",
                "password": "wrong-password",
            },
            follow_redirects=False,
        )

    assert response.status_code == 401
    assert "Sign-in failed" in response.text
    assert "code=" not in response.text
