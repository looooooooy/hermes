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

import hermes_connector.bootstrap.windows as windows_bootstrap
from hermes_connector.adapters.platform.windows.instance_lock import WindowsInstanceLock
from hermes_connector.adapters.platform.windows.pairing_projection import (
    WindowsPairedProjectionStore,
)
from hermes_connector.adapters.platform.windows.private_state import (
    ensure_private_directory,
)
from hermes_connector.bootstrap.config import ConnectorConfig
from hermes_connector.bootstrap.runtime import build_service_runner
from hermes_connector.bootstrap.settings import ConnectorRuntimeSettings
from hermes_connector.domain.pairing import PairedProjection

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows formal runtime required")
NOW = datetime(2026, 8, 8, 5, 0, tzinfo=UTC)


class _Reached(RuntimeError):
    pass


class _Logger:
    def emit(self, **_kwargs) -> None:
        return None


class _Component:
    name = "noop"

    async def start(self) -> None:
        return None

    async def ready(self) -> bool:
        return True

    async def run(self) -> None:
        return None

    async def drain(self) -> None:
        return None

    async def stop(self) -> None:
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
        display_name="Windows ServiceRunner Checkpoint",
        connector_version="2026.8-test",
        credential_store="dpapi",
        token_file=None,
    )


async def _paired(settings: ConnectorRuntimeSettings) -> None:
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


def _gateway():
    authority = capture_windows_host_authority(
        profile="default",
        host_bundle_id="com.hermes.windows-service-runner-checkpoint",
    ).bind_runtime("generation-service-runner-checkpoint")
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


def test_windows_instance_lock_is_a_valid_explicit_service_runner_factory(
    tmp_path: Path,
) -> None:
    state = ensure_private_directory(tmp_path / "state")
    runner = build_service_runner(
        lock_path=state / "connector.lock",
        components=(_Component(),),
        config=ConnectorConfig(),
        logger=_Logger(),
        instance_lock_type=WindowsInstanceLock,
    )
    assert runner is not None


@pytest.mark.asyncio
async def test_formal_build_reaches_service_runner_when_runner_is_sentinel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    await _paired(settings)
    resource = _gateway()
    monkeypatch.setattr(
        windows_bootstrap,
        "build_service_runner",
        lambda **_kwargs: (_ for _ in ()).throw(_Reached("service-runner")),
    )
    try:
        with pytest.raises(_Reached, match="service-runner"):
            windows_bootstrap.build_windows_runtime(
                settings,
                release_id="2026.08.08-service-runner-checkpoint",
                config=ConnectorConfig(),
                logger=_Logger(),
            )
    finally:
        resource.stop(time.monotonic() + 3.0)
