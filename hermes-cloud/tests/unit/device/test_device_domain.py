import inspect
from dataclasses import fields, replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from hermes_cloud.domain.persistence import PairingSessionState
from hermes_cloud.modules.device.domain import (
    ALLOWED_DEVICE_LIFECYCLE_TRANSITIONS,
    PAIRING_CHALLENGE_TTL,
    PAIRING_SESSION_TTL,
    DeviceCredential,
    DeviceCredentialStatus,
    DeviceLifecycle,
    DeviceLifecycleState,
    InvalidDeviceLifecycleTransition,
    PairingKeyMaterial,
    PairingOffer,
    PairingSession,
    fingerprint_ed25519_public_key,
    require_device_lifecycle_transition,
)

NOW = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)
TENANT_ID = UUID("11111111-1111-4111-8111-111111111111")
WORKSPACE_ID = UUID("22222222-2222-4222-8222-222222222222")
AGENT_ID = UUID("33333333-3333-4333-8333-333333333333")
DEVICE_ID = UUID("44444444-4444-4444-8444-444444444444")
PAIRING_SESSION_ID = UUID("55555555-5555-4555-8555-555555555555")
CREDENTIAL_ID = UUID("66666666-6666-4666-8666-666666666666")
PUBLIC_KEY = bytes(range(32))
FINGERPRINT = "630dcd2966c4336691125448bbb25b4ff412a49c732db2c8abc1b8581bd710dd"


def _pairing_session(**changes: object) -> PairingSession:
    values: dict[str, object] = {
        "tenant_id": TENANT_ID,
        "pairing_session_id": PAIRING_SESSION_ID,
        "workspace_id": WORKSPACE_ID,
        "agent_id": AGENT_ID,
        "device_id": None,
        "pairing_code_digest": "a" * 64,
        "state": PairingSessionState.PENDING,
        "failed_attempts": 0,
        "expires_at": NOW + PAIRING_SESSION_TTL,
        "claimed_at": None,
        "confirmed_at": None,
        "created_at": NOW,
    }
    values.update(changes)
    return PairingSession(**values)  # type: ignore[arg-type]


def _pairing_offer(**changes: object) -> PairingOffer:
    values: dict[str, object] = {
        "pairing_offer_id": PAIRING_SESSION_ID,
        "pairing_code_digest": "a" * 64,
        "bootstrap_secret_digest": "b" * 64,
        "algorithm": "ed25519",
        "public_key": PUBLIC_KEY,
        "credential_fingerprint": FINGERPRINT,
        "key_id": FINGERPRINT,
        "device_key": "connector-macos-01",
        "device_name": "Office Mac",
        "platform": "macos",
        "connector_version": "1.0.0",
        "state": PairingSessionState.PENDING,
        "revision": 0,
        "expires_at": NOW + PAIRING_SESSION_TTL,
        "claimed_at": None,
        "created_at": NOW,
    }
    values.update(changes)
    return PairingOffer(**values)  # type: ignore[arg-type]


def _key_material(**changes: object) -> PairingKeyMaterial:
    values: dict[str, object] = {
        "tenant_id": TENANT_ID,
        "pairing_session_id": PAIRING_SESSION_ID,
        "algorithm": "ed25519",
        "public_key": PUBLIC_KEY,
        "credential_fingerprint": FINGERPRINT,
        "key_id": FINGERPRINT,
        "device_key": "connector-macos-01",
        "device_name": "Office Mac",
        "platform": "macos",
        "scopes": ("session.observe",),
        "claim_id": UUID(int=7),
        "claimed_by_user_id": UUID(int=8),
        "challenge_id": None,
        "challenge_digest": None,
        "challenge_expires_at": None,
        "owner_confirmed_at": None,
        "confirmation_digest": None,
        "revision": 1,
        "created_at": NOW,
        "updated_at": NOW,
    }
    values.update(changes)
    return PairingKeyMaterial(**values)  # type: ignore[arg-type]


def test_pairing_session_is_five_minutes_and_failure_bounded() -> None:
    assert PAIRING_SESSION_TTL == timedelta(minutes=5)
    assert _pairing_session().state is PairingSessionState.PENDING

    with pytest.raises(ValueError, match="five minutes"):
        _pairing_session(expires_at=NOW + timedelta(minutes=5, microseconds=1))
    with pytest.raises(ValueError, match="exactly five minutes"):
        _pairing_offer(expires_at=NOW + timedelta(minutes=4, seconds=59))

    with pytest.raises(ValueError, match="legacy failed attempts"):
        _pairing_session(failed_attempts=1)


def test_unauthenticated_pairing_offer_cannot_assert_authorized_scope() -> None:
    offer = _pairing_offer()
    assert offer.state is PairingSessionState.PENDING
    assert "confirmed_at" not in {field.name for field in fields(PairingOffer)}
    assert {
        "tenant_id",
        "workspace_id",
        "agent_id",
        "device_id",
        "owner_user_id",
    }.isdisjoint(field.name for field in fields(PairingOffer))
    assert "failed_attempts" not in {field.name for field in fields(PairingOffer)}
    assert offer.bootstrap_secret_digest != offer.pairing_code_digest
    assert {"pairing_code", "bootstrap_secret"}.isdisjoint(
        field.name for field in fields(PairingOffer)
    )


def test_pairing_material_accepts_only_matching_ed25519_public_key_fingerprint() -> (
    None
):
    material = _key_material()

    assert fingerprint_ed25519_public_key(PUBLIC_KEY) == FINGERPRINT
    assert material.public_key == PUBLIC_KEY
    assert {"private_key", "pairing_code", "challenge"}.isdisjoint(
        field.name for field in fields(PairingKeyMaterial)
    )

    with pytest.raises(ValueError, match="32 bytes"):
        _key_material(public_key=b"not-an-ed25519-key")
    with pytest.raises(ValueError, match="fingerprint"):
        _key_material(credential_fingerprint="f" * 64)


def test_pairing_material_requires_complete_claim_and_challenge_binding() -> None:
    assert PAIRING_CHALLENGE_TTL == timedelta(seconds=60)
    with pytest.raises(ValueError, match="claim binding"):
        _key_material(claimed_by_user_id=None)

    with pytest.raises(ValueError, match="challenge binding"):
        _key_material(
            claim_id=UUID(int=7),
            claimed_by_user_id=UUID(int=8),
            challenge_digest="b" * 64,
        )

    material = _key_material(
        claim_id=UUID(int=7),
        claimed_by_user_id=UUID(int=8),
        challenge_id=UUID(int=9),
        challenge_digest="b" * 64,
        challenge_expires_at=NOW + timedelta(minutes=1),
        owner_confirmed_at=NOW,
        revision=2,
    )
    assert material.challenge_expires_at is not None

    with pytest.raises(ValueError, match="sixty seconds"):
        _key_material(
            challenge_id=UUID(int=9),
            challenge_digest="b" * 64,
            challenge_expires_at=NOW + timedelta(seconds=61),
            owner_confirmed_at=NOW,
            revision=2,
        )


def test_device_lifecycle_uses_a_separate_frozen_transition_graph() -> None:
    assert ALLOWED_DEVICE_LIFECYCLE_TRANSITIONS == {
        DeviceLifecycleState.PENDING: frozenset(
            {
                DeviceLifecycleState.ACTIVE,
                DeviceLifecycleState.REVOKED,
                DeviceLifecycleState.RETIRED,
            }
        ),
        DeviceLifecycleState.ACTIVE: frozenset(
            {
                DeviceLifecycleState.SUSPENDED,
                DeviceLifecycleState.REVOKED,
                DeviceLifecycleState.RETIRED,
            }
        ),
        DeviceLifecycleState.SUSPENDED: frozenset(
            {
                DeviceLifecycleState.ACTIVE,
                DeviceLifecycleState.REVOKED,
                DeviceLifecycleState.RETIRED,
            }
        ),
        DeviceLifecycleState.REVOKED: frozenset(),
        DeviceLifecycleState.RETIRED: frozenset(),
    }
    require_device_lifecycle_transition(
        DeviceLifecycleState.PENDING,
        DeviceLifecycleState.ACTIVE,
    )
    with pytest.raises(InvalidDeviceLifecycleTransition):
        require_device_lifecycle_transition(
            DeviceLifecycleState.REVOKED,
            DeviceLifecycleState.ACTIVE,
        )

    documentation = inspect.getdoc(require_device_lifecycle_transition)
    assert documentation is not None
    assert "pending --> active" in documentation
    assert "Allowed transitions" in documentation


def test_device_lifecycle_does_not_mix_connectivity_with_authorization_state() -> None:
    lifecycle = DeviceLifecycle(
        tenant_id=TENANT_ID,
        device_id=DEVICE_ID,
        workspace_id=WORKSPACE_ID,
        agent_id=AGENT_ID,
        state=DeviceLifecycleState.PENDING,
        revision=0,
        updated_at=NOW,
    )
    assert "offline" not in {state.value for state in DeviceLifecycleState}
    assert not hasattr(lifecycle, "connected")


def test_device_credential_contains_public_key_and_revocation_is_monotonic() -> None:
    credential = DeviceCredential(
        tenant_id=TENANT_ID,
        credential_id=CREDENTIAL_ID,
        device_id=DEVICE_ID,
        algorithm="ed25519",
        key_id=FINGERPRINT,
        public_key=PUBLIC_KEY,
        credential_fingerprint=FINGERPRINT,
        status=DeviceCredentialStatus.ACTIVE,
        issued_at=NOW,
        expires_at=NOW + timedelta(days=30),
        revoked_at=None,
    )

    revoked = credential.revoke(NOW + timedelta(minutes=1))
    assert revoked.status is DeviceCredentialStatus.REVOKED
    assert revoked.revoked_at == NOW + timedelta(minutes=1)
    assert revoked.revoke(NOW + timedelta(minutes=2)) == revoked

    with pytest.raises(ValueError, match="revoked"):
        replace(
            credential,
            status=DeviceCredentialStatus.REVOKED,
            revoked_at=None,
        )
