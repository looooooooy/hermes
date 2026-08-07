"""Connector use cases with infrastructure-lazy public exports."""

from __future__ import annotations

from importlib import import_module
from typing import Any

__all__ = [
    "RequiredCapabilityUnavailable",
    "ServiceRunner",
    "ServiceState",
    "Supervisor",
    "SupervisorPhase",
    "SupervisorStartError",
    "SupervisorStopError",
    "negotiate_local_capabilities",
]

_EXPORTS = {
    "RequiredCapabilityUnavailable": (
        "hermes_connector.application.capability_negotiation",
        "RequiredCapabilityUnavailable",
    ),
    "negotiate_local_capabilities": (
        "hermes_connector.application.capability_negotiation",
        "negotiate_local_capabilities",
    ),
    "ServiceRunner": (
        "hermes_connector.application.service_runner",
        "ServiceRunner",
    ),
    "ServiceState": (
        "hermes_connector.application.service_runner",
        "ServiceState",
    ),
    "Supervisor": (
        "hermes_connector.application.supervisor",
        "Supervisor",
    ),
    "SupervisorPhase": (
        "hermes_connector.application.supervisor",
        "SupervisorPhase",
    ),
    "SupervisorStartError": (
        "hermes_connector.application.supervisor",
        "SupervisorStartError",
    ),
    "SupervisorStopError": (
        "hermes_connector.application.supervisor",
        "SupervisorStopError",
    ),
}


def __getattr__(name: str) -> Any:
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute_name = target
    value = getattr(import_module(module_name), attribute_name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted({*globals(), *__all__})
