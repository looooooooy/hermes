"""Device public identity and challenge-signing boundary."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class DevicePublicIdentity:
    """Public, serializable metadata for one private device identity."""

    key_handle: str
    algorithm: str
    public_key: str
    fingerprint: str


class DeviceIdentityPort(Protocol):
    def check_available(self) -> None:
        """Validate secure identity support without creating or reading a key.

        Input/unit: none. Deadline: no external secret operation is permitted.
        Idempotency: repeatable. Effect: adapter availability validation only.
        Return: ``None``. Errors: secure identity provider unavailable.
        """

    async def get_or_create(self) -> DevicePublicIdentity:
        """Load or atomically create the stable device identity.

        Input/unit: none. Deadline: provider-defined bounded secure-store
        deadline. Idempotency: concurrent creators converge without overwrite.
        Effect: may create one private key in the OS secure store.
        Return: public key, fingerprint, algorithm, and opaque key handle only.
        Errors: unavailable, malformed key, timeout, or cancellation.
        """

    async def sign_challenge(self, key_handle: str, challenge: bytes) -> bytes:
        """Sign one bounded server challenge with the referenced private key.

        Input/unit: opaque key handle and 16..4096 challenge bytes. Deadline:
        provider-defined bounded secure-store deadline. Idempotency: Ed25519
        signing is deterministic and has no durable side effect. Effect:
        secure key read/sign only. Return: raw 64-byte signature. Errors:
        missing/mismatched key, invalid input, unavailable store, or cancellation.
        """

    async def delete_identity(self, key_handle: str) -> bool:
        """Delete the referenced private device key.

        Input/unit: opaque key handle. Deadline: provider-defined bounded
        secure-store deadline. Idempotency: repeated deletion is safe. Effect:
        deletes only the matching OS secure-store key. Return: whether deleted.
        Errors: mismatched handle, unavailable store, timeout, or cancellation.
        """
