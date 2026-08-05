from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from uuid import UUID


def _empty_mapping() -> Mapping[str, object]:
    return MappingProxyType({})


@dataclass(frozen=True, slots=True)
class LocalHello:
    contract_version: int
    message_type: str
    client_instance_id: UUID
    profile: str
    required_capabilities: tuple[str, ...]
    optional_capabilities: tuple[str, ...]
    extensions: Mapping[str, object] = field(default_factory=_empty_mapping)


@dataclass(frozen=True, slots=True)
class LocalWelcome:
    contract_version: int
    message_type: str
    runtime_generation: str
    profile: str
    accepted_capabilities: tuple[str, ...]
    unavailable_optional_capabilities: tuple[str, ...]
    extensions: Mapping[str, object] = field(default_factory=_empty_mapping)


@dataclass(frozen=True, slots=True)
class LocalGatewayErrorResponse:
    code: int
    reason: str


@dataclass(frozen=True, slots=True)
class CloudEnvelope:
    contract_version: int
    message_id: UUID
    message_type: str
    tenant_id: str
    device_id: str
    sequence: int
    sent_at: datetime
    payload: Mapping[str, object]
    traceparent: str | None = None
    idempotency_key: str | None = None
    extensions: Mapping[str, object] = field(default_factory=_empty_mapping)
