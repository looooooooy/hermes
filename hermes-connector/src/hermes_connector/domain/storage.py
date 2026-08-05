from __future__ import annotations

from dataclasses import dataclass


class StorageError(RuntimeError):
    code: int | None = None
    error_name = "storage_error"


class StorageFrameTooLarge(StorageError):
    code = 4302
    error_name = "frame_too_large"

    def __init__(self) -> None:
        super().__init__("durable payload exceeds 262144 bytes")


class StorageOverloaded(StorageError):
    code = 4305
    error_name = "overloaded"

    def __init__(self) -> None:
        super().__init__("storage write queue is full")


class StorageDeadlineExceeded(StorageError):
    code = 4306
    error_name = "deadline_exceeded_before_effect"

    def __init__(self) -> None:
        super().__init__("storage write deadline exceeded before effect")


class StorageEffectUnknown(StorageError):
    code = 4307
    error_name = "effect_unknown"

    def __init__(self) -> None:
        super().__init__("storage write deadline exceeded after execution started")


class IdempotencyConflict(StorageError):
    code = 4308
    error_name = "idempotency_conflict"

    def __init__(self) -> None:
        super().__init__("message id already exists with a different durable value")


class StorageSequenceConflict(StorageError):
    error_name = "sequence_conflict"

    def __init__(self) -> None:
        super().__init__("durable cloud sequence does not match the expected cursor")


class StorageFatalError(StorageError):
    pass


class StorageFull(StorageFatalError):
    error_name = "storage_full"

    def __init__(self) -> None:
        super().__init__("SQLite storage is full")


class StorageReadOnly(StorageFatalError):
    error_name = "storage_read_only"

    def __init__(self) -> None:
        super().__init__("SQLite storage is read-only")


class StorageCorrupt(StorageFatalError):
    error_name = "storage_corrupt"

    def __init__(self) -> None:
        super().__init__("SQLite storage is corrupt")


class StorageUnavailable(StorageFatalError):
    error_name = "storage_unavailable"

    def __init__(self) -> None:
        super().__init__("SQLite storage is unavailable")


class StorageStopped(StorageError):
    error_name = "storage_stopped"

    def __init__(self) -> None:
        super().__init__("storage is not accepting durable writes")


@dataclass(frozen=True, slots=True)
class InboxRecord:
    message_id: str
    digest: str
    state: str
    payload: bytes


@dataclass(frozen=True, slots=True)
class InboxPutResult:
    record: InboxRecord
    inserted: bool


@dataclass(frozen=True, slots=True)
class OutboxRecord:
    row_id: int
    message_id: str
    stream: str
    sequence: int
    state: str
    payload: bytes


@dataclass(frozen=True, slots=True)
class ObserverOutboxRecord:
    message_id: str
    payload_digest: str
    connector_sequence: int
    message_type: str
    profile: str
    session_key: str
    runtime_generation: str
    runtime_session_id: str
    event_sequence: int
    payload: bytes
    frame: bytes
    state: str
    transport_epoch_id: str | None = None


@dataclass(frozen=True, slots=True)
class SessionCatalogOutboxRecord:
    message_id: str
    payload_digest: str
    connector_sequence: int
    message_type: str
    profile: str
    runtime_generation: str
    snapshot_id: str | None
    catalog_revision: int | None
    page_index: int | None
    is_last: bool | None
    catalog_sequence: int | None
    payload: bytes
    frame: bytes
    state: str
    transport_epoch_id: str | None = None
    rejection_reason: str | None = None
    rejection_snapshot_id: str | None = None
    rejection_expected_page_index: int | None = None
    rejection_expected_catalog_sequence: int | None = None


@dataclass(frozen=True, slots=True)
class CommandRecord:
    command_id: str
    message_id: str
    digest: str
    state: str
    delivery_payload: bytes
    receipt_payload: bytes
    result_payload: bytes | None
    expires_at: str
    receipt_revision: int
    revision: int
    receipt_acknowledged: bool
    result_acknowledged: bool
    created_at: str
    updated_at: str
    completed_at: str | None


@dataclass(frozen=True, slots=True)
class CommandPutResult:
    record: CommandRecord
    inserted: bool


@dataclass(frozen=True, slots=True)
class CommandOutboxRecord:
    command_id: str
    message_type: str
    payload: bytes
    revision: int
    created_at: str = ""


@dataclass(frozen=True, slots=True)
class CloudSessionCheckpoint:
    previous_connection_id: str | None
    next_outbound_sequence: int
    next_inbound_sequence: int
    reconciliation_required: bool
    transport_epoch_id: str | None = None
    runtime_generation: str | None = None
    fresh_epoch_required: bool = True
    transport_recovery_floor: int = 0


@dataclass(frozen=True, slots=True)
class TransportFrameRecord:
    message_id: str
    epoch_id: str
    sequence: int
    message_type: str
    business_kind: str
    business_key: str
    business_revision: int
    runtime_generation: str | None
    frame: bytes
    state: str
    created_at: str
    updated_at: str
    settled_at: str | None


@dataclass(frozen=True, slots=True)
class OwnerControlRecord:
    request_id: str
    request_digest: str
    control_transport_id: str
    operation: str
    request_payload: bytes
    scope_payload: bytes
    response_payload: bytes | None
    state: str
    response_revision: int
    transport_received: bool
    created_at: str
    updated_at: str
    completed_at: str | None


@dataclass(frozen=True, slots=True)
class OwnerControlPutResult:
    record: OwnerControlRecord
    inserted: bool


@dataclass(frozen=True, slots=True)
class SQLiteDiagnostics:
    journal_mode: str
    foreign_keys: bool
    synchronous: int
    busy_timeout_ms: int
