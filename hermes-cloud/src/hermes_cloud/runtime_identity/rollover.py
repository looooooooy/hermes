"""Runtime generation rollover handling."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RuntimeRolloverResult:
    old_generation: str
    new_generation: str
    invalidated: bool


def rollover_runtime(
    old_generation: str,
    new_generation: str,
) -> RuntimeRolloverResult:
    """Create the transition result for a runtime generation change."""
    return RuntimeRolloverResult(
        old_generation=old_generation,
        new_generation=new_generation,
        invalidated=old_generation != new_generation,
    )


__all__ = ["RuntimeRolloverResult", "rollover_runtime"]
