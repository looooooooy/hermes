"""Connector startup gate for verified Hermes runtime binding."""

from __future__ import annotations

from dataclasses import dataclass

from .binding import RuntimeBinding
from .descriptor import RuntimeDescriptor
from .verification import RuntimeVerifier


@dataclass(frozen=True, slots=True)
class RuntimeStartupResult:
    descriptor: RuntimeDescriptor
    binding: RuntimeBinding


class RuntimeStartupGate:
    """Require a verified local runtime before cloud registration."""

    def __init__(self, verifier: RuntimeVerifier) -> None:
        self._verifier = verifier

    def prepare(self, descriptor: RuntimeDescriptor) -> RuntimeStartupResult:
        result = self._verifier.verify(descriptor)
        if not result.verified:
            raise RuntimeError("local runtime verification failed")

        binding = RuntimeBinding.discovered(descriptor)
        binding.verify()
        binding.activate()

        return RuntimeStartupResult(
            descriptor=descriptor,
            binding=binding,
        )
