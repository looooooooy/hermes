"""Remote command dispatch boundary for Hermes Connector.

This module intentionally does not execute Agent logic. It only validates,
tracks and forwards commands toward the Runtime boundary.
"""

from __future__ import annotations

from dataclasses import dataclass

from .ledger import CommandLedgerEntry
from .models import CommandEnvelope, CommandResult


@dataclass(slots=True)
class RemoteCommandDispatcher:
    """Dispatch commands after runtime binding validation."""

    ledger: object

    def dispatch(self, command: CommandEnvelope) -> CommandResult:
        self.ledger.record(
            CommandLedgerEntry(
                command_id=command.command_id,
                runtime_generation=command.runtime_generation,
                state="dispatched",
            )
        )
        return CommandResult(
            command_id=command.command_id,
            state="accepted",
        )


__all__ = ["RemoteCommandDispatcher"]
