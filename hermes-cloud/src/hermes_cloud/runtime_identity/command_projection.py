"""Projection boundary for runtime remote commands."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RuntimeCommandProjection:
    command_id: str
    runtime_id: str
    runtime_generation: str
    session_id: str
    action: str
    state: str = "received"


class RuntimeCommandProjectionService:
    def project(self, command: RuntimeCommandProjection) -> RuntimeCommandProjection:
        return command
