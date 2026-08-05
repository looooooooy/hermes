"""Verified macOS adapter implementations."""

from hermes_connector.adapters.platform.macos.agent_discovery import (
    MacOSAgentDiscovery,
)
from hermes_connector.adapters.platform.macos.availability import AVAILABILITY
from hermes_connector.adapters.platform.macos.credentials import (
    MacOSKeychainCloudTokenProvider,
)
from hermes_connector.adapters.platform.macos.device_identity import (
    MacOSKeychainDeviceIdentity,
)
from hermes_connector.adapters.platform.macos.instance_lock import (
    MacOSInstanceLock,
)
from hermes_connector.adapters.platform.macos.keychain import (
    MacOSKeychainSecretStore,
)
from hermes_connector.adapters.platform.macos.local_gateway_transport import (
    MacOSLocalGatewayConnection,
    MacOSLocalGatewayTransport,
)
from hermes_connector.adapters.platform.macos.local_runtime_preflight import (
    MacOSLocalRuntimePreflight,
)
from hermes_connector.adapters.platform.macos.observer_client import (
    MacOSObserverClient,
)
from hermes_connector.adapters.platform.macos.observer_discovery import (
    MacOSObserverEndpointDiscovery,
)
from hermes_connector.adapters.platform.macos.session_catalog_client import (
    MacOSSessionCatalogClient,
)

__all__ = [
    "AVAILABILITY",
    "MacOSAgentDiscovery",
    "MacOSInstanceLock",
    "MacOSKeychainCloudTokenProvider",
    "MacOSKeychainDeviceIdentity",
    "MacOSKeychainSecretStore",
    "MacOSLocalGatewayConnection",
    "MacOSLocalGatewayTransport",
    "MacOSLocalRuntimePreflight",
    "MacOSObserverClient",
    "MacOSObserverEndpointDiscovery",
    "MacOSSessionCatalogClient",
]
