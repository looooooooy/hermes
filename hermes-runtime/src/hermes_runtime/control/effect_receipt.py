"""Runtime effect receipt models."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class EffectReceipt:
    command_id: str
    event_id: str
    state: str
    detail: str | None = None


COMPLETED = "completed"
FAILED = "failed"
EFFECT_UNKNOWN = "effect_unknown"
