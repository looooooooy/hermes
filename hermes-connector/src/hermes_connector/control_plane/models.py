"""Models for remote command delivery."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CommandEnvelope:
    command_id: str
    runtime_generation: str
    session_id: str
    action: str


@dataclass(frozen=True, slots=True)
class CommandResult:
    command_id: str
    state: str
    effect_state: str
