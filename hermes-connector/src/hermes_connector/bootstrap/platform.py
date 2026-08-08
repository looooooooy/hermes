from __future__ import annotations

import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from hermes_connector.adapters.platform.availability import PlatformUnavailable
from hermes_connector.ports.instance_lock import InstanceLockPort
from hermes_connector.ports.local_gateway import (
    AgentDiscoveryPort,
    LocalGatewayTransportPort,
)

MetadataValidator = Callable[[os.stat_result], None]


class AgentDiscoveryFactory(Protocol):
    def __call__(
        self,
        registry_directory: Path,
        socket_directory: Path,
    ) -> AgentDiscoveryPort: ...


class LocalGatewayTransportFactory(Protocol):
    def __call__(self) -> LocalGatewayTransportPort: ...


class InstanceLockFactory(Protocol):
    def __call__(
        self,
        path: str | os.PathLike[str],
        *,
        metadata_validator: MetadataValidator | None = None,
    ) -> InstanceLockPort: ...


@dataclass(frozen=True)
class PlatformAdapterTypes:
    platform_name: str
    agent_discovery_type: AgentDiscoveryFactory
    local_gateway_transport_type: LocalGatewayTransportFactory
    instance_lock_type: InstanceLockFactory


def select_platform_adapters(
    platform_name: str | None = None,
) -> PlatformAdapterTypes:
    """Select only verified adapters at the process composition root."""

    selected = sys.platform if platform_name is None else platform_name
    if selected == "darwin":
        from hermes_connector.adapters.platform.macos import (
            AVAILABILITY,
            MacOSAgentDiscovery,
            MacOSInstanceLock,
            MacOSLocalGatewayTransport,
        )

        AVAILABILITY.require_available()
        return PlatformAdapterTypes(
            platform_name=AVAILABILITY.platform_name,
            agent_discovery_type=MacOSAgentDiscovery,
            local_gateway_transport_type=MacOSLocalGatewayTransport,
            instance_lock_type=MacOSInstanceLock,
        )
    if selected in {"cygwin", "win32"}:
        if sys.platform not in {"cygwin", "win32"}:
            raise PlatformUnavailable(
                "Hermes Connector Windows adapters require a Windows host"
            )
        from hermes_connector.adapters.platform.windows.agent_discovery import (
            WindowsAgentDiscovery,
        )
        from hermes_connector.adapters.platform.windows.availability import (
            AVAILABILITY,
        )
        from hermes_connector.adapters.platform.windows.instance_lock import (
            WindowsInstanceLock,
        )
        from hermes_connector.adapters.platform.windows.local_gateway_transport import (
            WindowsLocalGatewayTransport,
        )

        AVAILABILITY.require_available()
        return PlatformAdapterTypes(
            platform_name=AVAILABILITY.platform_name,
            agent_discovery_type=WindowsAgentDiscovery,
            local_gateway_transport_type=WindowsLocalGatewayTransport,
            instance_lock_type=WindowsInstanceLock,
        )
    if selected == "linux":
        from hermes_connector.adapters.platform.linux import AVAILABILITY

        AVAILABILITY.require_available()
    raise PlatformUnavailable(
        f"Hermes Connector is unavailable on {selected}: unsupported platform"
    )
