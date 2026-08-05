"""Explicit host-platform adapter boundaries."""

from hermes_connector.adapters.platform.availability import (
    PlatformAvailability,
    PlatformUnavailable,
)

__all__ = ["PlatformAvailability", "PlatformUnavailable"]
