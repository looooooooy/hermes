from __future__ import annotations

import os
from types import SimpleNamespace
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

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows Named Pipes required")


@pytest.mark.asyncio
async def test_high_level_control_client_attaches_and_dispatches() -> None:
    authority = capture_windows_host_authority(
        profile="default",
        host_bundle_id="com.hermes.windows-control-client-test",
    ).bind_runtime("generation-windows-control-client-1")
    observed: list[dict] = []

    def dispatcher(request: dict, _transport: object) -> dict:
        observed.append(request)
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
    runtime_authority = SimpleNamespace(
        pid=authority.pid,
        profile=authority.profile,
        runtime_generation=authority.runtime_generation,
        process_identity=authority.process_identity,
    )
    client = WindowsControlRelayClient(
        runtime_authority,
        client_instance_id=UUID("22222222-2222-4222-8222-222222222222"),
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
        assert observed[0]["params"]["relay_local_only"] is True
    finally:
        await client.close()
        registration.close()
    assert client.is_open is False
