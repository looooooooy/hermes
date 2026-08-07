"""Runtime descriptor verification for Hermes Connector.

The connector must verify the local Hermes runtime identity before exposing
itself as ready to Cloud.
"""

from __future__ import annotations

from dataclasses import dataclass

from .binding import RuntimeBinding, RuntimeBindingState
from .descriptor import RuntimeDescriptor


@dataclass(frozen=True, slots=True)
class RuntimeVerificationResult:
    verified: bool
    binding: RuntimeBinding
    reason: str


class RuntimeVerifier:
    """Verify that a discovered runtime matches connector expectations."""

    def verify(
        self,
        descriptor: RuntimeDescriptor,
        expected_generation: str | None = None,
        expected_profile: str | None = None,
    ) -> RuntimeVerificationResult:
        binding = RuntimeBinding.from_descriptor(descriptor)

        if expected_generation and descriptor.runtime_generation != expected_generation:
            return RuntimeVerificationResult(
                False,
                binding.with_state(RuntimeBindingState.STALE),
                "runtime_generation_mismatch",
            )

        if expected_profile and descriptor.profile != expected_profile:
            return RuntimeVerificationResult(
                False,
                binding.with_state(RuntimeBindingState.STALE),
                "profile_mismatch",
            )

        return RuntimeVerificationResult(
            True,
            binding.with_state(RuntimeBindingState.VERIFIED),
            "runtime_verified",
        )


__all__ = [
    "RuntimeVerificationResult",
    "RuntimeVerifier",
]
