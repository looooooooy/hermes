"""Verified macOS adapter implementations."""

from hermes_connector.adapters.platform.macos import observer_client as _observer_client
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
from hermes_connector.adapters.platform.macos.observer_discovery import (
    MacOSObserverEndpointDiscovery,
)
from hermes_connector.adapters.platform.macos.session_catalog_client import (
    MacOSSessionCatalogClient,
)
from hermes_connector.ports.observer import ObserverResnapshotRequired

# Compatibility bridge: the historical macOS observer module defined this error
# locally. Rebind its global so existing imports and runtime raises now use the
# platform-neutral port contract without changing the public macOS import path.
_observer_client.ObserverResnapshotRequired = ObserverResnapshotRequired
MacOSObserverClient = _observer_client.MacOSObserverClient

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
