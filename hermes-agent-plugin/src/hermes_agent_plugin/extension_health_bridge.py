"""Runtime extension health bridge.

Provides a small boundary object used by host integrations to expose
runtime extension state without leaking Hermes internals into plugins.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from time import time
from typing import Any


@dataclass(slots=True)
class ExtensionHealthBridge:
    """Collects extension readiness facts for runtime projection."""

    runtime_id: str
    runtime_generation: str
    profile: str
    _extensions: dict[str, dict[str, Any]] = field(default_factory=dict)

    def register_extension(
        self,
        name: str,
        version: str,
        capabilities: list[str] | None = None,
    ) -> None:
        self._extensions[name] = {
            "name": name,
            "version": version,
            "state": "registered",
            "capabilities": sorted(capabilities or []),
            "updated_at": time(),
        }

    def mark_ready(self, name: str) -> None:
        extension = self._extensions[name]
        extension["state"] = "ready"
        extension["updated_at"] = time()

    def mark_failed(self, name: str, reason: str) -> None:
        extension = self._extensions[name]
        extension["state"] = "failed"
        extension["error"] = reason
        extension["updated_at"] = time()

    def snapshot(self) -> dict[str, Any]:
        return {
            "runtime": {
                "runtime_id": self.runtime_id,
                "runtime_generation": self.runtime_generation,
                "profile": self.profile,
            },
            "extensions": list(self._extensions.values()),
            "ready": all(
                item["state"] == "ready"
                for item in self._extensions.values()
            ) if self._extensions else False,
        }


__all__ = ["ExtensionHealthBridge"]
