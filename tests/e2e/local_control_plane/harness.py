"""Real Plugin ↔ Connector local control-plane harness."""

from __future__ import annotations

import asyncio
import json
import stat
import tempfile
import threading
from pathlib import Path
from uuid import UUID

from hermes_connector.adapters.contract_codec import (
    decode_local_gateway_response,
)
from hermes_connector.adapters.platform.macos.agent_discovery import (
    MacOSAgentDiscovery,
)
from hermes_connector.adapters.platform.macos.local_gateway_transport import (
    MacOSLocalGatewayTransport,
)
from hermes_connector.application.local_gateway_client import (
    LocalGatewayClient,
)
from hermes_connector.bootstrap.config import ConnectorConfig
from hermes_connector.domain.contract_messages import (
    LocalGatewayErrorResponse,
)
from tests.e2e.plugin_test_runtime import LocalGatewayTestRuntime

from hermes_agent_plugin.adapters.platform.macos import (
    MacOSLocalGatewayPaths,
)
from hermes_agent_plugin.adapters.platform.macos.runtime_descriptor_v2 import (
    RUNTIME_DESCRIPTOR_FIELDS,
    MacOSRuntimeAuthorityV2,
)

from .contract_authority import LocalGatewayContractAuthority
from .models import ActiveSessionEvidence, RejectionEvidence


class _RecordingSessionState:
    def __init__(self) -> None:
        self.invalidations: list[tuple[str, str]] = []

    async def invalidate_runtime(
        self,
        previous_generation: str,
        current_generation: str,
    ) -> None:
        self.invalidations.append((previous_generation, current_generation))


def _settings(root: Path) -> MacOSLocalGatewayPaths:
    return MacOSLocalGatewayPaths(
        local_gateway_registry_directory=root / "local-registry",
        local_gateway_socket_directory=root / "local-sockets",
        control_registry_directory=root / "control-registry",
        control_socket_directory=root / "control-sockets",
        observer_registry_directory=root / "observer-registry",
        observer_socket_directory=root / "observer-sockets",
    )


def _descriptor_is_trusted(
    descriptor_path: Path,
    *,
    authority: MacOSRuntimeAuthorityV2,
) -> bool:
    value = json.loads(descriptor_path.read_text(encoding="utf-8"))
    socket_path = Path(value["socket_path"])
    return (
        set(value) == set(RUNTIME_DESCRIPTOR_FIELDS)
        and value["version"] == 2
        and value["pid"] == authority.pid
        and value["profile"] == authority.profile
        and value["runtime_generation"] == authority.runtime_generation
        and value["instance_id"] == authority.instance_id
        and value["host_bundle_id"] == authority.host_bundle_id
        and value["process_start_time_ns"] == authority.process_identity.start_time_ns
        and value["process_executable"]
        == str(authority.process_identity.executable_path)
        and value["process_executable_device"]
        == authority.process_identity.executable_device
        and value["process_executable_inode"]
        == authority.process_identity.executable_inode
        and stat.S_IMODE(descriptor_path.stat().st_mode) == 0o600
        and stat.S_IMODE(descriptor_path.parent.stat().st_mode) == 0o700
        and socket_path.is_socket()
        and stat.S_IMODE(socket_path.stat().st_mode) == 0o600
    )


def _new_thread_names(
    initial_thread_ids: frozenset[int | None],
) -> tuple[str, ...]:
    return tuple(
        sorted(
            thread.name
            for thread in threading.enumerate()
            if thread.ident not in initial_thread_ids
            and thread.is_alive()
            and thread.name.startswith("local-gateway-")
        )
    )


def _new_async_task_names(
    initial_tasks: frozenset[asyncio.Task[object]],
) -> tuple[str, ...]:
    current = asyncio.current_task()
    return tuple(
        sorted(
            task.get_name()
            for task in asyncio.all_tasks()
            if task is not current and task not in initial_tasks and not task.done()
        )
    )


async def exercise_active_session(
    authority: LocalGatewayContractAuthority,
) -> ActiveSessionEvidence:
    initial_thread_ids = frozenset(thread.ident for thread in threading.enumerate())
    initial_tasks = frozenset(asyncio.all_tasks())
    session_state = _RecordingSessionState()
    plugin: LocalGatewayTestRuntime | None = None
    client: LocalGatewayClient | None = None
    runner: asyncio.Task[None] | None = None
    descriptor_path: Path | None = None
    socket_path: Path | None = None

    with tempfile.TemporaryDirectory(
        prefix="hlg-e2e-",
        dir="/tmp",
    ) as raw_root:
        root = Path(raw_root).resolve(strict=True)
        settings = _settings(root)
        plugin = LocalGatewayTestRuntime(
            generation_factory=lambda: authority.welcome["runtime_generation"],
            macos_local_gateway_paths=settings,
            available_capabilities=frozenset(
                authority.welcome["accepted_capabilities"]
            ),
        )
        try:
            plugin.install()
            plugin.start(timeout_s=2.0)
            descriptor_paths = tuple(
                settings.local_gateway_registry_directory.glob("gateway-*.json")
            )
            if len(descriptor_paths) != 1:
                raise AssertionError("Plugin did not publish one descriptor")
            descriptor_path = descriptor_paths[0]
            descriptor_trusted = _descriptor_is_trusted(
                descriptor_path,
                authority=plugin.runtime_authority,
            )
            descriptor = json.loads(descriptor_path.read_text(encoding="utf-8"))
            socket_path = Path(descriptor["socket_path"])

            discovery = MacOSAgentDiscovery(
                settings.local_gateway_registry_directory,
                settings.local_gateway_socket_directory,
            )
            endpoints = await discovery.discover(authority.hello["profile"])
            client = LocalGatewayClient(
                profile=authority.hello["profile"],
                client_instance_id=UUID(authority.hello["client_instance_id"]),
                required_capabilities=tuple(authority.hello["required_capabilities"]),
                optional_capabilities=tuple(authority.hello["optional_capabilities"]),
                discovery=discovery,
                transport=MacOSLocalGatewayTransport(),
                session_state=session_state,
                config=ConnectorConfig(
                    local_connect_timeout_seconds=1.0,
                    local_rpc_deadline_seconds=2.0,
                    local_max_reconnect_attempts=1,
                    local_discovery_poll_interval_seconds=30.0,
                ),
            )
            await client.start()
            runner = asyncio.create_task(
                client.run(),
                name="e2e-local-gateway-runner",
            )
            ready = await asyncio.wait_for(client.ready(), timeout=2.0)
            if not ready:
                await runner
                raise AssertionError("Connector did not become ready")

            plugin_ready = plugin.ready
            connector_state = client.state.value
            state_history = tuple(state.value for state in client.state_history)
            runtime_generation = client.runtime_generation
            accepted_capabilities = client.accepted_capabilities
            unavailable_optional_capabilities = client.unavailable_optional_capabilities
            endpoint_count = len(endpoints)
        finally:
            if client is not None:
                await client.drain()
                await client.stop()
            if runner is not None:
                await asyncio.wait_for(runner, timeout=2.0)
            if plugin is not None:
                plugin.stop(timeout_s=2.0)

        await asyncio.sleep(0)
        return ActiveSessionEvidence(
            plugin_ready=plugin_ready,
            descriptor_trusted=descriptor_trusted,
            endpoint_count=endpoint_count,
            connector_state=connector_state,
            state_history=state_history,
            runtime_generation=runtime_generation,
            accepted_capabilities=accepted_capabilities,
            unavailable_optional_capabilities=unavailable_optional_capabilities,
            descriptor_removed=(
                descriptor_path is not None and not descriptor_path.exists()
            ),
            socket_removed=(socket_path is not None and not socket_path.exists()),
            leaked_async_tasks=_new_async_task_names(initial_tasks),
            leaked_threads=_new_thread_names(initial_thread_ids),
        )


async def exercise_incompatible_contract(
    authority: LocalGatewayContractAuthority,
) -> RejectionEvidence:
    initial_thread_ids = frozenset(thread.ident for thread in threading.enumerate())
    initial_tasks = frozenset(asyncio.all_tasks())
    plugin: LocalGatewayTestRuntime | None = None
    descriptor_path: Path | None = None
    socket_path: Path | None = None
    error_code: int | None = None
    error_reason: str | None = None
    plugin_still_ready = False

    with tempfile.TemporaryDirectory(
        prefix="hlg-reject-",
        dir="/tmp",
    ) as raw_root:
        root = Path(raw_root).resolve(strict=True)
        settings = _settings(root)
        plugin = LocalGatewayTestRuntime(
            generation_factory=lambda: authority.welcome["runtime_generation"],
            macos_local_gateway_paths=settings,
            available_capabilities=frozenset(
                authority.welcome["accepted_capabilities"]
            ),
        )
        try:
            plugin.install()
            plugin.start(timeout_s=2.0)
            descriptor_path = next(
                settings.local_gateway_registry_directory.glob("gateway-*.json")
            )
            descriptor = json.loads(descriptor_path.read_text(encoding="utf-8"))
            socket_path = Path(descriptor["socket_path"])
            endpoints = await MacOSAgentDiscovery(
                settings.local_gateway_registry_directory,
                settings.local_gateway_socket_directory,
            ).discover(authority.hello["profile"])
            if len(endpoints) != 1:
                raise AssertionError("trusted Plugin endpoint is undiscoverable")

            incompatible_hello = dict(authority.hello)
            incompatible_hello["contract_version"] = authority.version + 1
            frame = json.dumps(
                incompatible_hello,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            connection = await MacOSLocalGatewayTransport().connect(endpoints[0])
            try:
                response = await connection.exchange(frame)
            finally:
                await connection.close()
            rejection = decode_local_gateway_response(response)
            if not isinstance(rejection, LocalGatewayErrorResponse):
                raise AssertionError(  # noqa: TRY004 - E2E assertion, not API input
                    "incompatible contract received LocalWelcome"
                )
            error_code = rejection.code
            error_reason = rejection.reason
            plugin_still_ready = plugin.ready
        finally:
            if plugin is not None:
                plugin.stop(timeout_s=2.0)

        await asyncio.sleep(0)
        return RejectionEvidence(
            error_code=error_code,
            error_reason=error_reason,
            plugin_still_ready=plugin_still_ready,
            command_effects=0,
            descriptor_removed=(
                descriptor_path is not None and not descriptor_path.exists()
            ),
            socket_removed=(socket_path is not None and not socket_path.exists()),
            leaked_async_tasks=_new_async_task_names(initial_tasks),
            leaked_threads=_new_thread_names(initial_thread_ids),
        )
