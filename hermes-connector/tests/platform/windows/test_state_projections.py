from __future__ import annotations

import asyncio
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest

import hermes_connector.adapters.platform.windows.pairing_projection as windows_pairing
from hermes_connector.adapters.platform.windows.instance_identity import (
    UnsafeInstanceIdentity,
    WindowsInstanceIdentityStore,
)
from hermes_connector.adapters.platform.windows.pairing_projection import (
    UnsafePairingProjection,
    WindowsPairedProjectionStore,
    WindowsPairingOfferProjectionStore,
)
from hermes_connector.adapters.platform.windows.private_state import (
    atomic_write_private_file,
    ensure_private_directory,
    read_private_file,
    validate_private_file,
)
from hermes_connector.domain.pairing import PairedProjection, PairingOfferProjection

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows private state required")
NOW = datetime(2026, 8, 8, 1, 0, 0, tzinfo=UTC)


def _root(tmp_path: Path) -> Path:
    return ensure_private_directory(tmp_path / "state")


def _paired_projection() -> PairedProjection:
    return PairedProjection(
        tenant_id=UUID("66666666-6666-4666-8666-666666666666"),
        device_id=UUID("77777777-7777-4777-8777-777777777777"),
        credential_id=UUID("88888888-8888-4888-8888-888888888888"),
        agent_id=UUID("99999999-9999-4999-8999-999999999999"),
        scopes=("session.observe", "session.control.request"),
        key_handle="hermes-device-key:v1:fingerprint",
        credential_fingerprint="SHA256:" + "B" * 43,
        token_expires_at=NOW + timedelta(seconds=300),
        lifecycle_state="active",
    )


def _offer(identifier: str, *, seconds: int = 300) -> PairingOfferProjection:
    return PairingOfferProjection(
        pairing_offer_id=UUID(identifier),
        key_handle="hermes-device-key:v1:fingerprint",
        credential_fingerprint="SHA256:" + "B" * 43,
        expires_at=NOW + timedelta(seconds=seconds),
    )


def test_windows_instance_identity_is_private_stable_and_concurrent(tmp_path: Path) -> None:
    root = _root(tmp_path)
    path = root / "instances.json"

    with ThreadPoolExecutor(max_workers=8) as executor:
        identities = tuple(
            executor.map(
                lambda _: WindowsInstanceIdentityStore(path).load_or_create(),
                range(16),
            )
        )

    assert len(set(identities)) == 1
    current = identities[0]
    assert current.connector_instance_id != current.client_instance_id
    validate_private_file(path)
    raw = read_private_file(path, maximum=4_096)
    assert raw is not None
    assert json.loads(raw) == {
        "client_instance_id": str(current.client_instance_id),
        "connector_instance_id": str(current.connector_instance_id),
        "version": 1,
    }


def test_windows_instance_identity_corruption_is_not_replaced(tmp_path: Path) -> None:
    root = _root(tmp_path)
    path = root / "instances.json"
    atomic_write_private_file(path, b"{not-json", maximum=4_096)
    original = read_private_file(path, maximum=4_096)

    with pytest.raises(UnsafeInstanceIdentity):
        WindowsInstanceIdentityStore(path).load_or_create()

    assert read_private_file(path, maximum=4_096) == original


@pytest.mark.asyncio
async def test_windows_pairing_projection_is_private_and_contains_no_secret(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    path = root / "paired.json"
    projection = _paired_projection()
    store = WindowsPairedProjectionStore(path)

    await store.save(projection)

    assert await store.load() == projection
    validate_private_file(path)
    raw_bytes = read_private_file(path, maximum=16_384)
    assert raw_bytes is not None
    raw = raw_bytes.decode("utf-8")
    assert json.loads(raw)["lifecycle_state"] == "active"
    for forbidden in (
        "access_token",
        "pairing_offer_secret",
        "pairing_code",
        "signing_payload",
        "signature",
    ):
        assert forbidden not in raw


@pytest.mark.asyncio
async def test_windows_offer_compare_delete_preserves_newer_offer(tmp_path: Path) -> None:
    root = _root(tmp_path)
    store = WindowsPairingOfferProjectionStore(root / "pairing-offer.json")
    current = _offer("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    await store.save(current)

    assert not await store.delete_if_matches(
        UUID("22222222-2222-4222-8222-222222222222")
    )
    assert await store.load() == current
    assert await store.delete_if_matches(current.pairing_offer_id)
    assert await store.load() is None


@pytest.mark.asyncio
async def test_windows_offer_save_waits_for_compare_delete_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _root(tmp_path)
    path = root / "pairing-offer.json"
    old = _offer("22222222-2222-4222-8222-222222222222")
    new = _offer("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", seconds=600)
    deleter = WindowsPairingOfferProjectionStore(path)
    saver = WindowsPairingOfferProjectionStore(path)
    await deleter.save(old)

    paused = threading.Event()
    release = threading.Event()
    original_delete = windows_pairing.delete_private_file

    def controlled_delete(candidate: str | Path) -> bool:
        if Path(candidate) == path and not paused.is_set():
            paused.set()
            if not release.wait(timeout=5):
                raise RuntimeError("test release timed out")
        return original_delete(candidate)

    monkeypatch.setattr(windows_pairing, "delete_private_file", controlled_delete)
    delete_task = asyncio.create_task(deleter.delete_if_matches(old.pairing_offer_id))
    assert await asyncio.to_thread(paused.wait, 2)
    save_task = asyncio.create_task(saver.save(new))
    await asyncio.sleep(0.1)
    assert not save_task.done()

    release.set()
    assert await delete_task
    await save_task
    assert await saver.load() == new


@pytest.mark.asyncio
async def test_windows_projection_corruption_fails_closed_without_overwrite(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    path = root / "paired.json"
    atomic_write_private_file(path, b"{not-json", maximum=16_384)
    original = read_private_file(path, maximum=16_384)

    with pytest.raises(UnsafePairingProjection):
        await WindowsPairedProjectionStore(path).save(_paired_projection())

    assert read_private_file(path, maximum=16_384) == original
