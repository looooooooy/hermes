"""Authoritative runtime identity descriptor."""

from __future__ import annotations

import os
import time
import uuid
from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class RuntimeDescriptor:
    """Identity of one running Hermes runtime instance.

    This object intentionally does not contain Agent internals.
    It is the binding contract used by extensions and connectors.
    """

    runtime_id: str
    runtime_generation: str
    profile: str
    process_id: int
    started_at: float
    host_bundle_id: str = "hermes"

    @classmethod
    def create(cls, profile: str = "default", host_bundle_id: str = "hermes") -> "RuntimeDescriptor":
        generation = f"runtime-{uuid.uuid4().hex}"
        return cls(
            runtime_id=f"rt-{uuid.uuid4().hex}",
            runtime_generation=generation,
            profile=profile,
            process_id=os.getpid(),
            started_at=time.time(),
            host_bundle_id=host_bundle_id,
        )

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)
