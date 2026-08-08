from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path

import pytest

from hermes_connector.adapters.platform.windows.dpapi_secret_store import (
    WindowsDPAPISecretStore,
)
from hermes_connector.adapters.platform.windows.private_state import (
    UnsafeWindowsPrivateState,
    atomic_write_private_file,
    read_private_file,
    validate_private_directory,
    validate_private_file,
)
from hermes_connector.ports.secure_storage import SecureStorageError

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows security APIs required")


def _store(tmp_path: Path, *, account: str = "connector-instance:test") -> WindowsDPAPISecretStore:
    root = tmp_path / "hermes-private-secrets"
    WindowsDPAPISecretStore.provision_root(root)
    store = WindowsDPAPISecretStore(
        root_directory=root,
        service="wiki.seaotter.hermes.connector.test.v1",
        account=account,
    )
    store.check_available()
    return store


def test_private_root_and_atomic_file_keep_current_user_only_acl(tmp_path: Path) -> None:
    store = _store(tmp_path)
    validate_private_directory(store.path.parent)

    target = store.path.parent / "state.bin"
    atomic_write_private_file(target, b"state-v1", maximum=1024)
    validate_private_file(target)
    assert read_private_file(target, maximum=1024) == b"state-v1"

    atomic_write_private_file(target, b"state-v2", maximum=1024)
    validate_private_file(target)
    assert read_private_file(target, maximum=1024) == b"state-v2"


def test_private_state_rejects_default_inherited_acl_directory(tmp_path: Path) -> None:
    inherited = tmp_path / "inherited-default"
    inherited.mkdir()
    with pytest.raises(UnsafeWindowsPrivateState):
        validate_private_directory(inherited)


@pytest.mark.asyncio
async def test_dpapi_roundtrip_create_only_upsert_and_compare_delete(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = b"first-secret-token"
    second = b"second-secret-token"

    assert await store.read_secret() is None
    assert await store.create_secret(first) is True
    assert await store.create_secret(second) is False
    assert await store.read_secret() == first

    protected = read_private_file(store.path, maximum=100_000)
    assert protected is not None
    assert first not in protected
    assert second not in protected

    await store.write_secret(second)
    assert await store.read_secret() == second
    assert await store.delete_secret_if_matches(hashlib.sha256(first).digest()) is False
    assert await store.read_secret() == second
    assert await store.delete_secret_if_matches(hashlib.sha256(second).digest()) is True
    assert await store.read_secret() is None
    assert await store.delete_secret() is False


@pytest.mark.asyncio
async def test_dpapi_concurrent_create_has_exactly_one_winner(tmp_path: Path) -> None:
    store_a = _store(tmp_path, account="connector-instance:race")
    store_b = WindowsDPAPISecretStore(
        root_directory=store_a.path.parent,
        service="wiki.seaotter.hermes.connector.test.v1",
        account="connector-instance:race",
    )
    first = b"race-secret-a"
    second = b"race-secret-b"

    results = await asyncio.gather(
        store_a.create_secret(first),
        store_b.create_secret(second),
    )
    assert sorted(results) == [False, True]
    assert await store_a.read_secret() in {first, second}


@pytest.mark.asyncio
async def test_dpapi_entropy_rejects_cross_slot_ciphertext_substitution(tmp_path: Path) -> None:
    source = _store(tmp_path, account="connector-instance:source")
    target = _store(tmp_path, account="connector-instance:target")
    secret = b"slot-bound-secret"
    assert await source.create_secret(secret) is True

    protected = read_private_file(source.path, maximum=100_000)
    assert protected is not None
    atomic_write_private_file(target.path, protected, maximum=100_000)

    with pytest.raises(SecureStorageError):
        await target.read_secret()


@pytest.mark.asyncio
async def test_dpapi_rejects_multiline_or_nul_secret_payloads(tmp_path: Path) -> None:
    store = _store(tmp_path)
    for secret in (b"line-one\nline-two", b"nul\x00secret", b""):
        with pytest.raises(SecureStorageError):
            await store.write_secret(secret)
