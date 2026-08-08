from __future__ import annotations

import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from hermes_agent_plugin.adapters.local_protocol.handshake_v1 import (
    LocalContractV1Adapter,
)
from hermes_agent_plugin.adapters.platform.windows.local_gateway_transport import (
    create_local_gateway_resource,
)
from hermes_agent_plugin.adapters.platform.windows.runtime_authority import (
    capture_windows_host_authority,
)

from hermes_connector.adapters.platform.windows.dpapi_secret_store import (
    WindowsDPAPISecretStore,
)
from hermes_connector.adapters.platform.windows.instance_identity import (
    WindowsInstanceIdentityStore,
)
from hermes_connector.adapters.platform.windows.pairing_projection import (
    WindowsPairedProjectionStore,
)
from hermes_connector.adapters.platform.windows.private_state import (
    ensure_private_directory,
    validate_private_file,
)
from hermes_connector.adapters.secure_store_device_identity import (
    SecureStoreDeviceIdentity,
)
from hermes_connector.bootstrap.config import ConnectorConfig
from hermes_connector.bootstrap.settings import ConnectorRuntimeSettings
from hermes_connector.bootstrap.windows import build_windows_runtime
from hermes_connector.domain.pairing import PairedProjection

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows formal runtime required")
NOW = datetime(2026, 8, 8, 4, 30, tzinfo=UTC)
_DEVICE_KEY_SERVICE = "wiki.seaotter.hermes.connector.device-key.v1"


class _Logger:
    def emit(self, **_kwargs) -> None:
        return None


def _settings(tmp_path: Path) -> ConnectorRuntimeSettings:
    state = ensure_private_directory(tmp_path / "state")
    roles = state / "roles"
    return ConnectorRuntimeSettings(
        profile="default",
        database_file=state / "connector.sqlite3",
        lock_file=state / "connector.lock",
        state_directory=state,
        local_gateway_registry_directory=roles / "local-registry",
        local_gateway_socket_directory=roles / "local-socket",
        observer_registry_directory=roles / "observer-registry",
        observer_socket_directory=roles / "observer-socket",
        control_registry_directory=roles / "control-registry",
        control_socket_directory=roles / "control-socket",
        cloud_endpoint="wss://cloud.example.test/api/v1/connectors/ws",
        cloud_api_endpoint="https://cloud.example.test",
        display_name="Windows Real Paired State",
        connector_version="2026.8-test",
        credential_store="dpapi",
        token_file=None,
    )


async def _prepare_real_paired_state(settings: ConnectorRuntimeSettings) -> None:
    identities = WindowsInstanceIdentityStore(settings.instance_state_file).load_or_create()
    account = f"connector-instance:{identities.connector_instance_id}"
    identity = SecureStoreDeviceIdentity(
        WindowsDPAPISecretStore(
            root_directory=settings.state_directory,
            service=_DEVICE_KEY_SERVICE,
            account=account,
        )
    )
    public = await identity.get_or_create()
    await WindowsPairedProjectionStore(settings.paired_projection_file).save(
        PairedProjection(
            tenant_id=UUID("66666666-6666-4666-8666-666666666666"),
            device_id=UUID("77777777-7777-4777-8777-777777777777"),
            credential_id=UUID("88888888-8888-4888-8888-888888888888"),
            agent_id=UUID("99999999-9999-4999-8999-999999999999"),
            scopes=("session.observe", "session.control.request"),
            key_handle=public.key_handle,
            credential_fingerprint=public.fingerprint,
            token_expires_at=NOW + timedelta(minutes=15),
            lifecycle_state="active",
        )
    )


def _start_gateway():
    authority = capture_windows_host_authority(
        profile="default",
        host_bundle_id="com.hermes.windows-formal-real-state",
    ).bind_runtime("generation-formal-real-state-1")
    resource = create_local_gateway_resource(
        authority=authority,
        hello_handler=LocalContractV1Adapter(
            runtime_generation=authority.runtime_generation,
            available_capabilities=frozenset(
                {
                    "session.observe",
                    "session.control",
                    "session.catalog.v1",
                    "session.observe.output-parity.v1",
                }
            ),
        ).handle_hello,
    )
    resource.start(time.monotonic() + 3.0)
    return resource


@pytest.mark.asyncio
async def test_formal_runtime_builds_from_real_dpapi_paired_state(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    await _prepare_real_paired_state(settings)
    resource = _start_gateway()
    try:
        runtime = build_windows_runtime(
            settings,
            release_id="2026.08.08-real-state",
            config=ConnectorConfig(),
            logger=_Logger(),
        )
    finally:
        resource.stop(time.monotonic() + 3.0)

    assert runtime.identities == WindowsInstanceIdentityStore(
        settings.instance_state_file
    ).load_or_create()
    assert runtime.device_identity is not None
    assert runtime.command_lane is not None
    assert runtime.owner_control_lane is not None
    assert runtime.observer_intent_lane is not None
    assert runtime.session_catalog_sync is not None


@pytest.mark.asyncio
async def test_real_runtime_sqlite_and_lock_files_are_current_user_private(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    await _prepare_real_paired_state(settings)
    resource = _start_gateway()
    runtime = None
    try:
        runtime = build_windows_runtime(
            settings,
            release_id="2026.08.08-real-state-acl",
            config=ConnectorConfig(),
            logger=_Logger(),
        )
        await runtime.storage.start()
        validate_private_file(settings.database_file)
        runtime.runner._lock.acquire()
        validate_private_file(settings.lock_file)
    finally:
        if runtime is not None:
            runtime.runner._lock.close()
            await runtime.storage.stop()
        resource.stop(time.monotonic() + 3.0)
