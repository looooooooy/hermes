"""Public entry point for the Hermes Agent Plugin."""

from __future__ import annotations

from typing import Any

__all__ = [
    "HermesAgentPluginExtension",
    "HermesHostCompatibilityError",
    "register",
]


def __getattr__(name: str) -> Any:
    if name == "HermesAgentPluginExtension":
        from .adapters.host.extension import HermesAgentPluginExtension

        value = HermesAgentPluginExtension
    elif name in {"HermesHostCompatibilityError", "register"}:
        from .bootstrap.registration import HermesHostCompatibilityError, register

        value = {
            "HermesHostCompatibilityError": HermesHostCompatibilityError,
            "register": register,
        }[name]
    else:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    globals()[name] = value
    return value
