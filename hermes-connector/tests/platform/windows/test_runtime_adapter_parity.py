from __future__ import annotations

import asyncio
import os
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID

import pytest
from hermes_agent_plugin.adapters.local_protocol.handshake_v1 import LocalContractV1Adapter
from hermes_agent_plugin.adapters.platform.windows.control_relay import (
    start_control_endpoint,
)
from hermes_agent_plugin.adapters.platform.windows.local_gateway_transport import (
    create_local_gateway_resource,
)
from hermes_agent_plugin.adapters.platform.windows.runtime_authority import (
    capture_windows_host_authority,
)

from hermes_connector.adapters.local_runtime_preflight import LocalRuntimePreflight
from hermes_connector.adapters.platform.windows.agent_discovery import WindowsAgentDiscovery
from hermes_connector.adapters.platform.windows.dpapi_secret_store import (
    WindowsDPAPISecretStore,
)
from hermes_connector.adapters.platform.windows.local_gateway_transport import (
    WindowsLocalGatewayTransport,
)
from hermes_connector.adapters.platform.windows.pairing_command_lock import (
    PairingCommandLockTimeout,
    WindowsPairingCommandLock,
)
from hermes_connector.adapters.platform.windows.plugin_control_relay import (
    WindowsPluginControlRelay,
    WindowsPluginOwnerControlChannelFactory,
)
from hermes_connector.adapters.platform.windows.private_state import (
    ensure_private_directory,
)
from hermes_connector.adapters.platform.windows.process_identity import (
    normalize_process_identity,
)
from hermes_connector.adapters.secure_store_cloud_token import SecureStoreCloudTokenProvider
from hermes_connector.domain.cloud_protocol import CommandDelivery
from hermes_connector.domain.local_gateway import LocalRuntimeAuthority
from hermes_connector.domain.owner_control import OwnerControlOutcomeUnknown

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows runtime adapters required")
NOW = datetime(2026, 8, 8, 2, 30, tzinfo=UTC)


def _authorities(*, capabilities: tuple[str, ...] = ("session.control",)):
    plugin = capture_windows_host_authority(
        profile="default",
        host_bundle_id="com.hermes.windows-runtime-adapter-test",
    ).bind_runtime("generation-runtime-adapter-1")
    identity = normalize_process_identity(plugin.process_identity)
    assert identity is not None
    runtime = LocalRuntimeAuthority(
        profile=plugin.profile,
        runtime_generation=plugin.runtime_generation,
        instance_id=plugin.instance_id,
        host_bundle_id=plugin.host_bundle_id,
        process_identity=identity,
        required_capabilities=(),
        optional_capabilities=capabilities,
    )
    return plugin, runtime


@pytest.mark.asyncio
async def test_secure_store_cloud_token_roundtrip_uses_dpapi_ciphertext(tmp_path) -> None:
    root = ensure_private_directory(tmp_path / "secure")
    store = WindowsDPAPISecretStore(
        root_directory=root,
        service="wiki.seaotter.hermes.connector.cloud-token.v1",
        account="connector-instance:test",
    )
    provider = SecureStoreCloudTokenProvider(store)
    provider.check()

    token = "cloud-token-runtime-adapter-123"
    await provider.store_access_token(token)

    assert await provider.access_token() == token
    assert token.encode() not in store.path.read_bytes()
    await provider.clear_access_token()
    with pytest.raises(Exception, match="credential"):
        await provider.access_token()


@pytest.mark.asyncio
async def test_windows_pairing_command_lock_serializes_and_releases_on_cancel(tmp_path) -> None:
    root = ensure_private_directory(tmp_path / "state")
    path = root / "pairing-command.lock"
    first = WindowsPairingCommandLock(
        path,
        timeout_seconds=1.0,
        poll_interval_seconds=0.01,
    )
    second = WindowsPairingCommandLock(
        path,
        timeout_seconds=0.1,
        poll_interval_seconds=0.01,
    )

    async with first:
        with pytest.raises(PairingCommandLockTimeout):
            async with second:
                raise AssertionError("second lock must not enter")

        waiting = asyncio.create_task(
            WindowsPairingCommandLock(
                path,
                timeout_seconds=2.0,
                poll_interval_seconds=0.01,
            ).__aenter__()
        )
        await asyncio.sleep(0.05)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting

    async with WindowsPairingCommandLock(path, timeout_seconds=0.5):
        pass


def test_windows_local_preflight_requires_exact_peer_without_protocol_frame() -> None:
    plugin, runtime = _authorities(capabilities=("session.observe", "session.control"))
    resource = create_local_gateway_resource(
        authority=plugin,
        hello_handler=LocalContractV1Adapter(
            runtime_generation=plugin.runtime_generation,
            available_capabilities=frozenset({"session.observe", "session.control"}),
        ).handle_hello,
    )
    resource.start(time.monotonic() + 3.0)
    try:
        discovery = WindowsAgentDiscovery(timeout_seconds=1.0)
        preflight = LocalRuntimePreflight(
            discovery=discovery,
            transport=WindowsLocalGatewayTransport(connect_timeout_seconds=1.0),
            timeout_seconds=1.0,
        )
        endpoint = preflight.verify("default")
        assert endpoint is not None
        assert endpoint.runtime_generation == runtime.runtime_generation
        assert endpoint.process_identity == runtime.process_identity
        assert preflight.verify("other") is None
    finally:
        resource.stop(time.monotonic() + 3.0)


@pytest.mark.asyncio
async def test_windows_command_relay_acquires_and_mutates_real_control_pipe() -> None:
    plugin, runtime = _authorities()
    observed: list[dict] = []

    def dispatcher(request: dict, _transport: object) -> dict:
        observed.append(request)
        method = request.get("method")
        if method == "session.control.acquire":
            return {
                "jsonrpc": "2.0",
                "id": request.get("id"),
                "result": {"lease_id": "lease-win-1"},
            }
        if method == "prompt.submit":
            params = request["params"]
            assert params["lease_id"] == "lease-win-1"
            assert params["client_request_id"] == "client-request-1"
            assert params["session_key"] == "session-a"
            return {
                "jsonrpc": "2.0",
                "id": request.get("id"),
                "result": {"status": "accepted"},
            }
        raise AssertionError(f"unexpected method {method}")

    registration = start_control_endpoint(authority=plugin, dispatcher=dispatcher)
    current = runtime

    async def authority() -> LocalRuntimeAuthority:
        return current

    relay = WindowsPluginControlRelay(
        profile="default",
        user_id="device-win-runtime",
        provider="hermes-cloud",
        authority=authority,
        timeout_seconds=2.0,
    )
    command = CommandDelivery(
        command_id=UUID("11111111-1111-4111-8111-111111111111"),
        connector_instance_id=UUID("22222222-2222-4222-8222-222222222222"),
        client_instance_id=UUID("33333333-3333-4333-8333-333333333333"),
        session_key="session-a",
        profile="default",
        client_request_id="client-request-1",
        method="prompt.submit",
        params={
            "runtime_session_id": "runtime-session-1",
            "runtime_generation": runtime.runtime_generation,
            "prompt": "hello",
        },
        issued_at=NOW,
        expires_at=NOW + timedelta(seconds=30),
        revision=1,
    )
    try:
        result = await relay.execute(command)
    finally:
        registration.close()

    assert result == {"status": "accepted"}
    assert [item["method"] for item in observed] == [
        "session.control.acquire",
        "prompt.submit",
    ]


@pytest.mark.asyncio
async def test_windows_owner_control_channel_is_persistent_and_maps_local_status() -> None:
    plugin, runtime = _authorities()
    observed: list[dict] = []

    def dispatcher(request: dict, _transport: object) -> dict:
        observed.append(request)
        method = request.get("method")
        if method == "session.control.acquire":
            return {
                "jsonrpc": "2.0",
                "id": request.get("id"),
                "result": {"lease_id": "owner-lease-1"},
            }
        if method == "session.control.status":
            return {
                "jsonrpc": "2.0",
                "id": request.get("id"),
                "result": {"controller_kind": "local", "status": "active"},
            }
        if method == "prompt.submit":
            return {
                "jsonrpc": "2.0",
                "id": request.get("id"),
                "result": {"status": "unknown"},
            }
        raise AssertionError(f"unexpected method {method}")

    registration = start_control_endpoint(authority=plugin, dispatcher=dispatcher)

    async def authority() -> LocalRuntimeAuthority:
        return runtime

    scope = SimpleNamespace(
        control_transport_id=UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
        principal_id="device-owner-win",
        client_instance_id=UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
        session_key="session-owner",
        profile="default",
    )
    factory = WindowsPluginOwnerControlChannelFactory(
        profile="default",
        provider="hermes-cloud",
        authority=authority,
    )
    channel = None
    try:
        channel = await factory.open(
            scope=scope,
            request_id=UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc"),
            timeout_seconds=2.0,
        )
        await channel.execute(
            operation="session.control.acquire",
            request_id=UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd"),
            body={"runtime_session_id": "runtime-owner-1"},
            timeout_seconds=2.0,
        )
        status = await channel.execute(
            operation="session.control.status",
            request_id=UUID("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"),
            body={},
            timeout_seconds=2.0,
        )
        assert status["controller_kind"] == "desktop"
        with pytest.raises(OwnerControlOutcomeUnknown):
            await channel.execute(
                operation="prompt.submit",
                request_id=UUID("ffffffff-ffff-4fff-8fff-ffffffffffff"),
                body={"prompt": "hello"},
                timeout_seconds=2.0,
            )
    finally:
        if channel is not None:
            await channel.close()
        registration.close()

    assert [item["method"] for item in observed] == [
        "session.control.acquire",
        "session.control.status",
        "prompt.submit",
    ]
    assert observed[1]["params"]["runtime_session_id"] == "runtime-owner-1"
