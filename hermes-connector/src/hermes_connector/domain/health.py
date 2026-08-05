from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class SupervisorPhase(StrEnum):
    NEW = "new"
    STARTING = "starting"
    READY = "ready"
    DRAINING = "draining"
    STOPPED = "stopped"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ComponentSnapshot:
    name: str
    state: str


@dataclass(frozen=True, slots=True)
class HealthSnapshot:
    live: bool
    ready: bool
    phase: SupervisorPhase
    components: tuple[ComponentSnapshot, ...]
    failure_category: str | None = None
