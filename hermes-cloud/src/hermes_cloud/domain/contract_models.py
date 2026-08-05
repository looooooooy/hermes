"""Platform-neutral models decoded from the core contract."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CloudEnvelope:
    contract_version: int
    message_id: str
    message_type: str
    tenant_id: str
    device_id: str
    sequence: int
    sent_at: str
    payload: dict[str, Any]
    traceparent: str | None = None
    idempotency_key: str | None = None
    extensions: dict[str, dict[str, Any]] | None = None
