"""Cloud-side projection of a verified Hermes runtime identity."""

from __future__ import annotations

from collections.abc import Mapping
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

    @classmethod
    def from_mapping(
        cls,
        payload: Mapping[str, object],
    ) -> RuntimeIdentityProjection:
        return cls(
            connector_id=_required_text(payload, "connector_id"),
            runtime_id=_required_text(payload, "runtime_id"),
            runtime_generation=_required_text(payload, "runtime_generation"),
            profile=_required_text(payload, "profile"),
            descriptor_hash=_required_text(payload, "descriptor_hash"),
            extensions=_canonical_text_tuple(payload.get("extensions", ()), "extensions"),
            capabilities=_canonical_text_tuple(
                payload.get("capabilities", ()),
                "capabilities",
            ),
        )

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

def _required_text(payload: Mapping[str, object], field_name: str) -> str:
    value = payload.get(field_name)
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be text")
    canonical = value.strip()
    if not canonical or canonical != value:
        raise ValueError(f"{field_name} must be canonical text")
    return canonical


def _canonical_text_tuple(value: object, field_name: str) -> tuple[str, ...]:
    if isinstance(value, str):
        raise TypeError(f"{field_name} must be a collection")
    try:
        items = tuple(value)
    except TypeError as error:
        raise TypeError(f"{field_name} must be a collection") from error
    if any(
        not isinstance(item, str) or not item or item != item.strip()
        for item in items
    ):
        raise ValueError(f"{field_name} must contain canonical text")
    if len(items) != len(set(items)):
        raise ValueError(f"{field_name} must be unique")
    return items
