"""Compatibility aliases for the former POSIX discovery import path."""

from hermes_connector.adapters.platform.macos.agent_discovery import (
    DEFAULT_MAX_CANDIDATES,
    MAX_DESCRIPTOR_BYTES,
    MacOSAgentDiscovery,
)

PosixAgentDiscovery = MacOSAgentDiscovery

__all__ = [
    "DEFAULT_MAX_CANDIDATES",
    "MAX_DESCRIPTOR_BYTES",
    "PosixAgentDiscovery",
]
