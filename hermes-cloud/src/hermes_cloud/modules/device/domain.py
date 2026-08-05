"""Infrastructure-neutral device pairing and credential values."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import Enum
from hashlib import sha256
from types import MappingProxyType
from typing import Final
from uuid import UUID

from hermes_cloud.domain.persistence import PairingSessionState

PAIRING_SESSION_TTL: Final = timedelta(minutes=5)
PAIRING_CHALLENGE_TTL: Final = timedelta(seconds=60)
PAIRING_SCOPES: Final = frozenset(
    {
        "session.observe",
        "session.control.request",
    }
)
_SHA256_DIGEST = re.compile(r"^[0-9a-f]{64}$")


def _require_aware(value: datetime, field: str) -> None:
    if value.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone")


def _require_digest(value: str, field: str) -> None:
    if _SHA256_DIGEST.fullmatch(value) is None:
        raise ValueError(f"{field} must be a canonical SHA-256 digest")


def _require_text(value: str, field: str, *, max_bytes: int = 128) -> None:
    if not value.strip() or len(value.encode("utf-8")) > max_bytes:
        raise ValueError(f"{field} must contain 1 to {max_bytes} UTF-8 bytes")


def fingerprint_ed25519_public_key(public_key: bytes) -> str:
    """Return the canonical fingerprint for one raw Ed25519 public key."""

    if not isinstance(public_key, bytes) or len(public_key) != 32:
        raise ValueError("Ed25519 public key must contain exactly 32 bytes")
    return sha256(public_key).hexdigest()


@dataclass(frozen=True, slots=True)
class PairingOffer:
    """Tenant-neutral enrollment offer created by an unauthenticated Connector."""

    pairing_offer_id: UUID
    pairing_code_digest: str
    bootstrap_secret_digest: str
    algorithm: str
    public_key: bytes
    credential_fingerprint: str
    key_id: str
    device_key: str
    device_name: str
    platform: str
    connector_version: str
    state: PairingSessionState
    revision: int
    expires_at: datetime
    claimed_at: datetime | None
    created_at: datetime

    def __post_init__(self) -> None:
        _require_digest(self.pairing_code_digest, "pairing_code_digest")
        _require_digest(self.bootstrap_secret_digest, "bootstrap_secret_digest")
        if self.bootstrap_secret_digest == self.pairing_code_digest:
            raise ValueError("bootstrap secret and pairing code digests must differ")
        if self.algorithm != "ed25519":
            raise ValueError("pairing offer algorithm must be ed25519")
        if (
            fingerprint_ed25519_public_key(self.public_key)
            != self.credential_fingerprint
        ):
            raise ValueError("pairing offer fingerprint does not match")
        for field, value in (
            ("key_id", self.key_id),
            ("device_key", self.device_key),
            ("device_name", self.device_name),
            ("platform", self.platform),
            ("connector_version", self.connector_version),
        ):
            _require_text(value, field)
        for field, value in (
            ("created_at", self.created_at),
            ("expires_at", self.expires_at),
        ):
            _require_aware(value, field)
        if self.claimed_at is not None:
            _require_aware(self.claimed_at, "claimed_at")
        if not self.created_at < self.expires_at:
            raise ValueError("pairing offer expiry must follow creation")
        if self.expires_at - self.created_at != PAIRING_SESSION_TTL:
            raise ValueError("pairing offer expiry must be exactly five minutes")
        if not 0 <= self.revision <= 1:
            raise ValueError("pairing offer revision is outside the allowed range")
        if self.claimed_at is not None and self.claimed_at < self.created_at:
            raise ValueError("pairing offer claim must not precede creation")
        if self.state is PairingSessionState.PENDING and self.claimed_at is not None:
            raise ValueError("pending pairing offer must not contain claim state")
        if self.state is PairingSessionState.PENDING and self.revision != 0:
            raise ValueError("pending pairing offer must have revision zero")
        if self.state is PairingSessionState.CLAIMED and self.claimed_at is None:
            raise ValueError("claimed pairing offer must contain claim state")
        if self.state is PairingSessionState.CLAIMED and self.revision != 1:
            raise ValueError("claimed pairing offer must have revision one")
        if (
            self.state
            in {
                PairingSessionState.EXPIRED,
                PairingSessionState.CANCELLED,
            }
            and self.revision != 1
        ):
            raise ValueError("terminal pairing offer must have revision one")
        if self.state is PairingSessionState.CONFIRMED:
            raise ValueError("pairing offer cannot enter the confirmed session state")


@dataclass(frozen=True, slots=True)
class PairingSession:
    tenant_id: UUID
    pairing_session_id: UUID
    workspace_id: UUID
    agent_id: UUID
    device_id: UUID | None
    pairing_code_digest: str
    state: PairingSessionState
    failed_attempts: int
    expires_at: datetime
    claimed_at: datetime | None
    confirmed_at: datetime | None
    created_at: datetime

    def __post_init__(self) -> None:
        _require_digest(self.pairing_code_digest, "pairing_code_digest")
        for field, value in (
            ("created_at", self.created_at),
            ("expires_at", self.expires_at),
        ):
            _require_aware(value, field)
        for field, value in (
            ("claimed_at", self.claimed_at),
            ("confirmed_at", self.confirmed_at),
        ):
            if value is not None:
                _require_aware(value, field)
        if not self.created_at < self.expires_at:
            raise ValueError("pairing expiry must follow creation")
        if self.expires_at - self.created_at > PAIRING_SESSION_TTL:
            raise ValueError("pairing expiry must not exceed five minutes")
        if self.failed_attempts != 0:
            raise ValueError("legacy failed attempts must remain zero")
        if self.claimed_at is not None and self.claimed_at < self.created_at:
            raise ValueError("pairing claim must not precede creation")
        if self.confirmed_at is not None and (
            self.claimed_at is None or self.confirmed_at < self.claimed_at
        ):
            raise ValueError("pairing confirmation must follow claim")
        if self.state is PairingSessionState.PENDING and any(
            value is not None
            for value in (self.device_id, self.claimed_at, self.confirmed_at)
        ):
            raise ValueError("pending pairing must not contain claim state")
        if self.state is PairingSessionState.CLAIMED and (
            self.device_id is None
            or self.claimed_at is None
            or self.confirmed_at is not None
        ):
            raise ValueError("claimed pairing must contain only claim state")
        if self.state is PairingSessionState.CONFIRMED and (
            self.device_id is None
            or self.claimed_at is None
            or self.confirmed_at is None
        ):
            raise ValueError("confirmed pairing must contain confirmation state")


@dataclass(frozen=True, slots=True)
class PairingKeyMaterial:
    """Public enrollment material; private keys and plaintext codes are absent."""

    tenant_id: UUID
    pairing_session_id: UUID
    algorithm: str
    public_key: bytes
    credential_fingerprint: str
    key_id: str
    device_key: str
    device_name: str
    platform: str
    scopes: tuple[str, ...]
    claim_id: UUID | None
    claimed_by_user_id: UUID | None
    challenge_id: UUID | None
    challenge_digest: str | None
    challenge_expires_at: datetime | None
    owner_confirmed_at: datetime | None
    confirmation_digest: str | None
    revision: int
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        if self.algorithm != "ed25519":
            raise ValueError("device public key algorithm must be ed25519")
        actual_fingerprint = fingerprint_ed25519_public_key(self.public_key)
        _require_digest(self.credential_fingerprint, "credential_fingerprint")
        if self.credential_fingerprint != actual_fingerprint:
            raise ValueError("device public key fingerprint does not match")
        for field, value in (
            ("key_id", self.key_id),
            ("device_key", self.device_key),
            ("device_name", self.device_name),
            ("platform", self.platform),
        ):
            _require_text(value, field)
        if (
            not self.scopes
            or len(set(self.scopes)) != len(self.scopes)
            or not set(self.scopes).issubset(PAIRING_SCOPES)
        ):
            raise ValueError("pairing scopes must be non-empty, unique, and allowed")
        if self.revision < 0:
            raise ValueError("pairing material revision must not be negative")
        _require_aware(self.created_at, "created_at")
        _require_aware(self.updated_at, "updated_at")
        if self.updated_at < self.created_at:
            raise ValueError("pairing material update must not precede creation")
        claim_values = (
            self.claim_id,
            self.claimed_by_user_id,
        )
        if any(value is None for value in claim_values) and any(
            value is not None for value in claim_values
        ):
            raise ValueError("pairing claim binding must be complete")
        challenge_values = (
            self.challenge_id,
            self.challenge_digest,
            self.challenge_expires_at,
            self.owner_confirmed_at,
        )
        if any(value is None for value in challenge_values) and any(
            value is not None for value in challenge_values
        ):
            raise ValueError("pairing challenge binding must be complete")
        if self.challenge_digest is not None:
            _require_digest(self.challenge_digest, "challenge_digest")
            assert self.challenge_expires_at is not None
            assert self.owner_confirmed_at is not None
            _require_aware(self.challenge_expires_at, "challenge_expires_at")
            _require_aware(self.owner_confirmed_at, "owner_confirmed_at")
            if self.challenge_expires_at <= self.owner_confirmed_at:
                raise ValueError(
                    "pairing challenge expiry must follow owner confirmation"
                )
            if (
                self.challenge_expires_at - self.owner_confirmed_at
                > PAIRING_CHALLENGE_TTL
            ):
                raise ValueError("pairing challenge must not exceed sixty seconds")
            if self.claim_id is None or self.claimed_by_user_id is None:
                raise ValueError("pairing challenge requires a claim binding")
        if self.confirmation_digest is not None:
            _require_digest(self.confirmation_digest, "confirmation_digest")
            if self.challenge_digest is None:
                raise ValueError("pairing confirmation requires a challenge binding")
        stage_revision = (
            3
            if self.confirmation_digest is not None
            else 2
            if self.challenge_digest is not None
            else 1
        )
        if self.revision not in {stage_revision, stage_revision + 1}:
            raise ValueError("pairing material revision does not match its stage")


class DeviceLifecycleState(str, Enum):
    PENDING = "pending"
    ACTIVE = "active"
    SUSPENDED = "suspended"
    REVOKED = "revoked"
    RETIRED = "retired"


ALLOWED_DEVICE_LIFECYCLE_TRANSITIONS: Final[
    Mapping[DeviceLifecycleState, frozenset[DeviceLifecycleState]]
] = MappingProxyType(
    {
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
)


class InvalidDeviceLifecycleTransition(ValueError):
    """Raised when a device authorization lifecycle edge is not allowed."""


def require_device_lifecycle_transition(
    from_state: DeviceLifecycleState,
    to_state: DeviceLifecycleState,
) -> None:
    """Validate a frozen device authorization state change.

    ASCII state graph::

        pending --> active <--> suspended
           |          |             |
           +------> revoked <-------+
           |          ^
           +------> retired <-------+

    Allowed transitions::

        pending   | active, revoked, retired
        active    | suspended, revoked, retired
        suspended | active, revoked, retired
        revoked   | none
        retired   | none

    Connectivity such as ``offline`` is deliberately not part of this graph.
    """

    if to_state not in ALLOWED_DEVICE_LIFECYCLE_TRANSITIONS[from_state]:
        raise InvalidDeviceLifecycleTransition(
            "device lifecycle transition is not allowed: "
            f"{from_state.value} -> {to_state.value}"
        )


@dataclass(frozen=True, slots=True)
class DeviceLifecycle:
    tenant_id: UUID
    device_id: UUID
    workspace_id: UUID
    agent_id: UUID
    state: DeviceLifecycleState
    revision: int
    updated_at: datetime

    def __post_init__(self) -> None:
        if self.revision < 0:
            raise ValueError("device lifecycle revision must not be negative")
        _require_aware(self.updated_at, "updated_at")


class DeviceCredentialStatus(str, Enum):
    ACTIVE = "active"
    REVOKED = "revoked"
    EXPIRED = "expired"


@dataclass(frozen=True, slots=True)
class DeviceCredential:
    tenant_id: UUID
    credential_id: UUID
    device_id: UUID
    algorithm: str
    key_id: str
    public_key: bytes
    credential_fingerprint: str
    status: DeviceCredentialStatus
    issued_at: datetime
    expires_at: datetime | None
    revoked_at: datetime | None

    def __post_init__(self) -> None:
        if self.algorithm != "ed25519":
            raise ValueError("device credential algorithm must be ed25519")
        if (
            fingerprint_ed25519_public_key(self.public_key)
            != self.credential_fingerprint
        ):
            raise ValueError("device credential fingerprint does not match")
        _require_text(self.key_id, "key_id")
        _require_aware(self.issued_at, "issued_at")
        if self.expires_at is not None:
            _require_aware(self.expires_at, "expires_at")
            if self.expires_at <= self.issued_at:
                raise ValueError("device credential expiry must follow issue time")
        if self.revoked_at is not None:
            _require_aware(self.revoked_at, "revoked_at")
            if self.revoked_at < self.issued_at:
                raise ValueError("device credential revocation must follow issue time")
        if self.status is DeviceCredentialStatus.REVOKED:
            if self.revoked_at is None:
                raise ValueError("revoked device credential requires revoked_at")
        elif self.revoked_at is not None:
            raise ValueError("active or expired credential cannot have revoked_at")

    def revoke(self, now: datetime) -> DeviceCredential:
        """Return a monotonic, idempotently revoked credential."""

        _require_aware(now, "now")
        if self.status is DeviceCredentialStatus.REVOKED:
            return self
        if now < self.issued_at:
            raise ValueError("device credential cannot be revoked before issue")
        return replace(
            self,
            status=DeviceCredentialStatus.REVOKED,
            revoked_at=now,
        )


@dataclass(frozen=True, slots=True)
class PairingSnapshot:
    """One internally consistent view of an offer and its optional binding."""

    offer: PairingOffer
    session: PairingSession | None = None
    material: PairingKeyMaterial | None = None
    lifecycle: DeviceLifecycle | None = None
    credential: DeviceCredential | None = None


@dataclass(frozen=True, slots=True)
class DeviceAuthenticationBinding:
    """Authoritative credential scope used for token issue and WSS admission."""

    tenant_id: UUID
    device_id: UUID
    credential_id: UUID
    workspace_id: UUID
    agent_id: UUID
    scopes: tuple[str, ...]
    public_key: bytes
    lifecycle_state: DeviceLifecycleState
    lifecycle_revision: int
    credential_status: DeviceCredentialStatus
    credential_expires_at: datetime | None

    def __post_init__(self) -> None:
        if (
            not self.scopes
            or len(self.scopes) != len(set(self.scopes))
            or not set(self.scopes).issubset(PAIRING_SCOPES)
        ):
            raise ValueError("device authentication scopes are invalid")
        if len(self.public_key) != 32:
            raise ValueError("device authentication key must contain 32 bytes")
        if self.lifecycle_revision < 0:
            raise ValueError("device lifecycle revision must not be negative")
        if self.credential_expires_at is not None:
            _require_aware(self.credential_expires_at, "credential_expires_at")


@dataclass(frozen=True, slots=True)
class DeviceAuthenticationChallenge:
    """Digest-only, single-use proof challenge."""

    tenant_id: UUID
    challenge_id: UUID
    device_id: UUID
    credential_id: UUID
    challenge_digest: str
    issued_at: datetime
    expires_at: datetime
    consumed_at: datetime | None

    def __post_init__(self) -> None:
        _require_digest(self.challenge_digest, "challenge_digest")
        _require_aware(self.issued_at, "issued_at")
        _require_aware(self.expires_at, "expires_at")
        if self.consumed_at is not None:
            _require_aware(self.consumed_at, "consumed_at")
        if not self.issued_at < self.expires_at:
            raise ValueError("device challenge expiry must follow issue time")
        if self.expires_at - self.issued_at > PAIRING_CHALLENGE_TTL:
            raise ValueError("device challenge must not exceed sixty seconds")
        if self.consumed_at is not None and self.consumed_at < self.issued_at:
            raise ValueError("device challenge consumption must follow issue time")


@dataclass(frozen=True, slots=True)
class DeviceAuthenticationSnapshot:
    binding: DeviceAuthenticationBinding
    challenge: DeviceAuthenticationChallenge | None = None
