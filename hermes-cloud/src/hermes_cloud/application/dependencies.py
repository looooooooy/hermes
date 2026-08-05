"""Safe dependency probe result model."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from hermes_cloud.errors import ClassifiedError


class DependencyCriticality(str, Enum):
    CRITICAL = "CRITICAL"
    OPTIONAL = "OPTIONAL"


class DependencyStatus(str, Enum):
    HEALTHY = "HEALTHY"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True)
class DependencyProbeResult:
    """Public-safe outcome of one bounded dependency probe."""

    name: str
    criticality: DependencyCriticality
    status: DependencyStatus
    error: ClassifiedError | None = None

    @property
    def is_healthy(self) -> bool:
        return self.status is DependencyStatus.HEALTHY

    def as_dict(self) -> dict[str, object]:
        return {
            "criticality": self.criticality.value,
            "error": None if self.error is None else self.error.as_dict(),
            "name": self.name,
            "status": self.status.value,
        }
