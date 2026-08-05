"""Dependency readiness probe port."""

from __future__ import annotations

from typing import Protocol


class DependencyProbe(Protocol):
    """Check one named dependency within an explicit deadline."""

    name: str
    critical: bool
    deadline_seconds: float

    async def check(self) -> None:
        """Return on success or raise without exposing the error to snapshots."""
