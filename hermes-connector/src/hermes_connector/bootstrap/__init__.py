"""Runtime configuration with infrastructure-lazy public exports."""

from __future__ import annotations

from importlib import import_module
from typing import Any

__all__ = [
    "ConnectorConfig",
    "SafeStructuredLogger",
    "build_service_runner",
]

_EXPORTS = {
    "ConnectorConfig": (
        "hermes_connector.bootstrap.config",
        "ConnectorConfig",
    ),
    "SafeStructuredLogger": (
        "hermes_connector.bootstrap.safe_logging",
        "SafeStructuredLogger",
    ),
    "build_service_runner": (
        "hermes_connector.bootstrap.runtime",
        "build_service_runner",
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
