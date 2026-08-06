"""Cloud runtime command routing boundary."""

from __future__ import annotations

from dataclasses import dataclass

from .command_validation import validate_runtime_command


@dataclass(slots=True)
class RuntimeCommandRouter:
    validator: object
    projection: object

    def route(self, command: object) -> object:
        validate_runtime_command(command)
        return self.projection.record(command)


__all__ = ["RuntimeCommandRouter"]
