"""Authoritative runtime descriptor received from local Hermes runtime."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class RuntimeDescriptor:
    runtime_id: str
    runtime_generation: str
    profile: str
    extensions: tuple[str, ...] = field(default_factory=tuple)
    capabilities: tuple[str, ...] = field(default_factory=tuple)

    def fingerprint(self) -> str:
        payload = "|".join(
            [
                self.runtime_id,
                self.runtime_generation,
                self.profile,
                ",".join(self.extensions),
                ",".join(self.capabilities),
            ]
        )
        import hashlib

        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @classmethod
    def from_mapping(cls, value: dict[str, object]) -> "RuntimeDescriptor":
        return cls(
            runtime_id=str(value["runtime_id"]),
            runtime_generation=str(value["runtime_generation"]),
            profile=str(value["profile"]),
            extensions=tuple(value.get("extensions", ())),
            capabilities=tuple(value.get("capabilities", ())),
        )
