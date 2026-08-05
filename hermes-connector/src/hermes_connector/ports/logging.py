from __future__ import annotations

from enum import StrEnum
from typing import Protocol


class LogCategory(StrEnum):
    LIFECYCLE = "lifecycle"
    HEALTH = "health"


class LogState(StrEnum):
    STARTING = "starting"
    READY = "ready"
    DRAINING = "draining"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"


class SafeLogPort(Protocol):
    def emit(
        self,
        *,
        category: LogCategory,
        component: str,
        state: LogState,
    ) -> None:
        """Emit one payload-free lifecycle or health event.

        Input/unit: enum category/state and stable component identifier text.
        Deadline: synchronous bounded sink call; no network retry loop.
        Idempotency key: none; duplicate observations are allowed.
        Effect: appends one safe metadata-only log event without secrets/payloads.
        Return: ``None`` after sink acceptance. Errors: type, identifier, or sink
        failures.
        """
