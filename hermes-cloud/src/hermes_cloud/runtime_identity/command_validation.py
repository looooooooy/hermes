"""Validation boundary for runtime commands."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RuntimeCommandValidationResult:
    accepted: bool
    reason: str


def validate_runtime_command(
    *,
    command_generation: str,
    current_generation: str,
) -> RuntimeCommandValidationResult:
    if command_generation != current_generation:
        return RuntimeCommandValidationResult(
            accepted=False,
            reason="runtime_generation_mismatch",
        )
    return RuntimeCommandValidationResult(
        accepted=True,
        reason="ok",
    )
