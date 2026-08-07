"""Runtime binding primitives for Hermes Agent Plugin extensions.

This module intentionally contains no Agent internals. It models the boundary
between a loaded plugin extension and the authoritative Hermes runtime.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from time import time


class ExtensionState(str, Enum):
    REGISTERED = "registered"
    INITIALIZING = "initializing"
    READY = "ready"
    DEGRADED = "degraded"
    FAILED = "failed"
    STOPPED = "stopped"


@dataclass(frozen=True, slots=True)
class RuntimeBinding:
    runtime_id: str
    runtime_generation: str
    profile: str


@dataclass(slots=True)
class ExtensionStatus:
    name: str
    version: str
    state: ExtensionState = ExtensionState.REGISTERED
    runtime: RuntimeBinding | None = None
    capabilities: set[str] = field(default_factory=set)
    updated_at: float = field(default_factory=time)
    error: str | None = None

    def ready(self, runtime: RuntimeBinding) -> None:
        self.runtime = runtime
        self.state = ExtensionState.READY
        self.error = None
        self.updated_at = time()

    def fail(self, error: str) -> None:
        self.state = ExtensionState.FAILED
        self.error = error
        self.updated_at = time()

    def snapshot(self) -> dict[str, object]:
        return {
            "name": self.name,
            "version": self.version,
            "state": self.state.value,
            "capabilities": sorted(self.capabilities),
            "runtime_generation": (
                self.runtime.runtime_generation if self.runtime else None
            ),
            "updated_at": self.updated_at,
            "error": self.error,
        }


class ExtensionRegistry:
    """In-process registry owned by the runtime host."""

    def __init__(self) -> None:
        self._extensions: dict[str, ExtensionStatus] = {}

    def register(self, extension: ExtensionStatus) -> None:
        if extension.name in self._extensions:
            raise ValueError(f"extension already registered: {extension.name}")
        self._extensions[extension.name] = extension

    def mark_ready(self, name: str, runtime: RuntimeBinding) -> None:
        self._extensions[name].ready(runtime)

    def snapshot(self) -> list[dict[str, object]]:
        return [
            extension.snapshot()
            for extension in self._extensions.values()
        ]
