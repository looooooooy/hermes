"""Runtime identity fields exchanged during connector handshake."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .descriptor import RuntimeDescriptor


@dataclass(frozen=True, slots=True)
class RuntimeHandshakePayload:
    runtime_id: str
    runtime_generation: str
    profile: str
    descriptor_hash: str

    @classmethod
    def from_descriptor(cls, descriptor: RuntimeDescriptor) -> "RuntimeHandshakePayload":
        return cls(
            runtime_id=descriptor.runtime_id,
            runtime_generation=descriptor.runtime_generation,
            profile=descriptor.profile,
            descriptor_hash=descriptor.fingerprint(),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "runtime_id": self.runtime_id,
            "runtime_generation": self.runtime_generation,
            "profile": self.profile,
            "descriptor_hash": self.descriptor_hash,
        }
