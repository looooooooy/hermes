"""Tenant-neutral Connector pairing domain values."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal
from uuid import UUID

from hermes_connector.domain.identifiers import canonical_uuid


@dataclass(frozen=True, slots=True)
class PairingOfferRequest:
    connector_instance_id: UUID
    display_name: str
    platform_family: str
    connector_version: str
    key_algorithm: str
    public_key: str


@dataclass(frozen=True, slots=True)
class PairingOffer:
    pairing_offer_id: UUID
    pairing_code: str = field(repr=False)
    pairing_offer_secret: str = field(repr=False)
    credential_fingerprint: str
    state: str
    revision: int
    ttl_seconds: int
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class PairingOfferProjection:
    pairing_offer_id: UUID
    key_handle: str
    credential_fingerprint: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class PairingStartDisplay:
    pairing_code: str = field(repr=False)
    credential_fingerprint: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class PairingOfferStatus:
    pairing_offer_id: UUID
    pairing_session_id: UUID | None
    state: Literal["pending", "claimed", "confirmed", "expired", "cancelled"]
    activation_state: Literal[
        "waiting_owner",
        "waiting_owner_confirmation",
        "awaiting_proof",
        "active",
        "blocked",
    ]
    binding: DeviceBinding | None
    challenge: DeviceAuthenticationChallenge | None
    expires_at: datetime
    revision: int

    def __post_init__(self) -> None:
        canonical_uuid(self.pairing_offer_id)
        if self.pairing_session_id is not None:
            canonical_uuid(self.pairing_session_id)
        expected = {
            ("pending", "waiting_owner"): (False, False, False),
            ("claimed", "waiting_owner_confirmation"): (True, False, False),
            ("confirmed", "awaiting_proof"): (True, True, True),
            ("confirmed", "active"): (True, True, False),
            ("expired", "blocked"): (False, False, False),
            ("cancelled", "blocked"): (False, False, False),
        }.get((self.state, self.activation_state))
        actual = (
            self.pairing_session_id is not None,
            self.binding is not None,
            self.challenge is not None,
        )
        if (
            expected is None
            or actual != expected
            or self.binding is not None
            and not isinstance(self.binding, DeviceBinding)
            or self.challenge is not None
            and not isinstance(self.challenge, DeviceAuthenticationChallenge)
            or type(self.revision) is not int
            or self.revision < 1
        ):
            raise ValueError("pairing status field combination is invalid")


@dataclass(frozen=True, slots=True)
class PairingStatusDisplay:
    state: str
    activation_state: str
    credential_fingerprint: str
    expires_at: datetime
    revision: int | None


@dataclass(frozen=True, slots=True)
class PairingCancelDisplay:
    state: str


@dataclass(frozen=True, slots=True)
class DeviceBinding:
    tenant_id: UUID
    device_id: UUID
    credential_id: UUID
    agent_id: UUID
    scopes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DeviceAuthenticationChallenge:
    challenge_id: UUID
    signing_payload: str = field(repr=False)
    ttl_seconds: int
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class DeviceChallengeRequest:
    device_id: UUID
    credential_id: UUID


@dataclass(frozen=True, slots=True)
class DeviceChallengeProof:
    challenge_id: UUID
    signing_payload: str = field(repr=False)
    signature_algorithm: str
    signature: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class DeviceAuthenticationTokenRequest:
    device_id: UUID
    credential_id: UUID
    challenge_id: UUID
    signing_payload: str = field(repr=False)
    signature_algorithm: str
    signature: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class ConnectorToken:
    access_token: str = field(repr=False)
    token_type: str
    ttl_seconds: int
    expires_at: datetime
    binding: DeviceBinding


@dataclass(frozen=True, slots=True)
class PairedProjection:
    tenant_id: UUID
    device_id: UUID
    credential_id: UUID
    agent_id: UUID
    scopes: tuple[str, ...]
    key_handle: str
    credential_fingerprint: str
    token_expires_at: datetime
    lifecycle_state: str
