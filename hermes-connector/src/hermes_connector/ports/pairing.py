"""Cloud and projection boundaries for device pairing."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from hermes_connector.domain.pairing import (
    ConnectorToken,
    DeviceAuthenticationChallenge,
    DeviceAuthenticationTokenRequest,
    DeviceChallengeProof,
    DeviceChallengeRequest,
    PairedProjection,
    PairingOffer,
    PairingOfferProjection,
    PairingOfferRequest,
    PairingOfferStatus,
)


class DevicePairingCloudError(RuntimeError):
    """A redacted Cloud pairing/authentication failure."""

    __slots__ = ("code", "status_code")

    def __init__(self, *, code: str, status_code: int) -> None:
        super().__init__("device pairing cloud request failed")
        self.code = code
        self.status_code = status_code


class DevicePairingCloudPort(Protocol):
    async def create_pairing_offer(
        self,
        request: PairingOfferRequest,
        *,
        idempotency_key: UUID,
    ) -> PairingOffer: ...

    async def get_pairing_offer(
        self,
        pairing_offer_id: UUID,
        *,
        pairing_offer_secret: str,
    ) -> PairingOfferStatus: ...

    async def prove_pairing_session(
        self,
        pairing_session_id: UUID,
        proof: DeviceChallengeProof,
        *,
        pairing_offer_secret: str,
        idempotency_key: UUID,
    ) -> ConnectorToken: ...

    async def create_device_challenge(
        self,
        request: DeviceChallengeRequest,
        *,
        idempotency_key: UUID,
    ) -> DeviceAuthenticationChallenge: ...

    async def issue_device_token(
        self,
        request: DeviceAuthenticationTokenRequest,
        *,
        idempotency_key: UUID,
    ) -> ConnectorToken: ...


class PairingOfferProjectionStorePort(Protocol):
    async def load(self) -> PairingOfferProjection | None: ...

    async def save(self, projection: PairingOfferProjection) -> None: ...

    async def delete(self) -> bool: ...

    async def delete_if_matches(self, pairing_offer_id: UUID) -> bool:
        """Delete only when the stored offer version still has this identifier."""


class PairedProjectionStorePort(Protocol):
    async def load(self) -> PairedProjection | None: ...

    async def save(self, projection: PairedProjection) -> None: ...


class ConnectorTokenStorePort(Protocol):
    async def access_token(self) -> str: ...

    async def store_access_token(self, token: str) -> None: ...

    async def clear_access_token(self) -> None: ...
