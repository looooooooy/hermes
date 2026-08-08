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

from hermes_connector.adapters.platform.windows.instance_identity import (
    WindowsInstanceIdentityStore,
)
from hermes_connector.adapters.platform.windows.observer_client import (
    WindowsObserverClient,
)
from hermes_connector.adapters.platform.windows.pairing_projection import (
    WindowsPairedProjectionStore,
)
from hermes_connector.adapters.platform.windows.plugin_control_relay import (
    WindowsPluginControlRelay,
    WindowsPluginOwnerControlChannelFactory,
)
from hermes_connector.adapters.platform.windows.private_state import (
    ensure_private_directory,
    validate_private_file,
)
from hermes_connector.adapters.platform.windows.session_catalog_client import (
    WindowsSessionCatalogClient,
)
from hermes_connector.adapters.platform.windows.status_receipt import (
    WindowsStatusReceiptStore,
)
from hermes_connector.adapters.secure_store_cloud_token import (
    SecureStoreCloudTokenProvider,
)
from hermes_connector.adapters.secure_store_device_identity import (
    SecureStoreDeviceIdentity,
)
from hermes_connector.bootstrap.config import ConnectorConfig
from hermes_connector.bootstrap.settings import ConnectorRuntimeSettings
from hermes_connector.bootstrap.windows import (
    WindowsCredentialRuntimeForbidden,
    build_windows_pairing_runtime,
    build_windows_runtime,
    check_windows_runtime,
)
from hermes_connector.domain.pairing import PairedProjection

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows formal runtime required")
NOW = datetime(2026, 8, 8, 3, 30, tzinfo=UTC)


class _Logger:
    def __init__(self) -> None:
        self.events: list[tuple[object, str, object]] = []

    def emit(self, *, category: object, component: str, state: object) -> None:
        self.events.append((category, component, state))


def _settings(tmp_path: Path) -> ConnectorRuntimeSettings:
    state = ensure_private_directory(tmp_path / "state")
    roles = state / "role-spec"
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
        display_name="Windows Formal Runtime Test",
        connector_version="2026.8-test",
        credential_store="dpapi",
        token_file=None,
    )


def _paired_projection() -> PairedProjection:
    return PairedProjection(
        tenant_id=UUID("66666666-6666-4666-8666-666666666666"),
        device_id=UUID("77777777-7777-4777-8777-777777777777"),
        credential_id=UUID("88888888-8888-4888-8888-888888888888"),
        agent_id=UUID("99999999-9999-4999-8999-999999999999"),
        scopes=("session.observe", "session.control.request"),
        key_handle="hermes-device-key:v1:fingerprint",
        credential_fingerprint="SHA256:" + "B" * 43,
        token_expires_at=NOW + timedelta(minutes=15),
        lifecycle_state="active",
    )


async def _prepare_paired(settings: ConnectorRuntimeSettings) -> None:
    await WindowsPairedProjectionStore(settings.paired_projection_file).save(
        _paired_projection()
    )


def _start_local_gateway() -> object:
    authority = capture_windows_host_authority(
        profile="default",
        host_bundle_id="com.hermes.windows-formal-runtime-test",
    ).bind_runtime("generation-formal-runtime-1")
    capabilities = frozenset(
        {
            "session.observe",
            "session.control",
            "session.catalog.v1",
            "session.observe.output-parity.v1",
        }
    )
    resource = create_local_gateway_resource(
        authority=authority,
        hello_handler=LocalContractV1Adapter(
            runtime_generation=authority.runtime_generation,
            available_capabilities=capabilities,
        ).handle_hello,
    )
    resource.start(time.monotonic() + 3.0)
    return resource


@pytest.mark.asyncio
async def test_windows_formal_runtime_composes_verified_adapters_without_availability_switch(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    await _prepare_paired(settings)
    resource = _start_local_gateway()
    try:
        runtime = build_windows_runtime(
            settings,
            release_id="2026.08.08-win-formal",
            config=ConnectorConfig(),
            logger=_Logger(),
        )
    finally:
        resource.stop(time.monotonic() + 3.0)

    expected_identity_type = type(
        WindowsInstanceIdentityStore(settings.instance_state_file).load_or_create()
    )
    assert isinstance(runtime.identities, expected_identity_type)
    assert isinstance(runtime.device_identity, SecureStoreDeviceIdentity)
    assert isinstance(runtime.observer_client, WindowsObserverClient)
    assert isinstance(runtime.session_catalog_client, WindowsSessionCatalogClient)
    assert isinstance(runtime.control_relay, WindowsPluginControlRelay)
    assert isinstance(
        runtime.owner_control_factory,
        WindowsPluginOwnerControlChannelFactory,
    )
    assert isinstance(runtime.status_receipt._store, WindowsStatusReceiptStore)
    assert not any(
        "macos" in type(component).__module__ for component in runtime.components
    )


@pytest.mark.asyncio
async def test_windows_pairing_runtime_uses_dpapi_and_windows_projection_stores(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    runtime = build_windows_pairing_runtime(settings)
    try:
        assert isinstance(runtime.coordinator._identity, SecureStoreDeviceIdentity)
        assert isinstance(runtime.coordinator._token_store, SecureStoreCloudTokenProvider)
        assert "windows" in type(runtime.coordinator._paired_projection_store).__module__
        assert "windows" in type(runtime.coordinator._offer_projection_store).__module__
    finally:
        await runtime.aclose()


def test_windows_runtime_check_is_side_effect_bounded_and_dpapi_only(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    check_windows_runtime(settings)
    assert not settings.instance_state_file.exists()
    assert not settings.database_file.exists()
    assert not settings.lock_file.exists()

    file_mode = ConnectorRuntimeSettings(
        **{
            field: getattr(settings, field)
            for field in settings.__dataclass_fields__
            if field != "credential_store"
        },
        credential_store="file",
    )
    with pytest.raises(WindowsCredentialRuntimeForbidden):
        check_windows_runtime(file_mode)


@pytest.mark.asyncio
async def test_windows_sqlite_and_lock_files_inherit_required_private_acl(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    await _prepare_paired(settings)
    resource = _start_local_gateway()
    runtime = None
    try:
        runtime = build_windows_runtime(
            settings,
            release_id="2026.08.08-win-acl",
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
