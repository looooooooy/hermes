from __future__ import annotations

import base64
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from hermes_connector.application.file_credential_migration import (
    FileCredentialMigration,
    FileCredentialMigrationUnavailable,
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

NOW = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)
IDEMPOTENCY_KEY = UUID("33333333-3333-4333-8333-333333333333")
CHALLENGE_ID = UUID("55555555-5555-4555-8555-555555555555")


class _ProjectionStore:
    def __init__(self, projection: PairedProjection | None) -> None:
        self.projection = projection
        self.saved: list[PairedProjection] = []

    async def load(self) -> PairedProjection | None:
        return self.projection

    async def save(self, projection: PairedProjection) -> None:
        self.projection = projection
        self.saved.append(projection)


class _Target:
    def __init__(self) -> None:
        self.tokens: list[str] = []
        self.cleared = 0

    async def store_access_token(self, token: str) -> None:
        self.tokens.append(token)

    async def clear_access_token(self) -> None:
        self.tokens.clear()
        self.cleared += 1


class _Identity:
    def __init__(self) -> None:
        self.sign_calls: list[tuple[str, bytes]] = []

    async def sign_challenge(self, key_handle: str, challenge: bytes) -> bytes:
        self.sign_calls.append((key_handle, challenge))
        return b"\x02" * 64


class _Cloud:
    def __init__(
        self,
        *,
        challenge: DeviceAuthenticationChallenge,
        token: ConnectorToken,
    ) -> None:
        self.challenge = challenge
        self.token = token
        self.challenge_error: DevicePairingCloudError | None = None
        self.challenge_calls: list[tuple[DeviceChallengeRequest, UUID]] = []
        self.token_calls: list[tuple[DeviceAuthenticationTokenRequest, UUID]] = []

    async def create_device_challenge(
        self,
        request: DeviceChallengeRequest,
        *,
        idempotency_key: UUID,
    ) -> DeviceAuthenticationChallenge:
        self.challenge_calls.append((request, idempotency_key))
        if self.challenge_error is not None:
            raise self.challenge_error
        return self.challenge

    async def issue_device_token(
        self,
        request: DeviceAuthenticationTokenRequest,
        *,
        idempotency_key: UUID,
    ) -> ConnectorToken:
        self.token_calls.append((request, idempotency_key))
        return self.token


def _projection() -> PairedProjection:
    return PairedProjection(
        tenant_id=UUID("66666666-6666-4666-8666-666666666666"),
        device_id=UUID("77777777-7777-4777-8777-777777777777"),
        credential_id=UUID("88888888-8888-4888-8888-888888888888"),
        agent_id=UUID("99999999-9999-4999-8999-999999999999"),
        scopes=("session.observe",),
        key_handle="hermes-device-key:v1:" + "B" * 43,
        credential_fingerprint="SHA256:" + "B" * 43,
        token_expires_at=NOW - timedelta(seconds=1),
        lifecycle_state="active",
    )


def _challenge(*, expires_at: datetime) -> DeviceAuthenticationChallenge:
    payload = b"hermes-device-auth-v1\x00" + b"m" * 40
    return DeviceAuthenticationChallenge(
        challenge_id=CHALLENGE_ID,
        signing_payload=base64.urlsafe_b64encode(payload).rstrip(b"=").decode(),
        ttl_seconds=60,
        expires_at=expires_at,
    )


def _token(projection: PairedProjection) -> ConnectorToken:
    return ConnectorToken(
        access_token="N" * 64,
        token_type="Bearer",
        ttl_seconds=300,
        expires_at=NOW + timedelta(seconds=300),
        binding=DeviceBinding(
            tenant_id=projection.tenant_id,
            device_id=projection.device_id,
            credential_id=projection.credential_id,
            agent_id=projection.agent_id,
            scopes=projection.scopes,
        ),
    )


def _migration(
    projection_store: _ProjectionStore,
    cloud: _Cloud,
    target: _Target,
    identity: _Identity | None = None,
) -> FileCredentialMigration:
    return FileCredentialMigration(
        projection_store=projection_store,
        identity=identity or _Identity(),
        cloud=cloud,
        target=target,
        now=lambda: NOW,
        new_idempotency_key=lambda: IDEMPOTENCY_KEY,
    )


@pytest.mark.asyncio
async def test_migration_requires_existing_server_authoritative_projection() -> None:
    projection = _projection()
    cloud = _Cloud(
        challenge=_challenge(expires_at=NOW + timedelta(seconds=60)),
        token=_token(projection),
    )
    migration = _migration(_ProjectionStore(None), cloud, _Target())

    with pytest.raises(FileCredentialMigrationUnavailable):
        await migration.migrate()

    assert cloud.challenge_calls == []


@pytest.mark.asyncio
async def test_migration_ignores_expired_legacy_token_and_mints_new_bound_token() -> (
    None
):
    projection = _projection()
    projection_store = _ProjectionStore(projection)
    target = _Target()
    identity = _Identity()
    cloud = _Cloud(
        challenge=_challenge(expires_at=NOW + timedelta(seconds=60)),
        token=_token(projection),
    )
    migration = _migration(projection_store, cloud, target, identity)

    result = await migration.migrate()

    assert target.tokens == ["N" * 64]
    assert "legacy" not in repr(target.tokens)
    assert cloud.challenge_calls == [
        (
            DeviceChallengeRequest(
                device_id=projection.device_id,
                credential_id=projection.credential_id,
            ),
            IDEMPOTENCY_KEY,
        )
    ]
    assert len(identity.sign_calls) == 1
    assert len(cloud.token_calls) == 1
    assert projection_store.saved == [
        replace(
            projection,
            token_expires_at=NOW + timedelta(seconds=300),
        )
    ]
    assert result.device_id == projection.device_id
    assert "N" * 64 not in repr(result)


@pytest.mark.asyncio
async def test_migration_rejects_expired_challenge_before_sign_or_publish() -> None:
    projection = _projection()
    projection_store = _ProjectionStore(projection)
    target = _Target()
    identity = _Identity()
    cloud = _Cloud(
        challenge=_challenge(expires_at=NOW),
        token=_token(projection),
    )

    with pytest.raises(FileCredentialMigrationUnavailable, match="challenge"):
        await _migration(projection_store, cloud, target, identity).migrate()

    assert identity.sign_calls == []
    assert cloud.token_calls == []
    assert target.tokens == []
    assert projection_store.saved == []


@pytest.mark.asyncio
async def test_migration_cloud_error_does_not_publish_or_clear_formal_token() -> None:
    projection = _projection()
    projection_store = _ProjectionStore(projection)
    target = _Target()
    cloud = _Cloud(
        challenge=_challenge(expires_at=NOW + timedelta(seconds=60)),
        token=_token(projection),
    )
    cloud.challenge_error = DevicePairingCloudError(
        code="DEVICE_AUTH_UNAVAILABLE",
        status_code=403,
    )

    with pytest.raises(DevicePairingCloudError):
        await _migration(projection_store, cloud, target).migrate()

    assert target.tokens == []
    assert target.cleared == 0
    assert projection_store.saved == []
