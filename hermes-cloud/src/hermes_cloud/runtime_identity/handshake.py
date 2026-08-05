"""Runtime identity handshake adapter.

Converts connector-provided runtime identity payloads into the cloud runtime
identity service input boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class RuntimeHandshake:
    connector_id: str
    runtime_id: str
    runtime_generation: str
    profile: str
    descriptor_hash: str

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "RuntimeHandshake":
        runtime = payload.get("runtime")
        if not isinstance(runtime, Mapping):
            raise ValueError("runtime identity payload is missing runtime")

        return cls(
            connector_id=_required_text(payload, "connector_id"),
            runtime_id=_required_text(runtime, "runtime_id"),
            runtime_generation=_required_text(runtime, "runtime_generation"),
            profile=_required_text(runtime, "profile"),
            descriptor_hash=_required_text(runtime, "descriptor_hash"),
        )

    def as_identity_payload(self) -> dict[str, str]:
        return {
            "connector_id": self.connector_id,
            "runtime_id": self.runtime_id,
            "runtime_generation": self.runtime_generation,
            "profile": self.profile,
            "descriptor_hash": self.descriptor_hash,
        }


def _required_text(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"missing runtime identity field: {key}")
    return value.strip()
