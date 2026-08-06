"""Hermes Connector foundation package with infrastructure-lazy exports."""

from __future__ import annotations

from importlib import import_module
from typing import Any

__all__ = ["ConnectorConfig"]

_EXPORTS = {
    "ConnectorConfig": (
        "hermes_connector.bootstrap.config",
        "ConnectorConfig",
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
