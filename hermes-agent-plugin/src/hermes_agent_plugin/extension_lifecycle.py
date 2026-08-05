"""Runtime extension lifecycle coordination.

Keeps plugin extension state transitions separate from Hermes runtime internals.
The host owns the lifecycle; this module only records and projects extension
readiness for connector/runtime diagnostics.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .runtime_binding import (
    ExtensionRegistry,
    ExtensionState,
    ExtensionStatus,
    RuntimeBinding,
)


@dataclass(frozen=True, slots=True)
class ExtensionLifecycleResult:
    name: str
    state: str
    ready: bool


class ExtensionLifecycleCoordinator:
    """Coordinates extension registration and readiness publication."""

    def __init__(self, registry: ExtensionRegistry | None = None) -> None:
        self.registry = registry or ExtensionRegistry()

    def register_extension(
        self,
        name: str,
        version: str,
        capabilities: Iterable[str] = (),
    ) -> ExtensionStatus:
        extension = ExtensionStatus(
            name=name,
            version=version,
            capabilities=set(capabilities),
        )
        self.registry.register(extension)
        return extension

    def mark_ready(
        self,
        name: str,
        runtime: RuntimeBinding,
    ) -> ExtensionLifecycleResult:
        self.registry.mark_ready(name, runtime)
        extension = next(
            item for item in self.registry._extensions.values()
            if item.name == name
        )
        return ExtensionLifecycleResult(
            name=extension.name,
            state=extension.state.value,
            ready=extension.state is ExtensionState.READY,
        )

    def snapshot(self) -> list[dict[str, object]]:
        return self.registry.snapshot()


__all__ = [
    "ExtensionLifecycleCoordinator",
    "ExtensionLifecycleResult",
]
