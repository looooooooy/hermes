from __future__ import annotations

from base64 import urlsafe_b64decode, urlsafe_b64encode
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from hermes_cloud.adapters.connector_auth import HmacJwtConnectorAuthenticator
from hermes_cloud.domain.connector_gateway import (
    ConnectorAuthorizationRevoked,
    ConnectorAuthorizationSuspended,
    ConnectorAuthorizationUnavailable,
)
from hermes_cloud.modules.cloud_api.adapters.fastapi import BusinessApiApplication
from hermes_cloud.modules.cloud_api.domain import Principal
from hermes_cloud.modules.device.application import DevicePairingService
from hermes_cloud.platform.postgres.models import (
    AgentModel,
    DeviceLifecycleModel,
    RoleModel,
    TenantModel,
    UserModel,
    WorkspaceMembershipModel,
    WorkspaceModel,
)
from hermes_cloud.platform.sqlite.engine import build_sqlite_engine
from hermes_cloud.platform.sqlite.runtime import (
    SQLiteOperationScopedPairingRepository,
)
from hermes_cloud.platform.sqlite.schema import build_sqlite_metadata

NOW = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=1)
SIGNING_SECRET = b"s" * 48
TENANT_ID = UUID("33333333-3333-4333-8333-333333333333")
USER_ID = UUID("44444444-4444-4444-8444-444444444444")
OTHER_USER_ID = UUID("45454545-4545-4545-8545-454545454545")
WORKSPACE_ID = UUID("55555555-5555-4555-8555-555555555555")
AGENT_ID = UUID("66666666-6666-4666-8666-666666666666")
OWNER = Principal(
    tenant_id=TENANT_ID,
    user_id=USER_ID,
    provider="basic",
    refresh_session_id=UUID("12121212-1212-4212-8212-121212121212"),
)


def _b64url(value: bytes) -> str:
    return urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


class _OwnerAuthentication:
    def authenticate_access(self, token: str) -> Principal:
        if token == "owner-access":
            return OWNER
        if token == "other-owner-access":
            return Principal(
                tenant_id=TENANT_ID,
                user_id=OTHER_USER_ID,
                provider="basic",
                refresh_session_id=UUID("13131313-1313-4313-8313-131313131313"),
            )
        raise ValueError


def _seed(factory: sessionmaker[Session]) -> None:
    role_id = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
    with factory.begin() as session:
        session.add(
            TenantModel(
                tenant_id=TENANT_ID,
                slug="pairing",
                display_name="Pairing",
                status="active",
                created_at=NOW,
            )
        )
        session.flush()
        session.add_all(
            (
                UserModel(
                    tenant_id=TENANT_ID,
                    user_id=USER_ID,
                    subject="pairing-owner",
                    display_name="Pairing Owner",
                    email=None,
                    status="active",
                    created_at=NOW,
                ),
                RoleModel(
                    tenant_id=TENANT_ID,
                    role_id=role_id,
                    role_key="owner",
                    display_name="Owner",
                    scope_type="workspace",
                    permissions=[],
                    status="active",
                    version=1,
                    created_at=NOW,
                ),
            )
        )
        session.flush()
        session.add(
            WorkspaceModel(
                tenant_id=TENANT_ID,
                workspace_id=WORKSPACE_ID,
                workspace_key="pairing",
                display_name="Pairing",
                status="active",
                created_by=USER_ID,
                created_at=NOW,
            )
        )
        session.flush()
        session.add_all(
            (
                WorkspaceMembershipModel(
                    tenant_id=TENANT_ID,
                    workspace_membership_id=UUID(
                        "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
                    ),
                    workspace_id=WORKSPACE_ID,
                    user_id=USER_ID,
                    role_id=role_id,
                    status="active",
                    joined_at=NOW,
                    revoked_at=None,
                ),
                AgentModel(
                    tenant_id=TENANT_ID,
                    agent_id=AGENT_ID,
                    workspace_id=WORKSPACE_ID,
                    agent_key="agent-pairing",
                    status="active",
                    last_seen_at=NOW,
                    created_at=NOW,
                ),
            )
        )


def test_asgi_pairing_to_repeated_token_and_revocation_closes_authority(
    tmp_path: Path,
) -> None:
    engine = build_sqlite_engine(
        f"sqlite+pysqlite:///{tmp_path / 'pairing.sqlite3'}",
        allow_missing=True,
    )
    build_sqlite_metadata().create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    _seed(factory)
    repository = SQLiteOperationScopedPairingRepository(factory)
    clock = [NOW]
    service = DevicePairingService(
        repository=repository,
        signing_secret=SIGNING_SECRET,
        now=lambda: clock[0],
    )
    application = BusinessApiApplication(
        service=_OwnerAuthentication(),  # type: ignore[arg-type]
        pairing_service=service,
    )
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key().public_bytes_raw()
    owner_headers = {"Authorization": "Bearer owner-access"}

    try:
        with TestClient(application) as client:
            create = client.post(
                "/api/device-pairing/offers",
                headers={"Idempotency-Key": "01010101-0101-4101-8101-010101010101"},
                json={
                    "connector_instance_id": ("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
                    "display_name": "Hermes Connector",
                    "platform_family": "macos",
                    "connector_version": "1.0.0",
                    "key_algorithm": "Ed25519",
                    "public_key": _b64url(public_key),
                },
            )
            assert create.status_code == 201, create.text
            offer = create.json()

            claim = client.post(
                "/api/device-pairing/claims",
                headers={
                    **owner_headers,
                    "Idempotency-Key": "02020202-0202-4202-8202-020202020202",
                },
                json={
                    "pairing_code": offer["pairing_code"],
                    "workspace_id": str(WORKSPACE_ID),
                    "agent_id": str(AGENT_ID),
                    "device_display_name": "Office Mac",
                    "scopes": [
                        "session.observe",
                        "session.control.request",
                    ],
                    "expected_revision": 1,
                },
            )
            assert claim.status_code == 200, claim.text
            owner_view = claim.json()
            owner_status_path = (
                f"/api/device-pairing/sessions/{owner_view['pairing_session_id']}"
            )
            claimed_status = client.get(
                owner_status_path,
                headers=owner_headers,
            )
            assert claimed_status.status_code == 200, claimed_status.text
            assert claimed_status.headers["cache-control"] == "no-store"
            assert claimed_status.json() == owner_view
            assert claimed_status.json()["revision"] == 2
            assert claimed_status.json()["device_revision"] == 1
            serialized_owner_status = claimed_status.text
            assert offer["pairing_code"] not in serialized_owner_status
            assert offer["pairing_offer_secret"] not in serialized_owner_status
            assert _b64url(public_key) not in serialized_owner_status
            other_owner = client.get(
                owner_status_path,
                headers={"Authorization": "Bearer other-owner-access"},
            )
            unknown = client.get(
                "/api/device-pairing/sessions/14141414-1414-4414-8414-141414141414",
                headers=owner_headers,
            )
            assert other_owner.status_code == unknown.status_code == 404
            assert (
                other_owner.json()
                == unknown.json()
                == {
                    "code": "PAIRING_NOT_FOUND",
                    "reason": "pairing resource not found",
                }
            )

            confirm = client.post(
                (
                    "/api/device-pairing/sessions/"
                    f"{owner_view['pairing_session_id']}/confirm"
                ),
                headers={
                    **owner_headers,
                    "Idempotency-Key": "03030303-0303-4303-8303-030303030303",
                },
                json={
                    "credential_fingerprint": offer["credential_fingerprint"],
                    "expected_revision": owner_view["revision"],
                },
            )
            assert confirm.status_code == 200, confirm.text
            confirmed = confirm.json()
            confirmed_status = client.get(
                owner_status_path,
                headers=owner_headers,
            )
            assert confirmed_status.status_code == 200, confirmed_status.text
            assert confirmed_status.json() == confirmed
            assert confirmed_status.json()["revision"] == 3
            assert confirmed_status.json()["device_revision"] == 1

            status = client.get(
                f"/api/device-pairing/offers/{offer['pairing_offer_id']}",
                headers={"X-Hermes-Pairing-Offer": offer["pairing_offer_secret"]},
            )
            assert status.status_code == 200, status.text
            challenge = status.json()["challenge"]
            payload = urlsafe_b64decode(
                f"{challenge['signing_payload']}{'=' * (-len(challenge['signing_payload']) % 4)}"
            )
            signature = _b64url(private_key.sign(payload))

            proof_path = (
                f"/api/device-pairing/sessions/{owner_view['pairing_session_id']}/proof"
            )
            proof_headers = {
                "X-Hermes-Pairing-Offer": offer["pairing_offer_secret"],
                "Idempotency-Key": "04040404-0404-4404-8404-040404040404",
            }
            proof_body = {
                "challenge_id": challenge["challenge_id"],
                "signing_payload": challenge["signing_payload"],
                "signature_algorithm": "Ed25519",
                "signature": signature,
            }
            proof = client.post(
                proof_path,
                headers=proof_headers,
                json=proof_body,
            )
            assert proof.status_code == 200, proof.text
            initial_token = proof.json()
            active_status = client.get(
                owner_status_path,
                headers=owner_headers,
            )
            assert active_status.status_code == 200, active_status.text
            assert active_status.json()["state"] == "confirmed"
            assert active_status.json()["activation_state"] == "active"
            assert active_status.json()["revision"] == 4
            assert active_status.json()["device_revision"] == 2
            clock[0] = NOW + timedelta(seconds=301)
            active_after_offer_expiry = client.get(
                owner_status_path,
                headers=owner_headers,
            )
            assert active_after_offer_expiry.status_code == 200
            assert active_after_offer_expiry.json()["state"] == "confirmed"
            assert active_after_offer_expiry.json()["activation_state"] == "active"

            confirm_replay = client.post(
                (
                    "/api/device-pairing/sessions/"
                    f"{owner_view['pairing_session_id']}/confirm"
                ),
                headers={
                    **owner_headers,
                    "Idempotency-Key": "03030303-0303-4303-8303-030303030303",
                },
                json={
                    "credential_fingerprint": offer["credential_fingerprint"],
                    "expected_revision": owner_view["revision"],
                },
            )
            assert confirm_replay.status_code == 200, confirm_replay.text
            assert confirm_replay.json() == confirmed
            confirm_conflict = client.post(
                (
                    "/api/device-pairing/sessions/"
                    f"{owner_view['pairing_session_id']}/confirm"
                ),
                headers={
                    **owner_headers,
                    "Idempotency-Key": "03030303-0303-4303-8303-030303030303",
                },
                json={
                    "credential_fingerprint": offer["credential_fingerprint"],
                    "expected_revision": owner_view["revision"] + 1,
                },
            )
            assert confirm_conflict.status_code == 409, confirm_conflict.text
            assert confirm_conflict.json()["code"] == "IDEMPOTENCY_CONFLICT"

            proof_replay = client.post(
                proof_path,
                headers=proof_headers,
                json=proof_body,
            )
            assert proof_replay.status_code == 200, proof_replay.text
            assert proof_replay.json() == initial_token
            proof_conflict = client.post(
                proof_path,
                headers=proof_headers,
                json={
                    **proof_body,
                    "signature": _b64url(bytes(64)),
                },
            )
            assert proof_conflict.status_code == 409, proof_conflict.text
            assert proof_conflict.json()["code"] == "IDEMPOTENCY_CONFLICT"
            binding = initial_token["binding"]
            clock[0] = NOW + timedelta(seconds=17)

            later_challenge = client.post(
                "/api/device-auth/challenges",
                headers={"Idempotency-Key": "05050505-0505-4505-8505-050505050505"},
                json={
                    "device_id": binding["device_id"],
                    "credential_id": binding["credential_id"],
                },
            )
            assert later_challenge.status_code == 201, later_challenge.text
            later = later_challenge.json()
            later_payload = urlsafe_b64decode(
                f"{later['signing_payload']}{'=' * (-len(later['signing_payload']) % 4)}"
            )
            token_body = {
                "device_id": binding["device_id"],
                "credential_id": binding["credential_id"],
                "challenge_id": later["challenge_id"],
                "signing_payload": later["signing_payload"],
                "signature_algorithm": "Ed25519",
                "signature": _b64url(private_key.sign(later_payload)),
            }
            token_headers = {"Idempotency-Key": "06060606-0606-4606-8606-060606060606"}
            token = client.post(
                "/api/device-auth/tokens",
                headers=token_headers,
                json=token_body,
            )
            assert token.status_code == 200, token.text
            token_response = token.json()
            clock[0] += timedelta(seconds=17)
            token_replay = client.post(
                "/api/device-auth/tokens",
                headers=token_headers,
                json=token_body,
            )
            assert token_replay.status_code == 200, token_replay.text
            assert token_replay.json() == token_response
            connector_token = token_response["access_token"]

            authenticator = HmacJwtConnectorAuthenticator(
                SIGNING_SECRET,
                utc_now=lambda: clock[0],
                device_authority=repository,
            )
            identity = __import__("asyncio").run(
                authenticator.authenticate(connector_token)
            )
            assert identity.agent_id == str(AGENT_ID)

            replayed_challenge = client.post(
                "/api/device-auth/tokens",
                headers={"Idempotency-Key": "08080808-0808-4808-8808-080808080808"},
                json=token_body,
            )
            assert replayed_challenge.status_code == 409
            assert replayed_challenge.json()["code"] == "CHALLENGE_REPLAYED"

            expiring_challenge = client.post(
                "/api/device-auth/challenges",
                headers={"Idempotency-Key": "09090909-0909-4909-8909-090909090909"},
                json={
                    "device_id": binding["device_id"],
                    "credential_id": binding["credential_id"],
                },
            )
            assert expiring_challenge.status_code == 201, expiring_challenge.text
            expiring = expiring_challenge.json()
            expiring_payload = urlsafe_b64decode(
                f"{expiring['signing_payload']}"
                f"{'=' * (-len(expiring['signing_payload']) % 4)}"
            )
            clock[0] += timedelta(seconds=61)
            expired_challenge = client.post(
                "/api/device-auth/tokens",
                headers={"Idempotency-Key": "10101010-1010-4010-8010-101010101010"},
                json={
                    "device_id": binding["device_id"],
                    "credential_id": binding["credential_id"],
                    "challenge_id": expiring["challenge_id"],
                    "signing_payload": expiring["signing_payload"],
                    "signature_algorithm": "Ed25519",
                    "signature": _b64url(private_key.sign(expiring_payload)),
                },
            )
            assert expired_challenge.status_code == 410
            assert expired_challenge.json()["code"] == "CHALLENGE_EXPIRED"

            with factory.begin() as session:
                lifecycle = session.get(
                    DeviceLifecycleModel,
                    (TENANT_ID, UUID(binding["device_id"])),
                )
                assert lifecycle is not None
                lifecycle.state = "suspended"
            with pytest.raises(ConnectorAuthorizationSuspended):
                __import__("asyncio").run(authenticator.revalidate(identity))
            with factory.begin() as session:
                lifecycle = session.get(
                    DeviceLifecycleModel,
                    (TENANT_ID, UUID(binding["device_id"])),
                )
                assert lifecycle is not None
                lifecycle.state = "active"
                agent = session.get(AgentModel, (TENANT_ID, AGENT_ID))
                assert agent is not None
                agent.status = "disabled"
            with pytest.raises(ConnectorAuthorizationUnavailable):
                __import__("asyncio").run(authenticator.revalidate(identity))
            with factory.begin() as session:
                agent = session.get(AgentModel, (TENANT_ID, AGENT_ID))
                assert agent is not None
                agent.status = "active"

            revoke = client.post(
                f"/api/devices/{binding['device_id']}/revoke",
                headers={
                    **owner_headers,
                    "Idempotency-Key": "07070707-0707-4707-8707-070707070707",
                },
                json={
                    "reason": "device_lost",
                    "expected_revision": 2,
                },
            )
            assert revoke.status_code == 200, revoke.text
            revoked_status = client.get(
                owner_status_path,
                headers=owner_headers,
            )
            assert revoked_status.status_code == 200, revoked_status.text
            assert revoked_status.json()["state"] == "confirmed"
            assert revoked_status.json()["activation_state"] == "blocked"
            assert revoked_status.json()["revision"] == 4
            assert revoked_status.json()["device_revision"] == 3
            clock[0] = NOW + timedelta(seconds=301)
            revoked_after_offer_expiry = client.get(
                owner_status_path,
                headers=owner_headers,
            )
            assert revoked_after_offer_expiry.status_code == 200
            assert revoked_after_offer_expiry.json()["state"] == "confirmed"
            assert revoked_after_offer_expiry.json()["activation_state"] == "blocked"

            with pytest.raises(ConnectorAuthorizationRevoked):
                __import__("asyncio").run(authenticator.revalidate(identity))

            expiring_offer = client.post(
                "/api/device-pairing/offers",
                headers={"Idempotency-Key": ("15151515-1515-4515-8515-151515151515")},
                json={
                    "connector_instance_id": ("16161616-1616-4616-8616-161616161616"),
                    "display_name": "Expiring Connector",
                    "platform_family": "linux",
                    "connector_version": "1.0.0",
                    "key_algorithm": "Ed25519",
                    "public_key": _b64url(
                        Ed25519PrivateKey.generate().public_key().public_bytes_raw()
                    ),
                },
            )
            assert expiring_offer.status_code == 201, expiring_offer.text
            expiring_offer_body = expiring_offer.json()
            expiring_claim = client.post(
                "/api/device-pairing/claims",
                headers={
                    **owner_headers,
                    "Idempotency-Key": ("17171717-1717-4717-8717-171717171717"),
                },
                json={
                    "pairing_code": expiring_offer_body["pairing_code"],
                    "workspace_id": str(WORKSPACE_ID),
                    "agent_id": str(AGENT_ID),
                    "device_display_name": "Expiring Connector",
                    "scopes": ["session.observe"],
                    "expected_revision": 1,
                },
            )
            assert expiring_claim.status_code == 200, expiring_claim.text
            expiring_owner_view = expiring_claim.json()
            clock[0] += timedelta(seconds=301)
            expired_status = client.get(
                "/api/device-pairing/sessions/"
                f"{expiring_owner_view['pairing_session_id']}",
                headers=owner_headers,
            )
            assert expired_status.status_code == 200, expired_status.text
            assert expired_status.headers["cache-control"] == "no-store"
            assert expired_status.json()["state"] == "expired"
            assert expired_status.json()["activation_state"] == "blocked"
    finally:
        engine.dispose()
