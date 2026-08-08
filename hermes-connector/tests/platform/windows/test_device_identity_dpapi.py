from __future__ import annotations

import asyncio
import os
from base64 import urlsafe_b64decode

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from hermes_connector.adapters.platform.windows.dpapi_secret_store import (
    WindowsDPAPISecretStore,
)
from hermes_connector.adapters.secure_store_device_identity import (
    SecureStoreDeviceIdentity,
)

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI required")


def _decode_public_key(value: str) -> bytes:
    return urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _store(tmp_path, *, account: str = "connector-instance:test") -> WindowsDPAPISecretStore:
    root = tmp_path / "secure"
    if not root.exists():
        WindowsDPAPISecretStore.provision_root(root)
    return WindowsDPAPISecretStore(
        root_directory=root,
        service="wiki.seaotter.hermes.connector.device-key.v1",
        account=account,
    )


@pytest.mark.asyncio
async def test_device_identity_is_stable_and_signs_over_dpapi(tmp_path) -> None:
    store = _store(tmp_path)
    identity = SecureStoreDeviceIdentity(store)

    first = await identity.get_or_create()
    second = await SecureStoreDeviceIdentity(store).get_or_create()
    challenge = b"0123456789abcdef0123456789abcdef"
    signature = await identity.sign_challenge(first.key_handle, challenge)

    assert first == second
    assert first.algorithm == "Ed25519"
    Ed25519PublicKey.from_public_bytes(_decode_public_key(first.public_key)).verify(
        signature,
        challenge,
    )
    protected = store.path.read_bytes()
    plaintext = await store.read_secret()
    assert plaintext is not None
    assert plaintext not in protected

    assert await identity.delete_identity(first.key_handle)
    assert not await identity.delete_identity(first.key_handle)


@pytest.mark.asyncio
async def test_parallel_device_creators_converge_on_one_dpapi_identity(tmp_path) -> None:
    store = _store(tmp_path)
    first = SecureStoreDeviceIdentity(store, random_bytes=lambda _: b"\x11" * 32)
    second = SecureStoreDeviceIdentity(store, random_bytes=lambda _: b"\x22" * 32)

    left, right = await asyncio.gather(
        first.get_or_create(),
        second.get_or_create(),
    )

    assert left == right
    challenge = b"parallel-device-identity-challenge"
    signature = await second.sign_challenge(right.key_handle, challenge)
    Ed25519PublicKey.from_public_bytes(_decode_public_key(right.public_key)).verify(
        signature,
        challenge,
    )
