"""Runtime identity health projection primitives.

Keeps runtime availability separate from connector transport state.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from time import time


class RuntimeHealthState(str, Enum):
    UNKNOWN = "unknown"
    ACTIVE = "active"
    STALE = "stale"
    OFFLINE = "offline"


@dataclass(frozen=True, slots=True)
class RuntimeHealthProjection:
    runtime_id: str
    runtime_generation: str
    state: RuntimeHealthState
    updated_at: float = time()

    def as_dict(self) -> dict[str, object]:
        return {
            "runtime_id": self.runtime_id,
            "runtime_generation": self.runtime_generation,
            "state": self.state.value,
            "updated_at": self.updated_at,
        }
