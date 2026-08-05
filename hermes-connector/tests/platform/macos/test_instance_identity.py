from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import UUID

import pytest

from hermes_connector.adapters.platform.macos.instance_identity import (
    MacOSInstanceIdentityStore,
    UnsafeInstanceIdentity,
)


def test_instance_identifiers_are_atomic_private_and_stable(tmp_path: Path) -> None:
    state_file = tmp_path / "instances.json"
    store = MacOSInstanceIdentityStore(state_file)

    first = store.load_or_create()
    second = store.load_or_create()

    assert first == second
    assert isinstance(first.connector_instance_id, UUID)
    assert isinstance(first.client_instance_id, UUID)
    assert first.connector_instance_id != first.client_instance_id
    assert state_file.stat().st_mode & 0o777 == 0o600
    assert json.loads(state_file.read_text(encoding="utf-8")) == {
        "client_instance_id": str(first.client_instance_id),
        "connector_instance_id": str(first.connector_instance_id),
        "version": 1,
    }


def test_concurrent_first_loaders_converge_on_one_persisted_identity(
    tmp_path: Path,
) -> None:
    state_file = tmp_path / "instances.json"

    with ThreadPoolExecutor(max_workers=8) as executor:
        identities = tuple(
            executor.map(
                lambda _: MacOSInstanceIdentityStore(state_file).load_or_create(),
                range(16),
            )
        )

    assert len(set(identities)) == 1
    assert not tuple(tmp_path.glob(".instances.json.*.tmp"))


def test_check_path_has_no_creation_effect(tmp_path: Path) -> None:
    state_file = tmp_path / "instances.json"

    MacOSInstanceIdentityStore(state_file).check_path()

    assert not state_file.exists()


@pytest.mark.parametrize("unsafe_kind", ("corrupt", "mode", "symlink", "relative"))
def test_corrupt_or_unsafe_identity_state_fails_closed_without_replacement(
    tmp_path: Path,
    unsafe_kind: str,
) -> None:
    state_file = tmp_path / "instances.json"
    state_file.write_text("{not-json", encoding="utf-8")
    state_file.chmod(0o600)
    configured_path = state_file
    if unsafe_kind == "mode":
        state_file.chmod(0o644)
    elif unsafe_kind == "symlink":
        symlink = tmp_path / "instances-link.json"
        symlink.symlink_to(state_file)
        configured_path = symlink
    elif unsafe_kind == "relative":
        configured_path = Path("instances.json")

    original = state_file.read_bytes()
    with pytest.raises(UnsafeInstanceIdentity):
        MacOSInstanceIdentityStore(configured_path).load_or_create()

    assert state_file.read_bytes() == original
