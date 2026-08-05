"""Application coordinator for Connector device pairing."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable
from datetime import datetime
from uuid import UUID

from hermes_connector.domain.device_auth import (
    decode_device_signing_payload,
    encode_ed25519_signature,
)
from hermes_connector.domain.pairing import (
    DeviceChallengeProof,
    PairedProjection,
    PairingCancelDisplay,
    PairingOfferProjection,
    PairingOfferRequest,
    PairingOfferStatus,
    PairingStartDisplay,
    PairingStatusDisplay,
)
from hermes_connector.ports.device_identity import DeviceIdentityPort
from hermes_connector.ports.operation_lock import OperationLockPort
from hermes_connector.ports.pairing import (
    ConnectorTokenStorePort,
    DevicePairingCloudError,
    DevicePairingCloudPort,
    PairedProjectionStorePort,
    PairingOfferProjectionStorePort,
)
from hermes_connector.ports.secure_storage import SecureSecretStorePort


class PairingConflict(ValueError):
    """A pairing operation conflicts with durable local state."""


class PairingProtocolViolation(ValueError):
    """Cloud pairing data does not match the requested device identity."""


class PairingCoordinator:
    """Coordinate tenant-neutral device pairing without owning authorization."""

    def __init__(
        self,
        *,
        connector_instance_id: UUID,
        display_name: str,
        connector_version: str,
        identity: DeviceIdentityPort,
        cloud: DevicePairingCloudPort,
        offer_secret_store: SecureSecretStorePort,
        offer_projection_store: PairingOfferProjectionStorePort,
        paired_projection_store: PairedProjectionStorePort,
        token_store: ConnectorTokenStorePort,
        now: Callable[[], datetime],
        new_idempotency_key: Callable[[], UUID],
        command_lock: OperationLockPort | None = None,
    ) -> None:
        self._connector_instance_id = connector_instance_id
        self._display_name = display_name
        self._connector_version = connector_version
        self._identity = identity
        self._cloud = cloud
        self._offer_secret_store = offer_secret_store
        self._offer_projection_store = offer_projection_store
        self._paired_projection_store = paired_projection_store
        self._token_store = token_store
        self._now = now
        self._new_idempotency_key = new_idempotency_key
        self._operation_lock = asyncio.Lock()
        self._command_lock = command_lock

    async def start(self) -> PairingStartDisplay:
        async with self._operation_lock:
            if self._command_lock is None:
                return await self._start()
            async with self._command_lock:
                return await self._start()

    async def _start(self) -> PairingStartDisplay:
        if await self._paired_projection_store.load() is not None:
            raise PairingConflict("connector already has a paired projection")
        existing_offer = await self._offer_projection_store.load()
        if existing_offer is not None:
            if existing_offer.expires_at > self._now():
                raise PairingConflict("connector already has an active pairing offer")
            existing_secret = await self._offer_secret_store.read_secret()
            await self._cleanup_offer(existing_offer, existing_secret)
        public_identity = await self._identity.get_or_create()
        request = PairingOfferRequest(
            connector_instance_id=self._connector_instance_id,
            display_name=self._display_name,
            platform_family="macos",
            connector_version=self._connector_version,
            key_algorithm=public_identity.algorithm,
            public_key=public_identity.public_key,
        )
        offer = await self._cloud.create_pairing_offer(
            request,
            idempotency_key=self._new_idempotency_key(),
        )
        if offer.credential_fingerprint != public_identity.fingerprint:
            raise PairingProtocolViolation(
                "pairing credential fingerprint does not match"
            )
        await self._offer_secret_store.write_secret(
            offer.pairing_offer_secret.encode("ascii")
        )
        try:
            await self._offer_projection_store.save(
                PairingOfferProjection(
                    pairing_offer_id=offer.pairing_offer_id,
                    key_handle=public_identity.key_handle,
                    credential_fingerprint=offer.credential_fingerprint,
                    expires_at=offer.expires_at,
                )
            )
        except BaseException:
            await self._offer_secret_store.delete_secret_if_matches(
                hashlib.sha256(offer.pairing_offer_secret.encode("ascii")).digest()
            )
            raise
        return PairingStartDisplay(
            pairing_code=offer.pairing_code,
            credential_fingerprint=offer.credential_fingerprint,
            expires_at=offer.expires_at,
        )

    async def status(self) -> PairingStatusDisplay:
        async with self._operation_lock:
            if self._command_lock is None:
                return await self._status()
            async with self._command_lock:
                return await self._status()

    async def _status(self) -> PairingStatusDisplay:
        projection = await self._offer_projection_store.load()
        if projection is None:
            raise ValueError("pairing offer is unavailable")
        if projection.expires_at <= self._now():
            raw_secret = await self._offer_secret_store.read_secret()
            await self._cleanup_offer(projection, raw_secret)
            return PairingStatusDisplay(
                state="expired",
                activation_state="blocked",
                credential_fingerprint=projection.credential_fingerprint,
                expires_at=projection.expires_at,
                revision=None,
            )
        raw_secret = await self._offer_secret_store.read_secret()
        if raw_secret is None:
            raise ValueError("pairing offer is unavailable")
        try:
            response = await self._cloud.get_pairing_offer(
                projection.pairing_offer_id,
                pairing_offer_secret=raw_secret.decode("ascii"),
            )
        except DevicePairingCloudError as error:
            if error.status_code != 410 or error.code != "PAIRING_EXPIRED":
                raise
            await self._cleanup_offer(projection, raw_secret)
            return PairingStatusDisplay(
                state="expired",
                activation_state="blocked",
                credential_fingerprint=projection.credential_fingerprint,
                expires_at=projection.expires_at,
                revision=None,
            )
        if response.pairing_offer_id != projection.pairing_offer_id:
            raise PairingProtocolViolation("pairing offer response does not match")
        if response.expires_at != projection.expires_at:
            raise PairingProtocolViolation("pairing offer expiry does not match")
        if response.state in {"expired", "cancelled"}:
            await self._cleanup_offer(projection, raw_secret)
        if (
            response.state == "confirmed"
            and response.activation_state == "awaiting_proof"
        ):
            return await self._prove(
                response=response,
                projection=projection,
                pairing_offer_secret=raw_secret.decode("ascii"),
            )
        if response.state == "confirmed" and response.activation_state == "active":
            if response.binding is None:
                raise PairingProtocolViolation("active pairing binding is unavailable")
            await self._paired_projection_store.save(
                PairedProjection(
                    tenant_id=response.binding.tenant_id,
                    device_id=response.binding.device_id,
                    credential_id=response.binding.credential_id,
                    agent_id=response.binding.agent_id,
                    scopes=response.binding.scopes,
                    key_handle=projection.key_handle,
                    credential_fingerprint=projection.credential_fingerprint,
                    token_expires_at=self._now(),
                    lifecycle_state="active",
                )
            )
            await self._cleanup_offer(projection, raw_secret)
        return PairingStatusDisplay(
            state=response.state,
            activation_state=response.activation_state,
            credential_fingerprint=projection.credential_fingerprint,
            expires_at=response.expires_at,
            revision=response.revision,
        )

    async def cancel(self) -> PairingCancelDisplay:
        async with self._operation_lock:
            if self._command_lock is None:
                return await self._cancel()
            async with self._command_lock:
                return await self._cancel()

    async def _cancel(self) -> PairingCancelDisplay:
        projection = await self._offer_projection_store.load()
        raw_secret = await self._offer_secret_store.read_secret()
        await self._cleanup_offer(projection, raw_secret)
        return PairingCancelDisplay(state="cancelled_local")

    async def _prove(
        self,
        *,
        response: PairingOfferStatus,
        projection: PairingOfferProjection,
        pairing_offer_secret: str,
    ) -> PairingStatusDisplay:
        if (
            response.pairing_session_id is None
            or response.binding is None
            or response.challenge is None
        ):
            raise ValueError("pairing proof response is invalid")
        if response.challenge.expires_at <= self._now():
            raise ValueError("device signing challenge is expired")
        if response.challenge.expires_at > projection.expires_at:
            raise PairingProtocolViolation(
                "device signing challenge outlives pairing offer"
            )
        decoded_payload = decode_device_signing_payload(
            response.challenge.signing_payload
        )
        signature = await self._identity.sign_challenge(
            projection.key_handle,
            decoded_payload,
        )
        proof = DeviceChallengeProof(
            challenge_id=response.challenge.challenge_id,
            signing_payload=response.challenge.signing_payload,
            signature_algorithm="Ed25519",
            signature=encode_ed25519_signature(signature),
        )
        token = await self._cloud.prove_pairing_session(
            response.pairing_session_id,
            proof,
            pairing_offer_secret=pairing_offer_secret,
            idempotency_key=self._new_idempotency_key(),
        )
        if token.binding != response.binding:
            raise ValueError("pairing token binding does not match")
        await self._token_store.store_access_token(token.access_token)
        try:
            await self._paired_projection_store.save(
                PairedProjection(
                    tenant_id=token.binding.tenant_id,
                    device_id=token.binding.device_id,
                    credential_id=token.binding.credential_id,
                    agent_id=token.binding.agent_id,
                    scopes=token.binding.scopes,
                    key_handle=projection.key_handle,
                    credential_fingerprint=projection.credential_fingerprint,
                    token_expires_at=token.expires_at,
                    lifecycle_state="active",
                )
            )
        except BaseException:
            await self._token_store.clear_access_token()
            raise
        raw_secret = pairing_offer_secret.encode("ascii")
        await self._cleanup_offer(projection, raw_secret)
        return PairingStatusDisplay(
            state="confirmed",
            activation_state="active",
            credential_fingerprint=projection.credential_fingerprint,
            expires_at=token.expires_at,
            revision=response.revision,
        )

    async def _cleanup_offer(
        self,
        projection: PairingOfferProjection | None,
        raw_secret: bytes | None,
    ) -> None:
        if raw_secret is not None:
            await self._offer_secret_store.delete_secret_if_matches(
                hashlib.sha256(raw_secret).digest()
            )
        if projection is not None:
            await self._offer_projection_store.delete_if_matches(
                projection.pairing_offer_id
            )
