from __future__ import annotations

import asyncio
import os
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum, auto
from functools import partial
from pathlib import Path
from typing import Any

from sqlalchemy import URL, Engine, create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from hermes_connector.adapters.persistence.sqlite.repositories import (
    cloud_session as cloud_session_repository,
)
from hermes_connector.adapters.persistence.sqlite.repositories import (
    control_command as control_command_repository,
)
from hermes_connector.adapters.persistence.sqlite.repositories import (
    observer_outbox as observer_outbox_repository,
)
from hermes_connector.adapters.persistence.sqlite.repositories import (
    owner_control as owner_control_repository,
)
from hermes_connector.adapters.persistence.sqlite.repositories import (
    session_catalog_outbox as session_catalog_outbox_repository,
)
from hermes_connector.adapters.persistence.sqlite.repositories import (
    transport_journal as transport_journal_repository,
)
from hermes_connector.adapters.sqlite_migrations import (
    MigrationError,
    apply_migrations,
)
from hermes_connector.adapters.sqlite_models import (
    MAX_DURABLE_PAYLOAD_BYTES,
    InboxMessage,
    OutboxMessage,
    StreamCursor,
)
from hermes_connector.adapters.sqlite_policy import (
    SQLITE_FAILURES,
    SQLiteConnectionPolicy,
    map_sqlite_error,
)
from hermes_connector.domain.identifiers import canonical_uuid
from hermes_connector.domain.observer import StreamAck, StreamNack
from hermes_connector.domain.session_catalog import (
    SessionCatalogAck,
    SessionCatalogNack,
)
from hermes_connector.domain.storage import (
    CloudSessionCheckpoint,
    CommandOutboxRecord,
    CommandPutResult,
    CommandRecord,
    IdempotencyConflict,
    InboxPutResult,
    InboxRecord,
    ObserverOutboxRecord,
    OutboxRecord,
    OwnerControlPutResult,
    OwnerControlRecord,
    SessionCatalogOutboxRecord,
    SQLiteDiagnostics,
    StorageDeadlineExceeded,
    StorageEffectUnknown,
    StorageError,
    StorageFatalError,
    StorageFrameTooLarge,
    StorageOverloaded,
    StorageSequenceConflict,
    StorageStopped,
    TransportFrameRecord,
)
from hermes_connector.ports.configuration import StorageConfigPort

WriteFault = Callable[[str], None]
_STOP = object()
_ABANDONED = object()
_TRANSPORT_MESSAGE_TYPES = frozenset(
    {
        "connector.heartbeat",
        "command.receipt",
        "command.result",
        "control.response",
        "session.snapshot",
        "session.event",
        "session.snapshot.v2",
        "session.event.v2",
        "session.catalog.snapshot.page",
        "session.catalog.event",
    }
)
_TRANSPORT_BUSINESS_KINDS = frozenset(
    {
        "heartbeat",
        "command.receipt",
        "command.result",
        "control.response",
        "observer",
        "session_catalog",
    }
)
_TRANSPORT_BUSINESS_PAIRS = {
    "connector.heartbeat": "heartbeat",
    "command.receipt": "command.receipt",
    "command.result": "command.result",
    "control.response": "control.response",
    "session.snapshot": "observer",
    "session.event": "observer",
    "session.snapshot.v2": "observer",
    "session.event.v2": "observer",
    "session.catalog.snapshot.page": "session_catalog",
    "session.catalog.event": "session_catalog",
}
_OWNER_CONTROL_OPERATIONS = frozenset(
    {
        "control.transport.open",
        "session.control.acquire",
        "session.control.renew",
        "session.control.release",
        "session.control.status",
        "control.transport.close",
    }
)
_OWNER_CONTROL_STATES = frozenset(
    {"received", "executing", "completed", "effect_unknown"}
)


class _WritePhase(Enum):
    QUEUED = auto()
    STARTED = auto()
    ABANDONED = auto()


@dataclass(slots=True)
class _WriteRequest:
    operation: str
    arguments: tuple[object, ...]
    result: asyncio.Future[object]
    phase: _WritePhase = _WritePhase.QUEUED
    guard: threading.Lock = field(default_factory=threading.Lock)

    def begin(self) -> bool:
        with self.guard:
            if self.phase is _WritePhase.ABANDONED:
                return False
            self.phase = _WritePhase.STARTED
            return True

    def abandon_before_start(self) -> bool:
        with self.guard:
            if self.phase is _WritePhase.STARTED:
                return False
            self.phase = _WritePhase.ABANDONED
            return True


class SQLiteStorageComponent:
    """Bounded single-writer ORM component.

    Lifecycle:

        NEW -> STARTING -> ACCEPTING -> DRAINING -> STOPPING -> STOPPED
                    |          |
                    +--------> FATAL ------------------------> STOPPED

    The asyncio task owns queue order. Every database operation is dispatched
    to one dedicated worker thread and creates/closes its own ORM Session there;
    no Session crosses an asyncio task or thread boundary.
    """

    name = "sqlite_storage"

    def __init__(
        self,
        path: str | os.PathLike[str],
        config: StorageConfigPort,
        *,
        write_fault: WriteFault | None = None,
    ) -> None:
        self._path = Path(path)
        self._config = config
        self._write_fault = write_fault
        self._queue: asyncio.Queue[_WriteRequest | object] = asyncio.Queue(
            maxsize=config.bounded_queue_items
        )
        self._policy = SQLiteConnectionPolicy(
            busy_timeout_ms=config.storage_busy_timeout_ms
        )
        self._executor: ThreadPoolExecutor | None = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="hermes-sqlite",
        )
        self._engine: Engine | None = None
        self._session_factory: sessionmaker[Session] | None = None
        self._started = False
        self._accepting = False
        self._fatal_error_type: type[StorageFatalError] | None = None
        self._run_started = asyncio.Event()
        self._run_stopped = asyncio.Event()

    @property
    def queued_write_count(self) -> int:
        return self._queue.qsize()

    async def start(self) -> None:
        if self._started:
            raise RuntimeError("SQLite storage can only be started once")
        self._started = True
        try:
            await self._run_blocking(self._open)
        except asyncio.CancelledError:
            await asyncio.shield(self._shutdown_resources())
            raise
        except MigrationError:
            await self._shutdown_resources()
            raise
        except StorageFatalError:
            await self._shutdown_resources()
            raise
        except (RuntimeError, TypeError, ValueError):
            await self._shutdown_resources()
            raise
        except SQLITE_FAILURES as error:
            mapped = map_sqlite_error(error)
            self._fatal_error_type = type(mapped)
            await self._shutdown_resources()
            raise mapped from None
        self._accepting = True

    async def ready(self) -> bool:
        await self._run_started.wait()
        return (
            self._engine is not None
            and self._accepting
            and self._fatal_error_type is None
        )

    async def run(self) -> None:
        if self._engine is None:
            raise StorageStopped()
        self._run_started.set()
        try:
            while True:
                item = await self._queue.get()
                try:
                    if item is _STOP:
                        return
                    request = item
                    if not isinstance(request, _WriteRequest):
                        raise TypeError("invalid storage queue item")
                    if request.result.cancelled():
                        continue
                    try:
                        result = await self._run_blocking(
                            self._execute_request,
                            request,
                        )
                    except asyncio.CancelledError:
                        if not request.result.done():
                            request.result.set_exception(StorageStopped())
                        raise
                    except IdempotencyConflict:
                        if not request.result.done():
                            request.result.set_exception(IdempotencyConflict())
                        continue
                    except StorageSequenceConflict:
                        if not request.result.done():
                            request.result.set_exception(StorageSequenceConflict())
                        continue
                    except StorageOverloaded:
                        if not request.result.done():
                            request.result.set_exception(StorageOverloaded())
                        continue
                    except (TypeError, ValueError) as error:
                        if not request.result.done():
                            request.result.set_exception(error)
                        continue
                    except StorageFatalError as error:
                        mapped = error
                        self._mark_fatal(type(mapped))
                        if not request.result.done():
                            request.result.set_exception(type(mapped)())
                        self._fail_pending(type(mapped))
                        raise mapped from None
                    except SQLITE_FAILURES as error:
                        mapped = map_sqlite_error(error)
                        self._mark_fatal(type(mapped))
                        if not request.result.done():
                            request.result.set_exception(type(mapped)())
                        self._fail_pending(type(mapped))
                        raise mapped from None
                    if result is not _ABANDONED and not request.result.done():
                        request.result.set_result(result)
                finally:
                    self._queue.task_done()
        finally:
            self._accepting = False
            self._fail_pending(StorageStopped)
            await self._shutdown_resources()
            self._run_stopped.set()

    async def drain(self) -> None:
        self._accepting = False
        await self._queue.join()

    async def stop(self) -> None:
        self._accepting = False
        if self._run_started.is_set() and not self._run_stopped.is_set():
            await self._queue.join()
            self._queue.put_nowait(_STOP)
            await self._run_stopped.wait()
        else:
            await self._shutdown_resources()

    async def diagnostics(self) -> SQLiteDiagnostics:
        return await self._read(self._diagnostics)

    async def put_inbox(
        self,
        *,
        message_id: str,
        digest: str,
        payload: bytes,
        state: str = "received",
    ) -> InboxPutResult:
        _validate_payload(payload)
        return await self._submit(
            "put_inbox",
            message_id,
            digest,
            payload,
            state,
        )

    async def get_inbox(self, message_id: str) -> InboxRecord | None:
        return await self._read(self._get_inbox, message_id)

    async def append_outbox(
        self,
        *,
        message_id: str,
        stream: str,
        sequence: int,
        payload: bytes,
    ) -> OutboxRecord:
        _validate_payload(payload)
        if type(sequence) is not int or sequence < 0:
            raise ValueError("outbox sequence must be a non-negative integer")
        return await self._submit(
            "append_outbox",
            message_id,
            stream,
            sequence,
            payload,
        )

    async def pending_outbox(
        self,
        *,
        limit: int,
        after_sequence: int | None = None,
        stream: str | None = None,
        include_settled: bool = False,
    ) -> tuple[OutboxRecord, ...]:
        if type(limit) is not int or not 1 <= limit <= self._config.bounded_queue_items:
            raise ValueError("outbox read limit is outside configured bounds")
        if after_sequence is not None and (
            type(after_sequence) is not int or after_sequence < 0
        ):
            raise ValueError("outbox page sequence must be a non-negative integer")
        return await self._read(
            self._pending_outbox,
            limit,
            after_sequence,
            stream,
            include_settled,
        )

    async def ack_outbox(self, message_id: str) -> bool:
        return await self._submit("ack_outbox", message_id)

    async def advance_cursor(self, stream: str, sequence: int) -> int:
        if type(sequence) is not int or sequence < 0:
            raise ValueError("cursor sequence must be a non-negative integer")
        return await self._submit("advance_cursor", stream, sequence)

    async def get_cursor(self, stream: str) -> int | None:
        return await self._read(self._get_cursor, stream)

    async def append_observer_outbox(
        self,
        *,
        message_id: str,
        connector_sequence: int,
        transport_epoch_id: str | None = None,
        message_type: str,
        profile: str,
        session_key: str,
        runtime_generation: str,
        runtime_session_id: str,
        event_sequence: int,
        payload: bytes,
        frame: bytes,
    ) -> ObserverOutboxRecord:
        _validate_uuid_text(message_id, "Observer message id")
        if transport_epoch_id is not None:
            _validate_uuid_text(transport_epoch_id, "transport epoch id")
        _validate_text(message_type, "Observer message type", maximum=64)
        _validate_text(profile, "Observer profile", maximum=128)
        _validate_text(session_key, "Observer session key", maximum=256)
        _validate_text(runtime_generation, "runtime generation", maximum=128)
        _validate_text(
            runtime_session_id,
            "Observer runtime session id",
            maximum=256,
        )
        _validate_payload(payload)
        _validate_payload(frame)
        _validate_sequence(connector_sequence)
        _validate_sequence(event_sequence)
        if message_type not in {
            "session.snapshot",
            "session.event",
            "session.snapshot.v2",
            "session.event.v2",
        }:
            raise ValueError("Observer outbox message type is invalid")
        return await self._submit(
            "append_observer_outbox",
            message_id,
            connector_sequence,
            transport_epoch_id,
            message_type,
            profile,
            session_key,
            runtime_generation,
            runtime_session_id,
            event_sequence,
            payload,
            frame,
        )

    async def get_observer_outbox(
        self,
        message_id: str,
    ) -> ObserverOutboxRecord | None:
        _validate_uuid_text(message_id, "Observer message id")
        return await self._read(self._get_observer_outbox, message_id)

    async def get_observer_fact(
        self,
        *,
        transport_epoch_id: str | None = None,
        message_type: str,
        profile: str,
        session_key: str,
        runtime_generation: str,
        runtime_session_id: str,
        event_sequence: int,
    ) -> ObserverOutboxRecord | None:
        if transport_epoch_id is not None:
            _validate_uuid_text(transport_epoch_id, "transport epoch id")
        _validate_text(message_type, "Observer message type", maximum=64)
        if message_type not in {
            "session.snapshot",
            "session.event",
            "session.snapshot.v2",
            "session.event.v2",
        }:
            raise ValueError("Observer outbox message type is invalid")
        _validate_text(profile, "Observer profile", maximum=128)
        _validate_text(session_key, "Observer session key", maximum=256)
        _validate_text(runtime_generation, "runtime generation", maximum=128)
        _validate_text(
            runtime_session_id,
            "Observer runtime session id",
            maximum=256,
        )
        _validate_sequence(event_sequence)
        return await self._read(
            self._get_observer_fact,
            transport_epoch_id,
            message_type,
            profile,
            session_key,
            runtime_generation,
            runtime_session_id,
            event_sequence,
        )

    async def pending_observer_outbox(
        self,
        *,
        limit: int,
        after_sequence: int | None = None,
        include_settled: bool = False,
    ) -> tuple[ObserverOutboxRecord, ...]:
        _validate_command_limit(limit, self._config.bounded_queue_items)
        if after_sequence is not None:
            _validate_sequence(after_sequence)
        return await self._read(
            self._pending_observer_outbox,
            limit,
            after_sequence,
            include_settled,
        )

    async def ack_observer_outbox(self, ack: StreamAck) -> ObserverOutboxRecord:
        if type(ack) is not StreamAck:
            raise TypeError("Observer acknowledgement must be StreamAck")
        return await self._submit("ack_observer_outbox", ack)

    async def nack_observer_outbox(
        self,
        nack: StreamNack,
    ) -> ObserverOutboxRecord:
        if type(nack) is not StreamNack:
            raise TypeError("Observer rejection must be StreamNack")
        return await self._submit("nack_observer_outbox", nack)

    async def append_session_catalog_outbox(
        self,
        *,
        message_id: str,
        connector_sequence: int,
        transport_epoch_id: str | None = None,
        message_type: str,
        profile: str,
        runtime_generation: str,
        snapshot_id: str | None,
        catalog_revision: int | None,
        page_index: int | None,
        is_last: bool | None,
        catalog_sequence: int | None,
        payload: bytes,
        frame: bytes,
    ) -> SessionCatalogOutboxRecord:
        _validate_uuid_text(message_id, "session catalog message id")
        if transport_epoch_id is not None:
            _validate_uuid_text(transport_epoch_id, "transport epoch id")
        if snapshot_id is not None:
            _validate_uuid_text(snapshot_id, "session catalog snapshot id")
        _validate_sequence(connector_sequence)
        _validate_text(profile, "session catalog profile", maximum=128)
        _validate_text(runtime_generation, "runtime generation", maximum=128)
        _validate_payload(payload)
        _validate_payload(frame)
        if message_type == "session.catalog.snapshot.page":
            if (
                snapshot_id is None
                or catalog_revision is None
                or page_index is None
                or type(is_last) is not bool
                or catalog_sequence is not None
            ):
                raise ValueError("session catalog snapshot position is invalid")
            _validate_sequence(catalog_revision)
            _validate_sequence(page_index)
        elif message_type == "session.catalog.event":
            if (
                snapshot_id is not None
                or catalog_revision is not None
                or page_index is not None
                or is_last is not None
                or catalog_sequence is None
            ):
                raise ValueError("session catalog event position is invalid")
            _validate_sequence(catalog_sequence)
        else:
            raise ValueError("session catalog outbox message type is invalid")
        return await self._submit(
            "append_session_catalog_outbox",
            message_id,
            connector_sequence,
            transport_epoch_id,
            message_type,
            profile,
            runtime_generation,
            snapshot_id,
            catalog_revision,
            page_index,
            is_last,
            catalog_sequence,
            payload,
            frame,
        )

    async def get_session_catalog_outbox(
        self,
        message_id: str,
    ) -> SessionCatalogOutboxRecord | None:
        _validate_uuid_text(message_id, "session catalog message id")
        return await self._read(self._get_session_catalog_outbox, message_id)

    async def get_session_catalog_fact(
        self,
        *,
        transport_epoch_id: str | None,
        message_type: str,
        profile: str,
        runtime_generation: str,
        snapshot_id: str | None,
        catalog_revision: int | None,
        page_index: int | None,
        catalog_sequence: int | None,
    ) -> SessionCatalogOutboxRecord | None:
        if transport_epoch_id is not None:
            _validate_uuid_text(transport_epoch_id, "transport epoch id")
        if message_type not in {
            "session.catalog.snapshot.page",
            "session.catalog.event",
        }:
            raise ValueError("session catalog outbox message type is invalid")
        _validate_text(profile, "session catalog profile", maximum=128)
        _validate_text(runtime_generation, "runtime generation", maximum=128)
        if snapshot_id is not None:
            _validate_uuid_text(snapshot_id, "session catalog snapshot id")
        for value in (catalog_revision, page_index, catalog_sequence):
            if value is not None:
                _validate_sequence(value)
        return await self._read(
            self._get_session_catalog_fact,
            transport_epoch_id,
            message_type,
            profile,
            runtime_generation,
            snapshot_id,
            catalog_revision,
            page_index,
            catalog_sequence,
        )

    async def pending_session_catalog_outbox(
        self,
        *,
        limit: int,
        after_sequence: int | None = None,
        include_settled: bool = False,
    ) -> tuple[SessionCatalogOutboxRecord, ...]:
        _validate_command_limit(limit, self._config.bounded_queue_items)
        if after_sequence is not None:
            _validate_sequence(after_sequence)
        return await self._read(
            self._pending_session_catalog_outbox,
            limit,
            after_sequence,
            include_settled,
        )

    async def retire_session_catalog_outbox(self) -> None:
        await self._submit("retire_session_catalog_outbox")

    async def ack_session_catalog_outbox(
        self,
        *,
        profile: str,
        runtime_generation: str,
        acked_message_id: str,
        acked_payload_digest: str,
        acked_connector_sequence: int,
        ack_kind: str,
        snapshot_id: str | None,
        catalog_revision: int | None,
        page_index: int | None,
        is_last: bool | None,
        catalog_sequence: int | None,
    ) -> SessionCatalogOutboxRecord:
        _validate_text(profile, "session catalog profile", maximum=128)
        _validate_text(runtime_generation, "runtime generation", maximum=128)
        _validate_uuid_text(acked_message_id, "session catalog ACK message id")
        _validate_hex_digest(acked_payload_digest, "session catalog ACK digest")
        _validate_sequence(acked_connector_sequence)
        if snapshot_id is not None:
            _validate_uuid_text(snapshot_id, "session catalog snapshot id")
        if ack_kind == "snapshot_committed":
            if (
                snapshot_id is None
                or catalog_revision is None
                or page_index is None
                or type(is_last) is not bool
                or catalog_sequence is not None
            ):
                raise ValueError("session catalog snapshot ACK position is invalid")
            _validate_sequence(catalog_revision)
            _validate_sequence(page_index)
        elif ack_kind == "event_applied":
            if (
                snapshot_id is not None
                or catalog_revision is not None
                or page_index is not None
                or is_last is not None
                or catalog_sequence is None
            ):
                raise ValueError("session catalog event ACK position is invalid")
            _validate_sequence(catalog_sequence)
        else:
            raise ValueError("session catalog ACK kind is invalid")
        ack = SessionCatalogAck(
            profile=profile,
            runtime_generation=runtime_generation,
            acked_message_id=canonical_uuid(acked_message_id),
            acked_payload_digest=acked_payload_digest,
            acked_connector_sequence=acked_connector_sequence,
            ack_kind=ack_kind,
            snapshot_id=canonical_uuid(snapshot_id) if snapshot_id else None,
            catalog_revision=catalog_revision,
            page_index=page_index,
            is_last=is_last,
            catalog_sequence=catalog_sequence,
        )
        return await self._submit("ack_session_catalog_outbox", ack)

    async def nack_session_catalog_outbox(
        self,
        *,
        profile: str,
        runtime_generation: str,
        rejected_message_id: str,
        rejected_payload_digest: str,
        rejected_connector_sequence: int,
        reason: str,
        snapshot_id: str | None,
        expected_page_index: int | None,
        expected_catalog_sequence: int | None,
    ) -> SessionCatalogOutboxRecord:
        _validate_text(profile, "session catalog profile", maximum=128)
        _validate_text(runtime_generation, "runtime generation", maximum=128)
        _validate_uuid_text(rejected_message_id, "session catalog NACK message id")
        _validate_hex_digest(rejected_payload_digest, "session catalog NACK digest")
        _validate_sequence(rejected_connector_sequence)
        if snapshot_id is not None:
            _validate_uuid_text(snapshot_id, "session catalog snapshot id")
        if reason in {"page_gap", "revision_conflict"}:
            if (
                snapshot_id is None
                or expected_page_index is None
                or expected_catalog_sequence is not None
            ):
                raise ValueError("session catalog snapshot NACK position is invalid")
            _validate_sequence(expected_page_index)
        elif reason == "event_gap":
            if (
                snapshot_id is not None
                or expected_page_index is not None
                or expected_catalog_sequence is None
            ):
                raise ValueError("session catalog event NACK position is invalid")
            _validate_sequence(expected_catalog_sequence)
        elif reason in {
            "runtime_mismatch",
            "stale_writer",
            "contract_mismatch",
        }:
            if (
                snapshot_id is not None
                or expected_page_index is not None
                or expected_catalog_sequence is not None
            ):
                raise ValueError("session catalog NACK position is invalid")
        else:
            raise ValueError("session catalog NACK reason is invalid")
        nack = SessionCatalogNack(
            profile=profile,
            runtime_generation=runtime_generation,
            rejected_message_id=canonical_uuid(rejected_message_id),
            rejected_payload_digest=rejected_payload_digest,
            rejected_connector_sequence=rejected_connector_sequence,
            reason=reason,
            snapshot_id=canonical_uuid(snapshot_id) if snapshot_id else None,
            expected_page_index=expected_page_index,
            expected_catalog_sequence=expected_catalog_sequence,
        )
        return await self._submit("nack_session_catalog_outbox", nack)

    async def get_cloud_session(self) -> CloudSessionCheckpoint:
        return await self._read(self._get_cloud_session)

    async def begin_transport_epoch(
        self,
        *,
        epoch_id: str,
        runtime_generation: str,
        previous_connection_id: str | None,
        next_outbound_sequence: int,
        next_inbound_sequence: int,
    ) -> CloudSessionCheckpoint:
        _validate_uuid_text(epoch_id, "transport epoch id")
        _validate_text(runtime_generation, "runtime generation", maximum=128)
        if previous_connection_id is not None:
            _validate_uuid_text(previous_connection_id, "previous connection id")
        _validate_sequence(next_outbound_sequence)
        _validate_sequence(next_inbound_sequence)
        return await self._submit(
            "begin_transport_epoch",
            epoch_id,
            runtime_generation,
            previous_connection_id,
            next_outbound_sequence,
            next_inbound_sequence,
        )

    async def reconcile_transport_epoch(
        self,
        *,
        epoch_id: str,
        previous_connection_id: str,
        next_outbound_sequence: int,
        next_inbound_sequence: int,
    ) -> CloudSessionCheckpoint:
        _validate_uuid_text(epoch_id, "transport epoch id")
        _validate_uuid_text(previous_connection_id, "previous connection id")
        _validate_sequence(next_outbound_sequence)
        _validate_sequence(next_inbound_sequence)
        return await self._submit(
            "reconcile_transport_epoch",
            epoch_id,
            previous_connection_id,
            next_outbound_sequence,
            next_inbound_sequence,
        )

    async def commit_transport_handshake(
        self,
        *,
        epoch_id: str,
        previous_connection_id: str,
        next_outbound_sequence: int,
        next_inbound_sequence: int,
    ) -> CloudSessionCheckpoint:
        _validate_uuid_text(epoch_id, "transport epoch id")
        _validate_uuid_text(previous_connection_id, "previous connection id")
        _validate_sequence(next_outbound_sequence)
        _validate_sequence(next_inbound_sequence)
        return await self._submit(
            "commit_transport_handshake",
            epoch_id,
            previous_connection_id,
            next_outbound_sequence,
            next_inbound_sequence,
        )

    async def stage_transport_frame(
        self,
        *,
        epoch_id: str,
        sequence: int,
        message_id: str,
        message_type: str,
        business_kind: str,
        business_key: str,
        business_revision: int,
        runtime_generation: str | None,
        frame: bytes,
    ) -> TransportFrameRecord:
        _validate_uuid_text(epoch_id, "transport epoch id")
        _validate_uuid_text(message_id, "transport message id")
        if message_type not in _TRANSPORT_MESSAGE_TYPES:
            raise ValueError("transport message type is invalid")
        if business_kind not in _TRANSPORT_BUSINESS_KINDS:
            raise ValueError("transport business kind is invalid")
        if _TRANSPORT_BUSINESS_PAIRS[message_type] != business_kind:
            raise ValueError("transport message and business kind do not match")
        if business_kind == "heartbeat":
            if (
                business_revision != sequence
                or business_key != f"heartbeat-{business_revision}"
            ):
                raise ValueError("heartbeat business identity is invalid")
        else:
            _validate_uuid_text(business_key, "transport business key")
        if runtime_generation is not None:
            _validate_text(runtime_generation, "runtime generation", maximum=128)
        _validate_sequence(sequence)
        _validate_sequence(business_revision)
        _validate_payload(frame)
        return await self._submit(
            "stage_transport_frame",
            epoch_id,
            sequence,
            message_id,
            message_type,
            business_kind,
            business_key,
            business_revision,
            runtime_generation,
            frame,
        )

    async def mark_transport_sent(
        self,
        *,
        epoch_id: str,
        sequence: int,
    ) -> TransportFrameRecord:
        _validate_uuid_text(epoch_id, "transport epoch id")
        _validate_sequence(sequence)
        return await self._submit("mark_transport_sent", epoch_id, sequence)

    async def transport_frame(
        self,
        message_id: str,
    ) -> TransportFrameRecord | None:
        _validate_uuid_text(message_id, "transport message id")
        return await self._read(self._transport_frame, message_id)

    async def pending_transport_frames(
        self,
        *,
        epoch_id: str,
        limit: int,
        after_sequence: int | None = None,
    ) -> tuple[TransportFrameRecord, ...]:
        _validate_uuid_text(epoch_id, "transport epoch id")
        _validate_command_limit(limit, self._config.transport_journal_entries)
        if after_sequence is not None:
            _validate_sequence(after_sequence)
        return await self._read(
            self._pending_transport_frames,
            epoch_id,
            limit,
            after_sequence,
        )

    async def settle_transport_cursor(
        self,
        *,
        epoch_id: str,
        next_sequence: int,
    ) -> tuple[TransportFrameRecord, ...]:
        _validate_uuid_text(epoch_id, "transport epoch id")
        _validate_sequence(next_sequence)
        return await self._submit(
            "settle_transport_cursor",
            epoch_id,
            next_sequence,
        )

    async def advance_cloud_outbound(self, expected_sequence: int) -> int:
        _validate_sequence(expected_sequence)
        return await self._submit("advance_cloud_outbound", expected_sequence)

    async def advance_cloud_inbound(self, expected_sequence: int) -> int:
        _validate_sequence(expected_sequence)
        return await self._submit("advance_cloud_inbound", expected_sequence)

    async def begin_cloud_reconciliation(
        self,
        *,
        previous_connection_id: str,
        next_outbound_sequence: int,
        next_inbound_sequence: int,
    ) -> CloudSessionCheckpoint:
        _validate_uuid_text(previous_connection_id, "previous connection id")
        _validate_sequence(next_outbound_sequence)
        _validate_sequence(next_inbound_sequence)
        return await self._submit(
            "begin_cloud_reconciliation",
            previous_connection_id,
            next_outbound_sequence,
            next_inbound_sequence,
        )

    async def complete_cloud_reconciliation(
        self,
        *,
        previous_connection_id: str,
    ) -> CloudSessionCheckpoint:
        _validate_uuid_text(previous_connection_id, "previous connection id")
        return await self._submit(
            "complete_cloud_reconciliation",
            previous_connection_id,
        )

    async def put_command(
        self,
        *,
        command_id: str,
        message_id: str,
        digest: str,
        delivery_payload: bytes,
        receipt_payload: bytes,
        expires_at: str,
        revision: int,
    ) -> CommandPutResult:
        _validate_uuid_text(command_id, "command id")
        _validate_uuid_text(message_id, "command message id")
        _validate_command_digest(digest)
        _validate_utc_instant_text(expires_at, "command expiry")
        _validate_payload(delivery_payload)
        _validate_payload(receipt_payload)
        if type(revision) is not int or revision < 1:
            raise ValueError("command revision must be a positive integer")
        return await self._submit(
            "put_command",
            command_id,
            message_id,
            digest,
            delivery_payload,
            receipt_payload,
            expires_at,
            revision,
        )

    async def get_command(self, command_id: str) -> CommandRecord | None:
        _validate_uuid_text(command_id, "command id")
        return await self._read(self._get_command, command_id)

    async def claim_command(self, command_id: str) -> bool:
        _validate_uuid_text(command_id, "command id")
        return await self._submit("claim_command", command_id)

    async def complete_command(
        self,
        *,
        command_id: str,
        state: str,
        result_payload: bytes,
        revision: int,
    ) -> CommandRecord:
        _validate_uuid_text(command_id, "command id")
        _validate_text(state, "command completion state", maximum=16)
        if state not in {"succeeded", "failed", "unknown"}:
            raise ValueError("command completion state must be terminal")
        _validate_payload(result_payload)
        if type(revision) is not int or revision < 1:
            raise ValueError("command revision must be a positive integer")
        return await self._submit(
            "complete_command",
            command_id,
            state,
            result_payload,
            revision,
        )

    async def command_records(
        self,
        *,
        state: str | None,
        limit: int,
    ) -> tuple[CommandRecord, ...]:
        if state is not None:
            _validate_text(state, "command state", maximum=16)
            if state not in {
                "delivered",
                "executing",
                "succeeded",
                "failed",
                "unknown",
            }:
                raise ValueError("command state is invalid")
        _validate_command_limit(limit, self._config.bounded_queue_items)
        return await self._read(self._command_records, state, limit)

    async def pending_command_messages(
        self,
        *,
        limit: int,
        after_created_at: str | None = None,
        after_command_id: str | None = None,
        after_message_type: str | None = None,
    ) -> tuple[CommandOutboxRecord, ...]:
        _validate_command_limit(
            limit,
            self._config.command_retention_entries * 2,
        )
        cursor = (after_created_at, after_command_id, after_message_type)
        if any(value is not None for value in cursor):
            if not all(type(value) is str and value for value in cursor):
                raise ValueError("command outbox cursor must be complete")
            if after_message_type not in {"command.receipt", "command.result"}:
                raise ValueError("command outbox cursor message type is invalid")
        return await self._read(
            self._pending_command_messages,
            limit,
            after_created_at,
            after_command_id,
            after_message_type,
        )

    async def ack_command_message(
        self,
        *,
        command_id: str,
        message_type: str,
    ) -> bool:
        _validate_uuid_text(command_id, "command id")
        _validate_text(message_type, "command message type", maximum=64)
        if message_type not in {"command.receipt", "command.result"}:
            raise ValueError("command message type is invalid")
        return await self._submit(
            "ack_command_message",
            command_id,
            message_type,
        )

    async def prune_commands(
        self,
        *,
        completed_before: str,
        limit: int,
    ) -> int:
        _validate_utc_instant_text(completed_before, "command prune instant")
        _validate_command_limit(limit, self._config.bounded_queue_items)
        return await self._submit(
            "prune_commands",
            completed_before,
            limit,
        )

    async def put_owner_control(
        self,
        *,
        request_id: str,
        request_digest: str,
        control_transport_id: str,
        operation: str,
        request_payload: bytes,
        scope_payload: bytes,
    ) -> OwnerControlPutResult:
        _validate_uuid_text(request_id, "owner request id")
        _validate_uuid_text(control_transport_id, "control transport id")
        _validate_hex_digest(request_digest, "owner request digest")
        _validate_text(operation, "owner control operation", maximum=64)
        if operation not in _OWNER_CONTROL_OPERATIONS:
            raise ValueError("owner control operation is invalid")
        _validate_payload(request_payload)
        _validate_payload(scope_payload)
        return await self._submit(
            "put_owner_control",
            request_id,
            request_digest,
            control_transport_id,
            operation,
            request_payload,
            scope_payload,
        )

    async def put_owner_control_and_advance_inbound(
        self,
        *,
        expected_sequence: int,
        request_id: str,
        request_digest: str,
        control_transport_id: str,
        operation: str,
        request_payload: bytes,
        scope_payload: bytes,
    ) -> OwnerControlPutResult:
        _validate_sequence(expected_sequence)
        _validate_uuid_text(request_id, "owner request id")
        _validate_uuid_text(control_transport_id, "control transport id")
        _validate_hex_digest(request_digest, "owner request digest")
        _validate_text(operation, "owner control operation", maximum=64)
        if operation not in _OWNER_CONTROL_OPERATIONS:
            raise ValueError("owner control operation is invalid")
        _validate_payload(request_payload)
        _validate_payload(scope_payload)
        return await self._submit(
            "put_owner_control_and_advance_inbound",
            expected_sequence,
            request_id,
            request_digest,
            control_transport_id,
            operation,
            request_payload,
            scope_payload,
        )

    async def get_owner_control(
        self,
        request_id: str,
    ) -> OwnerControlRecord | None:
        _validate_uuid_text(request_id, "owner request id")
        return await self._read(self._get_owner_control, request_id)

    async def claim_owner_control(self, request_id: str) -> bool:
        _validate_uuid_text(request_id, "owner request id")
        return await self._submit("claim_owner_control", request_id)

    async def complete_owner_control(
        self,
        *,
        request_id: str,
        response_payload: bytes,
        response_revision: int = 1,
    ) -> OwnerControlRecord:
        _validate_uuid_text(request_id, "owner request id")
        _validate_payload(response_payload)
        if type(response_revision) is not int or response_revision < 1:
            raise ValueError("owner response revision must be positive")
        return await self._submit(
            "complete_owner_control",
            request_id,
            response_payload,
            response_revision,
        )

    async def mark_owner_control_effect_unknown(
        self,
        request_id: str,
    ) -> OwnerControlRecord:
        _validate_uuid_text(request_id, "owner request id")
        return await self._submit("mark_owner_control_effect_unknown", request_id)

    async def pending_owner_control(
        self,
        *,
        limit: int,
        after_created_at: str | None = None,
        after_request_id: str | None = None,
    ) -> tuple[OwnerControlRecord, ...]:
        _validate_command_limit(limit, self._config.command_retention_entries)
        cursor = (after_created_at, after_request_id)
        if any(value is not None for value in cursor) and not all(
            type(value) is str and value for value in cursor
        ):
            raise ValueError("owner control cursor must be complete")
        return await self._read(
            self._pending_owner_control,
            limit,
            after_created_at,
            after_request_id,
        )

    async def owner_control_records(
        self,
        *,
        state: str,
        limit: int,
    ) -> tuple[OwnerControlRecord, ...]:
        if type(state) is not str or state not in _OWNER_CONTROL_STATES:
            raise ValueError("owner control state is invalid")
        _validate_command_limit(limit, self._config.command_retention_entries)
        return await self._read(self._owner_control_records, state, limit)

    async def _submit(self, operation: str, *arguments: object) -> Any:
        self._ensure_writable()
        loop = asyncio.get_running_loop()
        result: asyncio.Future[object] = loop.create_future()
        request = _WriteRequest(operation, arguments, result)
        try:
            self._queue.put_nowait(request)
        except asyncio.QueueFull:
            raise StorageOverloaded() from None
        try:
            async with asyncio.timeout(self._config.storage_write_deadline_seconds):
                return await asyncio.shield(result)
        except TimeoutError:
            missed_start = request.abandon_before_start()
            result.cancel()
            if missed_start:
                raise StorageDeadlineExceeded() from None
            raise StorageEffectUnknown() from None

    async def _read(self, operation: Callable[..., Any], *arguments: object) -> Any:
        self._ensure_readable()
        task = asyncio.create_task(self._run_blocking(operation, *arguments))
        try:
            async with asyncio.timeout(self._config.storage_write_deadline_seconds):
                return await asyncio.shield(task)
        except TimeoutError:
            task.cancel()
            raise StorageDeadlineExceeded() from None
        except asyncio.CancelledError:
            task.cancel()
            raise
        except StorageFatalError as error:
            self._mark_fatal(type(error))
            raise
        except StorageError:
            raise
        except SQLITE_FAILURES as error:
            mapped = map_sqlite_error(error)
            self._mark_fatal(type(mapped))
            raise mapped from None

    async def _run_blocking(
        self,
        operation: Callable[..., Any],
        *arguments: object,
    ) -> Any:
        executor = self._executor
        if executor is None:
            raise StorageStopped()
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(executor, partial(operation, *arguments))

    def _open(self) -> None:
        engine = create_engine(
            URL.create("sqlite+pysqlite", database=str(self._path)),
            pool_size=1,
            max_overflow=0,
        )
        self._engine = engine
        self._policy.install(engine)
        apply_migrations(engine)
        self._session_factory = sessionmaker(
            bind=engine,
            autoflush=False,
            expire_on_commit=False,
        )
        with self._session_factory() as session, session.begin():
            owner_control_repository.recover_executing(session, now=_utc_now())
        diagnostics = self._diagnostics()
        if (
            diagnostics.journal_mode != "wal"
            or not diagnostics.foreign_keys
            or diagnostics.synchronous != 2
            or diagnostics.busy_timeout_ms != self._config.storage_busy_timeout_ms
        ):
            raise StorageFatalError("SQLite connection policy was not applied")

    def _execute_request(self, request: _WriteRequest) -> object:
        if not request.begin():
            return _ABANDONED
        return self._execute_write(request.operation, request.arguments)

    def _execute_write(
        self,
        operation: str,
        arguments: tuple[object, ...],
    ) -> object:
        if self._write_fault is not None:
            self._write_fault(operation)
        factory = self._require_session_factory()
        with factory() as session:
            with session.begin():
                if operation == "put_inbox":
                    result = self._put_inbox(session, *arguments)
                elif operation == "append_outbox":
                    result = self._append_outbox(session, *arguments)
                elif operation == "ack_outbox":
                    result = self._ack_outbox(session, *arguments)
                elif operation == "advance_cursor":
                    result = self._advance_cursor(session, *arguments)
                elif operation == "advance_cloud_outbound":
                    result = cloud_session_repository.advance_outbound(
                        session,
                        expected_sequence=_int_argument(arguments[0]),
                        updated_at=_utc_now(),
                    )
                elif operation == "begin_transport_epoch":
                    result = transport_journal_repository.begin_epoch(
                        session,
                        epoch_id=_text_argument(arguments[0]),
                        runtime_generation=_text_argument(arguments[1]),
                        previous_connection_id=(
                            _text_argument(arguments[2])
                            if arguments[2] is not None
                            else None
                        ),
                        next_outbound_sequence=_int_argument(arguments[3]),
                        next_inbound_sequence=_int_argument(arguments[4]),
                        now=_utc_now(),
                    )
                elif operation == "reconcile_transport_epoch":
                    now = _utc_now()
                    result, settled = transport_journal_repository.reconcile(
                        session,
                        epoch_id=_text_argument(arguments[0]),
                        previous_connection_id=_text_argument(arguments[1]),
                        next_outbound_sequence=_int_argument(arguments[2]),
                        next_inbound_sequence=_int_argument(arguments[3]),
                        now=now,
                    )
                    self._settle_business_transport(session, settled, now=now)
                elif operation == "commit_transport_handshake":
                    now = _utc_now()
                    checkpoint = cloud_session_repository.load(session)
                    settled = transport_journal_repository.settle_cursor(
                        session,
                        epoch_id=_text_argument(arguments[0]),
                        next_sequence=checkpoint.next_outbound_sequence,
                        now=now,
                    )
                    self._settle_business_transport(session, settled, now=now)
                    result = transport_journal_repository.commit_handshake(
                        session,
                        epoch_id=_text_argument(arguments[0]),
                        previous_connection_id=_text_argument(arguments[1]),
                        next_outbound_sequence=_int_argument(arguments[2]),
                        next_inbound_sequence=_int_argument(arguments[3]),
                        now=now,
                    )
                elif operation == "stage_transport_frame":
                    result = transport_journal_repository.stage(
                        session,
                        epoch_id=_text_argument(arguments[0]),
                        sequence=_int_argument(arguments[1]),
                        message_id=_text_argument(arguments[2]),
                        message_type=_text_argument(arguments[3]),
                        business_kind=_text_argument(arguments[4]),
                        business_key=_text_argument(arguments[5]),
                        business_revision=_int_argument(arguments[6]),
                        runtime_generation=(
                            _text_argument(arguments[7])
                            if arguments[7] is not None
                            else None
                        ),
                        frame=_bytes_argument(arguments[8]),
                        max_entries=self._config.transport_journal_entries,
                        now=_utc_now(),
                    )
                elif operation == "mark_transport_sent":
                    result = transport_journal_repository.mark_sent(
                        session,
                        epoch_id=_text_argument(arguments[0]),
                        sequence=_int_argument(arguments[1]),
                        now=_utc_now(),
                    )
                elif operation == "settle_transport_cursor":
                    now = _utc_now()
                    settled = transport_journal_repository.settle_cursor(
                        session,
                        epoch_id=_text_argument(arguments[0]),
                        next_sequence=_int_argument(arguments[1]),
                        now=now,
                    )
                    self._settle_business_transport(session, settled, now=now)
                    result = settled
                elif operation == "append_observer_outbox":
                    result = observer_outbox_repository.put(
                        session,
                        message_id=_text_argument(arguments[0]),
                        connector_sequence=_int_argument(arguments[1]),
                        transport_epoch_id=(
                            _text_argument(arguments[2])
                            if arguments[2] is not None
                            else None
                        ),
                        message_type=_text_argument(arguments[3]),
                        profile=_text_argument(arguments[4]),
                        session_key=_text_argument(arguments[5]),
                        runtime_generation=_text_argument(arguments[6]),
                        runtime_session_id=_text_argument(arguments[7]),
                        event_sequence=_int_argument(arguments[8]),
                        payload=_bytes_argument(arguments[9]),
                        frame=_bytes_argument(arguments[10]),
                        max_pending=self._config.bounded_queue_items,
                        now=_utc_now(),
                    )
                    if arguments[2] is not None:
                        transport_journal_repository.stage(
                            session,
                            epoch_id=_text_argument(arguments[2]),
                            sequence=_int_argument(arguments[1]),
                            message_id=_text_argument(arguments[0]),
                            message_type=_text_argument(arguments[3]),
                            business_kind="observer",
                            business_key=_text_argument(arguments[0]),
                            business_revision=_int_argument(arguments[8]),
                            runtime_generation=_text_argument(arguments[6]),
                            frame=_bytes_argument(arguments[10]),
                            max_entries=self._config.transport_journal_entries,
                            now=_utc_now(),
                        )
                elif operation == "ack_observer_outbox":
                    result = observer_outbox_repository.acknowledge(
                        session,
                        ack=arguments[0],  # type: ignore[arg-type]
                    )
                    transport_journal_repository.settle_message(
                        session,
                        message_id=result.message_id,
                        now=_utc_now(),
                    )
                elif operation == "nack_observer_outbox":
                    result = observer_outbox_repository.reject(
                        session,
                        nack=arguments[0],  # type: ignore[arg-type]
                    )
                    transport_journal_repository.settle_message(
                        session,
                        message_id=result.message_id,
                        now=_utc_now(),
                    )
                elif operation == "append_session_catalog_outbox":
                    now = _utc_now()
                    result = session_catalog_outbox_repository.put(
                        session,
                        message_id=_text_argument(arguments[0]),
                        connector_sequence=_int_argument(arguments[1]),
                        transport_epoch_id=(
                            _text_argument(arguments[2])
                            if arguments[2] is not None
                            else None
                        ),
                        message_type=_text_argument(arguments[3]),
                        profile=_text_argument(arguments[4]),
                        runtime_generation=_text_argument(arguments[5]),
                        snapshot_id=(
                            _text_argument(arguments[6])
                            if arguments[6] is not None
                            else None
                        ),
                        catalog_revision=(
                            _int_argument(arguments[7])
                            if arguments[7] is not None
                            else None
                        ),
                        page_index=(
                            _int_argument(arguments[8])
                            if arguments[8] is not None
                            else None
                        ),
                        is_last=(
                            bool(arguments[9]) if arguments[9] is not None else None
                        ),
                        catalog_sequence=(
                            _int_argument(arguments[10])
                            if arguments[10] is not None
                            else None
                        ),
                        payload=_bytes_argument(arguments[11]),
                        frame=_bytes_argument(arguments[12]),
                        max_pending=self._config.bounded_queue_items,
                        now=now,
                    )
                    if arguments[2] is not None:
                        business_revision = (
                            _int_argument(arguments[8])
                            if arguments[8] is not None
                            else _int_argument(arguments[10])
                        )
                        transport_journal_repository.stage(
                            session,
                            epoch_id=_text_argument(arguments[2]),
                            sequence=_int_argument(arguments[1]),
                            message_id=_text_argument(arguments[0]),
                            message_type=_text_argument(arguments[3]),
                            business_kind="session_catalog",
                            business_key=_text_argument(arguments[0]),
                            business_revision=business_revision,
                            runtime_generation=_text_argument(arguments[5]),
                            frame=_bytes_argument(arguments[12]),
                            max_entries=self._config.transport_journal_entries,
                            now=now,
                        )
                elif operation == "ack_session_catalog_outbox":
                    now = _utc_now()
                    result = session_catalog_outbox_repository.acknowledge(
                        session,
                        ack=arguments[0],  # type: ignore[arg-type]
                        now=now,
                        max_receipts=self._config.bounded_queue_items,
                    )
                    transport_journal_repository.settle_message(
                        session,
                        message_id=result.message_id,
                        now=now,
                    )
                elif operation == "nack_session_catalog_outbox":
                    now = _utc_now()
                    result = session_catalog_outbox_repository.reject(
                        session,
                        nack=arguments[0],  # type: ignore[arg-type]
                        now=now,
                    )
                    transport_journal_repository.settle_message(
                        session,
                        message_id=result.message_id,
                        now=now,
                    )
                elif operation == "retire_session_catalog_outbox":
                    now = _utc_now()
                    result = session_catalog_outbox_repository.retire_pending(
                        session,
                        now=now,
                    )
                    transport_journal_repository.retire_business_kind(
                        session,
                        business_kind="session_catalog",
                        now=now,
                    )
                elif operation == "advance_cloud_inbound":
                    result = cloud_session_repository.advance_inbound(
                        session,
                        expected_sequence=_int_argument(arguments[0]),
                        updated_at=_utc_now(),
                    )
                elif operation == "begin_cloud_reconciliation":
                    result = cloud_session_repository.begin_reconciliation(
                        session,
                        previous_connection_id=_text_argument(arguments[0]),
                        next_outbound_sequence=_int_argument(arguments[1]),
                        next_inbound_sequence=_int_argument(arguments[2]),
                        updated_at=_utc_now(),
                    )
                elif operation == "complete_cloud_reconciliation":
                    result = cloud_session_repository.complete_reconciliation(
                        session,
                        previous_connection_id=_text_argument(arguments[0]),
                        updated_at=_utc_now(),
                    )
                elif operation == "put_command":
                    result = control_command_repository.put(
                        session,
                        command_id=_text_argument(arguments[0]),
                        message_id=_text_argument(arguments[1]),
                        digest=_text_argument(arguments[2]),
                        delivery_payload=_bytes_argument(arguments[3]),
                        receipt_payload=_bytes_argument(arguments[4]),
                        expires_at=_text_argument(arguments[5]),
                        revision=_int_argument(arguments[6]),
                        max_entries=self._config.command_retention_entries,
                        now=_utc_now(),
                    )
                elif operation == "claim_command":
                    result = control_command_repository.claim(
                        session,
                        command_id=_text_argument(arguments[0]),
                        now=_utc_now(),
                    )
                elif operation == "complete_command":
                    result = control_command_repository.complete(
                        session,
                        command_id=_text_argument(arguments[0]),
                        state=_text_argument(arguments[1]),
                        result_payload=_bytes_argument(arguments[2]),
                        revision=_int_argument(arguments[3]),
                        now=_utc_now(),
                    )
                elif operation == "ack_command_message":
                    result = control_command_repository.acknowledge(
                        session,
                        command_id=_text_argument(arguments[0]),
                        message_type=_text_argument(arguments[1]),
                        now=_utc_now(),
                    )
                elif operation == "prune_commands":
                    result = control_command_repository.prune(
                        session,
                        completed_before=_text_argument(arguments[0]),
                        limit=_int_argument(arguments[1]),
                    )
                elif operation == "put_owner_control":
                    result = owner_control_repository.put(
                        session,
                        request_id=_text_argument(arguments[0]),
                        request_digest=_text_argument(arguments[1]),
                        control_transport_id=_text_argument(arguments[2]),
                        operation=_text_argument(arguments[3]),
                        request_payload=_bytes_argument(arguments[4]),
                        scope_payload=_bytes_argument(arguments[5]),
                        max_entries=self._config.command_retention_entries,
                        now=_utc_now(),
                    )
                elif operation == "put_owner_control_and_advance_inbound":
                    now = _utc_now()
                    result = owner_control_repository.put(
                        session,
                        request_id=_text_argument(arguments[1]),
                        request_digest=_text_argument(arguments[2]),
                        control_transport_id=_text_argument(arguments[3]),
                        operation=_text_argument(arguments[4]),
                        request_payload=_bytes_argument(arguments[5]),
                        scope_payload=_bytes_argument(arguments[6]),
                        max_entries=self._config.command_retention_entries,
                        now=now,
                    )
                    cloud_session_repository.advance_inbound(
                        session,
                        expected_sequence=_int_argument(arguments[0]),
                        updated_at=now,
                    )
                elif operation == "claim_owner_control":
                    result = owner_control_repository.claim(
                        session,
                        request_id=_text_argument(arguments[0]),
                        now=_utc_now(),
                    )
                elif operation == "complete_owner_control":
                    result = owner_control_repository.complete(
                        session,
                        request_id=_text_argument(arguments[0]),
                        response_payload=_bytes_argument(arguments[1]),
                        response_revision=_int_argument(arguments[2]),
                        now=_utc_now(),
                    )
                elif operation == "mark_owner_control_effect_unknown":
                    result = owner_control_repository.mark_effect_unknown(
                        session,
                        request_id=_text_argument(arguments[0]),
                        now=_utc_now(),
                    )
                else:
                    raise RuntimeError(f"unknown storage operation: {operation}")
            return result

    def _settle_business_transport(
        self,
        session: Session,
        settled: tuple[TransportFrameRecord, ...],
        *,
        now: str,
    ) -> None:
        for frame in settled:
            if frame.business_kind in {"command.receipt", "command.result"}:
                acknowledged = control_command_repository.acknowledge_transport(
                    session,
                    command_id=frame.business_key,
                    message_type=frame.business_kind,
                    revision=frame.business_revision,
                    now=now,
                )
                if not acknowledged:
                    raise StorageSequenceConflict()
            elif frame.business_kind == "control.response":
                acknowledged = owner_control_repository.mark_transport_received(
                    session,
                    request_id=frame.business_key,
                    response_revision=frame.business_revision,
                    now=now,
                )
                if not acknowledged:
                    raise StorageSequenceConflict()
            elif frame.business_kind == "observer":
                if not observer_outbox_repository.validate_transport_target(
                    session,
                    message_id=frame.business_key,
                    epoch_id=frame.epoch_id,
                    sequence=frame.sequence,
                    message_type=frame.message_type,
                    event_sequence=frame.business_revision,
                ):
                    raise StorageSequenceConflict()
            elif frame.business_kind == "session_catalog":
                if not session_catalog_outbox_repository.validate_transport_target(
                    session,
                    message_id=frame.business_key,
                    epoch_id=frame.epoch_id,
                    sequence=frame.sequence,
                    message_type=frame.message_type,
                    catalog_revision=frame.business_revision,
                ):
                    raise StorageSequenceConflict()

    def _put_inbox(
        self,
        session: Session,
        message_id: object,
        digest: object,
        payload: object,
        state: object,
    ) -> InboxPutResult:
        existing = session.get(InboxMessage, str(message_id))
        if existing is not None:
            if existing.digest != digest:
                raise IdempotencyConflict()
            return InboxPutResult(_inbox_record(existing), inserted=False)

        entity = InboxMessage(
            message_id=str(message_id),
            digest=str(digest),
            state=str(state),
            payload=bytes(payload),
            received_at=_utc_now(),
        )
        session.add(entity)
        session.flush()
        return InboxPutResult(_inbox_record(entity), inserted=True)

    def _get_inbox(self, message_id: object) -> InboxRecord | None:
        factory = self._require_session_factory()
        with factory() as session:
            entity = session.get(InboxMessage, str(message_id))
            return _inbox_record(entity) if entity is not None else None

    def _append_outbox(
        self,
        session: Session,
        message_id: object,
        stream: object,
        sequence: object,
        payload: object,
    ) -> OutboxRecord:
        existing = session.scalar(
            select(OutboxMessage).where(OutboxMessage.message_id == str(message_id))
        )
        if existing is not None:
            if (
                existing.stream != stream
                or existing.sequence != sequence
                or existing.payload != bytes(payload)
            ):
                raise IdempotencyConflict()
            return _outbox_record(existing)

        occupied_sequence = session.scalar(
            select(OutboxMessage).where(
                OutboxMessage.stream == str(stream),
                OutboxMessage.sequence == int(sequence),
            )
        )
        if occupied_sequence is not None:
            raise IdempotencyConflict()

        entity = OutboxMessage(
            message_id=str(message_id),
            stream=str(stream),
            sequence=int(sequence),
            state="pending",
            payload=bytes(payload),
            created_at=_utc_now(),
            acked_at=None,
        )
        session.add(entity)
        session.flush()
        return _outbox_record(entity)

    def _pending_outbox(
        self,
        limit: object,
        after_sequence: object,
        stream: object,
        include_settled: object,
    ) -> tuple[OutboxRecord, ...]:
        factory = self._require_session_factory()
        with factory() as session:
            statement = select(OutboxMessage)
            if not bool(include_settled):
                statement = statement.where(OutboxMessage.state == "pending")
            if after_sequence is not None:
                statement = statement.where(
                    OutboxMessage.sequence > int(after_sequence)
                )
            if stream is not None:
                statement = statement.where(OutboxMessage.stream == str(stream))
            entities = session.scalars(
                statement.order_by(OutboxMessage.sequence, OutboxMessage.id).limit(
                    int(limit)
                )
            ).all()
            return tuple(_outbox_record(entity) for entity in entities)

    def _ack_outbox(self, session: Session, message_id: object) -> bool:
        entity = session.scalar(
            select(OutboxMessage).where(OutboxMessage.message_id == str(message_id))
        )
        if entity is None:
            return False
        if entity.state != "acked":
            entity.state = "acked"
            entity.acked_at = _utc_now()
            session.flush()
        return True

    def _advance_cursor(
        self,
        session: Session,
        stream: object,
        sequence: object,
    ) -> int:
        entity = session.get(StreamCursor, str(stream))
        if entity is None:
            entity = StreamCursor(
                stream=str(stream),
                sequence=int(sequence),
                updated_at=_utc_now(),
            )
            session.add(entity)
            session.flush()
            return entity.sequence
        if int(sequence) > entity.sequence:
            entity.sequence = int(sequence)
            entity.updated_at = _utc_now()
            session.flush()
        return entity.sequence

    def _get_cursor(self, stream: object) -> int | None:
        factory = self._require_session_factory()
        with factory() as session:
            entity = session.get(StreamCursor, str(stream))
            return entity.sequence if entity is not None else None

    def _get_observer_outbox(
        self,
        message_id: object,
    ) -> ObserverOutboxRecord | None:
        factory = self._require_session_factory()
        with factory() as session:
            return observer_outbox_repository.get(session, str(message_id))

    def _get_observer_fact(
        self,
        transport_epoch_id: object,
        message_type: object,
        profile: object,
        session_key: object,
        runtime_generation: object,
        runtime_session_id: object,
        event_sequence: object,
    ) -> ObserverOutboxRecord | None:
        factory = self._require_session_factory()
        with factory() as session:
            return observer_outbox_repository.get_fact(
                session,
                transport_epoch_id=(
                    str(transport_epoch_id) if transport_epoch_id is not None else None
                ),
                message_type=str(message_type),
                profile=str(profile),
                session_key=str(session_key),
                runtime_generation=str(runtime_generation),
                runtime_session_id=str(runtime_session_id),
                event_sequence=int(event_sequence),
            )

    def _pending_observer_outbox(
        self,
        limit: object,
        after_sequence: object,
        include_settled: object,
    ) -> tuple[ObserverOutboxRecord, ...]:
        factory = self._require_session_factory()
        with factory() as session:
            return observer_outbox_repository.pending(
                session,
                limit=int(limit),
                after_sequence=(
                    int(after_sequence) if after_sequence is not None else None
                ),
                include_settled=bool(include_settled),
            )

    def _get_session_catalog_outbox(
        self,
        message_id: object,
    ) -> SessionCatalogOutboxRecord | None:
        factory = self._require_session_factory()
        with factory() as session:
            return session_catalog_outbox_repository.get(session, str(message_id))

    def _get_session_catalog_fact(
        self,
        transport_epoch_id: object,
        message_type: object,
        profile: object,
        runtime_generation: object,
        snapshot_id: object,
        catalog_revision: object,
        page_index: object,
        catalog_sequence: object,
    ) -> SessionCatalogOutboxRecord | None:
        factory = self._require_session_factory()
        with factory() as session:
            return session_catalog_outbox_repository.get_fact(
                session,
                transport_epoch_id=(
                    str(transport_epoch_id) if transport_epoch_id is not None else None
                ),
                message_type=str(message_type),
                profile=str(profile),
                runtime_generation=str(runtime_generation),
                snapshot_id=str(snapshot_id) if snapshot_id is not None else None,
                catalog_revision=(
                    int(catalog_revision) if catalog_revision is not None else None
                ),
                page_index=int(page_index) if page_index is not None else None,
                catalog_sequence=(
                    int(catalog_sequence) if catalog_sequence is not None else None
                ),
            )

    def _pending_session_catalog_outbox(
        self,
        limit: object,
        after_sequence: object,
        include_settled: object,
    ) -> tuple[SessionCatalogOutboxRecord, ...]:
        factory = self._require_session_factory()
        with factory() as session:
            return session_catalog_outbox_repository.pending(
                session,
                limit=int(limit),
                after_sequence=(
                    int(after_sequence) if after_sequence is not None else None
                ),
                include_settled=bool(include_settled),
            )

    def _get_cloud_session(self) -> CloudSessionCheckpoint:
        factory = self._require_session_factory()
        with factory() as session:
            return cloud_session_repository.load(session)

    def _transport_frame(
        self,
        message_id: object,
    ) -> TransportFrameRecord | None:
        factory = self._require_session_factory()
        with factory() as session:
            return transport_journal_repository.get(session, str(message_id))

    def _pending_transport_frames(
        self,
        epoch_id: object,
        limit: object,
        after_sequence: object,
    ) -> tuple[TransportFrameRecord, ...]:
        factory = self._require_session_factory()
        with factory() as session:
            return transport_journal_repository.pending(
                session,
                epoch_id=str(epoch_id),
                limit=int(limit),
                after_sequence=(
                    int(after_sequence) if after_sequence is not None else None
                ),
            )

    def _get_command(self, command_id: object) -> CommandRecord | None:
        factory = self._require_session_factory()
        with factory() as session:
            return control_command_repository.get(session, str(command_id))

    def _command_records(
        self,
        state: object,
        limit: object,
    ) -> tuple[CommandRecord, ...]:
        factory = self._require_session_factory()
        with factory() as session:
            return control_command_repository.records(
                session,
                state=str(state) if state is not None else None,
                limit=int(limit),
            )

    def _pending_command_messages(
        self,
        limit: object,
        after_created_at: object,
        after_command_id: object,
        after_message_type: object,
    ) -> tuple[CommandOutboxRecord, ...]:
        factory = self._require_session_factory()
        with factory() as session:
            return control_command_repository.pending_messages(
                session,
                limit=int(limit),
                after_created_at=(
                    _text_argument(after_created_at)
                    if after_created_at is not None
                    else None
                ),
                after_command_id=(
                    _text_argument(after_command_id)
                    if after_command_id is not None
                    else None
                ),
                after_message_type=(
                    _text_argument(after_message_type)
                    if after_message_type is not None
                    else None
                ),
            )

    def _get_owner_control(
        self,
        request_id: object,
    ) -> OwnerControlRecord | None:
        factory = self._require_session_factory()
        with factory() as session:
            return owner_control_repository.get(session, str(request_id))

    def _pending_owner_control(
        self,
        limit: object,
        after_created_at: object,
        after_request_id: object,
    ) -> tuple[OwnerControlRecord, ...]:
        factory = self._require_session_factory()
        with factory() as session:
            return owner_control_repository.pending(
                session,
                limit=int(limit),
                after_created_at=(
                    _text_argument(after_created_at)
                    if after_created_at is not None
                    else None
                ),
                after_request_id=(
                    _text_argument(after_request_id)
                    if after_request_id is not None
                    else None
                ),
            )

    def _owner_control_records(
        self,
        state: object,
        limit: object,
    ) -> tuple[OwnerControlRecord, ...]:
        factory = self._require_session_factory()
        with factory() as session:
            return owner_control_repository.records(
                session,
                state=_text_argument(state),
                limit=_int_argument(limit),
            )

    def _diagnostics(self) -> SQLiteDiagnostics:
        engine = self._require_engine()
        connection = engine.raw_connection()
        try:
            driver_connection = connection.driver_connection
            return self._policy.diagnostics(driver_connection)
        finally:
            connection.close()

    def _ensure_writable(self) -> None:
        if self._fatal_error_type is not None:
            raise self._fatal_error_type()
        if not self._accepting or self._engine is None:
            raise StorageStopped()

    def _ensure_readable(self) -> None:
        if self._fatal_error_type is not None:
            raise self._fatal_error_type()
        if self._engine is None:
            raise StorageStopped()

    def _require_engine(self) -> Engine:
        if self._engine is None:
            raise StorageStopped()
        return self._engine

    def _require_session_factory(self) -> sessionmaker[Session]:
        if self._session_factory is None:
            raise StorageStopped()
        return self._session_factory

    def _mark_fatal(self, error_type: type[StorageFatalError]) -> None:
        self._fatal_error_type = error_type
        self._accepting = False

    def _fail_pending(self, error_type: type[StorageError]) -> None:
        while True:
            try:
                item = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            try:
                if isinstance(item, _WriteRequest) and not item.result.done():
                    item.result.set_exception(error_type())
            finally:
                self._queue.task_done()

    async def _shutdown_resources(self) -> None:
        executor = self._executor
        if executor is None:
            return
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(executor, self._close_resources)
        executor.shutdown(wait=True, cancel_futures=True)
        self._executor = None

    def _close_resources(self) -> None:
        engine = self._engine
        self._session_factory = None
        self._engine = None
        if engine is not None:
            engine.dispose()


def _validate_payload(payload: bytes) -> None:
    if not isinstance(payload, bytes):
        raise TypeError("durable payload must be bytes")
    if len(payload) > MAX_DURABLE_PAYLOAD_BYTES:
        raise StorageFrameTooLarge()


def _validate_sequence(sequence: int) -> None:
    if type(sequence) is not int or sequence < 0:
        raise ValueError("cloud sequence must be a non-negative integer")


def _validate_uuid_text(value: object, name: str) -> None:
    if type(value) is not str:
        raise TypeError(f"{name} must be canonical UUID text")
    canonical_uuid(value)


def _validate_text(value: object, name: str, *, maximum: int) -> None:
    if type(value) is not str:
        raise TypeError(f"{name} must be text")
    if not 1 <= len(value) <= maximum:
        raise ValueError(f"{name} is outside length bounds")


def _validate_hex_digest(value: object, name: str) -> None:
    if type(value) is not str:
        raise TypeError(f"{name} must be text")
    if len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise ValueError(f"{name} must be lowercase sha256 hex")


def _validate_command_digest(value: object) -> None:
    if type(value) is not str:
        raise TypeError("command digest must be text")
    prefix = "sha256:"
    if not value.startswith(prefix):
        raise ValueError("command digest must use sha256 prefix")
    _validate_hex_digest(value[len(prefix) :], "command digest")


def _validate_utc_instant_text(value: object, name: str) -> None:
    _validate_text(value, name, maximum=64)
    assert isinstance(value, str)
    if not value.endswith("Z"):
        raise ValueError(f"{name} must be an RFC 3339 UTC instant")
    try:
        instant = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError(f"{name} must be an RFC 3339 UTC instant") from None
    if instant.utcoffset() != UTC.utcoffset(instant):
        raise ValueError(f"{name} must use UTC")


def _text_argument(value: object) -> str:
    if type(value) is not str:
        raise TypeError("storage text argument changed type")
    return value


def _int_argument(value: object) -> int:
    if type(value) is not int:
        raise TypeError("storage integer argument changed type")
    return value


def _bytes_argument(value: object) -> bytes:
    if type(value) is not bytes:
        raise TypeError("storage bytes argument changed type")
    return value


def _validate_command_limit(limit: int, maximum: int) -> None:
    if type(limit) is not int or not 1 <= limit <= maximum:
        raise ValueError("command read limit is outside configured bounds")


def _inbox_record(entity: InboxMessage) -> InboxRecord:
    return InboxRecord(
        message_id=entity.message_id,
        digest=entity.digest,
        state=entity.state,
        payload=bytes(entity.payload),
    )


def _outbox_record(entity: OutboxMessage) -> OutboxRecord:
    return OutboxRecord(
        row_id=entity.id,
        message_id=entity.message_id,
        stream=entity.stream,
        sequence=entity.sequence,
        state=entity.state,
        payload=bytes(entity.payload),
    )


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
