from __future__ import annotations

import os
import time
from pathlib import Path
from uuid import UUID

import pytest
from hermes_agent_plugin.adapters.local_protocol.handshake_v1 import LocalContractV1Adapter
from hermes_agent_plugin.adapters.platform.windows.local_gateway_transport import (
    create_local_gateway_resource,
)
from hermes_agent_plugin.adapters.platform.windows.local_relay import create_local_relay_backend
from hermes_agent_plugin.adapters.platform.windows.runtime_authority import (
    capture_windows_host_authority,
)

from hermes_connector.adapters.platform.windows.private_state import ensure_private_directory
from hermes_connector.bootstrap.config import ConnectorConfig
from hermes_connector.bootstrap.settings import ConnectorRuntimeSettings
from hermes_connector.bootstrap.windows_live_session import probe_windows_live_session

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows Named Pipes required")


def _authority():
    return capture_windows_host_authority(
        profile="default",
        host_bundle_id="com.hermes.windows-live-session-probe",
    ).bind_runtime("runtime-generation-live-1")


def _settings(tmp_path: Path) -> ConnectorRuntimeSettings:
    root = ensure_private_directory(tmp_path / "hermes")
    state = ensure_private_directory(root / "connector-state")
    runtime = root / "runtime"
    return ConnectorRuntimeSettings(
        cloud_endpoint="wss://cloud.example.test/connector/ws",
        cloud_api_endpoint="https://cloud.example.test/hermes",
        display_name="Hermes Connector",
        profile="default",
        connector_version="1.2.3",
        local_gateway_registry_directory=runtime / "local-gateway-registry",
        local_gateway_socket_directory=runtime / "local-gateway-pipe",
        control_registry_directory=runtime / "control-registry",
        control_socket_directory=runtime / "control-pipe",
        observer_registry_directory=runtime / "observer-registry",
        observer_socket_directory=runtime / "observer-pipe",
        state_directory=state,
        database_file=state / "connector.sqlite3",
        lock_file=state / "connector.lock",
        credential_store="dpapi",
        token_file=None,
    )


def _catalog_page(request_id: object, *, sessions: list[dict[str, object]]) -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {
            "subscription_id": "22222222-2222-4222-8222-222222222222",
            "snapshot_id": "33333333-3333-4333-8333-333333333333",
            "profile": "default",
            "runtime_generation": "runtime-generation-live-1",
            "catalog_revision": 7,
            "page_index": 0,
            "is_last": True,
            "sessions": sessions,
            "next_cursor": None,
        },
    }


def _entry() -> dict[str, object]:
    return {
        "session_key": "live-session-1",
        "surface": "gateway",
        "authority_revision": 3,
        "available_actions": ["prompt.submit"],
    }


async def _run_probe(tmp_path: Path, *, sessions: list[dict[str, object]]):
    authority = _authority()
    gateway_resource = create_local_gateway_resource(
        authority=authority,
        hello_handler=LocalContractV1Adapter(
            runtime_generation=authority.runtime_generation,
            available_capabilities=frozenset(
                {"session.observe", "session.catalog.v1"}
            ),
        ).handle_hello,
    )
    backend = create_local_relay_backend()

    def dispatch(request: dict, _transport: object) -> dict | None:
        method = request.get("method")
        if method == "session.catalog.subscribe":
            return _catalog_page(request.get("id"), sessions=sessions)
        if method == "session.catalog.unsubscribe":
            return None
        raise AssertionError(f"unexpected method: {method}")

    registration = backend.start_observer_endpoint(
        authority=authority,
        dispatch=dispatch,
        remove_observer_subscriptions=lambda _transport: None,
        observer_contract=1,
    )
    gateway_resource.start(time.monotonic() + 3.0)
    try:
        return await probe_windows_live_session(
            _settings(tmp_path),
            config=ConnectorConfig(
                local_connect_timeout_seconds=1.0,
                local_rpc_deadline_seconds=1.0,
                local_max_reconnect_attempts=1,
                local_reconnect_delay_seconds=0.01,
                local_discovery_poll_interval_seconds=1.0,
                start_deadline_seconds=3.0,
                stop_deadline_seconds=3.0,
            ),
        )
    finally:
        registration.close()
        gateway_resource.stop(time.monotonic() + 3.0)


@pytest.mark.asyncio
async def test_live_session_probe_requires_nonempty_current_generation_catalog(tmp_path: Path) -> None:
    evidence = await _run_probe(tmp_path, sessions=[_entry()])

    assert evidence.live_session_ok is True
    assert evidence.runtime_generation == "runtime-generation-live-1"


@pytest.mark.asyncio
async def test_live_session_probe_fails_closed_on_empty_catalog(tmp_path: Path) -> None:
    evidence = await _run_probe(tmp_path, sessions=[])

    assert evidence.live_session_ok is False
    assert evidence.runtime_generation == "runtime-generation-live-1"
