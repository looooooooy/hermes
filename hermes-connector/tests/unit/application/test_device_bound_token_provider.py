from __future__ import annotations

import base64
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from hermes_connector.adapters.platform.macos.credentials import (
    CloudCredentialUnavailable,
)
from hermes_connector.application.device_bound_token_provider import (
    DeviceAuthorizationUnavailable,
    DeviceBoundCloudTokenProvider,
)
from hermes_connector.domain.pairing import (
    ConnectorToken,
    DeviceAuthenticationChallenge,
    DeviceAuthenticationTokenRequest,
    DeviceBinding,
    DeviceChallengeRequest,
    PairedProjection,
)
from hermes_connector.ports.pairing import DevicePairingCloudError

NOW = datetime(2026, 7, 31, 11, 0, 0, tzinfo=UTC)
TENANT_ID = UUID("66666666-6666-4666-8666-666666666666")
DEVICE_ID = UUID("77777777-7777-4777-8777-777777777777")
CREDENTIAL_ID = UUID("88888888-8888-4888-8888-888888888888")
AGENT_ID = UUID("99999999-9999-4999-8999-999999999999")
CHALLENGE_ID = UUID("55555555-5555-4555-8555-555555555555")
IDEMPOTENCY_KEY = UUID("33333333-3333-4333-8333-333333333333")


def _projection(*, expires_at: datetime, state: str = "active") -> PairedProjection:
    return PairedProjection(
        tenant_id=TENANT_ID,
        device_id=DEVICE_ID,
        credential_id=CREDENTIAL_ID,
        agent_id=AGENT_ID,
        scopes=("session.observe",),
        key_handle="hermes-device-key:v1:fingerprint",
        credential_fingerprint="SHA256:" + "B" * 43,
        token_expires_at=expires_at,
        lifecycle_state=state,
    )


class _ProjectionStore:
    def __init__(self, projection: PairedProjection) -> None:
        self.projection = projection
        self.saved: list[PairedProjection] = []

    async def load(self) -> PairedProjection | None:
        return self.projection

    async def save(self, projection: PairedProjection) -> None:
        self.projection = projection
        self.saved.append(projection)


class _TokenStore:
    def __init__(self, token: str | None) -> None:
        self.token = token
        self.stored: list[str] = []
        self.cleared = 0

    async def access_token(self) -> str:
        if self.token is None:
            raise CloudCredentialUnavailable("cloud credential is unavailable")
        return self.token

    async def store_access_token(self, token: str) -> None:
        self.token = token
        self.stored.append(token)

    async def clear_access_token(self) -> None:
        self.token = None
        self.cleared += 1


class _Identity:
    def __init__(self, signature: bytes = b"\x02" * 64) -> None:
        self.sign_calls: list[tuple[str, bytes]] = []
        self.signature = signature

    async def sign_challenge(self, key_handle: str, challenge: bytes) -> bytes:
        self.sign_calls.append((key_handle, challenge))
        return self.signature


class _Cloud:
    def __init__(self) -> None:
        self.challenge: DeviceAuthenticationChallenge | None = None
        self.token: ConnectorToken | None = None
        self.challenge_calls: list[tuple[DeviceChallengeRequest, UUID]] = []
        self.token_calls: list[tuple[DeviceAuthenticationTokenRequest, UUID]] = []
        self.challenge_error: DevicePairingCloudError | None = None

    async def create_device_challenge(
        self,
        request: DeviceChallengeRequest,
        *,
        idempotency_key: UUID,
    ) -> DeviceAuthenticationChallenge:
        self.challenge_calls.append((request, idempotency_key))
        if self.challenge_error is not None:
            raise self.challenge_error
        assert self.challenge is not None
        return self.challenge

    async def issue_device_token(
        self,
        request: DeviceAuthenticationTokenRequest,
        *,
        idempotency_key: UUID,
    ) -> ConnectorToken:
        self.token_calls.append((request, idempotency_key))
        assert self.token is not None
        return self.token


@pytest.mark.asyncio
async def test_fresh_cached_token_is_returned_without_cloud_round_trip() -> None:
    provider = DeviceBoundCloudTokenProvider(
        projection_store=_ProjectionStore(
            _projection(expires_at=NOW + timedelta(minutes=5))
        ),
        token_store=_TokenStore("T" * 64),
        identity=_Identity(),
        cloud=_Cloud(),
        now=lambda: NOW,
        refresh_before=timedelta(seconds=30),
        new_idempotency_key=lambda: IDEMPOTENCY_KEY,
    )

    assert await provider.access_token() == "T" * 64
    assert provider.state == "ACTIVE"


@pytest.mark.asyncio
async def test_missing_token_renews_with_device_challenge_and_exact_binding() -> None:
    decoded_payload = b"hermes-device-auth-v1\x00" + b"r" * 40
    signing_payload = base64.urlsafe_b64encode(decoded_payload).rstrip(b"=").decode()
    binding = DeviceBinding(
        tenant_id=TENANT_ID,
        device_id=DEVICE_ID,
        credential_id=CREDENTIAL_ID,
        agent_id=AGENT_ID,
        scopes=("session.observe",),
    )
    cloud = _Cloud()
    cloud.challenge = DeviceAuthenticationChallenge(
        challenge_id=CHALLENGE_ID,
        signing_payload=signing_payload,
        ttl_seconds=60,
        expires_at=NOW + timedelta(seconds=60),
    )
    cloud.token = ConnectorToken(
        access_token="N" * 64,
        token_type="Bearer",
        ttl_seconds=300,
        expires_at=NOW + timedelta(seconds=300),
        binding=binding,
    )
    identity = _Identity()
    projection_store = _ProjectionStore(
        _projection(expires_at=NOW + timedelta(minutes=5))
    )
    token_store = _TokenStore(None)
    provider = DeviceBoundCloudTokenProvider(
        projection_store=projection_store,
        token_store=token_store,
        identity=identity,
        cloud=cloud,
        now=lambda: NOW,
        refresh_before=timedelta(seconds=30),
        new_idempotency_key=lambda: IDEMPOTENCY_KEY,
    )

    token = await provider.access_token()

    assert token == "N" * 64
    assert cloud.challenge_calls == [
        (
            DeviceChallengeRequest(
                device_id=DEVICE_ID,
                credential_id=CREDENTIAL_ID,
            ),
            IDEMPOTENCY_KEY,
        )
    ]
    assert identity.sign_calls == [
        ("hermes-device-key:v1:fingerprint", decoded_payload)
    ]
    assert cloud.token_calls == [
        (
            DeviceAuthenticationTokenRequest(
                device_id=DEVICE_ID,
                credential_id=CREDENTIAL_ID,
                challenge_id=CHALLENGE_ID,
                signing_payload=signing_payload,
                signature_algorithm="Ed25519",
                signature=base64.urlsafe_b64encode(b"\x02" * 64).rstrip(b"=").decode(),
            ),
            IDEMPOTENCY_KEY,
        )
    ]
    assert token_store.stored == ["N" * 64]
    assert projection_store.projection.token_expires_at == NOW + timedelta(seconds=300)
    assert projection_store.projection.lifecycle_state == "active"


@pytest.mark.asyncio
async def test_generic_device_auth_unavailable_is_blocked_not_revoked() -> None:
    cloud = _Cloud()
    cloud.challenge_error = DevicePairingCloudError(
        code="DEVICE_AUTH_UNAVAILABLE",
        status_code=403,
    )
    projection_store = _ProjectionStore(_projection(expires_at=NOW))
    token_store = _TokenStore("O" * 64)
    provider = DeviceBoundCloudTokenProvider(
        projection_store=projection_store,
        token_store=token_store,
        identity=_Identity(),
        cloud=cloud,
        now=lambda: NOW,
        refresh_before=timedelta(seconds=30),
        new_idempotency_key=lambda: IDEMPOTENCY_KEY,
    )

    with pytest.raises(DeviceAuthorizationUnavailable):
        await provider.access_token()

    assert token_store.cleared == 1
    assert projection_store.projection.lifecycle_state == "auth_blocked"
    assert provider.state == "AUTH_BLOCKED"
    assert provider.state != "REVOKED"

    with pytest.raises(DeviceAuthorizationUnavailable):
        await provider.access_token()

    assert len(cloud.challenge_calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("projection_state", "runtime_state"),
    (
        ("auth_blocked", "AUTH_BLOCKED"),
        ("suspended", "SUSPENDED"),
        ("revoked", "REVOKED"),
    ),
)
async def test_provider_restores_non_active_state_from_projection(
    projection_state: str,
    runtime_state: str,
) -> None:
    projection = replace(
        _projection(expires_at=NOW + timedelta(seconds=300)),
        lifecycle_state=projection_state,
    )
    cloud = _Cloud()
    provider = DeviceBoundCloudTokenProvider(
        projection_store=_ProjectionStore(projection),
        token_store=_TokenStore("T" * 64),
        identity=_Identity(),
        cloud=cloud,
        now=lambda: NOW,
        refresh_before=timedelta(seconds=30),
        new_idempotency_key=lambda: IDEMPOTENCY_KEY,
    )

    with pytest.raises(DeviceAuthorizationUnavailable):
        await provider.access_token()

    assert provider.state == runtime_state
    assert cloud.challenge_calls == []


@pytest.mark.parametrize(
    ("projection_state", "runtime_state"),
    (
        ("active", "ACTIVE"),
        ("auth_blocked", "AUTH_BLOCKED"),
        ("suspended", "SUSPENDED"),
        ("revoked", "REVOKED"),
    ),
)
def test_provider_bootstrap_restores_already_loaded_projection_state(
    projection_state: str,
    runtime_state: str,
) -> None:
    provider = DeviceBoundCloudTokenProvider(
        projection_store=_ProjectionStore(_projection(expires_at=NOW)),
        token_store=_TokenStore(None),
        identity=_Identity(),
        cloud=_Cloud(),
        now=lambda: NOW,
        refresh_before=timedelta(seconds=30),
        new_idempotency_key=lambda: IDEMPOTENCY_KEY,
        initial_lifecycle_state=projection_state,
    )

    assert provider.state == runtime_state


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error_code",
    (
        "DEVICE_REVOKED",
        "DEVICE_SUSPENDED",
    ),
)
async def test_http_error_cannot_infer_terminal_lifecycle_state(
    error_code: str,
) -> None:
    cloud = _Cloud()
    cloud.challenge_error = DevicePairingCloudError(
        code=error_code,
        status_code=403,
    )
    projection_store = _ProjectionStore(_projection(expires_at=NOW))
    token_store = _TokenStore("O" * 64)
    provider = DeviceBoundCloudTokenProvider(
        projection_store=projection_store,
        token_store=token_store,
        identity=_Identity(),
        cloud=cloud,
        now=lambda: NOW,
        refresh_before=timedelta(seconds=30),
        new_idempotency_key=lambda: IDEMPOTENCY_KEY,
    )

    with pytest.raises(DevicePairingCloudError) as raised:
        await provider.access_token()

    assert raised.value.code == error_code
    assert token_store.cleared == 0
    assert projection_store.projection.lifecycle_state == "active"
    assert provider.state == "ACTIVE"


@pytest.mark.asyncio
async def test_invalid_ed25519_signature_is_rejected_before_token_request() -> None:
    decoded_payload = b"hermes-device-auth-v1\x00" + b"r" * 40
    cloud = _Cloud()
    cloud.challenge = DeviceAuthenticationChallenge(
        challenge_id=CHALLENGE_ID,
        signing_payload=base64.urlsafe_b64encode(decoded_payload).rstrip(b"=").decode(),
        ttl_seconds=60,
        expires_at=NOW + timedelta(seconds=60),
    )
    provider = DeviceBoundCloudTokenProvider(
        projection_store=_ProjectionStore(_projection(expires_at=NOW)),
        token_store=_TokenStore(None),
        identity=_Identity(b"x" * 63),
        cloud=cloud,
        now=lambda: NOW,
        refresh_before=timedelta(seconds=30),
        new_idempotency_key=lambda: IDEMPOTENCY_KEY,
    )

    with pytest.raises(ValueError, match="signature"):
        await provider.access_token()

    assert cloud.token_calls == []


@pytest.mark.asyncio
async def test_explicit_wss_revoked_signal_persists_revoked_projection() -> None:
    projection_store = _ProjectionStore(
        _projection(expires_at=NOW + timedelta(seconds=300))
    )
    token_store = _TokenStore("T" * 64)
    provider = DeviceBoundCloudTokenProvider(
        projection_store=projection_store,
        token_store=token_store,
        identity=_Identity(),
        cloud=_Cloud(),
        now=lambda: NOW,
        refresh_before=timedelta(seconds=30),
        new_idempotency_key=lambda: IDEMPOTENCY_KEY,
    )

    await provider.apply_lifecycle_signal("revoked")

    assert token_store.cleared == 1
    assert projection_store.projection.lifecycle_state == "revoked"
    assert provider.state == "REVOKED"


@pytest.mark.asyncio
async def test_renewal_clears_new_token_when_projection_update_fails() -> None:
    decoded_payload = b"hermes-device-auth-v1\x00" + b"r" * 40
    signing_payload = base64.urlsafe_b64encode(decoded_payload).rstrip(b"=").decode()
    binding = DeviceBinding(
        tenant_id=TENANT_ID,
        device_id=DEVICE_ID,
        credential_id=CREDENTIAL_ID,
        agent_id=AGENT_ID,
        scopes=("session.observe",),
    )
    cloud = _Cloud()
    cloud.challenge = DeviceAuthenticationChallenge(
        challenge_id=CHALLENGE_ID,
        signing_payload=signing_payload,
        ttl_seconds=60,
        expires_at=NOW + timedelta(seconds=60),
    )
    cloud.token = ConnectorToken(
        access_token="N" * 64,
        token_type="Bearer",
        ttl_seconds=300,
        expires_at=NOW + timedelta(seconds=300),
        binding=binding,
    )

    class _FailingProjectionStore(_ProjectionStore):
        async def save(self, projection: PairedProjection) -> None:
            raise OSError("must-never-appear")

    token_store = _TokenStore(None)
    provider = DeviceBoundCloudTokenProvider(
        projection_store=_FailingProjectionStore(_projection(expires_at=NOW)),
        token_store=token_store,
        identity=_Identity(),
        cloud=cloud,
        now=lambda: NOW,
        refresh_before=timedelta(seconds=30),
        new_idempotency_key=lambda: IDEMPOTENCY_KEY,
    )

    with pytest.raises(OSError):
        await provider.access_token()

    assert token_store.token is None
    assert token_store.cleared == 1
