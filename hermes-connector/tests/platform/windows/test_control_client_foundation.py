from __future__ import annotations

import os
from uuid import UUID

import pytest
from hermes_agent_plugin.adapters.platform.windows.control_relay import (
    start_control_endpoint,
)
from hermes_agent_plugin.adapters.platform.windows.runtime_authority import (
    capture_windows_host_authority,
)

from hermes_connector.adapters.platform.windows.control_client import (
    WindowsControlRelayClient,
)
from hermes_connector.adapters.platform.windows.process_identity import (
    normalize_process_identity,
)
from hermes_connector.domain.local_gateway import LocalRuntimeAuthority

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows Named Pipes required")


@pytest.mark.asyncio
async def test_high_level_control_client_attaches_and_dispatches() -> None:
    authority = capture_windows_host_authority(
        profile="default",
        host_bundle_id="com.hermes.windows-control-client-test",
    ).bind_runtime("generation-windows-control-client-1")
    connector_identity = normalize_process_identity(authority.process_identity)
    assert connector_identity is not None
    runtime_authority = LocalRuntimeAuthority(
        profile=authority.profile,
        runtime_generation=authority.runtime_generation,
        instance_id=authority.instance_id,
        host_bundle_id=authority.host_bundle_id,
        process_identity=connector_identity,
        required_capabilities=("session.observe",),
        optional_capabilities=("session.control",),
    )
    observed: list[tuple[dict, object]] = []

    def dispatcher(request: dict, transport: object) -> dict:
        observed.append((request, transport))
        return {
            "jsonrpc": "2.0",
            "id": request.get("id"),
            "result": {
                "method": request.get("method"),
                "relay_local_only": request.get("params", {}).get("relay_local_only"),
            },
        }

    registration = start_control_endpoint(
        authority=authority,
        dispatcher=dispatcher,
    )
    client = WindowsControlRelayClient(
        runtime_authority,
        user_id="device-windows-control-test",
        provider="hermes-cloud",
        client_instance_id=UUID("22222222-2222-4222-8222-222222222222"),
        session_key="session-a",
    )
    try:
        await client.open()
        assert client.is_open
        response = await client.request(
            "session.control.status",
            {"durable_session_key": "session-a"},
        )
        assert response["result"] == {
            "method": "session.control.status",
            "relay_local_only": True,
        }
        assert len(observed) == 1
        relayed_request, transport = observed[0]
        assert relayed_request["params"]["relay_local_only"] is True
        assert transport.auth_claims == {
            "user_id": "device-windows-control-test",
            "provider": "hermes-cloud",
            "connection_role": "control",
            "client_instance_id": "22222222-2222-4222-8222-222222222222",
            "session_key": "session-a",
            "profile": "default",
        }
    finally:
        await client.close()
        registration.close()
    assert client.is_open is False
