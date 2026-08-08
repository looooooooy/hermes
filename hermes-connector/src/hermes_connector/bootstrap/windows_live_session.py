"""One-shot read-only live Session probe for Windows update health."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from uuid import uuid4

from hermes_connector.adapters.foundation_projection import (
    FoundationNoOpLocalProjectionInvalidator,
)
from hermes_connector.adapters.local_runtime_preflight import LocalRuntimePreflight
from hermes_connector.adapters.platform.windows.agent_discovery import (
    WindowsAgentDiscovery,
)
from hermes_connector.adapters.platform.windows.local_gateway_transport import (
    WindowsLocalGatewayTransport,
)
from hermes_connector.adapters.platform.windows.session_catalog_client import (
    WindowsSessionCatalogClient,
)
from hermes_connector.application.local_gateway_client import LocalGatewayClient
from hermes_connector.bootstrap.config import ConnectorConfig
from hermes_connector.bootstrap.settings import ConnectorRuntimeSettings

_REQUIRED_CAPABILITIES = ("session.observe",)
_OPTIONAL_CAPABILITIES = ("session.catalog.v1",)


@dataclass(frozen=True, slots=True)
class WindowsLiveSessionEvidence:
    live_session_ok: bool
    runtime_generation: str | None


async def probe_windows_live_session(
    settings: ConnectorRuntimeSettings,
    *,
    config: ConnectorConfig | None = None,
) -> WindowsLiveSessionEvidence:
    """Probe the current Host catalog without mutating Connector or Session state."""

    selected = config or ConnectorConfig()
    discovery = WindowsAgentDiscovery(
        settings.local_gateway_registry_directory,
        settings.local_gateway_socket_directory,
        timeout_seconds=selected.local_connect_timeout_seconds,
    )
    transport = WindowsLocalGatewayTransport(
        connect_timeout_seconds=selected.local_connect_timeout_seconds,
        io_timeout_seconds=selected.local_rpc_deadline_seconds,
    )
    preflight = LocalRuntimePreflight(
        discovery=discovery,
        transport=transport,
        timeout_seconds=selected.local_connect_timeout_seconds,
    )
    expected = preflight.verify(settings.profile)
    if expected is None:
        return WindowsLiveSessionEvidence(False, None)

    gateway = LocalGatewayClient(
        profile=settings.profile,
        client_instance_id=uuid4(),
        required_capabilities=_REQUIRED_CAPABILITIES,
        optional_capabilities=_OPTIONAL_CAPABILITIES,
        discovery=discovery,
        transport=transport,
        session_state=FoundationNoOpLocalProjectionInvalidator(),
        config=selected,
        expected_endpoint=expected,
    )
    catalog = WindowsSessionCatalogClient(
        authority=gateway.current_runtime_authority,
        connect_timeout_seconds=selected.local_connect_timeout_seconds,
        rpc_timeout_seconds=selected.local_rpc_deadline_seconds,
    )
    subscription = None
    run_task: asyncio.Task[None] | None = None
    generation: str | None = None
    try:
        await gateway.start()
        run_task = asyncio.create_task(
            gateway.run(),
            name="hermes-connector:live-session-probe-gateway",
        )
        async with asyncio.timeout(selected.start_deadline_seconds):
            if not await gateway.ready():
                return WindowsLiveSessionEvidence(False, None)
            authority = await gateway.current_runtime_authority()
            if authority is None:
                return WindowsLiveSessionEvidence(False, None)
            generation = authority.runtime_generation
            capabilities = {
                *authority.required_capabilities,
                *authority.optional_capabilities,
            }
            if "session.catalog.v1" not in capabilities:
                return WindowsLiveSessionEvidence(False, generation)
            subscription = await catalog.subscribe(
                profile=settings.profile,
                runtime_generation=generation,
                page_size=128,
            )
            async for page in subscription.pages():
                if page.runtime_generation != generation:
                    return WindowsLiveSessionEvidence(False, generation)
                if page.sessions:
                    return WindowsLiveSessionEvidence(True, generation)
                if page.is_last:
                    return WindowsLiveSessionEvidence(False, generation)
            return WindowsLiveSessionEvidence(False, generation)
    except (ConnectionError, OSError, RuntimeError, TimeoutError, ValueError):
        return WindowsLiveSessionEvidence(False, generation)
    finally:
        if subscription is not None:
            try:
                await subscription.close()
            except (ConnectionError, OSError, RuntimeError, TimeoutError):
                pass
        try:
            await catalog.aclose()
        except (ConnectionError, OSError, RuntimeError, TimeoutError):
            pass
        try:
            await gateway.drain()
        except (ConnectionError, OSError, RuntimeError, TimeoutError):
            pass
        if run_task is not None:
            try:
                async with asyncio.timeout(selected.stop_deadline_seconds):
                    await run_task
            except (ConnectionError, OSError, RuntimeError, TimeoutError):
                run_task.cancel()
                await asyncio.gather(run_task, return_exceptions=True)
        try:
            await gateway.stop()
        except (ConnectionError, OSError, RuntimeError, TimeoutError):
            pass


__all__ = ["WindowsLiveSessionEvidence", "probe_windows_live_session"]
