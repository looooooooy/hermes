"""In-memory runtime identity registry boundary.

Keeps runtime identity separate from connector/device identity.
"""

from __future__ import annotations

from dataclasses import dataclass

from .projection import RuntimeIdentityProjection


@dataclass(frozen=True, slots=True)
class RuntimeIdentityUpdateResult:
    created: bool
    changed: bool
    projection: RuntimeIdentityProjection


class RuntimeIdentityRegistry:
    """Authoritative runtime identity projection store boundary."""

    def __init__(self) -> None:
        self._items: dict[str, RuntimeIdentityProjection] = {}

    def upsert(self, projection: RuntimeIdentityProjection) -> RuntimeIdentityUpdateResult:
        current = self._items.get(projection.runtime_id)
        if current is None:
            self._items[projection.runtime_id] = projection
            return RuntimeIdentityUpdateResult(True, True, projection)
        changed = current != projection
        if changed:
            self._items[projection.runtime_id] = projection
        return RuntimeIdentityUpdateResult(False, changed, projection)

    def get(self, runtime_id: str) -> RuntimeIdentityProjection | None:
        return self._items.get(runtime_id)
