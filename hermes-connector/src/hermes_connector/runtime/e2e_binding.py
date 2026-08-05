"""Coordinates connector runtime identity verification flow."""

from __future__ import annotations

from dataclasses import dataclass

from .binding import RuntimeBinding, RuntimeBindingState
from .descriptor import RuntimeDescriptor
from .verification import RuntimeVerificationResult, RuntimeVerifier


@dataclass(frozen=True, slots=True)
class RuntimeBindingCoordinatorResult:
    binding: RuntimeBinding
    verification: RuntimeVerificationResult


class RuntimeBindingCoordinator:
    """Application boundary for discovery -> verify -> bind."""

    def __init__(self, verifier: RuntimeVerifier) -> None:
        self._verifier = verifier

    def bind(self, descriptor: RuntimeDescriptor) -> RuntimeBindingCoordinatorResult:
        verification = self._verifier.verify(descriptor)
        state = (
            RuntimeBindingState.VERIFIED
            if verification.verified
            else RuntimeBindingState.STALE
        )
        return RuntimeBindingCoordinatorResult(
            binding=RuntimeBinding(
                descriptor=descriptor,
                state=state,
            ),
            verification=verification,
        )
