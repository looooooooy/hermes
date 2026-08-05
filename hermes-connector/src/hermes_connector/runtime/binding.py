"""Runtime binding state machine for Connector."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .descriptor import RuntimeDescriptor


class RuntimeBindingState(str, Enum):
    UNKNOWN = "unknown"
    DISCOVERED = "discovered"
    VERIFIED = "verified"
    ACTIVE = "active"
    STALE = "stale"


@dataclass(slots=True)
class RuntimeBinding:
    descriptor: RuntimeDescriptor
    state: RuntimeBindingState = RuntimeBindingState.UNKNOWN

    def discover(self) -> None:
        self.state = RuntimeBindingState.DISCOVERED

    def verify(self, expected: RuntimeDescriptor) -> None:
        if self.descriptor.fingerprint() != expected.fingerprint():
            self.state = RuntimeBindingState.STALE
            raise ValueError("runtime descriptor mismatch")
        self.state = RuntimeBindingState.VERIFIED

    def activate(self) -> None:
        if self.state != RuntimeBindingState.VERIFIED:
            raise RuntimeError("runtime must be verified before activation")
        self.state = RuntimeBindingState.ACTIVE
