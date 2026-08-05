"""Runtime health projection primitives."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class RuntimeHealthState(str, Enum):
    INIT = "init"
    READY = "ready"
    DEGRADED = "degraded"
    FAILED = "failed"
    STOPPED = "stopped"


@dataclass(slots=True)
class RuntimeHealth:
    state: RuntimeHealthState = RuntimeHealthState.INIT
    extensions: dict[str, dict[str, Any]] = field(default_factory=dict)

    def register_extension(self, name: str, state: str = "registered") -> None:
        self.extensions[name] = {"state": state}

    def set_extension_state(self, name: str, state: str) -> None:
        if name not in self.extensions:
            self.register_extension(name)
        self.extensions[name]["state"] = state

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "extensions": self.extensions,
        }
