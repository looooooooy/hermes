from __future__ import annotations

from typing import Protocol

from hermes_connector.domain.observer import StreamAck, StreamNack
from hermes_connector.domain.storage import (
    CloudSessionCheckpoint,
    InboxPutResult,
    ObserverOutboxRecord,
    OutboxRecord,
    OwnerControlPutResult,
    OwnerControlRecord,
    SessionCatalogOutboxRecord,
    TransportFrameRecord,
)


class ReliableStoragePort(Protocol):
    async def begin_transport_epoch(
        self,
        *,
        epoch_id: str,
        runtime_generation: str,
        previous_connection_id: str | None,
        next_outbound_sequence: int,
        next_inbound_sequence: int,
    ) -> CloudSessionCheckpoint:
        """Atomically rotate transport epoch, retire old attempts, and reset cursors."""

    async def reconcile_transport_epoch(
        self,
        *,
        epoch_id: str,
        previous_connection_id: str,
        next_outbound_sequence: int,
        next_inbound_sequence: int,
    ) -> CloudSessionCheckpoint:
        """Apply a same-epoch authoritative Cloud transport cursor atomically."""

    async def commit_transport_handshake(
        self,
        *,
        epoch_id: str,
        previous_connection_id: str,
        next_outbound_sequence: int,
        next_inbound_sequence: int,
    ) -> CloudSessionCheckpoint:
        """Commit fresh hello/welcome cursor disposition outside the business journal."""

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
        """Persist one exact sequenced frame before any network send."""

    async def mark_transport_sent(
        self,
        *,
        epoch_id: str,
        sequence: int,
    ) -> TransportFrameRecord:
        """Atomically mark an attempt sent and CAS-advance its epoch cursor."""

    async def pending_transport_frames(
        self,
        *,
        epoch_id: str,
        limit: int,
        after_sequence: int | None = None,
    ) -> tuple[TransportFrameRecord, ...]:
        """Read active exact frames in one epoch for deterministic replay."""

    async def settle_transport_cursor(
        self,
        *,
        epoch_id: str,
        next_sequence: int,
    ) -> tuple[TransportFrameRecord, ...]:
        """Record Cloud durable transport receipt below its authoritative cursor."""

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
        """Persist an owner request before execution or inbound cursor advancement."""

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
        """Atomically persist one owner request and advance its inbound cursor."""

    async def claim_owner_control(self, request_id: str) -> bool:
        """Atomically claim a received owner request for at-most-once execution."""

    async def complete_owner_control(
        self,
        *,
        request_id: str,
        response_payload: bytes,
        response_revision: int = 1,
    ) -> OwnerControlRecord:
        """Persist the terminal owner response as business state."""

    async def get_owner_control(self, request_id: str) -> OwnerControlRecord | None:
        """Read one durable owner request/result ledger row."""

    async def mark_owner_control_effect_unknown(
        self,
        request_id: str,
    ) -> OwnerControlRecord:
        """Terminally recover a cancelled executing owner effect without replay."""

    async def pending_owner_control(
        self,
        *,
        limit: int,
        after_created_at: str | None = None,
        after_request_id: str | None = None,
    ) -> tuple[OwnerControlRecord, ...]:
        """Read terminal owner responses not yet received by Cloud transport."""

    async def owner_control_records(
        self,
        *,
        state: str,
        limit: int,
    ) -> tuple[OwnerControlRecord, ...]:
        """Read a bounded owner-ledger state page for deterministic recovery."""

    async def get_cursor(self, stream: str) -> int | None:
        """Read a stream's next durable sequence.

        Input/unit: stable ``stream`` key; sequence unit is one envelope.
        Deadline: adapter storage-read deadline in seconds.
        Idempotency/effect: repeatable and read-only. Return: next sequence or
        ``None`` when absent. Errors: ``StorageError`` or cancellation.
        """

    async def advance_cursor(self, stream: str, sequence: int) -> int:
        """Monotonically advance a stream cursor.

        Input/unit: stable ``stream`` key and non-negative envelope ``sequence``.
        Deadline: adapter storage-write deadline in seconds. Idempotency key:
        ``(stream, sequence)``. Effect: one durable monotonic cursor update.
        Return: committed next sequence. Errors: ``StorageError`` or cancellation.
        """

    async def pending_outbox(
        self,
        *,
        limit: int,
        after_sequence: int | None = None,
        stream: str | None = None,
        include_settled: bool = False,
    ) -> tuple[OutboxRecord, ...]:
        """Read one deterministic page of pending outbox rows.

        Input/unit: positive ``limit`` rows, optional exclusive envelope sequence,
        and optional stream key. Deadline: adapter storage-read deadline in seconds.
        Idempotency/effect: repeatable and read-only. Return: ordered tuple of at
        most ``limit`` records. Errors: ``StorageError`` or cancellation.
        """

    async def get_cloud_session(self) -> CloudSessionCheckpoint:
        """Read the singleton durable Cloud resume checkpoint.

        Input/unit: none; cursors are measured in envelopes.
        Deadline: adapter storage-read deadline in seconds.
        Idempotency/effect: repeatable and read-only.
        Return: ``CloudSessionCheckpoint``. Errors: ``StorageError`` or cancellation.
        """

    async def advance_cloud_outbound(self, expected_sequence: int) -> int:
        """CAS-advance the durable outbound cursor after a successful send.

        Input/unit: expected non-negative envelope sequence.
        Deadline: adapter storage-write deadline in seconds. Idempotency key:
        expected sequence. Effect: atomically increments the outbound cursor once.
        Return: new next sequence. Errors: sequence conflict, storage, cancellation.
        """

    async def advance_cloud_inbound(self, expected_sequence: int) -> int:
        """CAS-advance the inbound cursor after accepted processing.

        Input/unit: expected non-negative envelope sequence.
        Deadline: adapter storage-write deadline in seconds. Idempotency key:
        expected sequence. Effect: atomically increments the inbound cursor once.
        Return: new next sequence. Errors: sequence conflict, storage, cancellation.
        """

    async def begin_cloud_reconciliation(
        self,
        *,
        previous_connection_id: str,
        next_outbound_sequence: int,
        next_inbound_sequence: int,
    ) -> CloudSessionCheckpoint:
        """Persist authoritative cursors and begin reconciliation.

        Input/unit: canonical connection-id text and non-negative envelope cursors.
        Deadline: adapter storage-write deadline in seconds. Idempotency key:
        connection id plus both cursors. Effect: atomically replaces session cursors
        and marks reconciliation required. Return: committed checkpoint.
        Errors: validation, ``StorageError``, or cancellation.
        """

    async def complete_cloud_reconciliation(
        self,
        *,
        previous_connection_id: str,
    ) -> CloudSessionCheckpoint:
        """Complete durable Cloud reconciliation.

        Input/unit: canonical ``previous_connection_id`` text.
        Deadline: adapter storage-write deadline in seconds. Idempotency key:
        connection id. Effect: persists the id and clears reconciliation atomically.
        Return: committed checkpoint. Errors: validation, storage, or cancellation.
        """

    async def put_inbox(
        self,
        *,
        message_id: str,
        digest: str,
        payload: bytes,
        state: str = "received",
    ) -> InboxPutResult:
        """Persist one inbox message before returning.

        Input/unit: message-id key, digest text, payload bytes up to 262144, and
        state text. Deadline: adapter storage-write deadline in seconds.
        Idempotency key: ``message_id`` with matching digest/value.
        Effect: inserts at most one durable inbox row. Return: record plus inserted
        status. Errors: idempotency conflict, size, overload, storage, cancellation.
        """

    async def append_outbox(
        self,
        *,
        message_id: str,
        stream: str,
        sequence: int,
        payload: bytes,
    ) -> OutboxRecord:
        """Persist one sequenced outbox frame before returning.

        Input/unit: message-id, stream, non-negative envelope sequence, and payload
        bytes up to 262144. Deadline: adapter storage-write deadline in seconds.
        Idempotency key: ``message_id`` with matching stream/sequence/value.
        Effect: inserts at most one durable outbox row. Return: durable record.
        Errors: idempotency conflict, size, overload, storage, or cancellation.
        """

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
        """Persist one canonical Observer payload and exact envelope before send."""

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
        """Read one Observer fact by its authoritative runtime identity."""

    async def pending_observer_outbox(
        self,
        *,
        limit: int,
        after_sequence: int | None = None,
        include_settled: bool = False,
    ) -> tuple[ObserverOutboxRecord, ...]:
        """Read Observer frames in global sequence order for send or replay."""

    async def ack_observer_outbox(self, ack: StreamAck) -> ObserverOutboxRecord:
        """Settle only the Observer fact exactly identified by ``stream.ack``."""

    async def nack_observer_outbox(
        self,
        nack: StreamNack,
    ) -> ObserverOutboxRecord:
        """Retain and reject the fact exactly identified by ``stream.nack``."""

    async def append_session_catalog_outbox(
        self,
        **values: object,
    ) -> SessionCatalogOutboxRecord:
        """Atomically persist one catalog payload and its exact Cloud frame."""

    async def get_session_catalog_fact(
        self,
        **identity: object,
    ) -> SessionCatalogOutboxRecord | None:
        """Read the latest durable attempt for one authoritative catalog fact."""

    async def pending_session_catalog_outbox(
        self,
        *,
        limit: int,
        after_sequence: int | None = None,
        include_settled: bool = False,
    ) -> tuple[SessionCatalogOutboxRecord, ...]:
        """Read bounded catalog attempts in Connector sequence order."""

    async def retire_session_catalog_outbox(self) -> None:
        """Retire every pending catalog attempt after explicit capability loss."""
