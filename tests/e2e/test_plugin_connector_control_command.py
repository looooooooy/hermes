from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from uuid import UUID

import pytest
from hermes_connector.adapters.cloud.codec import ConnectorProtocolCodec
from hermes_connector.adapters.platform.macos.plugin_control_relay import (
    MacOSPluginControlRelay,
)
from hermes_connector.adapters.sqlite_storage import SQLiteStorageComponent
from hermes_connector.application.command_lane import CommandLane, CommandScope
from hermes_connector.bootstrap.config import ConnectorConfig
from hermes_connector.domain.contract_messages import CloudEnvelope
from tests.e2e.plugin_test_runtime import (
    create_connector_authority_provider,
    create_test_runtime_authority,
)

from hermes_agent_plugin.adapters.platform.macos import control_relay
from hermes_agent_plugin.adapters.platform.macos.local_gateway_paths import (
    MacOSLocalGatewayPaths,
)

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 7, 30, 8, 0, 1, tzinfo=UTC)


@pytest.mark.asyncio
async def test_plugin_uds_executes_one_durable_connector_command(
    tmp_path: Path,
) -> None:
    socket_root = Path("/tmp").resolve(strict=True)
    paths = MacOSLocalGatewayPaths(
        local_gateway_registry_directory=tmp_path / "local-registry",
        local_gateway_socket_directory=(
            socket_root / f"hlg-connector-e2e-{os.getpid()}"
        ),
        control_registry_directory=tmp_path / "control-registry",
        control_socket_directory=socket_root / f"hctl-connector-e2e-{os.getpid()}",
        observer_registry_directory=tmp_path / "observer-registry",
        observer_socket_directory=socket_root / f"hobs-connector-e2e-{os.getpid()}",
    )
    calls: list[tuple[dict, dict[str, str]]] = []
    local_lease = "opaque-local-lease-never-persist"

    def dispatch(request: dict, transport: object) -> dict:
        claims = dict(transport.auth_claims)
        calls.append((request, claims))
        if request["method"] == "session.control.acquire":
            result = {
                "lease_id": local_lease,
                "expires_at_epoch_ms": 1_785_400_000_000,
                "control_revision": 3,
                "controller_kind": "mobile",
                "controller_label": "Hermes Mobile",
                "pending_input": None,
            }
        else:
            assert request["method"] == "prompt.submit"
            assert request["params"]["lease_id"] == local_lease
            result = {
                "status": "accepted",
                "client_request_id": request["params"]["client_request_id"],
                "client_turn_id": request["params"]["client_turn_id"],
                "server_turn_id": "turn-server-e2e",
            }
        return {"jsonrpc": "2.0", "id": request["id"], "result": result}

    authority = create_test_runtime_authority(
        profile="default",
        runtime_generation="runtime-generation-1",
    )
    registration = control_relay.start_control_endpoint(
        authority=authority,
        dispatcher=dispatch,
        paths=paths,
    )
    storage = SQLiteStorageComponent(
        tmp_path / "connector.sqlite3",
        ConnectorConfig(),
    )
    await storage.start()
    runner = asyncio.create_task(storage.run())
    assert await storage.ready()
    try:
        codec = ConnectorProtocolCodec()
        payload = json.loads(
            (ROOT / "contracts/fixtures/valid/command-deliver-payload.json").read_text(
                encoding="utf-8"
            )
        )
        envelope = CloudEnvelope(
            contract_version=1,
            message_id=UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd"),
            message_type="command.deliver",
            tenant_id="tenant-1",
            device_id="device-1",
            sequence=1,
            sent_at=NOW,
            payload=MappingProxyType(payload),
        )
        relay = MacOSPluginControlRelay(
            registry_directory=paths.control_registry_directory,
            socket_directory=paths.control_socket_directory,
            profile="default",
            user_id="user-1",
            provider="hermes-cloud",
            authority=create_connector_authority_provider(authority),
        )
        lane = CommandLane(
            storage=storage,
            relay=relay,
            scope=CommandScope(
                tenant_id="tenant-1",
                device_id="device-1",
                connector_instance_id=UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
                profile="default",
                allowed_session_keys=frozenset({"durable-root-1"}),
            ),
            codec=codec,
            clock=lambda: NOW,
        )

        record = await lane.process(envelope)

        assert record.state == "succeeded"
        assert [request["method"] for request, _claims in calls] == [
            "session.control.acquire",
            "prompt.submit",
        ]
        assert all(
            claims
            == {
                "user_id": "user-1",
                "provider": "hermes-cloud",
                "connection_role": "control",
                "client_instance_id": "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
                "session_key": "durable-root-1",
                "profile": "default",
            }
            for _request, claims in calls
        )
        durable_bytes = b"".join(
            (
                record.delivery_payload,
                record.receipt_payload,
                record.result_payload or b"",
            )
        )
        assert local_lease.encode() not in durable_bytes
        assert b"relay_local_only" not in durable_bytes
        assert b"Continue the current task." not in durable_bytes
    finally:
        await storage.drain()
        await storage.stop()
        await runner
        registration.close()
