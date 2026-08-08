from __future__ import annotations

import hashlib
from base64 import urlsafe_b64decode

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from hermes_connector.adapters.secure_store_device_identity import (
    SecureStoreDeviceIdentity,
    UnsafeDeviceIdentity,
)


class _MemorySecureStore:
    def __init__(self, secret: bytes | None = None) -> None:
        self.secret = secret
        self.create_attempts: list[bytes] = []
        self.deleted = 0
        self.force_create_race_value: bytes | None = None
        self.replace_before_delete: bytes | None = None
        self.unavailable = False

    def check_available(self) -> None:
        if self.unavailable:
            raise RuntimeError("secure-store-secret-must-not-escape")

    async def read_secret(self) -> bytes | None:
        if self.unavailable:
            raise RuntimeError("secure-store-secret-must-not-escape")
        return self.secret

    async def create_secret(self, secret: bytes) -> bool:
        if self.unavailable:
            raise RuntimeError("secure-store-secret-must-not-escape")
        self.create_attempts.append(secret)
        if self.force_create_race_value is not None:
            self.secret = self.force_create_race_value
            self.force_create_race_value = None
            return False
        if self.secret is not None:
            return False
        self.secret = secret
        return True

    async def write_secret(self, secret: bytes) -> None:
        raise AssertionError("device identity must never overwrite an existing key")

    async def delete_secret(self) -> bool:
        raise AssertionError("device identity must use digest-bound compare-delete")

    async def delete_secret_if_matches(self, expected_sha256: bytes) -> bool:
        if self.unavailable:
            raise RuntimeError("secure-store-secret-must-not-escape")
        if self.replace_before_delete is not None:
            self.secret = self.replace_before_delete
            self.replace_before_delete = None
        if self.secret is None:
            return False
        if hashlib.sha256(self.secret).digest() != expected_sha256:
            return False
        self.secret = None
        self.deleted += 1
        return True


def _decode_public_key(value: str) -> bytes:
    return urlsafe_b64decode(value + "=" * (-len(value) % 4))


@pytest.mark.asyncio
async def test_secure_store_identity_is_stable_public_only_and_signs() -> None:
    store = _MemorySecureStore()
    identity = SecureStoreDeviceIdentity(store)

    first = await identity.get_or_create()
    second = await identity.get_or_create()
    challenge = b"0123456789abcdef0123456789abcdef"
    signature = await identity.sign_challenge(first.key_handle, challenge)

    assert first == second
    assert first.algorithm == "Ed25519"
    assert len(_decode_public_key(first.public_key)) == 32
    assert first.fingerprint.startswith("SHA256:")
    assert first.key_handle.startswith("hermes-device-key:v1:")
    Ed25519PublicKey.from_public_bytes(_decode_public_key(first.public_key)).verify(
        signature,
        challenge,
    )
    assert len(store.create_attempts) == 1
    assert store.secret is not None
    assert store.secret not in repr(identity).encode()
    assert not hasattr(first, "private_key")


@pytest.mark.asyncio
async def test_concurrent_create_loser_loads_winning_key_without_overwrite() -> None:
    winner_store = _MemorySecureStore()
    winner = await SecureStoreDeviceIdentity(winner_store).get_or_create()
    assert winner_store.secret is not None

    racing_store = _MemorySecureStore()
    racing_store.force_create_race_value = winner_store.secret
    identity = SecureStoreDeviceIdentity(
        racing_store,
        random_bytes=lambda _: b"\x11" * 32,
    )

    loaded = await identity.get_or_create()

    assert loaded == winner
    assert len(racing_store.create_attempts) == 1
    assert racing_store.create_attempts[0] != racing_store.secret


@pytest.mark.asyncio
async def test_digest_bound_delete_cannot_remove_recreated_key() -> None:
    store = _MemorySecureStore()
    identity = SecureStoreDeviceIdentity(store)
    old_public = await identity.get_or_create()

    replacement_store = _MemorySecureStore()
    await SecureStoreDeviceIdentity(replacement_store).get_or_create()
    assert replacement_store.secret is not None
    store.replace_before_delete = replacement_store.secret

    assert not await identity.delete_identity(old_public.key_handle)
    assert store.secret == replacement_store.secret
    assert store.deleted == 0


@pytest.mark.asyncio
async def test_corrupt_or_unavailable_store_fails_closed_without_replacement() -> None:
    corrupt = _MemorySecureStore(b"not-a-canonical-ed25519-seed")
    with pytest.raises(UnsafeDeviceIdentity):
        await SecureStoreDeviceIdentity(corrupt).get_or_create()
    assert corrupt.create_attempts == []
    assert corrupt.secret == b"not-a-canonical-ed25519-seed"

    unavailable = _MemorySecureStore()
    unavailable.unavailable = True
    identity = SecureStoreDeviceIdentity(unavailable)
    with pytest.raises(UnsafeDeviceIdentity) as caught:
        await identity.get_or_create()
    assert "secure-store-secret" not in str(caught.value)
    assert unavailable.create_attempts == []


@pytest.mark.asyncio
@pytest.mark.parametrize("challenge", (b"", b"too-short", b"x" * 4_097))
async def test_signing_rejects_unbounded_challenge(challenge: bytes) -> None:
    store = _MemorySecureStore()
    identity = SecureStoreDeviceIdentity(store)
    public = await identity.get_or_create()

    with pytest.raises(UnsafeDeviceIdentity):
        await identity.sign_challenge(public.key_handle, challenge)
