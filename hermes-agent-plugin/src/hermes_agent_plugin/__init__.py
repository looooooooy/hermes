"""Public entry point for the Hermes Agent Plugin."""

from __future__ import annotations

from typing import Any

__all__ = [
    "HermesAgentPluginExtension",
    "HermesHostCompatibilityError",
    "ExtensionLifecycleCoordinator",
    "ExtensionRegistry",
    "ExtensionState",
    "RuntimeBinding",
    "RuntimeHealthSnapshot",
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
    elif name in {
        "ExtensionLifecycleCoordinator",
        "ExtensionRegistry",
        "ExtensionState",
        "RuntimeBinding",
    }:
        from .runtime_binding import (
            ExtensionRegistry,
            ExtensionState,
            RuntimeBinding,
        )
        from .extension_lifecycle import ExtensionLifecycleCoordinator
        value = {
            "ExtensionLifecycleCoordinator": ExtensionLifecycleCoordinator,
            "ExtensionRegistry": ExtensionRegistry,
            "ExtensionState": ExtensionState,
            "RuntimeBinding": RuntimeBinding,
        }[name]
    elif name == "RuntimeHealthSnapshot":
        from .runtime_health import RuntimeHealthSnapshot
        value = RuntimeHealthSnapshot
    else:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    globals()[name] = value
    return value
