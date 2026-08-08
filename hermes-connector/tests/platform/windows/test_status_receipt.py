from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import hermes_connector.adapters.platform.windows.status_receipt as status_module
from hermes_connector.adapters.platform.windows.private_state import (
    ensure_private_directory,
    validate_private_file,
)
from hermes_connector.adapters.platform.windows.process_identity import (
    current_process_identity,
)
from hermes_connector.adapters.platform.windows.status_receipt import (
    UnsafeStatusReceipt,
    WindowsStatusReceiptStore,
)
from hermes_connector.application.readiness_status import ReadinessStatusComponent
from hermes_connector.domain.cloud_session import CloudSessionState
from hermes_connector.domain.local_gateway import (
    LocalRuntimeAuthority,
    ProcessIdentityEvidence,
)

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows private state required")
NOW = datetime(2026, 8, 8, 1, 45, tzinfo=UTC)
RELEASE_ID = "2026.08.08-win-status"


class _ReadySource:
    def __init__(self, ready: bool) -> None:
        self.value = ready

    async def ready(self) -> bool:
        return self.value


class _CloudSource(_ReadySource):
    def __init__(self, ready: bool, state: CloudSessionState) -> None:
        super().__init__(ready)
        self.state = state


def _store(tmp_path: Path) -> WindowsStatusReceiptStore:
    root = ensure_private_directory(tmp_path / "state")
    return WindowsStatusReceiptStore(root / "status.json")


def _authority(process: ProcessIdentityEvidence) -> LocalRuntimeAuthority:
    return LocalRuntimeAuthority(
        profile="default",
        runtime_generation="runtime-generation-win-1",
        instance_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        host_bundle_id="com.nousresearch.hermes.windows",
        process_identity=process,
        required_capabilities=("session.observe",),
        optional_capabilities=("session.control", "session.catalog.v1"),
    )


def _component(
    store: WindowsStatusReceiptStore,
    process: ProcessIdentityEvidence,
) -> tuple[ReadinessStatusComponent, _ReadySource, _ReadySource, _CloudSource]:
    authority = _authority(process)
    storage = _ReadySource(True)
    directory = _ReadySource(True)
    cloud = _CloudSource(True, CloudSessionState.ACTIVE)

    async def current_authority() -> LocalRuntimeAuthority:
        return authority

    return (
        ReadinessStatusComponent(
            store=store,
            release_id=RELEASE_ID,
            local_authority=current_authority,
            storage=storage,
            directory=directory,
            cloud=cloud,
            pid=os.getpid(),
            process_identity_provider=current_process_identity,
            now=lambda: NOW,
            refresh_interval_seconds=0.001,
        ),
        storage,
        directory,
        cloud,
    )


@pytest.mark.asyncio
async def test_windows_readiness_receipt_binds_live_process_and_lifecycle(tmp_path: Path) -> None:
    process = current_process_identity(os.getpid())
    assert process is not None
    store = _store(tmp_path)
    component, _storage, _directory, cloud = _component(store, process)

    await component.start()
    assert not store.path.exists()
    assert await component.ready()
    validate_private_file(store.path)

    current = store.read(
        now=NOW + timedelta(seconds=5),
        process_identity_provider=current_process_identity,
    )
    assert current is not None
    assert current.ready is True
    assert current.pid == os.getpid()
    assert current.process_identity == process
    assert current.runtime_generation == "runtime-generation-win-1"
    assert current.cloud_state is CloudSessionState.ACTIVE

    stale = store.read(
        now=NOW + timedelta(seconds=31),
        process_identity_provider=current_process_identity,
    )
    replaced = store.read(
        now=NOW + timedelta(seconds=5),
        process_identity_provider=lambda _pid: ProcessIdentityEvidence(
            start_time_ns=process.start_time_ns + 100,
            executable_path=process.executable_path,
            executable_device=process.executable_device,
            executable_inode=process.executable_inode,
        ),
    )
    assert stale is None
    assert replaced is None

    cloud.value = False
    cloud.state = CloudSessionState.RECONCILING
    await component.drain()
    drained = store.read(
        now=NOW,
        process_identity_provider=current_process_identity,
    )
    assert drained is not None
    assert drained.ready is False
    assert drained.cloud_state is CloudSessionState.RECONCILING

    await component.stop()
    assert not store.path.exists()


@pytest.mark.asyncio
async def test_windows_receipt_atomic_publish_failure_preserves_previous(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = current_process_identity(os.getpid())
    assert process is not None
    store = _store(tmp_path)
    component, _storage, _directory, _cloud = _component(store, process)
    await component.start()
    assert await component.ready()
    previous = store.path.read_bytes()

    def fail_write(*_args, **_kwargs) -> None:
        raise OSError("injected atomic write failure")

    monkeypatch.setattr(status_module, "atomic_write_private_file", fail_write)
    with pytest.raises(UnsafeStatusReceipt, match="published"):
        await component.drain()
    assert store.path.read_bytes() == previous


@pytest.mark.asyncio
async def test_windows_receipt_reader_rejects_unknown_or_corrupt_content(
    tmp_path: Path,
) -> None:
    process = current_process_identity(os.getpid())
    assert process is not None
    store = _store(tmp_path)
    component, _storage, _directory, _cloud = _component(store, process)
    await component.start()
    assert await component.ready()

    value = json.loads(store.path.read_text(encoding="utf-8"))
    value["access_token"] = "must-never-appear"
    store.path.write_text(json.dumps(value), encoding="utf-8")
    assert (
        store.read(now=NOW, process_identity_provider=current_process_identity)
        is None
    )

    store.path.write_text('{"ready":true,"ready":false}', encoding="utf-8")
    assert (
        store.read(now=NOW, process_identity_provider=current_process_identity)
        is None
    )
