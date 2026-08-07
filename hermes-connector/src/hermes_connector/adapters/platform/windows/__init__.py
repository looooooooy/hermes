"""Windows Connector platform foundation."""

from hermes_connector.adapters.platform.windows.agent_discovery import (
    WindowsAgentDiscovery,
)
from hermes_connector.adapters.platform.windows.availability import AVAILABILITY
from hermes_connector.adapters.platform.windows.instance_lock import WindowsInstanceLock
from hermes_connector.adapters.platform.windows.local_gateway_transport import (
    WindowsLocalGatewayTransport,
)

__all__ = [
    "AVAILABILITY",
    "WindowsAgentDiscovery",
    "WindowsInstanceLock",
    "WindowsLocalGatewayTransport",
]
