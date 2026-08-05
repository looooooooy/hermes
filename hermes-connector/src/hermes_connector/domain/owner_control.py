from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from uuid import UUID


class OwnerControlCallFailed(RuntimeError):
    def __init__(self, code: int, reason: str) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason


class OwnerControlOutcomeUnknown(RuntimeError):
    pass


def _empty_mapping() -> Mapping[str, object]:
    return MappingProxyType({})


@dataclass(frozen=True, slots=True)
class OwnerControlRequest:
    request_id: UUID
    control_transport_id: UUID
    operation: str
    issued_at: datetime
    expires_at: datetime
    body: Mapping[str, object]
    extensions: Mapping[str, object] = field(default_factory=_empty_mapping)


@dataclass(frozen=True, slots=True)
class OwnerControlResponse:
    request_id: UUID
    control_transport_id: UUID
    operation: str
    state: str
    completed_at: datetime
    result: Mapping[str, object] | None = None
    error: Mapping[str, object] | None = None
    extensions: Mapping[str, object] = field(default_factory=_empty_mapping)
