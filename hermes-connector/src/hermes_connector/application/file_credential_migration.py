"""One-shot migration from an explicit file token into macOS Keychain."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from uuid import UUID

from hermes_connector.domain.device_auth import (
    decode_device_signing_payload,
    encode_ed25519_signature,
)
from hermes_connector.domain.pairing import (
    DeviceAuthenticationTokenRequest,
    DeviceBinding,
    DeviceChallengeRequest,
    PairedProjection,
)
from hermes_connector.ports.device_identity import DeviceIdentityPort
from hermes_connector.ports.operation_lock import OperationLockPort
from hermes_connector.ports.pairing import (
    ConnectorTokenStorePort,
    DevicePairingCloudPort,
    PairedProjectionStorePort,
)


class FileCredentialMigrationUnavailable(ValueError):
    """The legacy credential cannot be migrated safely."""


@dataclass(frozen=True, slots=True)
class FileCredentialMigrationDisplay:
    device_id: UUID
    credential_fingerprint: str


class FileCredentialMigration:
    """Mint a fresh device-bound token without copying the legacy file token."""

    def __init__(
        self,
        *,
        projection_store: PairedProjectionStorePort,
        identity: DeviceIdentityPort,
        cloud: DevicePairingCloudPort,
        target: ConnectorTokenStorePort,
        now: Callable[[], datetime],
        new_idempotency_key: Callable[[], UUID],
        command_lock: OperationLockPort | None = None,
    ) -> None:
        self._projection_store = projection_store
        self._identity = identity
        self._cloud = cloud
        self._target = target
        self._now = now
        self._new_idempotency_key = new_idempotency_key
        self._command_lock = command_lock

    async def migrate(self) -> FileCredentialMigrationDisplay:
        if self._command_lock is None:
            return await self._migrate()
        async with self._command_lock:
            return await self._migrate()

    async def _migrate(self) -> FileCredentialMigrationDisplay:
        projection = await self._projection_store.load()
        if projection is None or projection.lifecycle_state != "active":
            raise FileCredentialMigrationUnavailable(
                "active server paired projection is required"
            )
        challenge = await self._cloud.create_device_challenge(
            DeviceChallengeRequest(
                device_id=projection.device_id,
                credential_id=projection.credential_id,
            ),
            idempotency_key=self._new_idempotency_key(),
        )
        received_at = self._now()
        if (
            challenge.expires_at <= received_at
            or challenge.expires_at
            > received_at + timedelta(seconds=challenge.ttl_seconds)
        ):
            raise FileCredentialMigrationUnavailable(
                "fresh device challenge is unavailable"
            )
        decoded_payload = decode_device_signing_payload(challenge.signing_payload)
        signature = await self._identity.sign_challenge(
            projection.key_handle,
            decoded_payload,
        )
        token = await self._cloud.issue_device_token(
            DeviceAuthenticationTokenRequest(
                device_id=projection.device_id,
                credential_id=projection.credential_id,
                challenge_id=challenge.challenge_id,
                signing_payload=challenge.signing_payload,
                signature_algorithm="Ed25519",
                signature=encode_ed25519_signature(signature),
            ),
            idempotency_key=self._new_idempotency_key(),
        )
        expected_binding = DeviceBinding(
            tenant_id=projection.tenant_id,
            device_id=projection.device_id,
            credential_id=projection.credential_id,
            agent_id=projection.agent_id,
            scopes=projection.scopes,
        )
        if token.binding != expected_binding or token.expires_at <= self._now():
            raise FileCredentialMigrationUnavailable(
                "fresh device token binding is invalid"
            )
        await self._target.store_access_token(token.access_token)
        updated_projection: PairedProjection = replace(
            projection,
            token_expires_at=token.expires_at,
        )
        try:
            await self._projection_store.save(updated_projection)
        except BaseException:
            await self._target.clear_access_token()
            raise
        return FileCredentialMigrationDisplay(
            device_id=projection.device_id,
            credential_fingerprint=projection.credential_fingerprint,
        )
