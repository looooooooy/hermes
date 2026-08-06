"""Bridge runtime execution receipts back to the remote control plane."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(frozen=True, slots=True)
class CommandReceipt:
    """Durable result returned after a runtime command reaches execution."""

    command_id: str
    runtime_generation: str
    session_id: str
    state: str
    detail: str | None = None
    created_at: str = ""

    def __post_init__(self) -> None:
        if not self.created_at:
            object.__setattr__(
                self,
                "created_at",
                datetime.now(timezone.utc).isoformat(),
            )


class CommandReceiptBridge:
    """Collects execution receipts produced by RuntimeEventConsumer."""

    def __init__(self) -> None:
        self._receipts: dict[str, CommandReceipt] = {}

    def publish(self, receipt: CommandReceipt) -> None:
        self._receipts[receipt.command_id] = receipt

    def get(self, command_id: str) -> CommandReceipt | None:
        return self._receipts.get(command_id)

    def snapshot(self) -> list[dict[str, object]]:
        return [
            {
                "command_id": item.command_id,
                "runtime_generation": item.runtime_generation,
                "session_id": item.session_id,
                "state": item.state,
                "detail": item.detail,
                "created_at": item.created_at,
            }
            for item in self._receipts.values()
        ]
