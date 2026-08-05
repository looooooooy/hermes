"""Device-bound short-lived Cloud token provider."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta
from enum import StrEnum
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
from hermes_connector.ports.cloud import (
    CloudCredentialUnavailable,
)
from hermes_connector.ports.device_identity import DeviceIdentityPort
from hermes_connector.ports.pairing import (
    ConnectorTokenStorePort,
    DevicePairingCloudError,
    DevicePairingCloudPort,
    PairedProjectionStorePort,
)


class DeviceAuthorizationUnavailable(ValueError):
    """The paired device cannot currently authenticate."""


class DeviceAuthorizationState(StrEnum):
    ACTIVE = "ACTIVE"
    AUTH_BLOCKED = "AUTH_BLOCKED"
    SUSPENDED = "SUSPENDED"
    REVOKED = "REVOKED"


_PROJECTION_LIFECYCLE_STATES = {
    "active": DeviceAuthorizationState.ACTIVE,
    "auth_blocked": DeviceAuthorizationState.AUTH_BLOCKED,
    "suspended": DeviceAuthorizationState.SUSPENDED,
    "revoked": DeviceAuthorizationState.REVOKED,
}


class DeviceBoundCloudTokenProvider:
    """Return only tokens bound to the server-authoritative paired projection."""

    def __init__(
        self,
        *,
        projection_store: PairedProjectionStorePort,
        token_store: ConnectorTokenStorePort,
        identity: DeviceIdentityPort,
        cloud: DevicePairingCloudPort,
        now: Callable[[], datetime],
        refresh_before: timedelta,
        new_idempotency_key: Callable[[], UUID],
        initial_lifecycle_state: str = "active",
    ) -> None:
        self._projection_store = projection_store
        self._token_store = token_store
        self._identity = identity
        self._cloud = cloud
        self._now = now
        self._refresh_before = refresh_before
        self._new_idempotency_key = new_idempotency_key
        self._state = _runtime_state(initial_lifecycle_state)

    @property
    def state(self) -> DeviceAuthorizationState:
        return self._state

    async def access_token(self) -> str:
        projection = await self._projection_store.load()
        if projection is None:
            raise DeviceAuthorizationUnavailable(
                "paired device authorization is unavailable"
            )
        self._state = _runtime_state(projection.lifecycle_state)
        if self._state is not DeviceAuthorizationState.ACTIVE:
            raise DeviceAuthorizationUnavailable(
                "paired device authorization is unavailable"
            )
        if projection.token_expires_at > self._now() + self._refresh_before:
            try:
                return await self._token_store.access_token()
            except CloudCredentialUnavailable:
                pass
        try:
            challenge = await self._cloud.create_device_challenge(
                DeviceChallengeRequest(
                    device_id=projection.device_id,
                    credential_id=projection.credential_id,
                ),
                idempotency_key=self._new_idempotency_key(),
            )
        except DevicePairingCloudError as error:
            await self._handle_cloud_error(projection, error)
            raise
        if challenge.expires_at <= self._now():
            raise DeviceAuthorizationUnavailable(
                "device authentication challenge is unavailable"
            )
        decoded_payload = decode_device_signing_payload(challenge.signing_payload)
        signature = await self._identity.sign_challenge(
            projection.key_handle,
            decoded_payload,
        )
        try:
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
        except DevicePairingCloudError as error:
            await self._handle_cloud_error(projection, error)
            raise
        expected_binding = DeviceBinding(
            tenant_id=projection.tenant_id,
            device_id=projection.device_id,
            credential_id=projection.credential_id,
            agent_id=projection.agent_id,
            scopes=projection.scopes,
        )
        if token.binding != expected_binding:
            raise DeviceAuthorizationUnavailable("device token binding does not match")
        await self._token_store.store_access_token(token.access_token)
        try:
            await self._projection_store.save(
                replace(projection, token_expires_at=token.expires_at)
            )
        except BaseException:
            await self._token_store.clear_access_token()
            raise
        return token.access_token

    async def _handle_cloud_error(
        self,
        projection: PairedProjection,
        error: DevicePairingCloudError,
    ) -> None:
        lifecycle = {
            "DEVICE_AUTH_UNAVAILABLE": ("auth_blocked", "AUTH_BLOCKED"),
        }.get(error.code)
        if lifecycle is None:
            return
        await self._token_store.clear_access_token()
        await self._projection_store.save(
            replace(projection, lifecycle_state=lifecycle[0])
        )
        self._state = DeviceAuthorizationState(lifecycle[1])
        raise DeviceAuthorizationUnavailable(
            "paired device authorization is unavailable"
        ) from None

    async def clear_access_token(self) -> None:
        await self._token_store.clear_access_token()

    async def apply_lifecycle_signal(self, signal: str) -> None:
        lifecycle = {
            "revoked": ("revoked", "REVOKED"),
            "suspended": ("suspended", "SUSPENDED"),
        }.get(signal)
        if lifecycle is None:
            raise ValueError("device lifecycle signal is invalid")
        projection = await self._projection_store.load()
        if projection is None:
            raise DeviceAuthorizationUnavailable(
                "paired device authorization is unavailable"
            )
        await self._token_store.clear_access_token()
        await self._projection_store.save(
            replace(projection, lifecycle_state=lifecycle[0])
        )
        self._state = DeviceAuthorizationState(lifecycle[1])

    def __repr__(self) -> str:
        return "DeviceBoundCloudTokenProvider(<device-bound-credentials>)"


def _runtime_state(lifecycle_state: str) -> DeviceAuthorizationState:
    try:
        return _PROJECTION_LIFECYCLE_STATES[lifecycle_state]
    except (KeyError, TypeError):
        raise ValueError("device lifecycle state is invalid") from None
