"""Cloud-side projection of a verified Hermes runtime identity."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class RuntimeIdentityProjection:
    """Read model representing a connector-bound Hermes runtime."""

    connector_id: str
    runtime_id: str
    runtime_generation: str
    profile: str
    descriptor_hash: str
    extensions: tuple[str, ...] = field(default_factory=tuple)
    capabilities: tuple[str, ...] = field(default_factory=tuple)

    def matches_generation(self, generation: str) -> bool:
        return self.runtime_generation == generation

    def as_dict(self) -> dict[str, object]:
        return {
            "connector_id": self.connector_id,
            "runtime_id": self.runtime_id,
            "runtime_generation": self.runtime_generation,
            "profile": self.profile,
            "descriptor_hash": self.descriptor_hash,
            "extensions": list(self.extensions),
            "capabilities": list(self.capabilities),
        }
