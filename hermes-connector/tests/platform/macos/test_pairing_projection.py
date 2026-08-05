from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest

from hermes_connector.adapters.platform.macos.pairing_projection import (
    MacOSPairedProjectionStore,
    MacOSPairingOfferProjectionStore,
    UnsafePairingProjection,
)
from hermes_connector.domain.pairing import (
    PairedProjection,
    PairingOfferProjection,
)

NOW = datetime(2026, 7, 31, 12, 0, 0, tzinfo=UTC)
CONNECTOR_ROOT = Path(__file__).resolve().parents[3]


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


@pytest.mark.asyncio
async def test_paired_projection_is_atomic_private_and_contains_no_secret(
    tmp_path: Path,
) -> None:
    tmp_path.chmod(0o700)
    path = tmp_path / "paired.json"
    store = MacOSPairedProjectionStore(path)
    projection = _paired_projection()

    await store.save(projection)

    assert await store.load() == projection
    assert path.stat().st_mode & 0o777 == 0o600
    raw = path.read_text(encoding="utf-8")
    assert json.loads(raw) == {
        "agent_id": "99999999-9999-4999-8999-999999999999",
        "credential_fingerprint": "SHA256:" + "B" * 43,
        "credential_id": "88888888-8888-4888-8888-888888888888",
        "device_id": "77777777-7777-4777-8777-777777777777",
        "key_handle": "hermes-device-key:v1:fingerprint",
        "lifecycle_state": "active",
        "scopes": ["session.observe", "session.control.request"],
        "tenant_id": "66666666-6666-4666-8666-666666666666",
        "token_expires_at": "2026-07-31T12:05:00Z",
        "version": 1,
    }
    for forbidden in (
        "access_token",
        "pairing_offer_secret",
        "pairing_code",
        "signing_payload",
        "signature",
    ):
        assert forbidden not in raw
    assert not tuple(tmp_path.glob(".paired.json.*.tmp"))


@pytest.mark.asyncio
async def test_temporary_offer_projection_omits_code_and_secret(tmp_path: Path) -> None:
    tmp_path.chmod(0o700)
    path = tmp_path / "pairing-offer.json"
    store = MacOSPairingOfferProjectionStore(path)
    projection = PairingOfferProjection(
        pairing_offer_id=UUID("22222222-2222-4222-8222-222222222222"),
        key_handle="hermes-device-key:v1:fingerprint",
        credential_fingerprint="SHA256:" + "B" * 43,
        expires_at=NOW + timedelta(seconds=300),
    )

    await store.save(projection)

    assert await store.load() == projection
    raw = path.read_text(encoding="utf-8")
    assert path.stat().st_mode & 0o777 == 0o600
    assert "pairing_code" not in raw
    assert "pairing_offer_secret" not in raw


@pytest.mark.asyncio
async def test_temporary_offer_compare_delete_preserves_newer_offer(
    tmp_path: Path,
) -> None:
    tmp_path.chmod(0o700)
    store = MacOSPairingOfferProjectionStore(tmp_path / "pairing-offer.json")
    current = PairingOfferProjection(
        pairing_offer_id=UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
        key_handle="hermes-device-key:v1:fingerprint",
        credential_fingerprint="SHA256:" + "B" * 43,
        expires_at=NOW + timedelta(seconds=300),
    )
    await store.save(current)

    assert (
        await store.delete_if_matches(UUID("22222222-2222-4222-8222-222222222222"))
        is False
    )
    assert await store.load() == current
    assert await store.delete_if_matches(current.pairing_offer_id) is True
    assert await store.load() is None


@pytest.mark.asyncio
async def test_two_process_adapters_serialize_save_against_stale_compare_delete(
    tmp_path: Path,
) -> None:
    tmp_path.chmod(0o700)
    path = tmp_path / "pairing-offer.json"
    delete_paused = tmp_path / "delete-paused"
    release_delete = tmp_path / "release-delete"
    old_offer_id = UUID("22222222-2222-4222-8222-222222222222")
    new_projection = PairingOfferProjection(
        pairing_offer_id=UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
        key_handle="hermes-device-key:v1:fingerprint",
        credential_fingerprint="SHA256:" + "B" * 43,
        expires_at=NOW + timedelta(seconds=600),
    )
    store = MacOSPairingOfferProjectionStore(path)
    await store.save(
        PairingOfferProjection(
            pairing_offer_id=old_offer_id,
            key_handle="hermes-device-key:v1:fingerprint",
            credential_fingerprint="SHA256:" + "B" * 43,
            expires_at=NOW + timedelta(seconds=300),
        )
    )
    script = """
import asyncio
import sys
import time
from pathlib import Path
from uuid import UUID

from hermes_connector.adapters.platform.macos.pairing_projection import (
    MacOSPairingOfferProjectionStore,
)

target = Path(sys.argv[1])
paused = Path(sys.argv[2])
release = Path(sys.argv[3])
original_unlink = Path.unlink

def controlled_unlink(path, *args, **kwargs):
    if path == target and not paused.exists():
        paused.write_text("ready", encoding="utf-8")
        while not release.exists():
            time.sleep(0.01)
    return original_unlink(path, *args, **kwargs)

Path.unlink = controlled_unlink
deleted = asyncio.run(
    MacOSPairingOfferProjectionStore(target).delete_if_matches(
        UUID("22222222-2222-4222-8222-222222222222")
    )
)
raise SystemExit(0 if deleted else 4)
"""
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(
        filter(
            None,
            (
                str(CONNECTOR_ROOT / "src"),
                environment.get("PYTHONPATH"),
            ),
        )
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        script,
        str(path),
        str(delete_paused),
        str(release_delete),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=environment,
    )
    save_task: asyncio.Task[None] | None = None
    save_completed_while_delete_paused = False
    try:
        for _ in range(200):
            if delete_paused.exists():
                break
            if process.returncode is not None:
                break
            await asyncio.sleep(0.01)
        assert delete_paused.exists()
        save_task = asyncio.create_task(store.save(new_projection))
        await asyncio.sleep(0.2)
        save_completed_while_delete_paused = save_task.done()
    finally:
        release_delete.write_text("release", encoding="utf-8")
        if save_task is not None:
            await save_task
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=5)

    assert process.returncode == 0, (
        stdout.decode("utf-8", errors="replace"),
        stderr.decode("utf-8", errors="replace"),
    )
    assert save_completed_while_delete_paused is False
    assert await store.load() == new_projection


@pytest.mark.asyncio
@pytest.mark.parametrize("unsafe_kind", ("corrupt", "mode", "symlink"))
async def test_unsafe_paired_projection_fails_closed(
    tmp_path: Path,
    unsafe_kind: str,
) -> None:
    tmp_path.chmod(0o700)
    path = tmp_path / "paired.json"
    path.write_text("{not-json", encoding="utf-8")
    path.chmod(0o600)
    configured_path = path
    if unsafe_kind == "mode":
        path.chmod(0o644)
    elif unsafe_kind == "symlink":
        link = tmp_path / "paired-link.json"
        link.symlink_to(path)
        configured_path = link

    with pytest.raises(UnsafePairingProjection):
        await MacOSPairedProjectionStore(configured_path).load()
