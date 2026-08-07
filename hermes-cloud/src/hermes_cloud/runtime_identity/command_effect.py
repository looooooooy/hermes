"""Command effect tracking boundary for runtime control."""

from dataclasses import dataclass


@dataclass(frozen=True)
class CommandEffectReceipt:
    command_id: str
    runtime_id: str
    state: str
    effect_state: str


class CommandEffectTracker:
    def create_receipt(
        self,
        command_id: str,
        runtime_id: str,
        state: str,
        effect_state: str,
    ) -> CommandEffectReceipt:
        return CommandEffectReceipt(
            command_id=command_id,
            runtime_id=runtime_id,
            state=state,
            effect_state=effect_state,
        )
