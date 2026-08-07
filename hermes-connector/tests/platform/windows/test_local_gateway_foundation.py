from __future__ import annotations

import os
import time
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

from hermes_connector.adapters.contract_codec import (
    decode_local_gateway_response,
    encode_local_hello,
)
from hermes_connector.adapters.platform.windows.agent_discovery import (
    WindowsAgentDiscovery,
)
from hermes_connector.adapters.platform.windows.instance_lock import (
    AlreadyRunning,
    WindowsInstanceLock,
)
from hermes_connector.adapters.platform.windows.local_gateway_transport import (
    WindowsLocalGatewayTransport,
)
from hermes_connector.domain.contract_messages import (
    LocalHello,
    LocalWelcome,
)

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows Named Pipes required")


def _runtime_authority():
    return capture_windows_host_authority(
        profile="default",
        host_bundle_id="com.hermes.windows-test",
    ).bind_runtime("generation-windows-1")


@pytest.mark.asyncio
async def test_plugin_discovery_and_connector_handshake_share_runtime_authority() -> None:
    authority = _runtime_authority()
    handler = LocalContractV1Adapter(
        runtime_generation=authority.runtime_generation,
        available_capabilities=frozenset({"session.observe", "session.control"}),
    ).handle_hello
    resource = create_local_gateway_resource(
        authority=authority,
        hello_handler=handler,
    )
    resource.start(time.monotonic() + 3.0)
    discovery = WindowsAgentDiscovery(timeout_seconds=2.0)
    transport = WindowsLocalGatewayTransport(
        connect_timeout_seconds=2.0,
        io_timeout_seconds=2.0,
    )
    try:
        endpoints = await discovery.discover("default")
        assert len(endpoints) == 1
        endpoint = endpoints[0]
        assert endpoint.pid == authority.pid
        assert endpoint.profile == authority.profile
        assert endpoint.runtime_generation == authority.runtime_generation
        assert endpoint.instance_id == authority.instance_id
        assert endpoint.host_bundle_id == authority.host_bundle_id
        assert endpoint.process_identity.start_time_ns == authority.process_identity.start_time_ns
        assert endpoint.process_identity.executable_path == authority.process_identity.executable_path

        connection = await transport.connect(endpoint)
        try:
            response = await connection.exchange(
                encode_local_hello(
                    LocalHello(
                        contract_version=1,
                        message_type="local.hello",
                        client_instance_id=UUID("11111111-1111-4111-8111-111111111111"),
                        profile="default",
                        required_capabilities=("session.observe",),
                        optional_capabilities=("session.control",),
                    )
                )
            )
        finally:
            await connection.close()
        welcome = decode_local_gateway_response(response)
        assert isinstance(welcome, LocalWelcome)
        assert welcome.runtime_generation == authority.runtime_generation
        assert welcome.profile == "default"
        assert welcome.accepted_capabilities == ("session.control", "session.observe")
        assert welcome.unavailable_optional_capabilities == ()
    finally:
        await discovery.aclose()
        resource.stop(time.monotonic() + 3.0)


@pytest.mark.asyncio
async def test_wrong_profile_does_not_discover_endpoint() -> None:
    authority = _runtime_authority()
    resource = create_local_gateway_resource(
        authority=authority,
        hello_handler=LocalContractV1Adapter(
            runtime_generation=authority.runtime_generation,
        ).handle_hello,
    )
    resource.start(time.monotonic() + 3.0)
    discovery = WindowsAgentDiscovery(timeout_seconds=0.1)
    try:
        assert await discovery.discover("other") == ()
    finally:
        resource.stop(time.monotonic() + 3.0)


def test_windows_instance_lock_is_nonblocking_and_releasable(tmp_path) -> None:
    lock_path = tmp_path / "connector.lock"
    first = WindowsInstanceLock(lock_path)
    second = WindowsInstanceLock(lock_path)
    first.acquire()
    try:
        with pytest.raises(AlreadyRunning):
            second.acquire()
    finally:
        first.close()
    second.acquire()
    second.close()
