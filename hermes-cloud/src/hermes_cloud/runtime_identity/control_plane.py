"""Cloud-side runtime control-plane projection."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class RuntimeProjectionState(str, Enum):
    UNKNOWN = "unknown"
    ACTIVE = "active"
    STALE = "stale"
    OFFLINE = "offline"


@dataclass(frozen=True, slots=True)
class RuntimeControlProjection:
    runtime_id: str
    generation: str
    profile: str
    state: RuntimeProjectionState


class RuntimeControlProjectionService:
    def __init__(self) -> None:
        self._items: dict[str, RuntimeControlProjection] = {}

    def apply(self, projection: RuntimeControlProjection) -> None:
        self._items[projection.runtime_id] = projection

    def get(self, runtime_id: str) -> RuntimeControlProjection | None:
        return self._items.get(runtime_id)
