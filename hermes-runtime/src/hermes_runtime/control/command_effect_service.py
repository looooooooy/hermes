"""Runtime command effect service boundary.

This module keeps command execution separated from transport and routing.
Remote commands become runtime effects only after validation and session binding.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .effect_receipt import EffectReceipt


@dataclass(frozen=True, slots=True)
class RuntimeEffectRequest:
    command_id: str
    runtime_generation: str
    session_id: str
    action: str


class CommandEffectService:
    """Coordinates execution of validated runtime commands.

    It deliberately does not call an Agent directly. The injected handler is
    owned by Runtime Authority and can enqueue an internal runtime event.
    """

    def __init__(self, handler: Callable[[RuntimeEffectRequest], str]) -> None:
        self._handler = handler

    def execute(self, request: RuntimeEffectRequest) -> EffectReceipt:
        try:
            detail = self._handler(request)
            return EffectReceipt(
                command_id=request.command_id,
                runtime_generation=request.runtime_generation,
                session_id=request.session_id,
                state="completed",
                detail=detail,
            )
        except Exception as error:  # noqa: BLE001
            return EffectReceipt(
                command_id=request.command_id,
                runtime_generation=request.runtime_generation,
                session_id=request.session_id,
                state="failed",
                detail=str(error),
            )
