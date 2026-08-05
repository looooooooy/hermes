from __future__ import annotations

import hashlib
from base64 import urlsafe_b64decode

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from hermes_connector.adapters.platform.macos.device_identity import (
    MacOSKeychainDeviceIdentity,
    UnsafeDeviceIdentity,
)


class _MemorySecureStore:
    def __init__(self, secret: bytes | None = None) -> None:
        self.secret = secret
        self.create_attempts: list[bytes] = []
        self.deleted = 0
        self.available_checks = 0
        self.force_create_race_value: bytes | None = None
        self.replace_before_delete: bytes | None = None
        self.unavailable = False

    def check_available(self) -> None:
        self.available_checks += 1
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
        if self.unavailable:
            raise RuntimeError("secure-store-secret-must-not-escape")
        if self.replace_before_delete is not None:
            self.secret = self.replace_before_delete
            self.replace_before_delete = None
        if self.secret is None:
            return False
        self.secret = None
        self.deleted += 1
        return True

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

    def __repr__(self) -> str:
        return "_MemorySecureStore(<redacted>)"


def _decode_public_key(value: str) -> bytes:
    return urlsafe_b64decode(value + "=" * (-len(value) % 4))


@pytest.mark.asyncio
async def test_device_identity_is_stable_public_only_and_signs_challenges() -> None:
    store = _MemorySecureStore()
    identity = MacOSKeychainDeviceIdentity(store)

    first = await identity.get_or_create()
    second = await identity.get_or_create()
    signature = await identity.sign_challenge(
        first.key_handle,
        b"0123456789abcdef0123456789abcdef",
    )

    assert first == second
    assert first.algorithm == "Ed25519"
    assert len(_decode_public_key(first.public_key)) == 32
    assert first.fingerprint.startswith("SHA256:")
    assert first.key_handle.startswith("hermes-device-key:v1:")
    Ed25519PublicKey.from_public_bytes(_decode_public_key(first.public_key)).verify(
        signature,
        b"0123456789abcdef0123456789abcdef",
    )
    assert len(store.create_attempts) == 1
    assert store.secret is not None
    assert store.secret not in repr(identity).encode()
    assert store.secret not in repr(first).encode()
    assert not hasattr(first, "private_key")


@pytest.mark.asyncio
async def test_concurrent_create_loser_loads_winning_key_without_overwrite() -> None:
    winner_store = _MemorySecureStore()
    winner = await MacOSKeychainDeviceIdentity(winner_store).get_or_create()
    assert winner_store.secret is not None

    racing_store = _MemorySecureStore()
    racing_store.force_create_race_value = winner_store.secret
    identity = MacOSKeychainDeviceIdentity(
        racing_store,
        random_bytes=lambda _: b"\x11" * 32,
    )

    loaded = await identity.get_or_create()

    assert loaded == winner
    assert len(racing_store.create_attempts) == 1
    assert racing_store.create_attempts[0] != racing_store.secret


@pytest.mark.asyncio
async def test_device_identity_delete_is_handle_bound_and_idempotent() -> None:
    store = _MemorySecureStore()
    identity = MacOSKeychainDeviceIdentity(store)
    public = await identity.get_or_create()

    with pytest.raises(UnsafeDeviceIdentity):
        await identity.delete_identity("hermes-device-key:v1:wrong")
    assert store.secret is not None

    assert await identity.delete_identity(public.key_handle)
    assert not await identity.delete_identity(public.key_handle)
    assert store.deleted == 1


@pytest.mark.asyncio
async def test_device_identity_delete_cannot_remove_concurrently_recreated_key() -> (
    None
):
    old_store = _MemorySecureStore()
    identity = MacOSKeychainDeviceIdentity(old_store)
    old_public = await identity.get_or_create()

    replacement_store = _MemorySecureStore()
    await MacOSKeychainDeviceIdentity(replacement_store).get_or_create()
    assert replacement_store.secret is not None
    old_store.replace_before_delete = replacement_store.secret

    assert not await identity.delete_identity(old_public.key_handle)
    assert old_store.secret == replacement_store.secret
    assert old_store.deleted == 0


@pytest.mark.asyncio
async def test_corrupt_or_unavailable_secure_store_fails_closed_without_replacement() -> (
    None
):
    corrupt = _MemorySecureStore(b"not-a-canonical-ed25519-seed")
    corrupt_identity = MacOSKeychainDeviceIdentity(corrupt)

    with pytest.raises(UnsafeDeviceIdentity):
        await corrupt_identity.get_or_create()
    assert corrupt.create_attempts == []
    assert corrupt.secret == b"not-a-canonical-ed25519-seed"

    unavailable = _MemorySecureStore()
    unavailable.unavailable = True
    unavailable_identity = MacOSKeychainDeviceIdentity(unavailable)
    with pytest.raises(UnsafeDeviceIdentity) as caught:
        await unavailable_identity.get_or_create()
    assert "secure-store-secret" not in str(caught.value)
    with pytest.raises(UnsafeDeviceIdentity):
        await unavailable_identity.sign_challenge(
            "hermes-device-key:v1:unknown",
            b"0123456789abcdef",
        )
    assert unavailable.create_attempts == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "challenge",
    (
        b"",
        b"too-short",
        b"x" * 4_097,
    ),
)
async def test_challenge_signing_rejects_unbounded_or_invalid_input(
    challenge: bytes,
) -> None:
    store = _MemorySecureStore()
    identity = MacOSKeychainDeviceIdentity(store)
    public = await identity.get_or_create()

    with pytest.raises(UnsafeDeviceIdentity):
        await identity.sign_challenge(public.key_handle, challenge)
