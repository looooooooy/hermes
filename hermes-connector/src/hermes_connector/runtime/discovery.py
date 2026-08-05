"""Local Hermes runtime descriptor discovery."""

from __future__ import annotations

from pathlib import Path

from .descriptor import RuntimeDescriptor


class RuntimeDescriptorDiscovery:
    """Read a runtime descriptor published by the Hermes runtime host."""

    def __init__(self, manifest_path: str | Path) -> None:
        self._manifest_path = Path(manifest_path)

    def discover(self) -> RuntimeDescriptor:
        if not self._manifest_path.exists():
            raise RuntimeError("runtime_descriptor_not_found")

        return RuntimeDescriptor.from_json(
            self._manifest_path.read_text(encoding="utf-8")
        )


__all__ = [
    "RuntimeDescriptorDiscovery",
]
