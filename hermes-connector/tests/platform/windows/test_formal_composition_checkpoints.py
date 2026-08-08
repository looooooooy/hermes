from __future__ import annotations

import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from hermes_agent_plugin.adapters.local_protocol.handshake_v1 import LocalContractV1Adapter
from hermes_agent_plugin.adapters.platform.windows.local_gateway_transport import (
    create_local_gateway_resource,
)
from hermes_agent_plugin.adapters.platform.windows.runtime_authority import (
    capture_windows_host_authority,
)

import hermes_connector.bootstrap.windows as windows_bootstrap
from hermes_connector.adapters.platform.windows.pairing_projection import (
    WindowsPairedProjectionStore,
)
from hermes_connector.adapters.platform.windows.private_state import (
    ensure_private_directory,
)
from hermes_connector.bootstrap.config import ConnectorConfig
from hermes_connector.bootstrap.settings import ConnectorRuntimeSettings
from hermes_connector.domain.pairing import PairedProjection

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows formal runtime required")
NOW = datetime(2026, 8, 8, 4, 0, tzinfo=UTC)


class _Reached(RuntimeError):
    pass


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
        instance_state_file=state / "instance.json",
        paired_projection_file=state / "paired.json",
        pairing_offer_projection_file=state / "pairing-offer.json",
        pairing_command_lock_file=state / "pairing-command.lock",
        status_receipt_file=state / "status.json",
        state_directory=state,
        local_gateway_registry_directory=roles / "local-registry",
        local_gateway_socket_directory=roles / "local-socket",
        observer_registry_directory=roles / "observer-registry",
        observer_socket_directory=roles / "observer-socket",
        control_registry_directory=roles / "control-registry",
        control_socket_directory=roles / "control-socket",
        cloud_endpoint="wss://cloud.example.test/api/v1/connectors/ws",
        cloud_api_endpoint="https://cloud.example.test",
        display_name="Windows Formal Runtime Checkpoint",
        connector_version="2026.8-test",
        credential_store="dpapi",
        token_file=None,
    )


async def _prepare_paired(settings: ConnectorRuntimeSettings) -> None:
    await WindowsPairedProjectionStore(settings.paired_projection_file).save(
        PairedProjection(
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
    )


def _start_gateway():
    authority = capture_windows_host_authority(
        profile="default",
        host_bundle_id="com.hermes.windows-formal-checkpoint",
    ).bind_runtime("generation-formal-checkpoint-1")
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


def _build(settings: ConnectorRuntimeSettings) -> None:
    windows_bootstrap.build_windows_runtime(
        settings,
        release_id="2026.08.08-checkpoint",
        config=ConnectorConfig(),
        logger=_Logger(),
    )


@pytest.mark.asyncio
async def test_reaches_sqlite_constructor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path)
    await _prepare_paired(settings)
    resource = _start_gateway()
    monkeypatch.setattr(
        windows_bootstrap,
        "SQLiteStorageComponent",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(_Reached("sqlite")),
    )
    try:
        with pytest.raises(_Reached, match="sqlite"):
            _build(settings)
    finally:
        resource.stop(time.monotonic() + 3.0)


@pytest.mark.asyncio
async def test_reaches_local_gateway_constructor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path)
    await _prepare_paired(settings)
    resource = _start_gateway()
    monkeypatch.setattr(
        windows_bootstrap,
        "LocalGatewayClient",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(_Reached("local-gateway")),
    )
    try:
        with pytest.raises(_Reached, match="local-gateway"):
            _build(settings)
    finally:
        resource.stop(time.monotonic() + 3.0)


@pytest.mark.asyncio
async def test_reaches_cloud_wss_constructor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path)
    await _prepare_paired(settings)
    resource = _start_gateway()
    monkeypatch.setattr(
        windows_bootstrap,
        "build_cloud_wss_client",
        lambda **_kwargs: (_ for _ in ()).throw(_Reached("cloud-wss")),
    )
    try:
        with pytest.raises(_Reached, match="cloud-wss"):
            _build(settings)
    finally:
        resource.stop(time.monotonic() + 3.0)


@pytest.mark.asyncio
async def test_reaches_service_runner_constructor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path)
    await _prepare_paired(settings)
    resource = _start_gateway()
    monkeypatch.setattr(
        windows_bootstrap,
        "build_service_runner",
        lambda **_kwargs: (_ for _ in ()).throw(_Reached("service-runner")),
    )
    try:
        with pytest.raises(_Reached, match="service-runner"):
            _build(settings)
    finally:
        resource.stop(time.monotonic() + 3.0)
