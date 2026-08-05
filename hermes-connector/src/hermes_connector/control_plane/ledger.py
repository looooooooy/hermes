"""Durable command tracking primitives for connector control plane."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class CommandState(str, Enum):
    RECEIVED = "received"
    VALIDATED = "validated"
    DISPATCHED = "dispatched"
    ACCEPTED = "accepted"
    EFFECT_STARTED = "effect_started"
    COMPLETED = "completed"
    EFFECT_UNKNOWN = "effect_unknown"


@dataclass(slots=True)
class CommandLedgerEntry:
    command_id: str
    runtime_generation: str
    session_id: str | None
    action: str
    state: CommandState = CommandState.RECEIVED

    def transition(self, state: CommandState) -> None:
        self.state = state
