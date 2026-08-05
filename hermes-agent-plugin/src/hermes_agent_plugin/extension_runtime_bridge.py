"""Runtime extension bridge for Hermes Host SPI integration.

This module keeps the Host SPI adapter boundary thin. It connects an already
registered Hermes extension to the runtime health/lifecycle primitives without
exposing Agent internals to the plugin layer.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .extension_lifecycle import ExtensionLifecycleCoordinator
from .runtime_binding import RuntimeBinding


@dataclass(slots=True)
class ExtensionRuntimeBridge:
    """Coordinates extension readiness publication.

    The bridge intentionally does not own the Hermes runtime. The Core Host
    remains the authority for runtime identity; this object only records the
    extension lifecycle transition after the Host SPI registration succeeds.
    """

    lifecycle: ExtensionLifecycleCoordinator
    extension_name: str
    extension_version: str

    def registered(self) -> None:
        self.lifecycle.register(
            self.extension_name,
            self.extension_version,
        )

    def ready(self, runtime: RuntimeBinding, capabilities: set[str] | None = None) -> None:
        self.lifecycle.ready(
            self.extension_name,
            runtime,
            capabilities or set(),
        )

    def failed(self, error: Exception | str) -> None:
        self.lifecycle.failed(
            self.extension_name,
            str(error),
        )

    def snapshot(self) -> dict[str, Any]:
        return self.lifecycle.snapshot()


__all__ = ["ExtensionRuntimeBridge"]
