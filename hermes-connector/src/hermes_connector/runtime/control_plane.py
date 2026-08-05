"""Runtime control-plane state shared by connector orchestration.

This module intentionally keeps runtime identity separate from transport state.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class RuntimeControlState(str, Enum):
    STARTING = "starting"
    DISCOVERING = "discovering"
    VERIFYING = "verifying"
    BOUND = "bound"
    CONNECTING = "connecting"
    ACTIVE = "active"
    STALE = "stale"


@dataclass(frozen=True, slots=True)
class RuntimeControlSnapshot:
    runtime_id: str
    runtime_generation: str
    profile: str
    state: RuntimeControlState


class RuntimeControlPlane:
    def __init__(self) -> None:
        self._state: RuntimeControlSnapshot | None = None

    def update(self, snapshot: RuntimeControlSnapshot) -> None:
        self._state = snapshot

    def snapshot(self) -> RuntimeControlSnapshot | None:
        return self._state
