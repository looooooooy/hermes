"""Secure secret storage boundary used by platform-specific adapters."""

from __future__ import annotations

from typing import Protocol


class SecureStorageError(ValueError):
    """A platform secure store rejected or could not complete an operation."""


class SecureSecretStorePort(Protocol):
    def check_available(self) -> None:
        """Validate adapter availability without reading or mutating secrets.

        Input/unit: none. Deadline: no external operation is permitted.
        Idempotency: repeatable. Effect: metadata-only adapter validation.
        Return: ``None``. Errors: secure storage unavailable or unsafe.
        """

    async def read_secret(self) -> bytes | None:
        """Read one bounded opaque secret.

        Input/unit: none. Deadline: adapter-defined bounded storage deadline.
        Idempotency: repeatable lookup. Effect: secure storage read only.
        Return: secret bytes or ``None`` when absent. Errors: unavailable,
        timeout, malformed secret, or cancellation.
        """

    async def create_secret(self, secret: bytes) -> bool:
        """Create a secret only when absent.

        Input/unit: bounded secret bytes. Deadline: adapter-defined bounded
        storage deadline. Idempotency: duplicate create returns ``False`` and
        never overwrites. Effect: one secure storage create.
        Return: whether this call created the value. Errors: unavailable,
        timeout, unsafe input, or cancellation.
        """

    async def write_secret(self, secret: bytes) -> None:
        """Create or replace one bounded opaque secret.

        Input/unit: bounded secret bytes. Deadline: adapter-defined bounded
        storage deadline. Idempotency: same input converges on the same value.
        Effect: secure storage upsert. Return: ``None``. Errors: unavailable,
        timeout, unsafe input, or cancellation.
        """

    async def delete_secret(self) -> bool:
        """Delete one opaque secret when present.

        Input/unit: none. Deadline: adapter-defined bounded storage deadline.
        Idempotency: repeated deletion is safe. Effect: secure storage delete.
        Return: whether a value was deleted. Errors: unavailable, timeout, or
        cancellation.
        """

    async def delete_secret_if_matches(self, expected_sha256: bytes) -> bool:
        """Atomically delete only the exact secret version previously read.

        Input/unit: 32-byte SHA-256 digest of the expected stored bytes.
        Deadline: adapter-defined bounded storage deadline. Idempotency:
        repeated or stale deletion returns ``False``. Effect: deletes the
        matching secure-store item through an atomic verifier-bound delete; a
        concurrently updated or recreated item must survive. Return: whether
        the matching value was deleted. Errors: invalid digest, unavailable
        store, timeout, or cancellation.
        """
