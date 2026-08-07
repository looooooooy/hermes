"""Transport adapter for runtime command execution receipts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class ReceiptEnvelope:
    command_id: str
    runtime_generation: str
    session_id: str
    state: str
    detail: str | None = None


class ReceiptPublisher(Protocol):
    def publish(self, payload: dict[str, Any]) -> None:
        ...


class CommandReceiptTransport:
    """Converts runtime receipts into connector transport payloads."""

    def __init__(self, publisher: ReceiptPublisher) -> None:
        self._publisher = publisher

    def send(self, receipt: ReceiptEnvelope) -> dict[str, Any]:
        payload = {
            "type": "runtime.command.receipt",
            "command_id": receipt.command_id,
            "runtime_generation": receipt.runtime_generation,
            "session_id": receipt.session_id,
            "state": receipt.state,
            "detail": receipt.detail,
        }

        self._publisher.publish(payload)
        return payload
