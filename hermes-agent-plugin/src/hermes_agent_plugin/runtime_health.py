"""Runtime health projection for Hermes Agent Plugin extensions.

This module intentionally stays outside Agent internals. It projects the
extension boundary state that Connector/Cloud layers need in order to verify
that they are attached to the expected Hermes runtime.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from time import time

from .runtime_binding import ExtensionRegistry, RuntimeBinding


@dataclass(slots=True)
class RuntimeHealthSnapshot:
    runtime: RuntimeBinding
    extensions: list[dict[str, object]] = field(default_factory=list)
    updated_at: float = field(default_factory=time)

    @property
    def ready(self) -> bool:
        return all(
            extension.get("state") == "ready"
            for extension in self.extensions
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "runtime": {
                "runtime_id": self.runtime.runtime_id,
                "runtime_generation": self.runtime.runtime_generation,
                "profile": self.runtime.profile,
            },
            "extensions": self.extensions,
            "ready": self.ready,
            "updated_at": self.updated_at,
        }


class RuntimeHealthProjector:
    """Projects extension registry state without owning runtime lifecycle."""

    def __init__(self, registry: ExtensionRegistry) -> None:
        self._registry = registry

    def snapshot(self, runtime: RuntimeBinding) -> RuntimeHealthSnapshot:
        return RuntimeHealthSnapshot(
            runtime=runtime,
            extensions=self._registry.snapshot(),
        )


__all__ = ["RuntimeHealthProjector", "RuntimeHealthSnapshot"]
