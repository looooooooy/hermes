"""Hermes Agent Host SPI v1 adapters."""

from .extension import (
    ControlEndpointDescriptor,
    HermesAgentPluginExtension,
    ObserverEndpointDescriptor,
)
from .observer_v2 import (
    OUTPUT_PARITY_CAPABILITY,
    ObserverV2Projection,
    ObserverV2Violation,
    load_observer_v2_bundle,
)

__all__ = [
    "OUTPUT_PARITY_CAPABILITY",
    "ControlEndpointDescriptor",
    "HermesAgentPluginExtension",
    "ObserverEndpointDescriptor",
    "ObserverV2Projection",
    "ObserverV2Violation",
    "load_observer_v2_bundle",
]
