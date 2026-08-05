from __future__ import annotations

import asyncio
import json
import os
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from hermes_connector.adapters.platform.macos import status_receipt as receipt_module
from hermes_connector.adapters.platform.macos.status_receipt import (
    MacOSStatusReceiptStore,
)
from hermes_connector.application.readiness_status import ReadinessStatusComponent
from hermes_connector.domain.cloud_session import CloudSessionState
from hermes_connector.domain.local_gateway import (
    LocalRuntimeAuthority,
    ProcessIdentityEvidence,
)

NOW = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
PROCESS = ProcessIdentityEvidence(
    start_time_ns=1_786_000_000_123_000_000,
    executable_path=Path("/Applications/Hermes Connector.app/Contents/MacOS/python"),
    executable_device=41,
    executable_inode=73,
)
AUTHORITY = LocalRuntimeAuthority(
    profile="default",
    runtime_generation="runtime-generation-17",
    instance_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    host_bundle_id="com.nousresearch.hermes",
    process_identity=ProcessIdentityEvidence(
        start_time_ns=1_785_999_000_000_000_000,
        executable_path=Path("/Applications/Hermes.app/Contents/MacOS/Hermes"),
        executable_device=31,
        executable_inode=63,
    ),
    required_capabilities=("session.observe",),
    optional_capabilities=("session.control", "session.catalog.v1"),
)
RELEASE_ID = "2026.08.03-b2a"
EXPECTED_FIELDS = {
    "release_id",
    "pid",
    "process_start_time_ns",
    "process_executable",
    "process_executable_device",
    "process_executable_inode",
    "runtime_generation",
    "local_authority_identity",
    "cloud_state",
    "updated_at",
    "ready",
}


class _ReadySource:
    def __init__(self, ready: bool) -> None:
        self.value = ready

    async def ready(self) -> bool:
        return self.value


class _CloudSource(_ReadySource):
    def __init__(self, ready: bool, state: CloudSessionState) -> None:
        super().__init__(ready)
        self.state = state


def _store(tmp_path: Path) -> MacOSStatusReceiptStore:
    state = tmp_path / "state"
    state.mkdir(mode=0o700, parents=True)
    return MacOSStatusReceiptStore(state / "status.json")


def _component(
    store: MacOSStatusReceiptStore,
    *,
    authority: LocalRuntimeAuthority | None = AUTHORITY,
    storage: _ReadySource | None = None,
    directory: _ReadySource | None = None,
    cloud: _CloudSource | None = None,
    refresh_interval_seconds: float = 0.001,
) -> tuple[
    ReadinessStatusComponent,
    _ReadySource,
    _ReadySource,
    _CloudSource,
]:
    storage_source = storage or _ReadySource(True)
    directory_source = directory or _ReadySource(True)
    cloud_source = cloud or _CloudSource(True, CloudSessionState.ACTIVE)

    async def current_authority() -> LocalRuntimeAuthority | None:
        return authority

    component = ReadinessStatusComponent(
        store=store,
        release_id=RELEASE_ID,
        local_authority=current_authority,
        storage=storage_source,
        directory=directory_source,
        cloud=cloud_source,
        pid=os.getpid(),
        process_identity_provider=lambda pid: PROCESS if pid == os.getpid() else None,
        now=lambda: NOW,
        refresh_interval_seconds=refresh_interval_seconds,
    )
    return component, storage_source, directory_source, cloud_source


@pytest.mark.asyncio
async def test_receipt_is_published_only_after_every_activation_dependency_is_ready(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    component, _, _, _ = _component(store)

    await component.start()
    assert not store.path.exists()

    assert await component.ready() is True

    metadata = store.path.lstat()
    assert stat.S_ISREG(metadata.st_mode)
    assert stat.S_IMODE(metadata.st_mode) == 0o600
    assert metadata.st_uid == os.geteuid()
    payload = json.loads(store.path.read_bytes())
    assert set(payload) == EXPECTED_FIELDS
    assert payload == {
        "cloud_state": "active",
        "local_authority_identity": {
            "host_bundle_id": "com.nousresearch.hermes",
            "instance_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            "profile": "default",
        },
        "pid": os.getpid(),
        "process_executable": str(PROCESS.executable_path),
        "process_executable_device": 41,
        "process_executable_inode": 73,
        "process_start_time_ns": 1_786_000_000_123_000_000,
        "ready": True,
        "release_id": RELEASE_ID,
        "runtime_generation": "runtime-generation-17",
        "updated_at": "2026-08-03T12:00:00Z",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "authority",
        "storage_ready",
        "directory_ready",
        "cloud_ready",
        "cloud_state",
    ),
    (
        (None, True, True, True, CloudSessionState.ACTIVE),
        (AUTHORITY, False, True, True, CloudSessionState.ACTIVE),
        (AUTHORITY, True, False, True, CloudSessionState.ACTIVE),
        (AUTHORITY, True, True, False, CloudSessionState.RECONCILING),
        (
            LocalRuntimeAuthority(
                profile=AUTHORITY.profile,
                runtime_generation=AUTHORITY.runtime_generation,
                instance_id=AUTHORITY.instance_id,
                host_bundle_id=AUTHORITY.host_bundle_id,
                process_identity=AUTHORITY.process_identity,
                required_capabilities=("session.observe",),
                optional_capabilities=("session.control",),
            ),
            True,
            True,
            True,
            CloudSessionState.ACTIVE,
        ),
    ),
)
async def test_missing_authority_storage_cloud_or_catalog_capability_never_publishes(
    tmp_path: Path,
    authority: LocalRuntimeAuthority | None,
    storage_ready: bool,
    directory_ready: bool,
    cloud_ready: bool,
    cloud_state: CloudSessionState,
) -> None:
    store = _store(tmp_path)
    component, _, _, _ = _component(
        store,
        authority=authority,
        storage=_ReadySource(storage_ready),
        directory=_ReadySource(directory_ready),
        cloud=_CloudSource(cloud_ready, cloud_state),
    )

    await component.start()

    assert await component.ready() is False
    assert not store.path.exists()


@pytest.mark.asyncio
async def test_runtime_state_change_updates_receipt_and_graceful_stop_removes_it(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    component, _, directory, cloud = _component(store)
    await component.start()
    run_task = asyncio.create_task(component.run())
    assert await component.ready() is True
    cloud.value = False
    cloud.state = CloudSessionState.RECONCILING

    for _ in range(100):
        payload = json.loads(store.path.read_bytes())
        if payload["ready"] is False:
            break
        await asyncio.sleep(0.001)

    assert payload["ready"] is False
    assert payload["cloud_state"] == "reconciling"
    cloud.value = True
    cloud.state = CloudSessionState.ACTIVE
    for _ in range(100):
        payload = json.loads(store.path.read_bytes())
        if payload["ready"] is True:
            break
        await asyncio.sleep(0.001)
    assert payload["ready"] is True

    directory.value = False
    for _ in range(100):
        payload = json.loads(store.path.read_bytes())
        if payload["ready"] is False:
            break
        await asyncio.sleep(0.001)
    assert payload["ready"] is False
    directory.value = True

    await component.drain()
    assert json.loads(store.path.read_bytes())["ready"] is False
    await component.stop()
    await run_task
    assert not store.path.exists()


@pytest.mark.asyncio
async def test_atomic_publish_failure_preserves_previous_complete_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    component, _, _, _ = _component(store)
    await component.start()
    assert await component.ready() is True
    previous = store.path.read_bytes()

    monkeypatch.setattr(
        receipt_module.os,
        "replace",
        lambda *_args: (_ for _ in ()).throw(OSError("injected replace failure")),
    )

    with pytest.raises(OSError, match="replace failure"):
        await component.drain()
    assert store.path.read_bytes() == previous


def test_status_reader_accepts_only_current_exact_process_identity_and_ttl(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    component, _, _, _ = _component(store)
    asyncio.run(component.start())
    assert asyncio.run(component.ready()) is True

    current = store.read(
        now=NOW + timedelta(seconds=5),
        process_identity_provider=lambda pid: PROCESS if pid == os.getpid() else None,
    )
    stale = store.read(
        now=NOW + timedelta(seconds=31),
        process_identity_provider=lambda pid: PROCESS if pid == os.getpid() else None,
    )
    replaced_process = store.read(
        now=NOW + timedelta(seconds=5),
        process_identity_provider=lambda _pid: ProcessIdentityEvidence(
            start_time_ns=PROCESS.start_time_ns + 1,
            executable_path=PROCESS.executable_path,
            executable_device=PROCESS.executable_device,
            executable_inode=PROCESS.executable_inode,
        ),
    )

    assert current is not None and current.ready is True
    assert stale is None
    assert replaced_process is None


@pytest.mark.parametrize(
    "mutation",
    (
        lambda value: value.__setitem__("access_token", "must-never-appear"),
        lambda value: value.__setitem__("pid", True),
        lambda value: value.__setitem__("cloud_state", "unknown"),
        lambda value: value.__setitem__("updated_at", "2026-08-03T12:00:06Z"),
        lambda value: value["local_authority_identity"].__setitem__(
            "tenant_id", "must-never-appear"
        ),
    ),
)
def test_status_reader_rejects_unknown_or_malformed_fields(
    tmp_path: Path,
    mutation,
) -> None:
    store = _store(tmp_path)
    component, _, _, _ = _component(store)
    asyncio.run(component.start())
    assert asyncio.run(component.ready()) is True
    value = json.loads(store.path.read_bytes())
    mutation(value)
    store.path.write_text(json.dumps(value), encoding="utf-8")
    store.path.chmod(0o600)

    assert (
        store.read(
            now=NOW,
            process_identity_provider=lambda _pid: PROCESS,
        )
        is None
    )


def test_status_reader_rejects_unsafe_mode_hardlink_symlink_and_duplicate_fields(
    tmp_path: Path,
) -> None:
    for mutation in ("mode", "hardlink", "symlink", "duplicate"):
        store = _store(tmp_path / mutation)
        component, _, _, _ = _component(store)
        asyncio.run(component.start())
        assert asyncio.run(component.ready()) is True
        if mutation == "mode":
            store.path.chmod(0o644)
        elif mutation == "hardlink":
            os.link(store.path, store.path.with_name("second-link.json"))
        elif mutation == "symlink":
            target = store.path.with_name("attacker.json")
            target.write_bytes(store.path.read_bytes())
            target.chmod(0o600)
            store.path.unlink()
            store.path.symlink_to(target)
        else:
            raw = store.path.read_text(encoding="utf-8")
            store.path.write_text(raw[:-1] + ',"ready":true}', encoding="utf-8")

        assert (
            store.read(
                now=NOW,
                process_identity_provider=lambda _pid: PROCESS,
            )
            is None
        )
