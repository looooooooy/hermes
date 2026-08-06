"""Runtime action handlers for remote control commands.

These handlers intentionally operate on Runtime-owned session abstractions.
They do not call models directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


class SessionControlPort(Protocol):
    def interrupt(self) -> None: ...

    def resume(self) -> None: ...

    def approve(self, approval: dict[str, Any]) -> None: ...


@dataclass(frozen=True, slots=True)
class ActionResult:
    action: str
    state: str
    detail: str | None = None


class RuntimeActionHandlers:
    """Execute validated runtime actions against a bound session."""

    def interrupt(self, session: SessionControlPort) -> ActionResult:
        session.interrupt()
        return ActionResult("interrupt", "completed")

    def resume(self, session: SessionControlPort) -> ActionResult:
        session.resume()
        return ActionResult("resume", "completed")

    def approve(
        self,
        session: SessionControlPort,
        approval: dict[str, Any],
    ) -> ActionResult:
        session.approve(approval)
        return ActionResult("approve", "completed")
