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
from hermes_connector.adapters.platform.windows.private_state import ensure_private_directory
from hermes_connector.bootstrap.config import ConnectorConfig
from hermes_connector.bootstrap.settings import ConnectorRuntimeSettings
from hermes_connector.bootstrap.windows_service import build_windows_service_runner
from hermes_connector.domain.pairing import PairedProjection

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows formal runtime required")
NOW = datetime(2026, 8, 8, 5, 30, tzinfo=UTC)


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
        display_name="Windows Service Override",
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
        host_bundle_id="com.hermes.windows-service-override",
    ).bind_runtime("generation-service-override")
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
async def test_formal_runtime_builds_with_explicit_windows_service_builder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    await _paired(settings)
    resource = _gateway()

    def compose(**kwargs):
        kwargs.pop("instance_lock_type", None)
        kwargs.pop("metadata_validator", None)
        kwargs.pop("platform_name", None)
        return build_windows_service_runner(**kwargs)

    monkeypatch.setattr(windows_bootstrap, "build_service_runner", compose)
    try:
        runtime = windows_bootstrap.build_windows_runtime(
            settings,
            release_id="2026.08.08-service-override",
            config=ConnectorConfig(),
            logger=_Logger(),
        )
    finally:
        resource.stop(time.monotonic() + 3.0)

    assert runtime.runner is not None
