from __future__ import annotations

from base64 import urlsafe_b64decode, urlsafe_b64encode
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any
from uuid import UUID

import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from hermes_cloud.modules.cloud_api.domain import Principal
from hermes_cloud.modules.device.application import DevicePairingService
from hermes_cloud.modules.device.domain import PairingOffer

NOW = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)
SIGNING_SECRET = b"s" * 48
IDEMPOTENCY_KEY = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
TENANT_ID = UUID("33333333-3333-4333-8333-333333333333")
USER_ID = UUID("44444444-4444-4444-8444-444444444444")
PRINCIPAL = Principal(
    tenant_id=TENANT_ID,
    user_id=USER_ID,
    provider="basic",
    refresh_session_id=UUID("12121212-1212-4212-8212-121212121212"),
)


def _b64url(value: bytes) -> str:
    return urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


class _Repository:
    def __init__(self) -> None:
        self.offer: PairingOffer | None = None
        self.mutations: list[Any] = []

    def create_offer(self, offer: PairingOffer, *, mutation: Any) -> PairingOffer:
        if self.offer is not None:
            self.mutations.append(mutation)
            return self.offer
        self.offer = offer
        self.mutations.append(mutation)
        return offer


def test_create_offer_generates_exact_ttl_and_persists_only_digests() -> None:
    repository = _Repository()
    public_key = bytes(range(32))
    service = DevicePairingService(
        repository=repository,  # type: ignore[arg-type]
        signing_secret=SIGNING_SECRET,
        now=lambda: NOW,
    )

    response = service.create_offer(
        request={
            "connector_instance_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            "display_name": "Hermes Connector",
            "platform_family": "macos",
            "connector_version": "1.0.0",
            "key_algorithm": "Ed25519",
            "public_key": _b64url(public_key),
        },
        idempotency_key=IDEMPOTENCY_KEY,
    )

    assert response["ttl_seconds"] == 300
    assert response["revision"] == 1
    assert response["state"] == "pending"
    assert len(str(response["pairing_offer_secret"])) == 43
    assert str(response["credential_fingerprint"]).startswith("SHA256:")
    assert repository.offer is not None
    assert repository.offer.expires_at.timestamp() - NOW.timestamp() == 300
    assert repository.offer.public_key == public_key
    assert repository.offer.credential_fingerprint == sha256(public_key).hexdigest()
    assert (
        repository.offer.bootstrap_secret_digest
        == sha256(urlsafe_b64decode(f"{response['pairing_offer_secret']}=")).hexdigest()
    )
    assert not hasattr(repository.offer, "pairing_code")
    assert not hasattr(repository.offer, "bootstrap_secret")


def test_offer_generation_is_prf_replayable_without_plaintext_persistence() -> None:
    repository = _Repository()
    service = DevicePairingService(
        repository=repository,  # type: ignore[arg-type]
        signing_secret=SIGNING_SECRET,
        now=lambda: NOW,
    )
    request = {
        "connector_instance_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "display_name": "Hermes Connector",
        "platform_family": "macos",
        "connector_version": "1.0.0",
        "key_algorithm": "Ed25519",
        "public_key": _b64url(bytes(range(32))),
    }

    first = service.create_offer(request=request, idempotency_key=IDEMPOTENCY_KEY)
    second = service.create_offer(request=request, idempotency_key=IDEMPOTENCY_KEY)

    assert second == first


def test_offer_replay_keeps_original_expiry_when_clock_advances() -> None:
    repository = _Repository()
    clock = [NOW]
    service = DevicePairingService(
        repository=repository,  # type: ignore[arg-type]
        signing_secret=SIGNING_SECRET,
        now=lambda: clock[0],
    )
    request = {
        "connector_instance_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "display_name": "Hermes Connector",
        "platform_family": "macos",
        "connector_version": "1.0.0",
        "key_algorithm": "Ed25519",
        "public_key": _b64url(bytes(range(32))),
    }

    first = service.create_offer(request=request, idempotency_key=IDEMPOTENCY_KEY)
    clock[0] += timedelta(seconds=17)
    replay = service.create_offer(request=request, idempotency_key=IDEMPOTENCY_KEY)

    assert replay == first


def test_connector_jwt_binds_exact_authority_and_never_exceeds_one_hour() -> None:
    repository = _Repository()
    service = DevicePairingService(
        repository=repository,  # type: ignore[arg-type]
        signing_secret=SIGNING_SECRET,
        now=lambda: NOW,
        connector_token_ttl_seconds=900,
    )
    token = service.issue_connector_token(
        tenant_id=TENANT_ID,
        device_id=UUID("77777777-7777-4777-8777-777777777777"),
        credential_id=UUID("88888888-8888-4888-8888-888888888888"),
        agent_id=UUID("66666666-6666-4666-8666-666666666666"),
        scopes=("session.observe", "session.control.request"),
        token_id=UUID("99999999-9999-4999-8999-999999999999"),
    )
    claims = jwt.decode(
        token,
        SIGNING_SECRET,
        algorithms=["HS256"],
        options={"verify_exp": False, "verify_iat": False, "verify_nbf": False},
    )

    assert claims == {
        "tenant_id": str(TENANT_ID),
        "device_id": "77777777-7777-4777-8777-777777777777",
        "credential_id": "88888888-8888-4888-8888-888888888888",
        "agent_id": "66666666-6666-4666-8666-666666666666",
        "scopes": ["session.observe", "session.control.request"],
        "jti": "99999999-9999-4999-8999-999999999999",
        "iat": int(NOW.timestamp()),
        "nbf": int(NOW.timestamp()),
        "exp": int(NOW.timestamp()) + 900,
    }


def test_ed25519_verification_signs_the_decoded_domain_separated_payload() -> None:
    repository = _Repository()
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key().public_bytes_raw()
    service = DevicePairingService(
        repository=repository,  # type: ignore[arg-type]
        signing_secret=SIGNING_SECRET,
        now=lambda: NOW,
    )
    challenge_id = UUID("99999999-9999-4999-8999-999999999999")
    payload = service.signing_payload_for_challenge(challenge_id)
    signature = private_key.sign(payload)

    service.verify_device_proof(
        public_key=public_key,
        challenge_id=challenge_id,
        challenge_digest=sha256(payload).hexdigest(),
        signing_payload=_b64url(payload),
        signature=_b64url(signature),
    )

    assert payload.startswith(b"hermes-device-auth-v1\0")
