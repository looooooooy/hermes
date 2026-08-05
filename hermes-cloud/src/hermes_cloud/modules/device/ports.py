"""Device pairing ports and digest-only persistence commands."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from hermes_cloud.modules.device.domain import (
    PAIRING_CHALLENGE_TTL,
    PAIRING_SCOPES,
    DeviceAuthenticationChallenge,
    DeviceAuthenticationSnapshot,
    DeviceCredential,
    PairingOffer,
    PairingSnapshot,
)

_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_PAIRING_OPERATIONS = frozenset(
    {
        "create",
        "claim",
        "confirm",
        "proof",
        "cancel",
        "revoke",
        "device_challenge",
        "device_token",
    }
)


def _require_digest(value: str, field: str) -> None:
    if _DIGEST.fullmatch(value) is None:
        raise ValueError(f"{field} must be a canonical SHA-256 digest")


def _require_aware(value: datetime, field: str) -> None:
    if value.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone")


@dataclass(frozen=True, slots=True)
class PairingMutation:
    """One digest-bound mutation identity.

    ``request_digest`` covers canonical method, path, principal, and body.
    ``idempotency_key_digest`` and ``principal_digest`` contain no raw secret
    or principal. The repository commits the business effect and replay record
    in one transaction and rejects a reused key with a different digest.
    """

    pairing_mutation_id: UUID
    operation: str
    idempotency_key_digest: str
    principal_digest: str
    request_digest: str
    expected_revision: int
    created_at: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        if self.operation not in _PAIRING_OPERATIONS:
            raise ValueError("pairing mutation operation is invalid")
        for field, value in (
            ("idempotency_key_digest", self.idempotency_key_digest),
            ("principal_digest", self.principal_digest),
            ("request_digest", self.request_digest),
        ):
            _require_digest(value, field)
        if self.expected_revision < 0:
            raise ValueError("expected revision must not be negative")
        _require_aware(self.created_at, "created_at")
        _require_aware(self.expires_at, "expires_at")
        if self.expires_at <= self.created_at:
            raise ValueError("idempotency retention must follow creation")


class PairingIdempotencyConflict(RuntimeError):
    """An idempotency key was reused with a different canonical request."""


class PairingStateConflict(RuntimeError):
    """The expected pairing revision or state no longer exists."""


class PairingNotFound(RuntimeError):
    """The requested pairing resource does not exist."""


class PairingOfferAuthenticationFailed(RuntimeError):
    """The Connector-only pairing offer secret was rejected."""


class PairingClaimUnavailable(RuntimeError):
    """The pairing claim is unavailable without revealing why."""


class PairingClaimRateLimited(RuntimeError):
    """The authenticated owner exhausted the bounded claim window."""

    def __init__(self, retry_after_seconds: int) -> None:
        if not 1 <= retry_after_seconds <= 300:
            raise ValueError("claim retry-after must be between 1 and 300 seconds")
        self.retry_after_seconds = retry_after_seconds
        super().__init__("pairing claims are temporarily rate limited")


class PairingScopeUnavailable(RuntimeError):
    """The authenticated owner cannot bind the requested scope."""


class PairingExpired(RuntimeError):
    """The pairing offer or session is expired."""


class PairingChallengeReplayed(RuntimeError):
    """A single-use challenge was submitted after activation."""


class PairingChallengeExpired(RuntimeError):
    """A single-use device authentication challenge expired unused."""


class DeviceAuthenticationUnavailable(RuntimeError):
    """A device, credential, binding, or challenge is not authorized."""


class DeviceAuthorizationRevoked(DeviceAuthenticationUnavailable):
    """The authoritative device lifecycle is terminally revoked."""


class DeviceAuthorizationSuspended(DeviceAuthenticationUnavailable):
    """The authoritative device lifecycle is reversibly suspended."""


@dataclass(frozen=True, slots=True)
class ClaimPairingCommand:
    pairing_session_id: UUID
    tenant_id: UUID
    owner_user_id: UUID
    workspace_id: UUID
    agent_id: UUID
    device_id: UUID
    device_display_name: str
    scopes: tuple[str, ...]
    pairing_code_digest: str
    expected_revision: int
    now: datetime

    def __post_init__(self) -> None:
        _require_digest(self.pairing_code_digest, "pairing_code_digest")
        if (
            not self.device_display_name.strip()
            or len(self.device_display_name.encode("utf-8")) > 128
        ):
            raise ValueError("device display name is invalid")
        if (
            not self.scopes
            or len(set(self.scopes)) != len(self.scopes)
            or not set(self.scopes).issubset(PAIRING_SCOPES)
        ):
            raise ValueError("pairing scopes are invalid")
        if self.expected_revision != 0:
            raise ValueError("claim requires expected revision zero")
        _require_aware(self.now, "now")


@dataclass(frozen=True, slots=True)
class ConfirmPairingCommand:
    tenant_id: UUID
    owner_user_id: UUID
    pairing_session_id: UUID
    credential_fingerprint: str
    expected_revision: int
    challenge_id: UUID
    challenge_digest: str
    challenge_expires_at: datetime
    now: datetime

    def __post_init__(self) -> None:
        _require_digest(self.credential_fingerprint, "credential_fingerprint")
        _require_digest(self.challenge_digest, "challenge_digest")
        if self.expected_revision != 1:
            raise ValueError("owner confirmation requires expected revision one")
        _require_aware(self.challenge_expires_at, "challenge_expires_at")
        _require_aware(self.now, "now")
        if self.challenge_expires_at <= self.now:
            raise ValueError("challenge expiry must follow owner confirmation")
        if self.challenge_expires_at - self.now > PAIRING_CHALLENGE_TTL:
            raise ValueError("challenge expiry must not exceed sixty seconds")


@dataclass(frozen=True, slots=True)
class ActivatePairingCommand:
    tenant_id: UUID
    pairing_offer_id: UUID
    pairing_session_id: UUID
    bootstrap_secret_digest: str
    challenge_id: UUID
    challenge_digest: str
    confirmation_digest: str
    credential: DeviceCredential
    expected_revision: int
    now: datetime

    def __post_init__(self) -> None:
        for field, value in (
            ("bootstrap_secret_digest", self.bootstrap_secret_digest),
            ("challenge_digest", self.challenge_digest),
            ("confirmation_digest", self.confirmation_digest),
        ):
            _require_digest(value, field)
        if self.expected_revision != 2:
            raise ValueError("proof requires expected revision two")
        if self.credential.tenant_id != self.tenant_id:
            raise ValueError("credential tenant does not match proof")
        _require_aware(self.now, "now")


@dataclass(frozen=True, slots=True)
class CancelPairingCommand:
    tenant_id: UUID
    owner_user_id: UUID
    pairing_session_id: UUID
    expected_revision: int
    now: datetime

    def __post_init__(self) -> None:
        if self.expected_revision < 1:
            raise ValueError("cancel expected revision must be positive")
        _require_aware(self.now, "now")


@dataclass(frozen=True, slots=True)
class RevokeDeviceCommand:
    tenant_id: UUID
    owner_user_id: UUID
    device_id: UUID
    now: datetime

    def __post_init__(self) -> None:
        _require_aware(self.now, "now")


@dataclass(frozen=True, slots=True)
class CreateDeviceChallengeCommand:
    challenge: DeviceAuthenticationChallenge
    now: datetime

    def __post_init__(self) -> None:
        if self.challenge.consumed_at is not None:
            raise ValueError("new device challenge must be unused")
        _require_aware(self.now, "now")
        if self.challenge.issued_at != self.now:
            raise ValueError("device challenge issue time must match operation time")


@dataclass(frozen=True, slots=True)
class ConsumeDeviceChallengeCommand:
    device_id: UUID
    credential_id: UUID
    challenge_id: UUID
    challenge_digest: str
    proof_digest: str
    now: datetime

    def __post_init__(self) -> None:
        _require_digest(self.challenge_digest, "challenge_digest")
        _require_digest(self.proof_digest, "proof_digest")
        _require_aware(self.now, "now")


class PairingRepositoryPort(Protocol):
    """Persist pairing facts through one bounded transaction per operation."""

    def create_offer(
        self,
        offer: PairingOffer,
        *,
        mutation: PairingMutation,
    ) -> PairingOffer:
        """Create a tenant-neutral offer and its replay record atomically.

        Deadline: the caller owns the operation deadline. Idempotency: the same
        operation, principal digest, key digest, and request digest replays the
        stored business result; a different request digest raises
        ``PairingIdempotencyConflict``. Side effects: only public-key material,
        secret digests, and the mutation ledger are persisted.
        """

    def claim_offer(
        self,
        command: ClaimPairingCommand,
        *,
        mutation: PairingMutation,
    ) -> PairingSnapshot:
        """Resolve an offer by code digest and claim it atomically.

        The authenticated owner never supplies or discovers an offer UUID
        before a successful claim. Implementations must bind the digest lookup,
        pending-state check, revision check, and claim update in one CAS.
        """

    def confirm_owner(
        self,
        command: ConfirmPairingCommand,
        *,
        mutation: PairingMutation,
    ) -> PairingSnapshot: ...

    def activate_verified_credential(
        self,
        command: ActivatePairingCommand,
        *,
        mutation: PairingMutation,
    ) -> PairingSnapshot: ...

    def cancel_pairing(
        self,
        command: CancelPairingCommand,
        *,
        mutation: PairingMutation,
    ) -> PairingSnapshot: ...

    def expire_offer(
        self,
        pairing_offer_id: UUID,
        *,
        now: datetime,
    ) -> PairingOffer: ...

    def revoke_device(
        self,
        command: RevokeDeviceCommand,
        *,
        mutation: PairingMutation,
    ) -> PairingSnapshot: ...

    def replay_pairing_mutation(
        self,
        mutation: PairingMutation,
    ) -> PairingSnapshot | None:
        """Return the immutable ledger outcome before current-state checks.

        A matching operation, principal, and idempotency key with a different
        request digest raises ``PairingIdempotencyConflict``.
        """

    def get_offer(
        self,
        pairing_offer_id: UUID,
        *,
        bootstrap_secret_digest: str,
        now: datetime,
    ) -> PairingSnapshot: ...

    def get_pairing_for_proof(
        self,
        pairing_session_id: UUID,
        *,
        bootstrap_secret_digest: str,
        now: datetime,
    ) -> PairingSnapshot: ...

    def get_pairing_for_proof_history(
        self,
        pairing_session_id: UUID,
        *,
        bootstrap_secret_digest: str,
    ) -> PairingSnapshot:
        """Authenticate a proof binding without applying the current TTL."""

    def get_owner_pairing(
        self,
        *,
        tenant_id: UUID,
        owner_user_id: UUID,
        pairing_session_id: UUID,
        now: datetime,
    ) -> PairingSnapshot: ...

    def get_owner_pairing_status(
        self,
        *,
        tenant_id: UUID,
        owner_user_id: UUID,
        pairing_session_id: UUID,
    ) -> PairingSnapshot:
        """Return an immutable owner-scoped snapshot, including terminal history."""

    def create_device_challenge(
        self,
        command: CreateDeviceChallengeCommand,
        *,
        mutation: PairingMutation,
    ) -> DeviceAuthenticationSnapshot: ...

    def consume_device_challenge(
        self,
        command: ConsumeDeviceChallengeCommand,
        *,
        mutation: PairingMutation,
    ) -> DeviceAuthenticationSnapshot: ...

    def active_device_binding(
        self,
        *,
        tenant_id: UUID | None,
        device_id: UUID,
        credential_id: UUID,
        now: datetime,
    ) -> DeviceAuthenticationSnapshot: ...

    def active_legacy_device_binding(
        self,
        *,
        tenant_id: UUID,
        device_id: UUID,
        now: datetime,
    ) -> DeviceAuthenticationSnapshot: ...

    def get_device_challenge(
        self,
        *,
        device_id: UUID,
        credential_id: UUID,
        challenge_id: UUID,
        now: datetime,
    ) -> DeviceAuthenticationSnapshot: ...
