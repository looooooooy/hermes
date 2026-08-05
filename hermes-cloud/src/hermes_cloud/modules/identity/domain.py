"""Infrastructure-neutral identity values for cloud clients."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

_DIGEST = re.compile(r"^[0-9a-f]{64}$")


def require_aware_identity_time(value: datetime, field: str) -> None:
    if value.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone")


def require_sha256_digest(value: str, field: str) -> None:
    if _DIGEST.fullmatch(value) is None:
        raise ValueError(f"{field} must be a canonical SHA-256 digest")


def sha256_token_digest(token: str) -> str:
    """Digest an opaque token before crossing the persistence boundary."""

    if not isinstance(token, str) or not token:
        raise ValueError("token must not be empty")
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class Argon2PasswordHasher:
    """Hash and verify passwords while exposing only Argon2id encoded hashes."""

    def __init__(self, hasher: PasswordHasher | None = None) -> None:
        self._hasher = hasher or PasswordHasher()

    def hash(self, password: str) -> str:
        if not isinstance(password, str) or not password:
            raise ValueError("password must not be empty")
        password_hash = self._hasher.hash(password)
        if not password_hash.startswith("$argon2id$"):
            raise RuntimeError("password hasher did not produce Argon2id")
        return password_hash

    def verify(self, password_hash: str, password: str) -> bool:
        if not password_hash.startswith("$argon2id$"):
            return False
        try:
            return self._hasher.verify(password_hash, password)
        except (InvalidHashError, VerificationError):
            return False


@dataclass(frozen=True, slots=True)
class PasswordCredential:
    tenant_id: UUID
    credential_id: UUID
    user_id: UUID
    subject: str
    password_hash: str
    status: str
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        if not self.subject:
            raise ValueError("credential subject must not be empty")
        if not self.password_hash.startswith("$argon2id$"):
            raise ValueError("password hash must use Argon2id")
        if self.status not in {"active", "disabled"}:
            raise ValueError("credential status is invalid")
        require_aware_identity_time(self.created_at, "created_at")
        require_aware_identity_time(self.updated_at, "updated_at")


@dataclass(frozen=True, slots=True)
class RefreshSession:
    tenant_id: UUID
    refresh_session_id: UUID
    user_id: UUID
    token_digest: str
    rotation: int
    created_at: datetime
    rotated_at: datetime | None
    revoked_at: datetime | None
    expires_at: datetime
    retention_until: datetime

    def __post_init__(self) -> None:
        require_sha256_digest(self.token_digest, "token_digest")
        if self.rotation < 0:
            raise ValueError("refresh rotation must not be negative")
        for field, value in (
            ("created_at", self.created_at),
            ("expires_at", self.expires_at),
            ("retention_until", self.retention_until),
        ):
            require_aware_identity_time(value, field)
        for field, value in (
            ("rotated_at", self.rotated_at),
            ("revoked_at", self.revoked_at),
        ):
            if value is not None:
                require_aware_identity_time(value, field)
        if self.expires_at <= self.created_at:
            raise ValueError("refresh expiry must follow creation")
        if self.retention_until < self.expires_at:
            raise ValueError("refresh retention must include expiry")


@dataclass(frozen=True, slots=True)
class WebSocketTicketClaim:
    tenant_id: UUID
    ticket_digest: str
    principal_type: str
    principal_id: UUID
    refresh_session_id: UUID
    session_id: UUID | None

    def __post_init__(self) -> None:
        require_sha256_digest(self.ticket_digest, "ticket_digest")
        if self.principal_type not in {"user", "device", "connector"}:
            raise ValueError("ticket principal type is invalid")
        if self.session_id is not None and not isinstance(self.session_id, UUID):
            raise ValueError("ticket session id is invalid")


@dataclass(frozen=True, slots=True)
class WebSocketTicket:
    tenant_id: UUID
    ticket_id: UUID
    ticket_digest: str
    principal_type: str
    principal_id: UUID
    refresh_session_id: UUID
    session_id: UUID | None
    observer_scope: tuple[str, ...]
    issued_at: datetime
    expires_at: datetime
    consumed_at: datetime | None
    retention_until: datetime

    def __post_init__(self) -> None:
        WebSocketTicketClaim(
            tenant_id=self.tenant_id,
            ticket_digest=self.ticket_digest,
            principal_type=self.principal_type,
            principal_id=self.principal_id,
            refresh_session_id=self.refresh_session_id,
            session_id=self.session_id,
        )
        if not self.observer_scope or len(set(self.observer_scope)) != len(
            self.observer_scope
        ):
            raise ValueError("observer scope must be non-empty and unique")
        for field, value in (
            ("issued_at", self.issued_at),
            ("expires_at", self.expires_at),
            ("retention_until", self.retention_until),
        ):
            require_aware_identity_time(value, field)
        if self.consumed_at is not None:
            require_aware_identity_time(self.consumed_at, "consumed_at")
        if self.expires_at <= self.issued_at:
            raise ValueError("ticket expiry must follow issue time")
        if self.retention_until < self.expires_at:
            raise ValueError("ticket retention must include expiry")


class RefreshSessionUnavailable(RuntimeError):
    """The expected refresh session state no longer exists."""


class WebSocketTicketUnavailable(RuntimeError):
    """The ticket is absent, expired, consumed, or bound to another principal."""
