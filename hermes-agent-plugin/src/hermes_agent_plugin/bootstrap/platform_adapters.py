"""Select and inject host-platform adapters at the composition root."""

from __future__ import annotations

import sys
from collections.abc import Callable

from ..adapters.platform.capabilities import LocalGatewayPlatformCapabilities
from ..ports.local_relay import configure_local_relay_backend


def select_platform_local_gateway_capabilities() -> LocalGatewayPlatformCapabilities:
    """Return the truthful Local Gateway capability record for this host."""

    if sys.platform == "darwin":
        from ..adapters.platform.macos.availability import (
            LOCAL_GATEWAY_CAPABILITIES,
        )
    elif sys.platform.startswith("linux"):
        from ..adapters.platform.linux.availability import (
            LOCAL_GATEWAY_CAPABILITIES,
        )
    elif sys.platform == "win32":
        from ..adapters.platform.windows.availability import (
            LOCAL_GATEWAY_CAPABILITIES,
        )
    else:
        return LocalGatewayPlatformCapabilities(
            platform=sys.platform,
            available=False,
            transport=None,
            features=frozenset(),
            unavailable_reason="unsupported_local_gateway_platform",
        )
    return LOCAL_GATEWAY_CAPABILITIES


LOCAL_GATEWAY_CAPABILITIES = select_platform_local_gateway_capabilities()
LOCAL_GATEWAY_AVAILABLE = LOCAL_GATEWAY_CAPABILITIES.available


def configure_platform_adapters() -> object | None:
    """Inject a real backend or an explicit fail-closed platform backend."""

    if sys.platform == "darwin":
        from ..adapters.platform.macos import local_gateway_paths
        from ..adapters.platform.macos.local_relay import (
            create_local_relay_backend,
        )

        backend = create_local_relay_backend(
            local_gateway_paths.load_local_gateway_paths()
        )

        def configured_local_relay_backend():
            return backend
    elif sys.platform.startswith("linux"):
        from ..adapters.platform.linux.local_relay import (
            create_local_relay_backend as configured_local_relay_backend,
        )
    elif sys.platform == "win32":
        from ..adapters.platform.windows.local_relay import (
            create_local_relay_backend as configured_local_relay_backend,
        )
    else:
        from ..adapters.platform.capabilities import (
            UnavailableLocalRelayBackend,
        )

        backend = UnavailableLocalRelayBackend("unsupported_local_relay_platform")

        def configured_local_relay_backend():
            return backend

    configure_local_relay_backend(configured_local_relay_backend)
    if sys.platform == "darwin":
        return create_macos_endpoint_opener(backend=backend)
    return None


def create_macos_endpoint_opener(
    *,
    backend: object,
    host_authority_factory: Callable[..., object] | None = None,
) -> object:
    """Compose the process-scoped macOS Host endpoint opener."""

    from ..adapters.platform.macos.host_endpoint_opener import (
        MacOSHostEndpointOpener,
    )
    from ..adapters.platform.macos.runtime_descriptor_v2 import (
        capture_macos_host_authority,
    )

    return MacOSHostEndpointOpener(
        backend=backend,
        host_authority_factory=(
            capture_macos_host_authority
            if host_authority_factory is None
            else host_authority_factory
        ),
    )


__all__ = [
    "LOCAL_GATEWAY_AVAILABLE",
    "LOCAL_GATEWAY_CAPABILITIES",
    "configure_platform_adapters",
    "create_macos_endpoint_opener",
    "select_platform_local_gateway_capabilities",
]
