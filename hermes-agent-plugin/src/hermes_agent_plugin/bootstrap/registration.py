"""Hermes Agent host registration."""

from __future__ import annotations

from typing import Any

from ..adapters.host.extension import HermesAgentPluginExtension
from ..adapters.host.spi_v1 import (
    PublicHostSpiContractUnavailable,
    load_public_host_spi_factories,
)
from ..host_compatibility import (
    HOST_SPI_VERSION,
    REQUIRED_HOST_CAPABILITIES,
    validate_host_context,
)
from .platform_adapters import (
    configure_platform_adapters,
    select_platform_update_safety_opener,
)

INCOMPATIBLE_HOST_MESSAGE = (
    "Hermes Agent Host SPI v1 is unavailable; "
    "hermes-agent-plugin requires a host exposing "
    "gateway_extension_spi_version=1 and register_gateway_extension()"
)
PUBLIC_HOST_CONTRACT_UNAVAILABLE_MESSAGE = (
    "Hermes Agent Host SPI v1 public DTO contract is unavailable; "
    "hermes-agent-plugin requires hermes_cli.extension_host_v1"
)


class HermesHostCompatibilityError(RuntimeError):
    """Raised when Hermes does not expose the required public Host SPI."""


def register(context: Any) -> HermesAgentPluginExtension:
    """Register the process-level extension with Hermes Agent.

    The extension instance is returned so the runtime binding layer can expose
    health and lifecycle state without reaching into Hermes Agent internals.
    """
    validation = validate_host_context(context)
    if not validation.compatible:
        if validation.missing_required_capabilities:
            missing = ", ".join(validation.missing_required_capabilities)
            raise HermesHostCompatibilityError(
                "Hermes Agent Host SPI v1 required capabilities are unavailable: "
                f"{missing}"
            )
        raise HermesHostCompatibilityError(INCOMPATIBLE_HOST_MESSAGE)
    register_extension = validation.register_extension
    if register_extension is None:
        raise HermesHostCompatibilityError(INCOMPATIBLE_HOST_MESSAGE)
    try:
        host_spi_factories = load_public_host_spi_factories()
    except PublicHostSpiContractUnavailable as error:
        raise HermesHostCompatibilityError(
            PUBLIC_HOST_CONTRACT_UNAVAILABLE_MESSAGE
        ) from error
    endpoint_opener = configure_platform_adapters()
    extension = HermesAgentPluginExtension(
        host_spi_factories=host_spi_factories,
        endpoint_opener=endpoint_opener,
        update_safety_opener=select_platform_update_safety_opener(),
    )
    register_extension(
        extension,
        spi_version=HOST_SPI_VERSION,
    )
    return extension


__all__ = [
    "HOST_SPI_VERSION",
    "INCOMPATIBLE_HOST_MESSAGE",
    "PUBLIC_HOST_CONTRACT_UNAVAILABLE_MESSAGE",
    "REQUIRED_HOST_CAPABILITIES",
    "HermesHostCompatibilityError",
    "register",
]
