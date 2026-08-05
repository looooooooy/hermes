from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID


class SessionCatalogResnapshotRequired(RuntimeError):
    """The active local/Cloud catalog snapshot must be replaced in full."""


@dataclass(frozen=True, slots=True)
class SessionCatalogEntry:
    session_key: str
    surface: str
    authority_revision: int
    available_actions: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SessionCatalogSnapshotPage:
    profile: str
    runtime_generation: str
    snapshot_id: UUID
    catalog_revision: int
    page_index: int
    is_last: bool
    sessions: tuple[SessionCatalogEntry, ...]


@dataclass(frozen=True, slots=True)
class LocalSessionCatalogPage:
    subscription_id: UUID
    snapshot_id: UUID
    profile: str
    runtime_generation: str
    catalog_revision: int
    page_index: int
    is_last: bool
    sessions: tuple[SessionCatalogEntry, ...]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class SessionCatalogEvent:
    profile: str
    runtime_generation: str
    catalog_sequence: int
    action: str
    entry: SessionCatalogEntry


@dataclass(frozen=True, slots=True)
class SessionCatalogAck:
    profile: str
    runtime_generation: str
    acked_message_id: UUID
    acked_payload_digest: str
    acked_connector_sequence: int
    ack_kind: str
    snapshot_id: UUID | None
    catalog_revision: int | None
    page_index: int | None
    is_last: bool | None
    catalog_sequence: int | None


@dataclass(frozen=True, slots=True)
class SessionCatalogNack:
    profile: str
    runtime_generation: str
    rejected_message_id: UUID
    rejected_payload_digest: str
    rejected_connector_sequence: int
    reason: str
    snapshot_id: UUID | None
    expected_page_index: int | None
    expected_catalog_sequence: int | None


__all__ = [
    "LocalSessionCatalogPage",
    "SessionCatalogAck",
    "SessionCatalogEntry",
    "SessionCatalogEvent",
    "SessionCatalogNack",
    "SessionCatalogResnapshotRequired",
    "SessionCatalogSnapshotPage",
]
