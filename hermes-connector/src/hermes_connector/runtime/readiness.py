"""Connector runtime readiness state."""

from __future__ import annotations

from dataclasses import dataclass

from .binding import RuntimeBinding


@dataclass(frozen=True, slots=True)
class RuntimeReadiness:
    verified: bool
    active: bool
    runtime_generation: str

    @classmethod
    def from_binding(cls, binding: RuntimeBinding) -> "RuntimeReadiness":
        return cls(
            verified=binding.state.value in {"verified", "active"},
            active=binding.state.value == "active",
            runtime_generation=binding.runtime_generation,
        )
