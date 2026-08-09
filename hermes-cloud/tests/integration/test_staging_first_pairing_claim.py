from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

from fastapi.testclient import TestClient

from deploy.test_server.scripts.migrate_sqlite import main as migrate_sqlite
from deploy.test_server.scripts.seed_test_data import main as seed_test_data
from hermes_cloud.adapters.business_api_runtime import (
    build_production_business_api_application,
)

TENANT_ID = UUID("a495873f-cc49-5e21-b9fd-a581e3159ec8")
VERIFIER = "v" * 43
CHALLENGE = base64.urlsafe_b64encode(
    hashlib.sha256(VERIFIER.encode("ascii")).digest()
).rstrip(b"=").decode("ascii")
STATE = "staging-first-claim-state-0123456789"
REDIRECT = "http://127.0.0.1:55407/oauth/callback"


def _write_private(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o600)
    return path


def _environment(tmp_path: Path) -> dict[str, str]:
    database = tmp_path / "hermes-cloud.db"
    dsn = _write_private(
        tmp_path / "database.dsn",
        f"sqlite+pysqlite:///{database}",
    )
    initial_password = _write_private(
        tmp_path / "initial-password",
        "staging-integration-password",
    )
    signing_secret = _write_private(tmp_path / "business-signing", "s" * 48)
    connector_secret = _write_private(tmp_path / "connector-signing", "c" * 48)
    observer_keyring = _write_private(
        tmp_path / "observer-keyring.json",
        json.dumps(
            {
                "version": 1,
                "tenants": {
                    str(TENANT_ID): {
                        "current": "v1",
                        "keys": {
                            "v1": base64.b64encode(b"k" * 32).decode("ascii"),
                        },
                    }
                },
            },
            separators=(",", ":"),
        ),
    )
    return {
        "HERMES_MIGRATION_DSN_FILE": str(dsn),
        "HERMES_BOOTSTRAP_DSN_FILE": str(dsn),
        "HERMES_RUNTIME_DSN_FILE": str(dsn),
        "HERMES_INITIAL_USER_PASSWORD_FILE": str(initial_password),
        "HERMES_SIGNING_SECRET_FILE": str(signing_secret),
        "HERMES_CONNECTOR_SIGNING_SECRET_FILE": str(connector_secret),
        "HERMES_OBSERVER_KEYRING_FILE": str(observer_keyring),
        "HERMES_SEED_TENANT_SLUG": "android-test",
        "HERMES_SEED_TENANT_DISPLAY_NAME": "Android Test",
        "HERMES_SEED_USERNAME": "android-user",
        "HERMES_SEED_USER_DISPLAY_NAME": "Android User",
        "HERMES_SEED_WORKSPACE_KEY": "android",
        "HERMES_SEED_WORKSPACE_DISPLAY_NAME": "Android",
        "HERMES_SEED_OWNER_CONTROL_ENABLED": "true",
        "HERMES_SEED_AGENT_KEY": "android-agent",
        "HERMES_SEED_DEVICE_KEY": "android-device",
    }


def _access_token(client: TestClient) -> str:
    authorization = client.post(
        "/auth/native/authorize",
        params={
            "code_challenge": CHALLENGE,
            "code_challenge_method": "S256",
            "redirect_uri": REDIRECT,
            "state": STATE,
            "provider": "basic",
        },
        data={
            "code_challenge": CHALLENGE,
            "code_challenge_method": "S256",
            "redirect_uri": REDIRECT,
            "state": STATE,
            "provider": "basic",
            "username": "android-user",
            "password": "staging-integration-password",
        },
        follow_redirects=False,
    )
    assert authorization.status_code == 303
    code = parse_qs(urlsplit(authorization.headers["location"]).query)["code"][0]
    exchange = client.post(
        "/auth/native/token",
        json={"code": code, "code_verifier": VERIFIER},
    )
    assert exchange.status_code == 200
    token = exchange.json()["access_token"]
    assert isinstance(token, str) and token
    return token


def test_first_pairing_claim_after_real_staging_migration_and_seed(tmp_path: Path) -> None:
    environment = _environment(tmp_path)
    migrate_sqlite(["--apply"], environment=environment)
    seed_test_data(["--apply"], environment=environment)

    application = build_production_business_api_application(environment=environment)
    with TestClient(application, base_url="https://api.example.test") as client:
        access_token = _access_token(client)
        authorization = {"Authorization": f"Bearer {access_token}"}

        context = client.get(
            "/api/onboarding/pairing-context",
            headers=authorization,
        )
        assert context.status_code == 200
        targets = context.json()["targets"]
        assert len(targets) == 1
        target = targets[0]

        public_key = base64.urlsafe_b64encode(bytes(range(32))).rstrip(b"=").decode("ascii")
        offer = client.post(
            "/api/device-pairing/offers",
            headers={"Idempotency-Key": "11111111-1111-4111-8111-111111111111"},
            json={
                "connector_instance_id": "22222222-2222-4222-8222-222222222222",
                "display_name": "Hermes workstation",
                "platform_family": "macos",
                "connector_version": "0.1.0",
                "key_algorithm": "Ed25519",
                "public_key": public_key,
            },
        )
        assert offer.status_code == 201
        offer_body = offer.json()
        assert offer_body["state"] == "pending"
        assert offer_body["revision"] == 1

        claim = client.post(
            "/api/device-pairing/claims",
            headers={
                **authorization,
                "Idempotency-Key": "33333333-3333-4333-8333-333333333333",
            },
            json={
                "pairing_code": offer_body["pairing_code"],
                "workspace_id": target["workspace_id"],
                "agent_id": target["agent_id"],
                "device_display_name": "Hermes workstation",
                "scopes": ["session.observe", "session.control.request"],
                "expected_revision": offer_body["revision"],
            },
        )

    assert claim.status_code == 200, claim.text
    payload = claim.json()
    assert payload["state"] == "claimed"
    assert payload["activation_state"] == "waiting_owner_confirmation"
    assert payload["binding"]["workspace_id"] == target["workspace_id"]
    assert payload["binding"]["agent_id"] == target["agent_id"]
