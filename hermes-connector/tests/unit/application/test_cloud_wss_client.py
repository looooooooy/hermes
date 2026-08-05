from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from uuid import UUID

import pytest

from hermes_connector.adapters.cloud.codec import ConnectorProtocolCodec
from hermes_connector.application.cloud_wss_client import (
    CloudClientConfig,
    CloudWSSClient,
    ExponentialBackoff,
    LocalRuntimeAuthorityChanged,
    LocalRuntimeAuthorityUnavailable,
    ProtocolViolation,
    RequiredCapabilityUnavailable,
    ServerSessionDirective,
    UnsupportedCloudMessage,
)
from hermes_connector.domain.cloud_protocol import (
    ConnectorHeartbeat,
    ConnectorWelcome,
)
from hermes_connector.domain.cloud_session import CloudSessionState
from hermes_connector.domain.contract_messages import CloudEnvelope
from hermes_connector.domain.local_gateway import (
    LocalRuntimeAuthority,
    ProcessIdentityEvidence,
)
from hermes_connector.domain.observer import SessionEvent, SessionSnapshot
from hermes_connector.domain.owner_control import (
    OwnerControlRequest,
    OwnerControlResponse,
)
from hermes_connector.domain.session_catalog import (
    SessionCatalogEvent,
    SessionCatalogSnapshotPage,
)
from hermes_connector.domain.storage import (
    CloudSessionCheckpoint,
    CommandOutboxRecord,
    ObserverOutboxRecord,
    OutboxRecord,
    OwnerControlPutResult,
    OwnerControlRecord,
    SessionCatalogOutboxRecord,
    StorageSequenceConflict,
    TransportFrameRecord,
)
from hermes_connector.ports.cloud import CloudConnectionClosed

NOW = datetime(2026, 7, 30, 12, 0, 0, tzinfo=UTC)
CONNECTION_ID = UUID("22222222-2222-4222-8222-222222222222")
EPOCH_ID = UUID("85000000-0000-4000-8000-000000000001")
EPOCH_ID_2 = UUID("85000000-0000-4000-8000-000000000002")
MESSAGE_ID = UUID("44444444-4444-4444-8444-444444444444")
CONTRACTS = Path(__file__).parents[4] / "contracts"
_PROCESS_IDENTITY = ProcessIdentityEvidence(
    start_time_ns=1_000,
    executable_path=Path("/private/fixture/hermes-python"),
    executable_device=41,
    executable_inode=73,
)


def _runtime_authority(
    generation: str = "runtime-authoritative",
    *,
    required: tuple[str, ...] = ("session.observe",),
    optional: tuple[str, ...] = ("session.control",),
) -> LocalRuntimeAuthority:
    return LocalRuntimeAuthority(
        profile="default",
        runtime_generation=generation,
        instance_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        host_bundle_id="com.nousresearch.hermes",
        process_identity=_PROCESS_IDENTITY,
        required_capabilities=required,
        optional_capabilities=optional,
    )


_DEFAULT_RUNTIME_AUTHORITY = _runtime_authority()


class _Connection:
    def __init__(self, inbound: list[bytes]) -> None:
        self.inbound = inbound
        self.sent: list[bytes] = []
        self.closed: list[tuple[int, str]] = []
        self.send_timeouts: list[float] = []
        self.receive_timeouts: list[float] = []

    async def send(self, frame: bytes, *, timeout_seconds: float = 0) -> None:
        self.send_timeouts.append(timeout_seconds)
        self.sent.append(frame)

    async def receive(self, *, timeout_seconds: float = 0) -> bytes:
        self.receive_timeouts.append(timeout_seconds)
        return self.inbound.pop(0)

    async def close(
        self,
        *,
        code: int,
        reason: str,
        timeout_seconds: float = 0,
    ) -> None:
        assert timeout_seconds > 0
        self.closed.append((code, reason))


class _AuthorityChangingConnection(_Connection):
    def __init__(self, inbound: list[bytes], authority: _RuntimeAuthority) -> None:
        super().__init__(inbound)
        self._authority = authority
        self.change_after_send = False

    async def send(self, frame: bytes, *, timeout_seconds: float = 0) -> None:
        await super().send(frame, timeout_seconds=timeout_seconds)
        if self.change_after_send:
            self.change_after_send = False
            self._authority.current = _runtime_authority("runtime-changed-during-send")


class _RemoteClosedConnection(_Connection):
    def __init__(self, inbound: list[bytes], *, code: int, reason: str) -> None:
        super().__init__(inbound)
        self._remote_code = code
        self._remote_reason = reason

    async def receive(self, *, timeout_seconds: float = 0) -> bytes:
        if self.inbound:
            return await super().receive(timeout_seconds=timeout_seconds)
        raise CloudConnectionClosed(
            code=self._remote_code,
            reason=self._remote_reason,
        )


class _BlockingAfterWelcomeConnection(_Connection):
    def __init__(self, inbound: list[bytes]) -> None:
        super().__init__(inbound)
        self.receive_blocked = asyncio.Event()
        self.receive_cancelled = asyncio.Event()

    async def receive(self, *, timeout_seconds: float = 0) -> bytes:
        if self.inbound:
            return await super().receive(timeout_seconds=timeout_seconds)
        self.receive_blocked.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.receive_cancelled.set()
            raise
        raise AssertionError("unreachable")


class _Transport:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection
        self.tokens: list[str] = []

    async def connect(self, endpoint: str, *, token: str):
        assert endpoint == "wss://cloud.example.test/connector"
        self.tokens.append(token)
        return self.connection


class _TokenProvider:
    def __init__(self) -> None:
        self.cleared = False

    async def access_token(self) -> str:
        return "secret-token-must-not-be-logged"

    async def clear_access_token(self) -> None:
        self.cleared = True


class _RuntimeAuthority:
    def __init__(
        self,
        current: LocalRuntimeAuthority | None = _DEFAULT_RUNTIME_AUTHORITY,
    ) -> None:
        self.current = current
        self.calls = 0

    async def current_runtime_authority(self) -> LocalRuntimeAuthority | None:
        self.calls += 1
        return self.current


class _FailingClearTokenProvider(_TokenProvider):
    def __init__(self, failure: BaseException) -> None:
        super().__init__()
        self._failure = failure

    async def clear_access_token(self) -> None:
        raise self._failure


class _LifecycleTokenProvider(_TokenProvider):
    def __init__(self) -> None:
        super().__init__()
        self.lifecycle_signals: list[str] = []

    async def apply_lifecycle_signal(self, signal: str) -> None:
        self.lifecycle_signals.append(signal)
        await self.clear_access_token()


class _Storage:
    def __init__(self) -> None:
        self.cursors = {
            "cloud.connector.outbound": 0,
            "cloud.connector.inbound": 0,
        }
        self.pending_limits: list[int] = []
        self.pending_calls: list[tuple[int, int | None, str | None]] = []
        self.pending_records: tuple[OutboxRecord, ...] = ()
        self.inbox_writes = 0
        self.outbox_writes = 0
        self.previous_connection_id: str | None = None
        self.reconciliation_required = False
        self.events: list[str] = []
        self.transport_epoch_id: str | None = None
        self.runtime_generation: str | None = None
        self.fresh_epoch_required = True
        self.transport_records: list[TransportFrameRecord] = []
        self.transport_pending_calls: list[tuple[str, int, int | None]] = []
        self.owner_records: dict[str, OwnerControlRecord] = {}
        self.atomic_owner_writes = 0
        self.owner_record_calls: list[tuple[str, int]] = []
        self.pending_owner_calls: list[tuple[int, str | None, str | None]] = []

    async def get_cursor(self, stream: str) -> int | None:
        return self.cursors.get(stream)

    async def advance_cursor(self, stream: str, sequence: int) -> int:
        self.cursors[stream] = max(self.cursors.get(stream, 0), sequence)
        return self.cursors[stream]

    async def pending_outbox(
        self,
        *,
        limit: int,
        after_sequence: int | None = None,
        stream: str | None = None,
        include_settled: bool = False,
    ) -> tuple[OutboxRecord, ...]:
        self.pending_limits.append(limit)
        self.pending_calls.append((limit, after_sequence, stream))
        records = tuple(
            record
            for record in self.pending_records
            if (include_settled or record.state == "pending")
            and (after_sequence is None or record.sequence > after_sequence)
            and (stream is None or record.stream == stream)
        )
        return records[:limit]

    async def get_cloud_session(self) -> CloudSessionCheckpoint:
        return CloudSessionCheckpoint(
            previous_connection_id=self.previous_connection_id,
            next_outbound_sequence=self.cursors["cloud.connector.outbound"],
            next_inbound_sequence=self.cursors["cloud.connector.inbound"],
            reconciliation_required=self.reconciliation_required,
            transport_epoch_id=self.transport_epoch_id,
            runtime_generation=self.runtime_generation,
            fresh_epoch_required=self.fresh_epoch_required,
        )

    async def begin_transport_epoch(
        self,
        *,
        epoch_id: str,
        runtime_generation: str,
        previous_connection_id: str | None,
        next_outbound_sequence: int,
        next_inbound_sequence: int,
    ) -> CloudSessionCheckpoint:
        self.events.append("begin_transport_epoch")
        if self.transport_epoch_id != epoch_id:
            self.transport_records = [
                replace(record, state="retired")
                if record.state in {"staged", "sent"}
                else record
                for record in self.transport_records
            ]
        self.transport_epoch_id = epoch_id
        self.runtime_generation = runtime_generation
        self.fresh_epoch_required = False
        self.previous_connection_id = previous_connection_id
        self.cursors["cloud.connector.outbound"] = next_outbound_sequence
        self.cursors["cloud.connector.inbound"] = next_inbound_sequence
        return await self.get_cloud_session()

    async def reconcile_transport_epoch(
        self,
        *,
        epoch_id: str,
        previous_connection_id: str,
        next_outbound_sequence: int,
        next_inbound_sequence: int,
    ) -> CloudSessionCheckpoint:
        assert epoch_id == self.transport_epoch_id
        self.events.append("reconcile_transport_epoch")
        self.previous_connection_id = previous_connection_id
        self.cursors["cloud.connector.outbound"] = next_outbound_sequence
        self.cursors["cloud.connector.inbound"] = next_inbound_sequence
        self.transport_records = [
            replace(record, state="settled")
            if record.epoch_id == epoch_id
            and record.sequence < next_outbound_sequence
            and record.state in {"staged", "sent"}
            else replace(record, state="staged")
            if record.epoch_id == epoch_id
            and record.sequence >= next_outbound_sequence
            and record.state in {"sent", "settled"}
            else record
            for record in self.transport_records
        ]
        return await self.get_cloud_session()

    async def commit_transport_handshake(
        self,
        *,
        epoch_id: str,
        previous_connection_id: str,
        next_outbound_sequence: int,
        next_inbound_sequence: int,
    ) -> CloudSessionCheckpoint:
        assert epoch_id == self.transport_epoch_id
        self.events.append("commit_transport_handshake")
        self.previous_connection_id = previous_connection_id
        self.cursors["cloud.connector.outbound"] = next_outbound_sequence
        self.cursors["cloud.connector.inbound"] = next_inbound_sequence
        return await self.get_cloud_session()

    async def stage_transport_frame(self, **values: object) -> TransportFrameRecord:
        self.events.append("stage_transport_frame")
        for record in self.transport_records:
            if (
                record.epoch_id == values["epoch_id"]
                and record.business_kind == values["business_kind"]
                and record.business_key == values["business_key"]
                and record.business_revision == values["business_revision"]
            ):
                return record
        record = TransportFrameRecord(
            message_id=str(values["message_id"]),
            epoch_id=str(values["epoch_id"]),
            sequence=int(values["sequence"]),
            message_type=str(values["message_type"]),
            business_kind=str(values["business_kind"]),
            business_key=str(values["business_key"]),
            business_revision=int(values["business_revision"]),
            runtime_generation=(
                str(values["runtime_generation"])
                if values["runtime_generation"] is not None
                else None
            ),
            frame=bytes(values["frame"]),
            state="staged",
            created_at="now",
            updated_at="now",
            settled_at=None,
        )
        self.transport_records.append(record)
        return record

    async def mark_transport_sent(
        self,
        *,
        epoch_id: str,
        sequence: int,
    ) -> TransportFrameRecord:
        self.events.append("mark_transport_sent")
        assert self.cursors["cloud.connector.outbound"] == sequence
        for index, record in enumerate(self.transport_records):
            if record.epoch_id == epoch_id and record.sequence == sequence:
                sent = replace(record, state="sent")
                self.transport_records[index] = sent
                self.cursors["cloud.connector.outbound"] += 1
                return sent
        raise StorageSequenceConflict()

    async def pending_transport_frames(
        self,
        *,
        epoch_id: str,
        limit: int,
        after_sequence: int | None = None,
    ) -> tuple[TransportFrameRecord, ...]:
        self.transport_pending_calls.append((epoch_id, limit, after_sequence))
        records = tuple(
            record
            for record in self.transport_records
            if record.epoch_id == epoch_id
            and record.state in {"staged", "sent"}
            and (after_sequence is None or record.sequence > after_sequence)
        )
        return tuple(sorted(records, key=lambda record: record.sequence))[:limit]

    async def settle_transport_cursor(
        self,
        *,
        epoch_id: str,
        next_sequence: int,
    ) -> tuple[TransportFrameRecord, ...]:
        settled: list[TransportFrameRecord] = []
        for index, record in enumerate(self.transport_records):
            if (
                record.epoch_id == epoch_id
                and record.sequence < next_sequence
                and record.state in {"staged", "sent"}
            ):
                changed = replace(record, state="settled")
                self.transport_records[index] = changed
                settled.append(changed)
                if changed.business_kind == "control.response":
                    owner = self.owner_records.get(changed.business_key)
                    if owner is not None:
                        self.owner_records[changed.business_key] = replace(
                            owner,
                            transport_received=True,
                        )
        return tuple(settled)

    async def put_owner_control(self, **values: object) -> OwnerControlPutResult:
        self.events.append("put_owner_control")
        request_id = str(values["request_id"])
        existing = self.owner_records.get(request_id)
        if existing is not None:
            return OwnerControlPutResult(existing, inserted=False)
        record = OwnerControlRecord(
            request_id=request_id,
            request_digest=str(values["request_digest"]),
            control_transport_id=str(values["control_transport_id"]),
            operation=str(values["operation"]),
            request_payload=bytes(values["request_payload"]),
            scope_payload=bytes(values["scope_payload"]),
            response_payload=None,
            state="received",
            response_revision=1,
            transport_received=False,
            created_at="now",
            updated_at="now",
            completed_at=None,
        )
        self.owner_records[request_id] = record
        return OwnerControlPutResult(record, inserted=True)

    async def put_owner_control_and_advance_inbound(
        self,
        *,
        expected_sequence: int,
        **values: object,
    ) -> OwnerControlPutResult:
        self.atomic_owner_writes += 1
        result = await self.put_owner_control(**values)
        await self.advance_cloud_inbound(expected_sequence)
        return result

    async def get_owner_control(self, request_id: str) -> OwnerControlRecord | None:
        return self.owner_records.get(request_id)

    async def claim_owner_control(self, request_id: str) -> bool:
        self.events.append("claim_owner_control")
        record = self.owner_records.get(request_id)
        if record is None or record.state != "received":
            return False
        self.owner_records[request_id] = replace(record, state="executing")
        return True

    async def complete_owner_control(
        self,
        *,
        request_id: str,
        response_payload: bytes,
        response_revision: int = 1,
    ) -> OwnerControlRecord:
        self.events.append("complete_owner_control")
        record = self.owner_records[request_id]
        completed = replace(
            record,
            response_payload=response_payload,
            response_revision=response_revision,
            state="completed",
            completed_at="now",
        )
        self.owner_records[request_id] = completed
        return completed

    async def mark_owner_control_effect_unknown(
        self,
        request_id: str,
    ) -> OwnerControlRecord:
        self.events.append("owner_effect_unknown")
        record = self.owner_records[request_id]
        if record.state in {"completed", "effect_unknown"}:
            return record
        payload = json.dumps(
            {
                "request_id": record.request_id,
                "control_transport_id": record.control_transport_id,
                "operation": record.operation,
                "state": "unknown",
                "completed_at": "2026-07-30T12:00:00Z",
                "error": {"code": 4307, "reason": "effect_unknown"},
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        unknown = replace(
            record,
            state="effect_unknown",
            response_payload=payload,
            completed_at="now",
        )
        self.owner_records[request_id] = unknown
        return unknown

    async def pending_owner_control(
        self,
        *,
        limit: int,
        after_created_at: str | None = None,
        after_request_id: str | None = None,
    ) -> tuple[OwnerControlRecord, ...]:
        self.pending_owner_calls.append((limit, after_created_at, after_request_id))
        records = sorted(
            (
                record
                for record in self.owner_records.values()
                if record.state in {"completed", "effect_unknown"}
                and not record.transport_received
            ),
            key=lambda record: (record.created_at, record.request_id),
        )
        if after_created_at is not None and after_request_id is not None:
            records = [
                record
                for record in records
                if (record.created_at, record.request_id)
                > (after_created_at, after_request_id)
            ]
        return tuple(records[:limit])

    async def owner_control_records(
        self,
        *,
        state: str,
        limit: int,
    ) -> tuple[OwnerControlRecord, ...]:
        self.owner_record_calls.append((state, limit))
        return tuple(
            record for record in self.owner_records.values() if record.state == state
        )[:limit]

    async def advance_cloud_outbound(self, expected_sequence: int) -> int:
        current = self.cursors["cloud.connector.outbound"]
        if current != expected_sequence:
            raise StorageSequenceConflict()
        self.cursors["cloud.connector.outbound"] = current + 1
        return current + 1

    async def advance_cloud_inbound(self, expected_sequence: int) -> int:
        self.events.append("advance_inbound")
        current = self.cursors["cloud.connector.inbound"]
        if current != expected_sequence:
            raise StorageSequenceConflict()
        self.cursors["cloud.connector.inbound"] = current + 1
        return current + 1

    async def begin_cloud_reconciliation(
        self,
        *,
        previous_connection_id: str,
        next_outbound_sequence: int,
        next_inbound_sequence: int,
    ) -> CloudSessionCheckpoint:
        self.previous_connection_id = previous_connection_id
        self.cursors["cloud.connector.outbound"] = next_outbound_sequence
        self.cursors["cloud.connector.inbound"] = next_inbound_sequence
        self.reconciliation_required = True
        return await self.get_cloud_session()

    async def complete_cloud_reconciliation(
        self,
        *,
        previous_connection_id: str,
    ) -> CloudSessionCheckpoint:
        self.previous_connection_id = previous_connection_id
        self.reconciliation_required = False
        return await self.get_cloud_session()

    async def put_inbox(self, **_kwargs):
        self.inbox_writes += 1
        raise AssertionError("session traffic must not enter the business inbox")

    async def append_outbox(self, **_kwargs):
        self.outbox_writes += 1
        raise AssertionError("session traffic must not enter the business outbox")


class _CommandLane:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.acknowledged: list[tuple[str, str]] = []
        self.processed = False
        command_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        self.messages = (
            CommandOutboxRecord(
                command_id=command_id,
                message_type="command.receipt",
                payload=(
                    CONTRACTS / "fixtures/valid/command-receipt-payload.json"
                ).read_bytes(),
                revision=1,
            ),
            CommandOutboxRecord(
                command_id=command_id,
                message_type="command.result",
                payload=(
                    CONTRACTS / "fixtures/valid/command-result-payload.json"
                ).read_bytes(),
                revision=2,
            ),
        )

    async def process(self, _envelope: CloudEnvelope) -> object:
        self.processed = True
        self.events.append("process_command")
        return object()

    async def recover_inflight(self, *, limit: int) -> int:
        assert limit > 0
        self.events.append("recover_commands")
        return 0

    async def pending_cloud_messages(
        self,
        *,
        limit: int,
        after_created_at: str | None = None,
        after_command_id: str | None = None,
        after_message_type: str | None = None,
    ) -> tuple[CommandOutboxRecord, ...]:
        if not self.processed:
            return ()
        acknowledged = set(self.acknowledged)
        pending = tuple(
            message
            for message in self.messages
            if (message.command_id, message.message_type) not in acknowledged
            and (
                after_created_at is None
                or (
                    message.created_at,
                    message.command_id,
                    message.message_type,
                )
                > (
                    after_created_at,
                    after_command_id or "",
                    after_message_type or "",
                )
            )
        )
        return pending[:limit]

    async def acknowledge_cloud_message(
        self,
        *,
        command_id: str,
        message_type: str,
    ) -> bool:
        self.acknowledged.append((command_id, message_type))
        return True


class _PagedCommandLane:
    def __init__(
        self,
        codec: ConnectorProtocolCodec,
        *,
        executing: int,
        messages: int,
    ) -> None:
        self.executing = executing
        self.recover_calls: list[int] = []
        self.pending_calls: list[tuple[int, str | None, str | None, str | None]] = []
        receipt = codec.decode_command_receipt(
            (CONTRACTS / "fixtures/valid/command-receipt-payload.json").read_bytes()
        )
        self.messages = tuple(
            CommandOutboxRecord(
                command_id=f"aaaaaaaa-aaaa-4aaa-8aaa-{index:012d}",
                message_type="command.receipt",
                payload=codec.encode_command_receipt(
                    replace(
                        receipt,
                        command_id=UUID(f"aaaaaaaa-aaaa-4aaa-8aaa-{index:012d}"),
                    )
                ),
                revision=1,
                created_at=f"2026-07-30T12:00:{index:02d}Z",
            )
            for index in range(1, messages + 1)
        )

    async def process(self, _envelope: CloudEnvelope) -> object:
        return object()

    async def recover_inflight(self, *, limit: int) -> int:
        self.recover_calls.append(limit)
        recovered = min(limit, self.executing)
        self.executing -= recovered
        return recovered

    async def pending_cloud_messages(
        self,
        *,
        limit: int,
        after_created_at: str | None = None,
        after_command_id: str | None = None,
        after_message_type: str | None = None,
    ) -> tuple[CommandOutboxRecord, ...]:
        self.pending_calls.append(
            (
                limit,
                after_created_at,
                after_command_id,
                after_message_type,
            )
        )
        records = self.messages
        if (
            after_created_at is not None
            and after_command_id is not None
            and after_message_type is not None
        ):
            records = tuple(
                record
                for record in records
                if (record.created_at, record.command_id, record.message_type)
                > (after_created_at, after_command_id, after_message_type)
            )
        return records[:limit]

    async def acknowledge_cloud_message(
        self,
        *,
        command_id: str,
        message_type: str,
    ) -> bool:
        return True


class _ObserverOutboundLane:
    def __init__(self, codec: ConnectorProtocolCodec, storage: _Storage) -> None:
        self.codec = codec
        self.storage = storage
        self.records: list[ObserverOutboxRecord] = []
        self.transport_notifications = 0
        self.acks: list[object] = []
        self.nacks: list[object] = []
        self.forced_snapshot_attempts: list[bool] = []

    async def stage_snapshot(
        self,
        snapshot: SessionSnapshot,
        *,
        connector_sequence: int,
        force_new_attempt: bool = False,
        transport_epoch_id: str | None = None,
    ) -> ObserverOutboxRecord:
        self.forced_snapshot_attempts.append(force_new_attempt)
        return self._stage(
            "session.snapshot",
            connector_sequence,
            self.codec.session_snapshot_payload(snapshot),
            snapshot.profile,
            snapshot.session_key,
            snapshot.runtime_generation,
            snapshot.runtime_session_id,
            snapshot.event_sequence,
            transport_epoch_id,
        )

    async def stage_event(
        self,
        event: SessionEvent,
        *,
        connector_sequence: int,
        transport_epoch_id: str | None = None,
    ) -> ObserverOutboxRecord:
        return self._stage(
            "session.event",
            connector_sequence,
            self.codec.session_event_payload(event),
            event.profile,
            event.session_key,
            event.runtime_generation,
            event.session_id,
            event.event_sequence,
            transport_epoch_id,
        )

    def _stage(
        self,
        message_type: str,
        sequence: int,
        payload: MappingProxyType | object,
        profile: str,
        session_key: str,
        generation: str,
        runtime_session_id: str,
        event_sequence: int,
        transport_epoch_id: str | None,
    ) -> ObserverOutboxRecord:
        message_id = UUID("83000000-0000-4000-8000-000000000001")
        assert isinstance(payload, MappingProxyType)
        frame = self.codec.encode_envelope(
            CloudEnvelope(
                contract_version=1,
                message_id=message_id,
                message_type=message_type,
                tenant_id="tenant-test",
                device_id="device-test",
                sequence=sequence,
                sent_at=NOW,
                payload=payload,
                idempotency_key=str(message_id),
            )
        )
        encoded_payload = frame
        record = ObserverOutboxRecord(
            message_id=str(message_id),
            payload_digest="a" * 64,
            connector_sequence=sequence,
            message_type=message_type,
            profile=profile,
            session_key=session_key,
            runtime_generation=generation,
            runtime_session_id=runtime_session_id,
            event_sequence=event_sequence,
            payload=encoded_payload,
            frame=frame,
            state="pending",
            transport_epoch_id=transport_epoch_id,
        )
        self.records.append(record)
        assert transport_epoch_id is not None
        self.storage.transport_records.append(
            TransportFrameRecord(
                message_id=str(message_id),
                epoch_id=transport_epoch_id,
                sequence=sequence,
                message_type=message_type,
                business_kind="observer",
                business_key=str(message_id),
                business_revision=event_sequence,
                runtime_generation=generation,
                frame=frame,
                state="staged",
                created_at="now",
                updated_at="now",
                settled_at=None,
            )
        )
        return record

    async def pending(
        self,
        *,
        limit: int,
        after_sequence: int | None = None,
        include_settled: bool = False,
    ) -> tuple[ObserverOutboxRecord, ...]:
        return tuple(
            record
            for record in self.records
            if (include_settled or record.state == "pending")
            and (after_sequence is None or record.connector_sequence > after_sequence)
        )[:limit]

    async def transport_sent(self, _record: ObserverOutboxRecord) -> None:
        self.transport_notifications += 1

    async def acknowledge(self, ack: object) -> ObserverOutboxRecord:
        self.acks.append(ack)
        return self.records[0]

    async def reject(self, nack: object) -> ObserverOutboxRecord:
        self.nacks.append(nack)
        return self.records[0]


class _ObserverIntentLane:
    def __init__(self) -> None:
        self.opened: list[object] = []
        self.closed: list[object] = []
        self.recoveries: list[object] = []
        self.acknowledgements: list[object] = []

    def raise_if_failed(self) -> None:
        return None

    async def open(self, intent: object) -> None:
        self.opened.append(intent)

    async def close(self, intent: object) -> None:
        self.closed.append(intent)

    async def recover(self, nack: object) -> None:
        self.recoveries.append(nack)

    async def acknowledge(self, ack: object) -> None:
        self.acknowledgements.append(ack)

    async def shutdown(self) -> None:
        return None


class _SessionCatalogOutboundLane:
    def __init__(self, codec: ConnectorProtocolCodec, storage: _Storage) -> None:
        self.codec = codec
        self.storage = storage
        self.records: list[SessionCatalogOutboxRecord] = []
        self.transport_notifications = 0
        self.acks: list[object] = []
        self.nacks: list[object] = []
        self.retired_pending = 0

    async def stage_snapshot_page(
        self,
        page: SessionCatalogSnapshotPage,
        *,
        connector_sequence: int,
        force_new_attempt: bool = False,
        transport_epoch_id: str | None = None,
    ) -> SessionCatalogOutboxRecord:
        del force_new_attempt
        return self._stage(
            message_type="session.catalog.snapshot.page",
            connector_sequence=connector_sequence,
            transport_epoch_id=transport_epoch_id,
            profile=page.profile,
            runtime_generation=page.runtime_generation,
            snapshot_id=str(page.snapshot_id),
            catalog_revision=page.catalog_revision,
            page_index=page.page_index,
            is_last=page.is_last,
            catalog_sequence=None,
            payload=self.codec.session_catalog_snapshot_page_payload(page),
        )

    async def stage_event(
        self,
        event: SessionCatalogEvent,
        *,
        connector_sequence: int,
        force_new_attempt: bool = False,
        transport_epoch_id: str | None = None,
    ) -> SessionCatalogOutboxRecord:
        del force_new_attempt
        return self._stage(
            message_type="session.catalog.event",
            connector_sequence=connector_sequence,
            transport_epoch_id=transport_epoch_id,
            profile=event.profile,
            runtime_generation=event.runtime_generation,
            snapshot_id=None,
            catalog_revision=None,
            page_index=None,
            is_last=None,
            catalog_sequence=event.catalog_sequence,
            payload=self.codec.session_catalog_event_payload(event),
        )

    def _stage(
        self,
        *,
        message_type: str,
        connector_sequence: int,
        transport_epoch_id: str | None,
        profile: str,
        runtime_generation: str,
        snapshot_id: str | None,
        catalog_revision: int | None,
        page_index: int | None,
        is_last: bool | None,
        catalog_sequence: int | None,
        payload: object,
    ) -> SessionCatalogOutboxRecord:
        assert isinstance(payload, MappingProxyType)
        message_id = UUID("89000000-0000-4000-8000-000000000001")
        frame = self.codec.encode_envelope(
            CloudEnvelope(
                contract_version=1,
                message_id=message_id,
                message_type=message_type,
                tenant_id="tenant-test",
                device_id="device-test",
                sequence=connector_sequence,
                sent_at=NOW,
                payload=payload,
                idempotency_key=str(message_id),
            )
        )
        record = SessionCatalogOutboxRecord(
            message_id=str(message_id),
            payload_digest="a" * 64,
            connector_sequence=connector_sequence,
            message_type=message_type,
            profile=profile,
            runtime_generation=runtime_generation,
            snapshot_id=snapshot_id,
            catalog_revision=catalog_revision,
            page_index=page_index,
            is_last=is_last,
            catalog_sequence=catalog_sequence,
            payload=frame,
            frame=frame,
            state="pending",
            transport_epoch_id=transport_epoch_id,
        )
        self.records.append(record)
        assert transport_epoch_id is not None
        self.storage.transport_records.append(
            TransportFrameRecord(
                message_id=str(message_id),
                epoch_id=transport_epoch_id,
                sequence=connector_sequence,
                message_type=message_type,
                business_kind="session_catalog",
                business_key=str(message_id),
                business_revision=(
                    page_index if page_index is not None else catalog_sequence or 0
                ),
                runtime_generation=runtime_generation,
                frame=frame,
                state="staged",
                created_at="now",
                updated_at="now",
                settled_at=None,
            )
        )
        return record

    async def pending(self, **_values: object):
        return tuple(record for record in self.records if record.state == "pending")

    async def transport_sent(self, _record: SessionCatalogOutboxRecord) -> None:
        self.transport_notifications += 1

    async def acknowledge(self, ack: object) -> SessionCatalogOutboxRecord:
        self.acks.append(ack)
        return self.records[0]

    async def reject(self, nack: object) -> SessionCatalogOutboxRecord:
        self.nacks.append(nack)
        return self.records[0]

    async def retire_pending(self) -> None:
        self.retired_pending += 1
        self.records = [
            replace(record, state="retired")
            if record.state == "pending"
            else record
            for record in self.records
        ]
        self.storage.transport_records = [
            replace(record, state="retired", settled_at="now")
            if record.business_kind == "session_catalog"
            and record.state in {"staged", "sent"}
            else record
            for record in self.storage.transport_records
        ]


class _SessionCatalogSync:
    def __init__(self) -> None:
        self.acks: list[object] = []
        self.nacks: list[object] = []

    async def acknowledge(self, ack: object) -> None:
        self.acks.append(ack)

    async def recover(self, nack: object) -> None:
        self.nacks.append(nack)

class _PublishingRecoveryIntentLane(_ObserverIntentLane):
    def __init__(self, snapshot: SessionSnapshot) -> None:
        super().__init__()
        self.snapshot = snapshot
        self.client: CloudWSSClient | None = None

    async def recover(self, nack: object) -> None:
        await super().recover(nack)
        assert self.client is not None
        await self.client.publish_observer_snapshot(
            self.snapshot,
            force_new_attempt=True,
        )


def _config(
    *,
    negotiation_timeout_seconds: float = 10.0,
    io_timeout_seconds: float = 0.05,
) -> CloudClientConfig:
    return CloudClientConfig(
        endpoint="wss://cloud.example.test/connector",
        tenant_id="tenant-test",
        device_id="device-test",
        connector_instance_id=UUID("11111111-1111-4111-8111-111111111111"),
        connector_version="1.0.0",
        negotiation_timeout_seconds=negotiation_timeout_seconds,
        io_timeout_seconds=io_timeout_seconds,
    )


def _envelope(
    codec: ConnectorProtocolCodec,
    *,
    message_type: str,
    sequence: int,
    payload: dict[str, object],
    idempotency_key: str | None = None,
) -> bytes:
    return codec.encode_envelope(
        CloudEnvelope(
            contract_version=1,
            message_id=UUID("33333333-3333-4333-8333-333333333333"),
            message_type=message_type,
            tenant_id="tenant-test",
            device_id="device-test",
            sequence=sequence,
            sent_at=NOW,
            payload=MappingProxyType(payload),
            idempotency_key=idempotency_key,
        )
    )


def _welcome_frame(
    codec: ConnectorProtocolCodec,
    *,
    accepted: tuple[str, ...] = ("session.observe", "session.control"),
    unavailable: tuple[str, ...] = (),
    resume_decision: str = "fresh",
    next_connector_sequence: int | None = None,
    next_cloud_sequence: int | None = None,
    max_in_flight: int = 4,
    sequence: int = 0,
) -> bytes:
    if next_connector_sequence is None:
        next_connector_sequence = 1 if resume_decision == "resumed" else 0
    if next_cloud_sequence is None:
        next_cloud_sequence = 1 if resume_decision == "resumed" else 0
    welcome = ConnectorWelcome(
        connection_id=CONNECTION_ID,
        server_generation="cloud-test",
        server_time=NOW,
        accepted_capabilities=accepted,
        unavailable_optional_capabilities=unavailable,
        resume_decision=resume_decision,
        next_connector_sequence=next_connector_sequence,
        next_cloud_sequence=next_cloud_sequence,
        heartbeat_interval_ms=20_000,
        max_in_flight=max_in_flight,
    )
    return _envelope(
        codec,
        message_type="connector.welcome",
        sequence=sequence,
        payload=json.loads(codec.encode_welcome(welcome)),
    )


def _client(
    connection: _Connection,
    storage: _Storage,
    *,
    config: CloudClientConfig | None = None,
    token_provider: _TokenProvider | None = None,
    command_lane: _CommandLane | None = None,
    owner_control_lane: object | None = None,
    runtime_authority: _RuntimeAuthority | None = None,
    observer_outbound_lane: object | None = None,
    observer_intent_lane: object | None = None,
    session_catalog_outbound_lane: object | None = None,
    session_catalog_sync: object | None = None,
    message_id: UUID = MESSAGE_ID,
    epoch_id: UUID = EPOCH_ID,
    message_id_factory: Callable[[], UUID] | None = None,
) -> CloudWSSClient:
    return CloudWSSClient(
        config=config or _config(),
        transport=_Transport(connection),
        token_provider=token_provider or _TokenProvider(),
        storage=storage,
        codec=ConnectorProtocolCodec(),
        runtime_authority=runtime_authority or _RuntimeAuthority(),
        utc_now=lambda: NOW,
        message_id_factory=message_id_factory or (lambda: message_id),
        epoch_id_factory=lambda: epoch_id,
        command_lane=command_lane,
        owner_control_lane=owner_control_lane,
        observer_outbound_lane=observer_outbound_lane,
        observer_intent_lane=observer_intent_lane,
        session_catalog_outbound_lane=session_catalog_outbound_lane,
        session_catalog_sync=session_catalog_sync,
    )


@pytest.mark.asyncio
async def test_client_negotiates_capabilities_and_authoritative_window() -> None:
    codec = ConnectorProtocolCodec()
    connection = _Connection([_welcome_frame(codec)])
    storage = _Storage()
    client = _client(connection, storage)

    await client.start()

    assert client.state is CloudSessionState.ACTIVE
    assert client.connection_id == CONNECTION_ID
    assert client.max_in_flight == 4
    assert storage.pending_limits == []
    hello_envelope = codec.decode_envelope(connection.sent[0])
    hello = codec.decode_hello_payload(hello_envelope.payload)
    assert hello.required_capabilities == ("session.observe",)
    assert hello.optional_capabilities == ("session.control",)
    assert hello.resume.next_outbound_sequence == 0
    assert storage.cursors["cloud.connector.outbound"] == 0
    assert storage.cursors["cloud.connector.inbound"] == 0
    assert storage.previous_connection_id == str(CONNECTION_ID)
    assert "commit_transport_handshake" in storage.events
    assert all(timeout > 0 for timeout in connection.send_timeouts)
    assert all(timeout > 0 for timeout in connection.receive_timeouts)


@pytest.mark.asyncio
async def test_initial_fresh_handshake_advance_commits_without_business_journal_gap() -> (
    None
):
    codec = ConnectorProtocolCodec()
    storage = _Storage()
    connection = _Connection(
        [
            _welcome_frame(
                codec,
                resume_decision="fresh",
                next_connector_sequence=1,
                next_cloud_sequence=1,
            )
        ]
    )
    client = _client(connection, storage)

    await client.start()

    hello = codec.decode_hello_payload(
        codec.decode_envelope(connection.sent[0]).payload
    )
    assert hello.resume.mode == "fresh"
    assert hello.resume.previous_connection_id is None
    assert hello.resume.next_outbound_sequence == 0
    assert hello.resume.next_inbound_sequence == 0
    assert storage.cursors == {
        "cloud.connector.outbound": 1,
        "cloud.connector.inbound": 1,
    }
    assert storage.transport_records == []
    assert storage.events.count("commit_transport_handshake") == 1


@pytest.mark.asyncio
async def test_existing_epoch_resumed_advances_handshake_without_journal_gap() -> None:
    codec = ConnectorProtocolCodec()
    storage = _Storage()
    storage.transport_epoch_id = str(EPOCH_ID)
    storage.runtime_generation = _DEFAULT_RUNTIME_AUTHORITY.runtime_generation
    storage.fresh_epoch_required = False
    storage.previous_connection_id = str(CONNECTION_ID)
    storage.cursors["cloud.connector.outbound"] = 2
    storage.cursors["cloud.connector.inbound"] = 3
    connection = _Connection(
        [
            _welcome_frame(
                codec,
                resume_decision="resumed",
                next_connector_sequence=3,
                next_cloud_sequence=4,
                sequence=3,
            )
        ]
    )
    client = _client(connection, storage)

    await client.start()

    hello = codec.decode_hello_payload(
        codec.decode_envelope(connection.sent[0]).payload
    )
    assert hello.resume.mode == "resume"
    assert hello.resume.next_outbound_sequence == 2
    assert hello.resume.next_inbound_sequence == 3
    assert storage.transport_epoch_id == str(EPOCH_ID)
    assert storage.cursors["cloud.connector.outbound"] == 3
    assert storage.cursors["cloud.connector.inbound"] == 4
    assert storage.events.count("commit_transport_handshake") == 1
    assert "reconcile_transport_epoch" not in storage.events


@pytest.mark.asyncio
async def test_heartbeat_is_journaled_exactly_before_transport_send() -> None:
    codec = ConnectorProtocolCodec()
    connection = _Connection([_welcome_frame(codec)])
    storage = _Storage()
    client = _client(connection, storage)
    await client.start()

    await client.send_heartbeat()

    assert len(storage.transport_records) == 1
    record = storage.transport_records[0]
    assert record.message_type == "connector.heartbeat"
    assert record.state == "sent"
    assert record.frame == connection.sent[1]
    assert storage.events.index("stage_transport_frame") < storage.events.index(
        "mark_transport_sent"
    )


@pytest.mark.asyncio
async def test_reset_required_keeps_epoch_and_replays_exact_journal_frame() -> None:
    codec = ConnectorProtocolCodec()
    exact = b"exact-old-frame"
    storage = _Storage()
    storage.transport_epoch_id = str(EPOCH_ID)
    storage.runtime_generation = _DEFAULT_RUNTIME_AUTHORITY.runtime_generation
    storage.fresh_epoch_required = False
    storage.previous_connection_id = str(CONNECTION_ID)
    storage.cursors["cloud.connector.outbound"] = 1
    storage.transport_records.append(
        TransportFrameRecord(
            message_id="86000000-0000-4000-8000-000000000001",
            epoch_id=str(EPOCH_ID),
            sequence=0,
            message_type="connector.heartbeat",
            business_kind="heartbeat",
            business_key="heartbeat-0",
            business_revision=0,
            runtime_generation=_DEFAULT_RUNTIME_AUTHORITY.runtime_generation,
            frame=exact,
            state="sent",
            created_at="now",
            updated_at="now",
            settled_at=None,
        )
    )
    connection = _Connection(
        [
            _welcome_frame(
                codec,
                resume_decision="reset_required",
                next_connector_sequence=0,
                next_cloud_sequence=0,
            )
        ]
    )
    client = _client(connection, storage)

    await client.start()

    assert storage.transport_epoch_id == str(EPOCH_ID)
    assert "begin_transport_epoch" not in storage.events
    assert connection.sent[1] == exact


@pytest.mark.asyncio
async def test_cloud_fresh_rotates_epoch_and_does_not_send_old_attempt() -> None:
    codec = ConnectorProtocolCodec()
    old_epoch = "85000000-0000-4000-8000-000000000099"
    storage = _Storage()
    storage.transport_epoch_id = old_epoch
    storage.runtime_generation = _DEFAULT_RUNTIME_AUTHORITY.runtime_generation
    storage.fresh_epoch_required = False
    storage.previous_connection_id = str(CONNECTION_ID)
    storage.transport_records.append(
        TransportFrameRecord(
            message_id="86000000-0000-4000-8000-000000000099",
            epoch_id=old_epoch,
            sequence=0,
            message_type="session.event",
            business_kind="observer",
            business_key="83000000-0000-4000-8000-000000000099",
            business_revision=0,
            runtime_generation="old-runtime",
            frame=b"must-not-send",
            state="staged",
            created_at="now",
            updated_at="now",
            settled_at=None,
        )
    )
    connection = _Connection([_welcome_frame(codec, resume_decision="fresh")])
    client = _client(connection, storage)

    await client.start()

    assert storage.transport_epoch_id == str(EPOCH_ID)
    assert storage.transport_records[0].state == "retired"
    assert b"must-not-send" not in connection.sent


@pytest.mark.asyncio
async def test_cloud_hello_uses_ready_local_runtime_authority() -> None:
    codec = ConnectorProtocolCodec()
    connection = _Connection(
        [
            _welcome_frame(
                codec,
                accepted=("session.observe",),
                unavailable=(),
            )
        ]
    )
    authority = _RuntimeAuthority(
        _runtime_authority(
            "runtime-from-local-hermes",
            optional=(),
        )
    )
    client = CloudWSSClient(
        config=_config(),
        transport=_Transport(connection),
        token_provider=_TokenProvider(),
        storage=_Storage(),
        codec=codec,
        runtime_authority=authority,
        utc_now=lambda: NOW,
        message_id_factory=lambda: UUID("44444444-4444-4444-8444-444444444444"),
    )

    await client.start()

    hello_envelope = codec.decode_envelope(connection.sent[0])
    hello = codec.decode_hello_payload(hello_envelope.payload)
    assert hello.runtime_generation == "runtime-from-local-hermes"
    assert hello.required_capabilities == ("session.observe",)
    assert hello.optional_capabilities == ()
    assert authority.calls > 0


@pytest.mark.asyncio
async def test_cloud_fails_before_network_when_local_runtime_is_not_ready() -> None:
    codec = ConnectorProtocolCodec()
    connection = _Connection([_welcome_frame(codec)])
    transport = _Transport(connection)
    client = CloudWSSClient(
        config=_config(),
        transport=transport,
        token_provider=_TokenProvider(),
        storage=_Storage(),
        codec=codec,
        runtime_authority=_RuntimeAuthority(None),
    )

    with pytest.raises(LocalRuntimeAuthorityUnavailable):
        await client.start()

    assert transport.tokens == []
    assert connection.sent == []


@pytest.mark.asyncio
async def test_generation_change_closes_stale_cloud_session_before_next_send() -> None:
    codec = ConnectorProtocolCodec()
    connection = _Connection([_welcome_frame(codec)])
    authority = _RuntimeAuthority()
    client = CloudWSSClient(
        config=_config(),
        transport=_Transport(connection),
        token_provider=_TokenProvider(),
        storage=_Storage(),
        codec=codec,
        runtime_authority=authority,
        utc_now=lambda: NOW,
    )
    await client.start()
    sent_before_change = len(connection.sent)
    authority.current = _runtime_authority("runtime-replaced")

    with pytest.raises(LocalRuntimeAuthorityChanged):
        await client.send_heartbeat()

    assert len(connection.sent) == sent_before_change
    assert connection.closed[-1] == (1012, "local_runtime_changed")
    assert client.state is CloudSessionState.DISCONNECTED


@pytest.mark.asyncio
async def test_runtime_loss_closes_cloud_session_before_next_send() -> None:
    codec = ConnectorProtocolCodec()
    connection = _Connection([_welcome_frame(codec)])
    authority = _RuntimeAuthority()
    client = CloudWSSClient(
        config=_config(),
        transport=_Transport(connection),
        token_provider=_TokenProvider(),
        storage=_Storage(),
        codec=codec,
        runtime_authority=authority,
        utc_now=lambda: NOW,
    )
    await client.start()
    sent_before_loss = len(connection.sent)
    authority.current = None

    with pytest.raises(LocalRuntimeAuthorityUnavailable):
        await client.send_heartbeat()

    assert len(connection.sent) == sent_before_loss
    assert connection.closed[-1] == (1012, "local_runtime_unavailable")
    assert client.state is CloudSessionState.DISCONNECTED


@pytest.mark.asyncio
async def test_generation_change_during_send_does_not_advance_durable_cursor() -> None:
    codec = ConnectorProtocolCodec()
    authority = _RuntimeAuthority()
    connection = _AuthorityChangingConnection(
        [_welcome_frame(codec)],
        authority,
    )
    storage = _Storage()
    client = CloudWSSClient(
        config=_config(),
        transport=_Transport(connection),
        token_provider=_TokenProvider(),
        storage=storage,
        codec=codec,
        runtime_authority=authority,
        utc_now=lambda: NOW,
    )
    await client.start()
    assert storage.cursors["cloud.connector.outbound"] == 0
    connection.change_after_send = True

    with pytest.raises(LocalRuntimeAuthorityChanged):
        await client.send_heartbeat()

    assert len(connection.sent) == 2
    assert storage.cursors["cloud.connector.outbound"] == 0
    assert connection.closed[-1] == (1012, "local_runtime_changed")
    assert client.state is CloudSessionState.DISCONNECTED


@pytest.mark.asyncio
async def test_restart_uses_durable_connection_and_sequence_resume() -> None:
    codec = ConnectorProtocolCodec()
    storage = _Storage()
    first = _Connection([_welcome_frame(codec)])
    await _client(first, storage).start()

    second = _Connection(
        [
            _welcome_frame(
                codec,
                resume_decision="resumed",
            )
        ]
    )
    restarted = _client(second, storage)

    await restarted.start()

    hello_envelope = codec.decode_envelope(second.sent[0])
    hello = codec.decode_hello_payload(hello_envelope.payload)
    assert hello_envelope.sequence == 0
    assert hello.resume.mode == "resume"
    assert hello.resume.previous_connection_id == CONNECTION_ID
    assert hello.resume.next_outbound_sequence == 0
    assert hello.resume.next_inbound_sequence == 0
    assert storage.cursors["cloud.connector.outbound"] == 1
    assert storage.cursors["cloud.connector.inbound"] == 1


@pytest.mark.asyncio
async def test_missing_required_capability_fails_closed() -> None:
    codec = ConnectorProtocolCodec()
    connection = _Connection(
        [
            _welcome_frame(
                codec,
                accepted=(),
                unavailable=("session.control",),
            )
        ]
    )
    storage = _Storage()
    client = _client(connection, storage)

    with pytest.raises(RequiredCapabilityUnavailable):
        await client.start()

    assert client.state is CloudSessionState.DISCONNECTED
    assert connection.closed


@pytest.mark.asyncio
async def test_reset_required_replays_bounded_pages_and_completes_reconciliation() -> (
    None
):
    codec = ConnectorProtocolCodec()
    connection = _Connection([_welcome_frame(codec, resume_decision="reset_required")])
    storage = _Storage()
    storage.transport_epoch_id = str(EPOCH_ID)
    storage.runtime_generation = _DEFAULT_RUNTIME_AUTHORITY.runtime_generation
    storage.fresh_epoch_required = False
    storage.previous_connection_id = str(CONNECTION_ID)
    storage.cursors["cloud.connector.outbound"] = 2
    storage.transport_records = [
        TransportFrameRecord(
            message_id="86000000-0000-4000-8000-000000000010",
            epoch_id=str(EPOCH_ID),
            sequence=0,
            message_type="connector.heartbeat",
            business_kind="heartbeat",
            business_key="heartbeat-0",
            business_revision=0,
            runtime_generation=_DEFAULT_RUNTIME_AUTHORITY.runtime_generation,
            frame=b"replay-0",
            state="sent",
            created_at="now",
            updated_at="now",
            settled_at=None,
        ),
        TransportFrameRecord(
            message_id="86000000-0000-4000-8000-000000000011",
            epoch_id=str(EPOCH_ID),
            sequence=1,
            message_type="connector.heartbeat",
            business_kind="heartbeat",
            business_key="heartbeat-1",
            business_revision=1,
            runtime_generation=_DEFAULT_RUNTIME_AUTHORITY.runtime_generation,
            frame=b"replay-1",
            state="sent",
            created_at="now",
            updated_at="now",
            settled_at=None,
        ),
    ]
    connection.inbound[0] = _welcome_frame(
        codec,
        resume_decision="reset_required",
        max_in_flight=1,
    )
    client = _client(connection, storage)

    await client.start()

    assert client.state is CloudSessionState.ACTIVE
    assert connection.sent[-2:] == [b"replay-0", b"replay-1"]
    assert storage.transport_pending_calls == [
        (str(EPOCH_ID), 1, None),
        (str(EPOCH_ID), 1, 0),
        (str(EPOCH_ID), 1, 1),
    ]
    assert storage.reconciliation_required is False
    assert storage.cursors["cloud.connector.outbound"] == 2
    assert storage.inbox_writes == 0
    assert storage.outbox_writes == 0


@pytest.mark.asyncio
async def test_cloud_heartbeat_sequence_gap_reconciles_to_authoritative_cursors() -> (
    None
):
    codec = ConnectorProtocolCodec()
    heartbeat = ConnectorHeartbeat(
        connection_id=CONNECTION_ID,
        sender_role="cloud",
        observed_at=NOW,
        next_outbound_sequence=7,
        next_inbound_sequence=0,
        session_state="active",
    )
    connection = _Connection(
        [
            _welcome_frame(codec),
            _envelope(
                codec,
                message_type="connector.heartbeat",
                sequence=3,
                payload=json.loads(codec.encode_heartbeat(heartbeat)),
            ),
        ]
    )
    client = _client(connection, _Storage())
    await client.start()

    await client.receive_one()

    assert client.state is CloudSessionState.ACTIVE


@pytest.mark.asyncio
async def test_client_heartbeat_reports_durable_cursors_and_session_state() -> None:
    codec = ConnectorProtocolCodec()
    connection = _Connection([_welcome_frame(codec)])
    storage = _Storage()
    storage.transport_epoch_id = str(EPOCH_ID)
    storage.runtime_generation = _DEFAULT_RUNTIME_AUTHORITY.runtime_generation
    storage.fresh_epoch_required = False
    storage.previous_connection_id = str(CONNECTION_ID)
    storage.cursors["cloud.connector.outbound"] = 5
    storage.cursors["cloud.connector.inbound"] = 7
    client = _client(
        connection,
        storage,
    )
    connection.inbound[0] = _welcome_frame(
        codec,
        resume_decision="resumed",
        next_connector_sequence=6,
        next_cloud_sequence=8,
        sequence=7,
    )
    await client.start()

    await client.send_heartbeat()

    envelope = codec.decode_envelope(connection.sent[-1])
    heartbeat = codec.decode_heartbeat_payload(envelope.payload)
    assert envelope.sequence == 6
    assert heartbeat.sender_role == "connector"
    assert heartbeat.next_outbound_sequence == 6
    assert heartbeat.next_inbound_sequence == 8
    assert heartbeat.session_state == "active"


@pytest.mark.asyncio
async def test_reserved_business_message_is_rejected_without_persistence() -> None:
    codec = ConnectorProtocolCodec()
    connection = _Connection(
        [
            _welcome_frame(codec),
            _envelope(
                codec,
                message_type="command.deliver",
                sequence=1,
                payload={},
            ),
        ]
    )
    storage = _Storage()
    client = _client(connection, storage)
    await client.start()

    with pytest.raises(UnsupportedCloudMessage):
        await client.receive_one()

    assert client.state is CloudSessionState.DISCONNECTED
    assert storage.inbox_writes == 0


@pytest.mark.asyncio
async def test_command_delivery_requires_negotiated_control_capability() -> None:
    codec = ConnectorProtocolCodec()
    connection = _Connection(
        [
            _welcome_frame(
                codec,
                accepted=("session.observe",),
                unavailable=("session.control",),
            ),
            _envelope(
                codec,
                message_type="command.deliver",
                sequence=1,
                payload=json.loads(
                    (
                        CONTRACTS / "fixtures/valid/command-deliver-payload.json"
                    ).read_bytes()
                ),
            ),
        ]
    )
    storage = _Storage()
    lane = _CommandLane(storage.events)
    client = _client(connection, storage, command_lane=lane)
    await client.start()

    with pytest.raises(UnsupportedCloudMessage):
        await client.receive_one()

    assert lane.processed is False
    assert storage.cursors["cloud.connector.inbound"] == 0
    assert connection.closed[-1] == (1003, "unsupported_message")


@pytest.mark.asyncio
async def test_command_delivery_sends_durable_outbox_without_business_ack() -> None:
    codec = ConnectorProtocolCodec()
    command_payload = json.loads(
        (CONTRACTS / "fixtures/valid/command-deliver-payload.json").read_bytes()
    )
    heartbeat = ConnectorHeartbeat(
        connection_id=CONNECTION_ID,
        sender_role="cloud",
        observed_at=NOW,
        next_outbound_sequence=1,
        next_inbound_sequence=2,
        session_state="active",
    )
    connection = _Connection(
        [
            _welcome_frame(codec, max_in_flight=1),
            _envelope(
                codec,
                message_type="command.deliver",
                sequence=0,
                payload=command_payload,
            ),
            _envelope(
                codec,
                message_type="connector.heartbeat",
                sequence=1,
                payload=json.loads(codec.encode_heartbeat(heartbeat)),
            ),
        ]
    )
    storage = _Storage()
    lane = _CommandLane(storage.events)
    client = _client(connection, storage, command_lane=lane)
    await client.start()

    await client.receive_one()

    assert storage.events.index("process_command") < storage.events.index(
        "advance_inbound"
    )
    assert storage.events.index("advance_inbound") < storage.events.index(
        "stage_transport_frame"
    )
    assert lane.acknowledged == []
    receipt = codec.decode_envelope(connection.sent[-2])
    assert receipt.message_type == "command.receipt"
    assert receipt.sequence == 0
    result = codec.decode_envelope(connection.sent[-1])
    assert result.message_type == "command.result"
    assert result.sequence == 1

    await client.receive_one()

    assert lane.acknowledged == []


@pytest.mark.asyncio
async def test_heartbeat_cursor_never_acks_command_messages() -> None:
    codec = ConnectorProtocolCodec()
    heartbeat = ConnectorHeartbeat(
        connection_id=CONNECTION_ID,
        sender_role="cloud",
        observed_at=NOW,
        next_outbound_sequence=1,
        next_inbound_sequence=2,
        session_state="active",
    )
    connection = _Connection(
        [
            _welcome_frame(codec, max_in_flight=1),
            _envelope(
                codec,
                message_type="command.deliver",
                sequence=0,
                payload=json.loads(
                    (
                        CONTRACTS / "fixtures/valid/command-deliver-payload.json"
                    ).read_bytes()
                ),
            ),
            _envelope(
                codec,
                message_type="connector.heartbeat",
                sequence=1,
                payload=json.loads(codec.encode_heartbeat(heartbeat)),
            ),
        ]
    )
    storage = _Storage()
    lane = _CommandLane(storage.events)
    client = _client(connection, storage, command_lane=lane)
    await client.start()
    await client.receive_one()

    await client.receive_one()

    assert lane.acknowledged == []


@pytest.mark.asyncio
async def test_disconnect_replays_unacknowledged_command_outbox() -> None:
    codec = ConnectorProtocolCodec()
    first = _Connection(
        [
            _welcome_frame(codec, max_in_flight=1),
            _envelope(
                codec,
                message_type="command.deliver",
                sequence=0,
                payload=json.loads(
                    (
                        CONTRACTS / "fixtures/valid/command-deliver-payload.json"
                    ).read_bytes()
                ),
            ),
        ]
    )
    storage = _Storage()
    lane = _CommandLane(storage.events)
    client = _client(first, storage, command_lane=lane)
    await client.start()
    await client.receive_one()
    await client._disconnect(code=1001, reason="test_reconnect")
    second = _Connection(
        [
            _welcome_frame(
                codec,
                max_in_flight=1,
                resume_decision="reset_required",
                sequence=1,
                next_connector_sequence=0,
                next_cloud_sequence=1,
            )
        ]
    )
    client._transport.connection = second

    await client.start()

    replayed_types = [
        codec.decode_envelope(frame).message_type for frame in second.sent
    ]
    assert replayed_types == [
        "connector.hello",
        "command.receipt",
        "command.result",
    ]
    assert lane.acknowledged == []


@pytest.mark.asyncio
async def test_command_recovery_and_outbox_drain_more_than_two_bounded_pages() -> None:
    codec = ConnectorProtocolCodec()
    storage = _Storage()
    lane = _PagedCommandLane(codec, executing=9, messages=5)
    connection = _Connection([_welcome_frame(codec, max_in_flight=4)])
    message_ids = iter(
        UUID(f"45000000-0000-4000-8000-{index:012d}") for index in range(1, 10)
    )
    client = _client(
        connection,
        storage,
        config=replace(_config(), command_outbox_batch_size=2),
        command_lane=lane,  # type: ignore[arg-type]
        message_id_factory=lambda: next(message_ids),
    )

    await client.start()

    assert lane.executing == 0
    assert lane.recover_calls == [4, 4, 4, 4]
    assert len(lane.pending_calls) >= 4
    assert [
        codec.decode_envelope(frame).sequence for frame in connection.sent[1:]
    ] == list(range(5))
    assert {
        record.business_key
        for record in storage.transport_records
        if record.business_kind == "command.receipt"
    } == {message.command_id for message in lane.messages}


@pytest.mark.asyncio
async def test_same_process_reconnect_recovers_command_cancelled_after_claim() -> None:
    codec = ConnectorProtocolCodec()
    storage = _Storage()
    lane = _PagedCommandLane(codec, executing=0, messages=0)
    first_connection = _Connection([_welcome_frame(codec, max_in_flight=2)])
    client = _client(first_connection, storage, command_lane=lane)  # type: ignore[arg-type]
    await client.start()
    await client._disconnect(code=1001, reason="transport_lost")

    recovery = _PagedCommandLane(codec, executing=1, messages=1)
    lane.executing = recovery.executing
    lane.messages = recovery.messages
    second_connection = _Connection(
        [_welcome_frame(codec, resume_decision="resumed", max_in_flight=2)]
    )
    client._transport.connection = second_connection

    await client.start()

    assert lane.executing == 0
    assert lane.recover_calls == [2, 2, 2]
    assert [
        codec.decode_envelope(frame).message_type for frame in second_connection.sent
    ] == ["connector.hello", "command.receipt"]


@pytest.mark.asyncio
async def test_local_server_directives_fail_closed_without_wire_invention() -> None:
    codec = ConnectorProtocolCodec()
    connection = _Connection([_welcome_frame(codec)])
    client = _client(connection, _Storage())
    await client.start()

    await client.apply_server_directive(ServerSessionDirective.DRAIN)
    assert client.state is CloudSessionState.DRAINING
    await client.stop()
    assert client.state is CloudSessionState.DISCONNECTED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "directive",
    (
        ServerSessionDirective.REVOKED,
        ServerSessionDirective.UPDATE_REQUIRED,
    ),
)
async def test_terminal_server_directives_disable_reconnect(
    directive: ServerSessionDirective,
) -> None:
    codec = ConnectorProtocolCodec()
    connection = _Connection([_welcome_frame(codec)])
    token_provider = _TokenProvider()
    client = _client(connection, _Storage(), token_provider=token_provider)
    await client.start()

    await client.apply_server_directive(directive)

    assert client.state is CloudSessionState.DISCONNECTED
    assert client.reconnect_allowed is False
    assert connection.closed[-1] == (1000, "")
    assert token_provider.cleared is True


@pytest.mark.asyncio
async def test_explicit_revoked_directive_reaches_device_lifecycle_provider() -> None:
    codec = ConnectorProtocolCodec()
    connection = _Connection([_welcome_frame(codec)])
    token_provider = _LifecycleTokenProvider()
    client = _client(connection, _Storage(), token_provider=token_provider)
    await client.start()

    await client.apply_server_directive(ServerSessionDirective.REVOKED)

    assert token_provider.lifecycle_signals == ["revoked"]
    assert token_provider.cleared is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reason", "expected_signal"),
    (
        ("device_authorization_revoked", "revoked"),
        ("device_authorization_suspended", "suspended"),
    ),
)
async def test_policy_close_applies_exact_device_lifecycle_signal(
    reason: str,
    expected_signal: str,
) -> None:
    codec = ConnectorProtocolCodec()
    connection = _RemoteClosedConnection(
        [_welcome_frame(codec)],
        code=1008,
        reason=reason,
    )
    token_provider = _LifecycleTokenProvider()
    client = _client(connection, _Storage(), token_provider=token_provider)
    await client.start()

    with pytest.raises(CloudConnectionClosed):
        await client.receive_one()

    assert client.state is CloudSessionState.DISCONNECTED
    assert client.reconnect_allowed is False
    assert token_provider.lifecycle_signals == [expected_signal]
    assert token_provider.cleared is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("code", "reason"),
    (
        (1008, "device_authorization_revoked_typo"),
        (1001, "device_authorization_revoked"),
        (1008, ""),
    ),
)
async def test_other_remote_close_is_non_terminal_disconnect(
    code: int,
    reason: str,
) -> None:
    codec = ConnectorProtocolCodec()
    connection = _RemoteClosedConnection(
        [_welcome_frame(codec)],
        code=code,
        reason=reason,
    )
    token_provider = _LifecycleTokenProvider()
    client = _client(connection, _Storage(), token_provider=token_provider)
    await client.start()

    with pytest.raises(CloudConnectionClosed):
        await client.receive_one()

    assert client.state is CloudSessionState.DISCONNECTED
    assert client.reconnect_allowed is True
    assert token_provider.lifecycle_signals == []
    assert token_provider.cleared is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("directive", "failure"),
    (
        (ServerSessionDirective.REVOKED, RuntimeError("secure storage failed")),
        (ServerSessionDirective.UPDATE_REQUIRED, asyncio.CancelledError()),
    ),
)
async def test_terminal_directive_disconnects_when_token_clear_fails_or_is_cancelled(
    directive: ServerSessionDirective,
    failure: BaseException,
) -> None:
    codec = ConnectorProtocolCodec()
    connection = _Connection([_welcome_frame(codec)])
    token_provider = _FailingClearTokenProvider(failure)
    client = _client(connection, _Storage(), token_provider=token_provider)
    await client.start()

    with pytest.raises(type(failure)):
        await client.apply_server_directive(directive)

    assert client.state is CloudSessionState.DISCONNECTED
    assert client.reconnect_allowed is False
    assert connection.closed[-1] == (1000, "")


class _OrdinaryDependencyFailure(Exception):
    pass


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_stage", ("token", "transport", "codec", "storage"))
async def test_start_cleans_up_after_any_ordinary_dependency_exception(
    failure_stage: str,
) -> None:
    codec = ConnectorProtocolCodec()
    connection = _Connection([_welcome_frame(codec)])
    storage = _Storage()
    token_provider = _TokenProvider()
    transport = _Transport(connection)

    async def fail_async(*_args, **_kwargs):
        raise _OrdinaryDependencyFailure(f"{failure_stage} failed")

    def fail_sync(*_args, **_kwargs):
        raise _OrdinaryDependencyFailure(f"{failure_stage} failed")

    if failure_stage == "token":
        token_provider.access_token = fail_async  # type: ignore[method-assign]
    elif failure_stage == "transport":
        transport.connect = fail_async  # type: ignore[method-assign]
    elif failure_stage == "codec":
        codec.hello_payload = fail_sync  # type: ignore[method-assign]
    else:
        storage.get_cloud_session = fail_async  # type: ignore[method-assign]

    client = CloudWSSClient(
        config=_config(),
        transport=transport,
        token_provider=token_provider,
        storage=storage,
        codec=codec,
        runtime_authority=_RuntimeAuthority(),
        utc_now=lambda: NOW,
    )

    with pytest.raises(_OrdinaryDependencyFailure, match=f"{failure_stage} failed"):
        await client.start()

    assert client.state is CloudSessionState.DISCONNECTED
    assert connection.closed == (
        []
        if failure_stage in {"token", "transport", "storage"}
        else [(1002, "negotiation_failed")]
    )


@pytest.mark.asyncio
async def test_start_cancellation_propagates_after_connection_cleanup() -> None:
    codec = ConnectorProtocolCodec()
    connection = _Connection([_welcome_frame(codec)])
    storage = _Storage()

    async def cancel_storage() -> CloudSessionCheckpoint:
        raise asyncio.CancelledError

    storage.get_cloud_session = cancel_storage  # type: ignore[method-assign]
    client = _client(connection, storage)

    with pytest.raises(asyncio.CancelledError):
        await client.start()

    assert client.state is CloudSessionState.DISCONNECTED
    assert connection.closed == []


@pytest.mark.asyncio
async def test_run_cancellation_cleans_up_receive_and_heartbeat_children() -> None:
    codec = ConnectorProtocolCodec()
    connection = _BlockingAfterWelcomeConnection([_welcome_frame(codec)])
    client = _client(connection, _Storage())
    await client.start()
    run_task = asyncio.create_task(client.run())
    await asyncio.wait_for(connection.receive_blocked.wait(), timeout=1)

    run_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run_task

    assert connection.receive_cancelled.is_set()
    await client.stop()


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("negotiation_timeout_seconds", float("nan")),
        ("negotiation_timeout_seconds", float("inf")),
        ("io_timeout_seconds", float("nan")),
        ("io_timeout_seconds", float("inf")),
    ),
)
def test_client_config_rejects_nonfinite_timeouts(
    field: str,
    value: float,
) -> None:
    values = {
        "negotiation_timeout_seconds": 10.0,
        "io_timeout_seconds": 10.0,
        field: value,
    }

    with pytest.raises(ValueError, match="timeout must be finite and positive"):
        _config(**values)


@pytest.mark.parametrize("value", (0, -1, True, 1.5))
def test_client_config_rejects_invalid_command_outbox_batch_size(
    value: object,
) -> None:
    with pytest.raises(
        ValueError,
        match="command outbox batch size must be a positive integer",
    ):
        replace(_config(), command_outbox_batch_size=value)


@pytest.mark.parametrize("value", (0, -1, True, 1.5))
def test_client_config_rejects_invalid_owner_control_capacity(
    value: object,
) -> None:
    with pytest.raises(
        ValueError,
        match="owner control max in flight must be a positive integer",
    ):
        replace(_config(), owner_control_max_in_flight=value)


@pytest.mark.asyncio
async def test_component_ready_and_drain_follow_session_state() -> None:
    codec = ConnectorProtocolCodec()
    connection = _Connection([_welcome_frame(codec)])
    client = _client(connection, _Storage())
    await client.start()

    assert await client.ready() is True
    await client.drain()
    assert client.state is CloudSessionState.DRAINING
    assert await client.ready() is False
    await client.stop()


def test_exponential_backoff_is_bounded_and_jitter_is_injectable() -> None:
    backoff = ExponentialBackoff(
        base_seconds=1.0,
        maximum_seconds=8.0,
        jitter_ratio=0.25,
        random_value=lambda: 1.0,
    )

    assert [backoff.delay(attempt) for attempt in range(5)] == [
        1.25,
        2.5,
        5.0,
        8.0,
        8.0,
    ]


class _BlockingConnection(_Connection):
    def __init__(self, inbound: list[bytes]) -> None:
        super().__init__(inbound)
        self.unblocked = asyncio.Event()

    async def receive(self, *, timeout_seconds: float = 0) -> bytes:
        self.receive_timeouts.append(timeout_seconds)
        if self.inbound:
            item = self.inbound.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        await self.unblocked.wait()
        raise ConnectionError("closed")

    async def close(
        self,
        *,
        code: int,
        reason: str,
        timeout_seconds: float = 0,
    ) -> None:
        await super().close(
            code=code,
            reason=reason,
            timeout_seconds=timeout_seconds,
        )
        self.unblocked.set()


class _BusyInboundConnection(_Connection):
    def __init__(self, welcome: bytes, codec: ConnectorProtocolCodec) -> None:
        super().__init__([welcome])
        self._codec = codec
        self._sequence = 1

    async def receive(self, *, timeout_seconds: float = 0) -> bytes:
        if self.inbound:
            return await super().receive(timeout_seconds=timeout_seconds)
        await asyncio.sleep(0)
        sequence = self._sequence
        self._sequence += 1
        heartbeat = ConnectorHeartbeat(
            connection_id=CONNECTION_ID,
            sender_role="cloud",
            observed_at=NOW,
            next_outbound_sequence=sequence,
            next_inbound_sequence=1,
            session_state="active",
        )
        return _envelope(
            self._codec,
            message_type="connector.heartbeat",
            sequence=sequence,
            payload=json.loads(self._codec.encode_heartbeat(heartbeat)),
        )


class _BlockingSendConnection(_Connection):
    def __init__(self, inbound: list[bytes]) -> None:
        super().__init__(inbound)
        self.blocked = asyncio.Event()

    async def send(self, frame: bytes, *, timeout_seconds: float = 0) -> None:
        if self.sent:
            await self.blocked.wait()
        await super().send(frame, timeout_seconds=timeout_seconds)


class _FirstApplicationSendGateConnection(_Connection):
    def __init__(self, inbound: list[bytes], *, fail_first: bool = False) -> None:
        super().__init__(inbound)
        self.application_send_entered = asyncio.Event()
        self.release_application_send = asyncio.Event()
        self.application_send_attempts: list[bytes] = []
        self._fail_first = fail_first

    async def send(self, frame: bytes, *, timeout_seconds: float = 0) -> None:
        if self.sent:
            self.application_send_attempts.append(frame)
            if len(self.application_send_attempts) == 1:
                self.application_send_entered.set()
                await self.release_application_send.wait()
                if self._fail_first:
                    raise ConnectionError("first application send failed")
        await super().send(frame, timeout_seconds=timeout_seconds)


def _online_reconciliation_scenario(
    codec: ConnectorProtocolCodec,
    *,
    fail_first: bool = False,
) -> tuple[_FirstApplicationSendGateConnection, _Storage, CloudWSSClient]:
    cloud_heartbeat = ConnectorHeartbeat(
        connection_id=CONNECTION_ID,
        sender_role="cloud",
        observed_at=NOW,
        next_outbound_sequence=0,
        next_inbound_sequence=0,
        session_state="active",
    )
    connection = _FirstApplicationSendGateConnection(
        [
            _welcome_frame(
                codec,
                resume_decision="reset_required",
                next_connector_sequence=0,
                next_cloud_sequence=0,
            ),
            _envelope(
                codec,
                message_type="connector.heartbeat",
                sequence=0,
                payload=json.loads(codec.encode_heartbeat(cloud_heartbeat)),
            ),
        ],
        fail_first=fail_first,
    )
    storage = _Storage()
    storage.transport_epoch_id = str(EPOCH_ID)
    storage.runtime_generation = _DEFAULT_RUNTIME_AUTHORITY.runtime_generation
    storage.fresh_epoch_required = False
    storage.previous_connection_id = str(CONNECTION_ID)
    storage.cursors["cloud.connector.outbound"] = 1
    storage.transport_records = [
        TransportFrameRecord(
            message_id="86000000-0000-4000-8000-000000000020",
            epoch_id=str(EPOCH_ID),
            sequence=0,
            message_type="connector.heartbeat",
            business_kind="heartbeat",
            business_key="heartbeat-replay-0",
            business_revision=0,
            runtime_generation=_DEFAULT_RUNTIME_AUTHORITY.runtime_generation,
            frame=_envelope(
                codec,
                message_type="connector.heartbeat",
                sequence=0,
                payload=json.loads(
                    codec.encode_heartbeat(
                        ConnectorHeartbeat(
                            connection_id=CONNECTION_ID,
                            sender_role="connector",
                            observed_at=NOW,
                            next_outbound_sequence=0,
                            next_inbound_sequence=0,
                            session_state="reconciling",
                        )
                    )
                ),
            ),
            state="sent",
            created_at="now",
            updated_at="now",
            settled_at=None,
        )
    ]
    return connection, storage, _client(connection, storage)


@pytest.mark.asyncio
async def test_periodic_heartbeat_is_not_suppressed_by_continuous_inbound() -> None:
    codec = ConnectorProtocolCodec()
    connection = _BusyInboundConnection(_welcome_frame(codec), codec)
    client = _client(connection, _Storage())
    await client.start()
    client._heartbeat_interval_ms = 1
    runner = asyncio.create_task(client.run())
    for _ in range(1_000):
        if len(connection.sent) >= 2:
            break
        await asyncio.sleep(0)

    await client.stop()
    await runner

    assert len(connection.sent) >= 2


@pytest.mark.asyncio
async def test_online_reconciliation_serializes_replay_and_heartbeat_sequences() -> (
    None
):
    codec = ConnectorProtocolCodec()
    connection, storage, client = _online_reconciliation_scenario(codec)
    reconciliation = asyncio.create_task(client.start())
    send_entered = asyncio.create_task(connection.application_send_entered.wait())
    done, _pending = await asyncio.wait(
        {reconciliation, send_entered},
        timeout=1,
        return_when=asyncio.FIRST_COMPLETED,
    )
    if reconciliation in done:
        reconciliation.result()
    assert send_entered in done
    heartbeat = asyncio.create_task(client.send_heartbeat())
    await asyncio.sleep(0)
    attempts_before_release = len(connection.application_send_attempts)
    connection.release_application_send.set()
    results = await asyncio.gather(
        reconciliation,
        heartbeat,
        return_exceptions=True,
    )

    sequences = [
        codec.decode_envelope(frame).sequence
        for frame in connection.application_send_attempts
    ]
    assert attempts_before_release == 1
    assert results == [None, None]
    assert sequences == [0, 1]
    assert len(sequences) == len(set(sequences))
    assert storage.cursors["cloud.connector.outbound"] == 2


@pytest.mark.asyncio
async def test_failed_replay_blocks_waiting_heartbeat_without_cursor_advance() -> None:
    codec = ConnectorProtocolCodec()
    connection, storage, client = _online_reconciliation_scenario(
        codec,
        fail_first=True,
    )
    reconciliation = asyncio.create_task(client.start())
    send_entered = asyncio.create_task(connection.application_send_entered.wait())
    done, _pending = await asyncio.wait(
        {reconciliation, send_entered},
        timeout=1,
        return_when=asyncio.FIRST_COMPLETED,
    )
    if reconciliation in done:
        reconciliation.result()
    assert send_entered in done
    heartbeat = asyncio.create_task(client.send_heartbeat())
    await asyncio.sleep(0)
    connection.release_application_send.set()
    results = await asyncio.gather(
        reconciliation,
        heartbeat,
        return_exceptions=True,
    )

    assert all(isinstance(result, ConnectionError) for result in results)
    assert len(connection.application_send_attempts) == 1
    assert storage.cursors["cloud.connector.outbound"] == 0


@pytest.mark.asyncio
async def test_failed_sequenced_send_does_not_advance_durable_cursor() -> None:
    codec = ConnectorProtocolCodec()
    connection = _FirstApplicationSendGateConnection(
        [_welcome_frame(codec)],
        fail_first=True,
    )
    storage = _Storage()
    client = _client(connection, storage)
    await client.start()

    heartbeat = asyncio.create_task(client.send_heartbeat())
    await connection.application_send_entered.wait()
    connection.release_application_send.set()

    with pytest.raises(ConnectionError, match="first application send failed"):
        await heartbeat
    assert storage.cursors["cloud.connector.outbound"] == 0


@pytest.mark.asyncio
async def test_blocked_heartbeat_send_obeys_io_deadline() -> None:
    codec = ConnectorProtocolCodec()
    connection = _BlockingSendConnection([_welcome_frame(codec)])
    client = _client(
        connection,
        _Storage(),
        config=_config(io_timeout_seconds=0.01),
    )
    await client.start()

    with pytest.raises(TimeoutError):
        await client.send_heartbeat()


class _RotatingTransport:
    def __init__(self, connections: list[_BlockingConnection]) -> None:
        self.connections = connections
        self.calls = 0

    async def connect(self, _endpoint: str, *, token: str):
        assert token == "secret-token-must-not-be-logged"
        connection = self.connections[self.calls]
        self.calls += 1
        return connection


@pytest.mark.asyncio
async def test_run_reconnects_with_injected_backoff_after_transport_loss() -> None:
    codec = ConnectorProtocolCodec()
    first = _BlockingConnection(
        [_welcome_frame(codec), ConnectionError("network_lost")]
    )
    second = _BlockingConnection([_welcome_frame(codec)])
    transport = _RotatingTransport([first, second])
    sleeps: list[float] = []

    async def record_sleep(delay: float) -> None:
        sleeps.append(delay)

    client = CloudWSSClient(
        config=_config(),
        transport=transport,
        token_provider=_TokenProvider(),
        storage=_Storage(),
        codec=codec,
        runtime_authority=_RuntimeAuthority(),
        utc_now=lambda: NOW,
        backoff=ExponentialBackoff(
            base_seconds=1,
            maximum_seconds=8,
            jitter_ratio=0,
            random_value=lambda: 0.5,
        ),
        sleep=record_sleep,
    )
    await client.start()
    runner = asyncio.create_task(client.run())
    for _ in range(100):
        if transport.calls == 2 and client.state is CloudSessionState.ACTIVE:
            break
        await asyncio.sleep(0)

    await client.stop()
    await runner

    assert transport.calls == 2
    assert sleeps == [1.0]
    assert client.state is CloudSessionState.DISCONNECTED


def test_client_accepts_owner_control_lane_dependency() -> None:
    parameters = inspect.signature(CloudWSSClient).parameters

    assert "owner_control_lane" in parameters


class _OwnerControlLane:
    def __init__(self, codec: ConnectorProtocolCodec) -> None:
        self._codec = codec
        self.requests: list[OwnerControlRequest] = []
        self.closed = 0
        self.processed = asyncio.Event()

    async def process(
        self,
        request: OwnerControlRequest,
    ) -> OwnerControlResponse:
        self.requests.append(request)
        response = self._codec.decode_control_response(
            (CONTRACTS / "fixtures/valid/control-response-status.json").read_bytes()
        )
        response = replace(
            response,
            request_id=request.request_id,
            control_transport_id=request.control_transport_id,
            operation=request.operation,
        )
        self.processed.set()
        return response

    async def close_all(self) -> None:
        self.closed += 1


class _BlockingOwnerControlLane(_OwnerControlLane):
    def __init__(self, codec: ConnectorProtocolCodec) -> None:
        super().__init__(codec)
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def process(
        self,
        request: OwnerControlRequest,
    ) -> OwnerControlResponse:
        self.requests.append(request)
        self.started.set()
        await self.release.wait()
        return await super().process(request)


class _PostCommitBlockingOwnerClaimStorage(_Storage):
    def __init__(self) -> None:
        super().__init__()
        self.claim_committed = asyncio.Event()

    async def claim_owner_control(self, request_id: str) -> bool:
        claimed = await super().claim_owner_control(request_id)
        self.claim_committed.set()
        await asyncio.Event().wait()
        return claimed


class _ConcurrentOwnerControlLane(_OwnerControlLane):
    def __init__(self, codec: ConnectorProtocolCodec) -> None:
        super().__init__(codec)
        self.active = 0
        self.maximum_active = 0

    async def process(
        self,
        request: OwnerControlRequest,
    ) -> OwnerControlResponse:
        self.requests.append(request)
        self.active += 1
        self.maximum_active = max(self.maximum_active, self.active)
        try:
            await asyncio.sleep(0)
            response = self._codec.decode_control_response(
                (CONTRACTS / "fixtures/valid/control-response-status.json").read_bytes()
            )
            return replace(
                response,
                request_id=request.request_id,
                control_transport_id=request.control_transport_id,
                operation=request.operation,
            )
        finally:
            self.active -= 1


class _AuthorityChangingOwnerControlLane(_OwnerControlLane):
    def __init__(
        self,
        codec: ConnectorProtocolCodec,
        authority: _RuntimeAuthority,
    ) -> None:
        super().__init__(codec)
        self._authority = authority

    async def process(
        self,
        request: OwnerControlRequest,
    ) -> OwnerControlResponse:
        response = await super().process(request)
        self._authority.current = _runtime_authority("runtime-owner-changed")
        return response


def _control_request_frame(
    codec: ConnectorProtocolCodec,
    *,
    sequence: int = 0,
    idempotency_key: str | None = None,
) -> bytes:
    payload = json.loads(
        (CONTRACTS / "fixtures/valid/control-request-status.json").read_bytes()
    )
    return _envelope(
        codec,
        message_type="control.request",
        sequence=sequence,
        payload=payload,
        idempotency_key=idempotency_key or payload["request_id"],
    )


async def _seed_received_owner(
    storage: _Storage,
    codec: ConnectorProtocolCodec,
    index: int,
) -> OwnerControlRequest:
    payload = json.loads(
        (CONTRACTS / "fixtures/valid/control-request-status.json").read_bytes()
    )
    payload["request_id"] = f"77777777-7777-4777-8777-{index:012d}"
    request = codec.decode_control_request_payload(payload)
    request_payload = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    await storage.put_owner_control(
        request_id=str(request.request_id),
        request_digest=hashlib.sha256(request_payload).hexdigest(),
        control_transport_id=str(request.control_transport_id),
        operation=request.operation,
        request_payload=request_payload,
        scope_payload=json.dumps(
            {
                "control_transport_id": str(request.control_transport_id),
                "device_id": "device-test",
                "tenant_id": "tenant-test",
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode(),
    )
    return request


async def _wait_until(predicate) -> None:
    for _ in range(100):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition was not reached")


@pytest.mark.asyncio
async def test_control_request_requires_negotiated_control_capability() -> None:
    codec = ConnectorProtocolCodec()
    lane = _OwnerControlLane(codec)
    connection = _Connection(
        [
            _welcome_frame(
                codec,
                accepted=("session.observe",),
                unavailable=("session.control",),
            ),
            _control_request_frame(codec),
        ]
    )
    client = _client(connection, _Storage(), owner_control_lane=lane)
    await client.start()

    with pytest.raises(UnsupportedCloudMessage):
        await client.receive_one()

    assert lane.requests == []
    assert connection.closed[-1] == (1003, "unsupported_message")


@pytest.mark.asyncio
async def test_control_request_is_durable_before_cursor_and_sends_journaled_response() -> (
    None
):
    codec = ConnectorProtocolCodec()
    lane = _OwnerControlLane(codec)
    connection = _Connection([_welcome_frame(codec), _control_request_frame(codec)])
    storage = _Storage()
    client = _client(connection, storage, owner_control_lane=lane)
    await client.start()

    await client.receive_one()
    await lane.processed.wait()
    await _wait_until(lambda: len(connection.sent) == 2)

    assert storage.cursors["cloud.connector.inbound"] == 1
    assert storage.inbox_writes == 0
    assert storage.outbox_writes == 0
    request_id = str(lane.requests[0].request_id)
    record = storage.owner_records[request_id]
    assert record.state == "completed"
    assert record.request_digest == hashlib.sha256(record.request_payload).hexdigest()
    assert json.loads(record.scope_payload) == {
        "control_transport_id": str(lane.requests[0].control_transport_id),
        "device_id": "device-test",
        "tenant_id": "tenant-test",
    }
    assert storage.events.index("put_owner_control") < storage.events.index(
        "advance_inbound"
    )
    assert storage.events.index("advance_inbound") < storage.events.index(
        "claim_owner_control"
    )
    assert storage.events.index("complete_owner_control") < storage.events.index(
        "stage_transport_frame"
    )
    assert storage.atomic_owner_writes == 1
    response_envelope = codec.decode_envelope(connection.sent[-1])
    response = codec.decode_control_response_payload(response_envelope.payload)
    assert response_envelope.message_type == "control.response"
    assert response_envelope.idempotency_key == str(response.request_id)
    assert response.request_id == lane.requests[0].request_id
    assert response.state == "succeeded"
    assert not hasattr(lane, "acknowledge_cloud_message")


@pytest.mark.asyncio
async def test_cancelled_owner_effect_is_persisted_unknown_and_replayed_without_reexecution() -> (
    None
):
    codec = ConnectorProtocolCodec()
    lane = _BlockingOwnerControlLane(codec)
    connection = _Connection([_welcome_frame(codec), _control_request_frame(codec)])
    storage = _Storage()
    client = _client(connection, storage, owner_control_lane=lane)
    await client.start()
    await client.receive_one()
    await lane.started.wait()

    await client.stop()

    request_id = str(lane.requests[0].request_id)
    assert storage.owner_records[request_id].state == "effect_unknown"
    assert storage.owner_records[request_id].response_payload is not None

    recovery_lane = _OwnerControlLane(codec)
    recovery_connection = _Connection(
        [
            _welcome_frame(
                codec,
                resume_decision="resumed",
                next_connector_sequence=1,
                next_cloud_sequence=2,
                sequence=1,
            )
        ]
    )
    recovery = _client(
        recovery_connection,
        storage,
        owner_control_lane=recovery_lane,
    )
    await recovery.start()

    assert recovery_lane.requests == []
    assert len(recovery_connection.sent) == 2
    response_envelope = codec.decode_envelope(recovery_connection.sent[-1])
    response = codec.decode_control_response_payload(response_envelope.payload)
    assert response.request_id == lane.requests[0].request_id
    assert response.state == "unknown"
    assert response.error == {"code": 4307, "reason": "effect_unknown"}


@pytest.mark.asyncio
async def test_cancel_after_owner_claim_commit_persists_unknown_without_restart() -> (
    None
):
    codec = ConnectorProtocolCodec()
    lane = _OwnerControlLane(codec)
    storage = _PostCommitBlockingOwnerClaimStorage()
    connection = _Connection([_welcome_frame(codec), _control_request_frame(codec)])
    client = _client(connection, storage, owner_control_lane=lane)
    await client.start()
    await client.receive_one()
    await storage.claim_committed.wait()

    await client.stop()

    record = next(iter(storage.owner_records.values()))
    assert record.state == "effect_unknown"
    assert record.response_payload is not None
    assert lane.requests == []


@pytest.mark.asyncio
async def test_received_owner_request_is_recovered_after_cursor_commit_without_reexecution() -> (
    None
):
    codec = ConnectorProtocolCodec()
    payload = json.loads(
        (CONTRACTS / "fixtures/valid/control-request-status.json").read_bytes()
    )
    request = codec.decode_control_request_payload(payload)
    storage = _Storage()
    await storage.put_owner_control(
        request_id=str(request.request_id),
        request_digest=hashlib.sha256(
            json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
        ).hexdigest(),
        control_transport_id=str(request.control_transport_id),
        operation=request.operation,
        request_payload=json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode(),
        scope_payload=json.dumps(
            {
                "control_transport_id": str(request.control_transport_id),
                "device_id": "device-test",
                "tenant_id": "tenant-test",
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode(),
    )
    storage.cursors["cloud.connector.inbound"] = 1
    storage.fresh_epoch_required = False
    storage.transport_epoch_id = str(EPOCH_ID)
    storage.runtime_generation = _DEFAULT_RUNTIME_AUTHORITY.runtime_generation
    storage.previous_connection_id = "22222222-2222-4222-8222-000000000001"
    lane = _OwnerControlLane(codec)
    connection = _Connection(
        [
            _welcome_frame(
                codec,
                resume_decision="resumed",
                next_connector_sequence=1,
                next_cloud_sequence=2,
                sequence=1,
            )
        ]
    )

    client = _client(connection, storage, owner_control_lane=lane)
    await client.start()
    await lane.processed.wait()
    await _wait_until(lambda: len(connection.sent) == 2)

    assert len(lane.requests) == 1
    assert storage.owner_records[str(request.request_id)].state == "completed"


@pytest.mark.asyncio
@pytest.mark.parametrize("tampered_field", ("request_digest", "scope_payload"))
async def test_owner_recovery_rejects_tampered_digest_or_scope_before_effect(
    tampered_field: str,
) -> None:
    codec = ConnectorProtocolCodec()
    payload = json.loads(
        (CONTRACTS / "fixtures/valid/control-request-status.json").read_bytes()
    )
    request = codec.decode_control_request_payload(payload)
    request_payload = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    storage = _Storage()
    inserted = await storage.put_owner_control(
        request_id=str(request.request_id),
        request_digest=hashlib.sha256(request_payload).hexdigest(),
        control_transport_id=str(request.control_transport_id),
        operation=request.operation,
        request_payload=request_payload,
        scope_payload=json.dumps(
            {
                "control_transport_id": str(request.control_transport_id),
                "device_id": "device-test",
                "tenant_id": "tenant-test",
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode(),
    )
    storage.owner_records[str(request.request_id)] = replace(
        inserted.record,
        **(
            {"request_digest": "f" * 64}
            if tampered_field == "request_digest"
            else {"scope_payload": b'{"tenant_id":"other"}'}
        ),
    )
    storage.transport_epoch_id = str(EPOCH_ID)
    storage.runtime_generation = _DEFAULT_RUNTIME_AUTHORITY.runtime_generation
    storage.fresh_epoch_required = False
    storage.previous_connection_id = str(CONNECTION_ID)
    lane = _OwnerControlLane(codec)
    connection = _Connection([_welcome_frame(codec, resume_decision="resumed")])
    client = _client(connection, storage, owner_control_lane=lane)

    with pytest.raises(ProtocolViolation, match="durable owner request"):
        await client.start()

    assert lane.requests == []


@pytest.mark.asyncio
async def test_owner_recovery_drains_more_than_two_bounded_batches_and_flushes_each() -> (
    None
):
    codec = ConnectorProtocolCodec()
    storage = _Storage()
    requests = [
        await _seed_received_owner(storage, codec, index) for index in range(1, 6)
    ]
    storage.transport_epoch_id = str(EPOCH_ID)
    storage.runtime_generation = _DEFAULT_RUNTIME_AUTHORITY.runtime_generation
    storage.fresh_epoch_required = False
    storage.previous_connection_id = str(CONNECTION_ID)
    lane = _ConcurrentOwnerControlLane(codec)
    connection = _Connection([_welcome_frame(codec, resume_decision="resumed")])
    message_ids = iter(
        UUID(f"44000000-0000-4000-8000-{index:012d}") for index in range(1, 10)
    )
    client = _client(
        connection,
        storage,
        config=replace(_config(), owner_control_max_in_flight=2),
        owner_control_lane=lane,
        message_id_factory=lambda: next(message_ids),
    )

    await client.start()

    assert {request.request_id for request in lane.requests} == {
        request.request_id for request in requests
    }
    assert lane.maximum_active == 2
    assert storage.owner_record_calls == [
        ("received", 2),
        ("received", 2),
        ("received", 2),
        ("received", 2),
    ]
    assert all(record.state == "completed" for record in storage.owner_records.values())
    assert len(connection.sent) == 6
    assert [
        record.sequence
        for record in storage.transport_records
        if record.business_kind == "control.response"
    ] == list(range(1, 6))


@pytest.mark.asyncio
async def test_terminal_owner_outbox_keyset_reaches_later_pages_before_settle() -> None:
    codec = ConnectorProtocolCodec()
    storage = _Storage()
    for index in range(41, 46):
        request = await _seed_received_owner(storage, codec, index)
        assert await storage.claim_owner_control(str(request.request_id))
        response = codec.decode_control_response(
            (CONTRACTS / "fixtures/valid/control-response-status.json").read_bytes()
        )
        response = replace(
            response,
            request_id=request.request_id,
            control_transport_id=request.control_transport_id,
            operation=request.operation,
        )
        await storage.complete_owner_control(
            request_id=str(request.request_id),
            response_payload=codec.encode_control_response(response),
        )
    storage.transport_epoch_id = str(EPOCH_ID)
    storage.runtime_generation = _DEFAULT_RUNTIME_AUTHORITY.runtime_generation
    storage.fresh_epoch_required = False
    storage.previous_connection_id = str(CONNECTION_ID)
    lane = _OwnerControlLane(codec)
    connection = _Connection([_welcome_frame(codec, resume_decision="resumed")])
    message_ids = iter(
        UUID(f"46000000-0000-4000-8000-{index:012d}") for index in range(1, 10)
    )
    client = _client(
        connection,
        storage,
        config=replace(_config(), owner_control_max_in_flight=2),
        owner_control_lane=lane,
        message_id_factory=lambda: next(message_ids),
    )

    await client.start()

    assert len(connection.sent) == 6
    assert len(storage.pending_owner_calls) == 4
    assert storage.pending_owner_calls[0] == (2, None, None)
    assert all(
        cursor[1] is not None and cursor[2] is not None
        for cursor in storage.pending_owner_calls[1:]
    )
    assert all(
        not record.transport_received for record in storage.owner_records.values()
    )


@pytest.mark.asyncio
async def test_owner_recovery_cancel_then_reconnect_drains_remaining_without_reexecution() -> (
    None
):
    codec = ConnectorProtocolCodec()
    storage = _Storage()
    requests = [
        await _seed_received_owner(storage, codec, index) for index in range(11, 16)
    ]
    storage.transport_epoch_id = str(EPOCH_ID)
    storage.runtime_generation = _DEFAULT_RUNTIME_AUTHORITY.runtime_generation
    storage.fresh_epoch_required = False
    storage.previous_connection_id = str(CONNECTION_ID)
    blocking = _BlockingOwnerControlLane(codec)
    first_connection = _Connection([_welcome_frame(codec, resume_decision="resumed")])
    first = _client(
        first_connection,
        storage,
        config=replace(_config(), owner_control_max_in_flight=2),
        owner_control_lane=blocking,
    )
    starting = asyncio.create_task(first.start())
    await _wait_until(lambda: len(blocking.requests) == 2)

    await first.stop()
    await asyncio.gather(starting, return_exceptions=True)

    first_ids = {request.request_id for request in blocking.requests}
    assert len(first_ids) == 2
    assert {
        UUID(record.request_id)
        for record in storage.owner_records.values()
        if record.state == "effect_unknown"
    } == first_ids

    lane = _ConcurrentOwnerControlLane(codec)
    second_connection = _Connection(
        [
            _welcome_frame(
                codec,
                resume_decision="resumed",
                next_connector_sequence=2,
                next_cloud_sequence=2,
                sequence=1,
            )
        ]
    )
    message_ids = iter(
        UUID(f"44000000-0000-4000-8000-{index:012d}") for index in range(20, 30)
    )
    second = _client(
        second_connection,
        storage,
        config=replace(_config(), owner_control_max_in_flight=2),
        owner_control_lane=lane,
        message_id_factory=lambda: next(message_ids),
    )
    await second.start()

    assert first_ids.isdisjoint({request.request_id for request in lane.requests})
    assert {request.request_id for request in lane.requests} == {
        request.request_id for request in requests
    } - first_ids
    assert all(
        record.state in {"completed", "effect_unknown"}
        for record in storage.owner_records.values()
    )
    assert len(second_connection.sent) == 6


@pytest.mark.asyncio
async def test_owner_recovery_stops_after_authority_change_without_claiming_later_pages() -> (
    None
):
    codec = ConnectorProtocolCodec()
    storage = _Storage()
    await asyncio.gather(
        *(_seed_received_owner(storage, codec, index) for index in range(31, 36))
    )
    storage.transport_epoch_id = str(EPOCH_ID)
    storage.runtime_generation = _DEFAULT_RUNTIME_AUTHORITY.runtime_generation
    storage.fresh_epoch_required = False
    storage.previous_connection_id = str(CONNECTION_ID)
    authority = _RuntimeAuthority()
    lane = _AuthorityChangingOwnerControlLane(codec, authority)
    connection = _Connection([_welcome_frame(codec, resume_decision="resumed")])
    client = _client(
        connection,
        storage,
        config=replace(_config(), owner_control_max_in_flight=2),
        owner_control_lane=lane,
        runtime_authority=authority,
    )

    with pytest.raises(LocalRuntimeAuthorityChanged):
        await client.start()

    assert len(lane.requests) <= 2
    assert (
        sum(record.state == "received" for record in storage.owner_records.values())
        >= 3
    )
    assert connection.closed[-1] == (1012, "local_runtime_changed")


@pytest.mark.asyncio
async def test_fresh_epoch_reenvelopes_terminal_owner_response_without_reexecution() -> (
    None
):
    codec = ConnectorProtocolCodec()
    first_lane = _OwnerControlLane(codec)
    first_connection = _Connection(
        [_welcome_frame(codec), _control_request_frame(codec)]
    )
    storage = _Storage()
    first = _client(first_connection, storage, owner_control_lane=first_lane)
    await first.start()
    await first.receive_one()
    await first_lane.processed.wait()
    await _wait_until(lambda: len(first_connection.sent) == 2)
    first_response = codec.decode_envelope(first_connection.sent[-1])
    await first.stop()

    storage.fresh_epoch_required = True
    second_lane = _OwnerControlLane(codec)
    second_connection = _Connection([_welcome_frame(codec)])
    second = _client(
        second_connection,
        storage,
        owner_control_lane=second_lane,
        message_id=UUID("44444444-4444-4444-8444-444444444445"),
        epoch_id=EPOCH_ID_2,
    )
    await second.start()

    assert second_lane.requests == []
    assert storage.transport_epoch_id == str(EPOCH_ID_2)
    second_response = codec.decode_envelope(second_connection.sent[-1])
    assert second_response.message_type == "control.response"
    assert second_response.sequence == 0
    assert second_response.message_id != first_response.message_id
    assert second_response.payload == first_response.payload


@pytest.mark.asyncio
async def test_control_request_rejects_sequence_or_idempotency_mismatch() -> None:
    codec = ConnectorProtocolCodec()
    for frame, reason in (
        (_control_request_frame(codec, sequence=2), "invalid_control_sequence"),
        (
            _control_request_frame(
                codec,
                idempotency_key="not-the-request-id",
            ),
            "invalid_control_request",
        ),
    ):
        lane = _OwnerControlLane(codec)
        connection = _Connection([_welcome_frame(codec), frame])
        client = _client(connection, _Storage(), owner_control_lane=lane)
        await client.start()

        with pytest.raises(ProtocolViolation):
            await client.receive_one()

        assert lane.requests == []
        assert connection.closed[-1] == (1002, reason)


@pytest.mark.asyncio
async def test_disconnect_closes_all_owner_control_channels() -> None:
    codec = ConnectorProtocolCodec()
    lane = _OwnerControlLane(codec)
    connection = _Connection([_welcome_frame(codec)])
    client = _client(connection, _Storage(), owner_control_lane=lane)
    await client.start()

    await client.stop()

    assert lane.closed == 1


@pytest.mark.asyncio
async def test_observer_event_is_staged_before_send_and_transport_is_not_ack() -> None:
    codec = ConnectorProtocolCodec()
    connection = _Connection([_welcome_frame(codec)])
    storage = _Storage()
    lane = _ObserverOutboundLane(codec, storage)
    client = _client(connection, storage, observer_outbound_lane=lane)
    await client.start()
    event = codec.decode_session_event(
        (CONTRACTS / "fixtures/valid/session-event-payload.json").read_bytes()
    )

    record = await client.publish_observer_event(event)

    assert lane.records == [record]
    assert connection.sent[-1] == record.frame
    assert lane.transport_notifications == 1
    assert lane.acks == []
    assert storage.cursors["cloud.connector.outbound"] == 1


@pytest.mark.asyncio
async def test_catalog_page_is_staged_before_send_and_transport_is_not_ack() -> None:
    codec = ConnectorProtocolCodec()
    connection = _Connection(
        [_welcome_frame(codec, accepted=("session.observe", "session.catalog.v1"))]
    )
    storage = _Storage()
    lane = _SessionCatalogOutboundLane(codec, storage)
    authority = _RuntimeAuthority(
        _runtime_authority(optional=("session.catalog.v1",))
    )
    client = _client(
        connection,
        storage,
        runtime_authority=authority,
        session_catalog_outbound_lane=lane,
    )
    await client.start()
    page = codec.decode_session_catalog_snapshot_page(
        (CONTRACTS / "fixtures/valid/session-catalog-snapshot-page.json").read_bytes()
    )
    page = replace(page, runtime_generation="runtime-authoritative")

    record = await client.publish_session_catalog_snapshot_page(page)

    assert lane.records == [record]
    assert connection.sent[-1] == record.frame
    assert lane.transport_notifications == 1
    assert lane.acks == []
    assert storage.cursors["cloud.connector.outbound"] == 1


@pytest.mark.asyncio
async def test_catalog_capability_lifecycle_is_connection_generation_bound() -> None:
    codec = ConnectorProtocolCodec()
    first = _Connection(
        [
            _welcome_frame(
                codec,
                accepted=("session.observe",),
                unavailable=("session.catalog.v1",),
            )
        ]
    )
    storage = _Storage()
    lane = _SessionCatalogOutboundLane(codec, storage)
    authority = _RuntimeAuthority(
        _runtime_authority(optional=("session.catalog.v1",))
    )
    client = _client(
        first,
        storage,
        runtime_authority=authority,
        session_catalog_outbound_lane=lane,
    )

    await client.start()
    initial = await asyncio.wait_for(
        client.wait_session_catalog_capability_change(-1),
        timeout=0.2,
    )
    assert initial == (1, False, True)
    assert lane.retired_pending == 1

    await client._disconnect(code=1001, reason="test_reconnect")
    disconnected = await asyncio.wait_for(
        client.wait_session_catalog_capability_change(initial[0]),
        timeout=0.2,
    )
    assert disconnected == (2, False, False)

    second = _Connection(
        [_welcome_frame(codec, accepted=("session.observe", "session.catalog.v1"))]
    )
    client._transport.connection = second
    await client.start()
    reenabled = await asyncio.wait_for(
        client.wait_session_catalog_capability_change(disconnected[0]),
        timeout=0.2,
    )
    assert reenabled == (3, True, False)


@pytest.mark.asyncio
async def test_unavailable_catalog_is_retired_before_transport_reconciliation() -> None:
    codec = ConnectorProtocolCodec()
    storage = _Storage()
    storage.transport_epoch_id = str(EPOCH_ID)
    storage.runtime_generation = "runtime-authoritative"
    storage.fresh_epoch_required = False
    storage.previous_connection_id = str(CONNECTION_ID)
    fixed = SessionCatalogOutboxRecord(
        message_id="89000000-0000-4000-8000-000000000010",
        payload_digest="a" * 64,
        connector_sequence=0,
        message_type="session.catalog.event",
        profile="default",
        runtime_generation="runtime-authoritative",
        snapshot_id=None,
        catalog_revision=None,
        page_index=None,
        is_last=None,
        catalog_sequence=8,
        payload=b"stale-catalog-payload",
        frame=b"stale-catalog-frame",
        state="pending",
        transport_epoch_id=str(EPOCH_ID),
    )
    storage.transport_records.append(
        TransportFrameRecord(
            message_id=fixed.message_id,
            epoch_id=str(EPOCH_ID),
            sequence=0,
            message_type=fixed.message_type,
            business_kind="session_catalog",
            business_key=fixed.message_id,
            business_revision=8,
            runtime_generation=fixed.runtime_generation,
            frame=fixed.frame,
            state="staged",
            created_at="now",
            updated_at="now",
            settled_at=None,
        )
    )
    lane = _SessionCatalogOutboundLane(codec, storage)
    lane.records.append(fixed)
    connection = _Connection(
        [
            _welcome_frame(
                codec,
                accepted=("session.observe",),
                unavailable=("session.catalog.v1",),
                resume_decision="reset_required",
                next_connector_sequence=0,
            )
        ]
    )
    client = _client(
        connection,
        storage,
        runtime_authority=_RuntimeAuthority(
            _runtime_authority(optional=("session.catalog.v1",))
        ),
        session_catalog_outbound_lane=lane,
    )

    await client.start()

    assert lane.retired_pending == 1
    assert fixed.frame not in connection.sent


@pytest.mark.asyncio
async def test_catalog_ack_is_the_only_business_commit_and_advances_inbound() -> None:
    codec = ConnectorProtocolCodec()
    ack_payload = json.loads(
        (CONTRACTS / "fixtures/valid/session-catalog-ack-snapshot.json").read_bytes()
    )
    ack_payload.update(
        {
            "runtime_generation": "runtime-authoritative",
            "acked_message_id": "89000000-0000-4000-8000-000000000001",
            "acked_payload_digest": "a" * 64,
            "acked_connector_sequence": 0,
        }
    )
    connection = _Connection(
        [
            _welcome_frame(codec, accepted=("session.observe", "session.catalog.v1")),
            _envelope(
                codec,
                message_type="session.catalog.ack",
                sequence=0,
                payload=ack_payload,
            ),
        ]
    )
    storage = _Storage()
    lane = _SessionCatalogOutboundLane(codec, storage)
    authority = _RuntimeAuthority(
        _runtime_authority(optional=("session.catalog.v1",))
    )
    client = _client(
        connection,
        storage,
        runtime_authority=authority,
        session_catalog_outbound_lane=lane,
    )
    await client.start()
    page = replace(
        codec.decode_session_catalog_snapshot_page(
            (
                CONTRACTS / "fixtures/valid/session-catalog-snapshot-page.json"
            ).read_bytes()
        ),
        runtime_generation="runtime-authoritative",
    )
    await client.publish_session_catalog_snapshot_page(page)

    await client.receive_one()

    assert len(lane.acks) == 1
    assert storage.cursors["cloud.connector.inbound"] == 1


@pytest.mark.asyncio
async def test_catalog_nack_settles_attempt_then_drives_full_resnapshot() -> None:
    codec = ConnectorProtocolCodec()
    nack_payload = json.loads(
        (CONTRACTS / "fixtures/valid/session-catalog-nack-page-gap.json").read_bytes()
    )
    nack_payload.update(
        {
            "runtime_generation": "runtime-authoritative",
            "rejected_message_id": "89000000-0000-4000-8000-000000000001",
            "rejected_payload_digest": "a" * 64,
            "rejected_connector_sequence": 0,
        }
    )
    connection = _Connection(
        [
            _welcome_frame(codec, accepted=("session.observe", "session.catalog.v1")),
            _envelope(
                codec,
                message_type="session.catalog.nack",
                sequence=0,
                payload=nack_payload,
            ),
        ]
    )
    storage = _Storage()
    lane = _SessionCatalogOutboundLane(codec, storage)
    sync = _SessionCatalogSync()
    client = _client(
        connection,
        storage,
        runtime_authority=_RuntimeAuthority(
            _runtime_authority(optional=("session.catalog.v1",))
        ),
        session_catalog_outbound_lane=lane,
        session_catalog_sync=sync,
    )
    await client.start()
    page = replace(
        codec.decode_session_catalog_snapshot_page(
            (
                CONTRACTS / "fixtures/valid/session-catalog-snapshot-page.json"
            ).read_bytes()
        ),
        runtime_generation="runtime-authoritative",
    )
    await client.publish_session_catalog_snapshot_page(page)

    await client.receive_one()

    assert len(lane.nacks) == 1
    assert sync.nacks == lane.nacks
    assert storage.cursors["cloud.connector.inbound"] == 1


@pytest.mark.asyncio
async def test_stream_ack_is_only_business_commit_and_advances_inbound_after_store() -> (
    None
):
    codec = ConnectorProtocolCodec()
    ack_payload = json.loads(
        (CONTRACTS / "fixtures/valid/stream-ack-payload.json").read_bytes()
    )
    connection = _Connection(
        [
            _welcome_frame(codec),
            _envelope(
                codec,
                message_type="stream.ack",
                sequence=0,
                payload=ack_payload,
            ),
        ]
    )
    storage = _Storage()
    lane = _ObserverOutboundLane(codec, storage)
    lane.records.append(
        ObserverOutboxRecord(
            message_id=ack_payload["observer_message_id"],
            payload_digest=ack_payload["payload_digest"],
            connector_sequence=ack_payload["connector_sequence"],
            message_type=ack_payload["observer_message_type"],
            profile=ack_payload["profile"],
            session_key=ack_payload["session_key"],
            runtime_generation=ack_payload["runtime_generation"],
            runtime_session_id=ack_payload["runtime_session_id"],
            event_sequence=ack_payload["event_sequence"],
            payload=b"payload",
            frame=b"frame",
            state="pending",
        )
    )
    client = _client(connection, storage, observer_outbound_lane=lane)
    await client.start()

    await client.receive_one()

    assert len(lane.acks) == 1
    assert storage.cursors["cloud.connector.inbound"] == 1


@pytest.mark.asyncio
async def test_stream_nack_advances_inbound_and_replacement_snapshot_uses_new_sequence() -> (
    None
):
    codec = ConnectorProtocolCodec()
    nack_payload = json.loads(
        (CONTRACTS / "fixtures/valid/stream-nack-payload.json").read_bytes()
    )
    nack_payload["connector_sequence"] = 0
    connection = _Connection(
        [
            _welcome_frame(codec),
            _envelope(
                codec,
                message_type="stream.nack",
                sequence=0,
                payload=nack_payload,
            ),
        ]
    )
    storage = _Storage()
    lane = _ObserverOutboundLane(codec, storage)
    lane.records.append(
        ObserverOutboxRecord(
            message_id=nack_payload["observer_message_id"],
            payload_digest=nack_payload["payload_digest"],
            connector_sequence=0,
            message_type=nack_payload["observer_message_type"],
            profile=nack_payload["profile"],
            session_key=nack_payload["session_key"],
            runtime_generation=nack_payload["runtime_generation"],
            runtime_session_id=nack_payload["runtime_session_id"],
            event_sequence=nack_payload["event_sequence"],
            payload=b"rejected-payload",
            frame=b"rejected-frame",
            state="pending",
        )
    )
    snapshot = codec.decode_session_snapshot(
        (CONTRACTS / "fixtures/valid/session-snapshot-payload.json").read_bytes()
    )
    snapshot = replace(snapshot, runtime_generation="runtime-authoritative")
    intents = _PublishingRecoveryIntentLane(snapshot)
    client = _client(
        connection,
        storage,
        observer_outbound_lane=lane,
        observer_intent_lane=intents,
    )
    intents.client = client
    await client.start()

    await client.receive_one()

    replacement = codec.decode_envelope(connection.sent[-1])
    assert replacement.message_type == "session.snapshot"
    assert replacement.sequence == 0
    assert lane.forced_snapshot_attempts == [True]
    assert len(lane.nacks) == 1
    assert len(intents.recoveries) == 1
    assert storage.cursors["cloud.connector.outbound"] == 1
    assert storage.cursors["cloud.connector.inbound"] == 1


@pytest.mark.asyncio
async def test_cloud_authorized_observe_open_is_only_subscription_target_source() -> (
    None
):
    codec = ConnectorProtocolCodec()
    open_payload = json.loads(
        (CONTRACTS / "fixtures/valid/session-observe-open-payload.json").read_bytes()
    )
    connection = _Connection(
        [
            _welcome_frame(codec),
            _envelope(
                codec,
                message_type="session.observe.open",
                sequence=0,
                payload=open_payload,
                idempotency_key=open_payload["request_id"],
            ),
        ]
    )
    storage = _Storage()
    intents = _ObserverIntentLane()
    client = _client(connection, storage, observer_intent_lane=intents)
    await client.start()

    await client.receive_one()

    assert len(intents.opened) == 1
    assert intents.opened[0].profile == "default"
    assert intents.opened[0].session_key == "session-root-1"
    assert intents.opened[0].target_source == "cloud_authorized_binding"
    assert storage.cursors["cloud.connector.inbound"] == 1


@pytest.mark.asyncio
async def test_v2_observe_open_dispatches_only_on_versioned_negotiated_lane() -> None:
    codec = ConnectorProtocolCodec()
    open_payload = json.loads(
        (CONTRACTS / "fixtures/valid/session-observe-open-v2-payload.json").read_bytes()
    )
    capabilities = ("session.observe", "session.observe.output-parity.v1")
    connection = _Connection(
        [
            _welcome_frame(codec, accepted=capabilities),
            _envelope(
                codec,
                message_type="session.observe.open.v2",
                sequence=0,
                payload=open_payload,
                idempotency_key=open_payload["request_id"],
            ),
        ]
    )
    storage = _Storage()
    intents = _ObserverIntentLane()
    authority = _RuntimeAuthority(
        _runtime_authority(
            optional=("session.observe.output-parity.v1",),
        )
    )
    client = _client(
        connection,
        storage,
        runtime_authority=authority,
        observer_intent_lane=intents,
    )
    await client.start()

    await client.receive_one()

    assert len(intents.opened) == 1
    assert intents.opened[0].observer_contract == 2
    assert storage.cursors["cloud.connector.inbound"] == 1


@pytest.mark.asyncio
async def test_disconnect_replays_exact_observer_message_id_sequence_and_frame() -> (
    None
):
    codec = ConnectorProtocolCodec()
    fixed = ObserverOutboxRecord(
        message_id="83000000-0000-4000-8000-000000000001",
        payload_digest="a" * 64,
        connector_sequence=0,
        message_type="session.snapshot",
        profile="default",
        session_key="session-root-1",
        runtime_generation="runtime-generation-1",
        runtime_session_id="runtime-session-1",
        event_sequence=4,
        payload=b"fixed-payload",
        frame=b"fixed-observer-frame",
        state="pending",
        transport_epoch_id=str(EPOCH_ID),
    )
    storage = _Storage()
    storage.transport_epoch_id = str(EPOCH_ID)
    storage.runtime_generation = _DEFAULT_RUNTIME_AUTHORITY.runtime_generation
    storage.fresh_epoch_required = False
    storage.previous_connection_id = str(CONNECTION_ID)
    storage.transport_records.append(
        TransportFrameRecord(
            message_id=fixed.message_id,
            epoch_id=str(EPOCH_ID),
            sequence=fixed.connector_sequence,
            message_type=fixed.message_type,
            business_kind="observer",
            business_key=fixed.message_id,
            business_revision=fixed.event_sequence,
            runtime_generation=fixed.runtime_generation,
            frame=fixed.frame,
            state="staged",
            created_at="now",
            updated_at="now",
            settled_at=None,
        )
    )
    lane = _ObserverOutboundLane(codec, storage)
    lane.records.append(fixed)
    first = _Connection(
        [_welcome_frame(codec, resume_decision="reset_required", max_in_flight=1)]
    )
    client = _client(first, storage, observer_outbound_lane=lane)

    await client.start()
    await client._disconnect(code=1001, reason="test_reconnect")
    second = _Connection(
        [
            _welcome_frame(
                codec,
                resume_decision="reset_required",
                max_in_flight=1,
                sequence=0,
                next_connector_sequence=0,
                next_cloud_sequence=0,
            )
        ]
    )
    client._transport.connection = second
    await client.start()

    assert first.sent[-1] == fixed.frame
    assert second.sent[-1] == fixed.frame
    assert lane.records[0].message_id == fixed.message_id
    assert lane.records[0].connector_sequence == fixed.connector_sequence
    assert lane.acks == []


@pytest.mark.asyncio
async def test_reconciliation_replays_interleaved_business_frames_from_one_journal() -> (
    None
):
    codec = ConnectorProtocolCodec()
    storage = _Storage()
    storage.transport_epoch_id = str(EPOCH_ID)
    storage.runtime_generation = _DEFAULT_RUNTIME_AUTHORITY.runtime_generation
    storage.fresh_epoch_required = False
    storage.previous_connection_id = str(CONNECTION_ID)
    storage.cursors["cloud.connector.outbound"] = 3
    storage.transport_records = [
        TransportFrameRecord(
            message_id="83000000-0000-4000-8000-000000000001",
            epoch_id=str(EPOCH_ID),
            sequence=1,
            message_type="session.snapshot",
            business_kind="observer",
            business_key="83000000-0000-4000-8000-000000000001",
            business_revision=4,
            runtime_generation="runtime-generation-1",
            frame=b"observer-sequence-1",
            state="sent",
            created_at="now",
            updated_at="now",
            settled_at=None,
        ),
        TransportFrameRecord(
            message_id="86000000-0000-4000-8000-000000000030",
            epoch_id=str(EPOCH_ID),
            sequence=2,
            message_type="command.receipt",
            business_kind="command.receipt",
            business_key="aaaaaaaa-aaaa-4aaa-8aaa-000000000002",
            business_revision=1,
            runtime_generation="runtime-generation-1",
            frame=b"command-sequence-2",
            state="sent",
            created_at="now",
            updated_at="now",
            settled_at=None,
        ),
    ]
    connection = _Connection(
        [
            _welcome_frame(
                codec,
                resume_decision="reset_required",
                next_connector_sequence=1,
                max_in_flight=1,
            )
        ]
    )
    client = _client(connection, storage)

    await client.start()

    assert connection.sent[-2:] == [
        b"observer-sequence-1",
        b"command-sequence-2",
    ]
    assert storage.cursors["cloud.connector.outbound"] == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("observer_state", ("acked", "rejected"))
async def test_reset_replays_settled_frames_to_the_pre_reconnect_checkpoint(
    observer_state: str,
) -> None:
    codec = ConnectorProtocolCodec()
    storage = _Storage()
    storage.transport_epoch_id = str(EPOCH_ID)
    storage.runtime_generation = _DEFAULT_RUNTIME_AUTHORITY.runtime_generation
    storage.fresh_epoch_required = False
    storage.previous_connection_id = str(CONNECTION_ID)
    storage.cursors["cloud.connector.outbound"] = 3
    storage.transport_records = [
        TransportFrameRecord(
            message_id="83000000-0000-4000-8000-000000000001",
            epoch_id=str(EPOCH_ID),
            sequence=1,
            message_type="session.event",
            business_kind="observer",
            business_key="83000000-0000-4000-8000-000000000001",
            business_revision=4,
            runtime_generation="runtime-generation-1",
            frame=b"settled-observer-sequence-1",
            state="settled",
            created_at="now",
            updated_at="now",
            settled_at="now",
        ),
        TransportFrameRecord(
            message_id="86000000-0000-4000-8000-000000000040",
            epoch_id=str(EPOCH_ID),
            sequence=2,
            message_type="command.receipt",
            business_kind="command.receipt",
            business_key="aaaaaaaa-aaaa-4aaa-8aaa-000000000002",
            business_revision=1,
            runtime_generation="runtime-generation-1",
            frame=b"settled-generic-sequence-2",
            state="settled",
            created_at="now",
            updated_at="now",
            settled_at="now",
        ),
    ]
    lane = _ObserverOutboundLane(codec, storage)
    lane.records.append(
        ObserverOutboxRecord(
            message_id="83000000-0000-4000-8000-000000000001",
            payload_digest="a" * 64,
            connector_sequence=1,
            message_type="session.event",
            profile="default",
            session_key="session-root-1",
            runtime_generation="runtime-generation-1",
            runtime_session_id="runtime-session-1",
            event_sequence=4,
            payload=b"settled-observer-payload",
            frame=b"settled-observer-sequence-1",
            state=observer_state,
        )
    )
    connection = _Connection(
        [
            _welcome_frame(
                codec,
                resume_decision="reset_required",
                next_connector_sequence=1,
                max_in_flight=1,
            )
        ]
    )
    client = _client(connection, storage, observer_outbound_lane=lane)

    await client.start()

    assert connection.sent[-2:] == [
        b"settled-observer-sequence-1",
        b"settled-generic-sequence-2",
    ]
    assert lane.records[0].state == observer_state
    assert storage.cursors["cloud.connector.outbound"] == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("observer_state", ("acked", "rejected"))
async def test_same_epoch_rewind_to_zero_replays_the_settled_frame(
    observer_state: str,
) -> None:
    codec = ConnectorProtocolCodec()
    storage = _Storage()
    storage.transport_epoch_id = str(EPOCH_ID)
    storage.runtime_generation = _DEFAULT_RUNTIME_AUTHORITY.runtime_generation
    storage.fresh_epoch_required = False
    storage.previous_connection_id = str(CONNECTION_ID)
    storage.cursors["cloud.connector.outbound"] = 1
    storage.transport_records.append(
        TransportFrameRecord(
            message_id="83000000-0000-4000-8000-000000000001",
            epoch_id=str(EPOCH_ID),
            sequence=0,
            message_type="session.event",
            business_kind="observer",
            business_key="83000000-0000-4000-8000-000000000001",
            business_revision=4,
            runtime_generation="runtime-generation-1",
            frame=b"settled-sequence-zero-frame",
            state="settled",
            created_at="now",
            updated_at="now",
            settled_at="now",
        )
    )
    lane = _ObserverOutboundLane(codec, storage)
    lane.records.append(
        ObserverOutboxRecord(
            message_id="83000000-0000-4000-8000-000000000001",
            payload_digest="a" * 64,
            connector_sequence=0,
            message_type="session.event",
            profile="default",
            session_key="session-root-1",
            runtime_generation="runtime-generation-1",
            runtime_session_id="runtime-session-1",
            event_sequence=4,
            payload=b"settled-sequence-zero-payload",
            frame=b"settled-sequence-zero-frame",
            state=observer_state,
        )
    )
    connection = _Connection(
        [
            _welcome_frame(
                codec,
                resume_decision="reset_required",
                next_connector_sequence=0,
                max_in_flight=1,
            )
        ]
    )
    client = _client(connection, storage, observer_outbound_lane=lane)

    await client.start()

    assert connection.sent[-1] == b"settled-sequence-zero-frame"
    assert lane.records[0].state == observer_state
    assert storage.cursors["cloud.connector.outbound"] == 1


@pytest.mark.asyncio
async def test_new_epoch_rollover_does_not_replay_settled_sequence_zero() -> None:
    codec = ConnectorProtocolCodec()
    storage = _Storage()
    storage.cursors["cloud.connector.outbound"] = 1
    lane = _ObserverOutboundLane(codec, storage)
    lane.records.append(
        ObserverOutboxRecord(
            message_id="83000000-0000-4000-8000-000000000001",
            payload_digest="a" * 64,
            connector_sequence=0,
            message_type="session.event",
            profile="default",
            session_key="session-root-1",
            runtime_generation="runtime-generation-old",
            runtime_session_id="runtime-session-old",
            event_sequence=4,
            payload=b"old-epoch-settled-payload",
            frame=b"old-epoch-settled-frame",
            state="acked",
        )
    )
    connection = _Connection(
        [
            _welcome_frame(
                codec,
                resume_decision="fresh",
                next_connector_sequence=0,
                max_in_flight=1,
            )
        ]
    )
    client = _client(connection, storage, observer_outbound_lane=lane)

    await client.start()

    assert b"old-epoch-settled-frame" not in connection.sent
    assert storage.cursors["cloud.connector.outbound"] == 0
    await client.send_heartbeat()
    connector_heartbeat_envelope = codec.decode_envelope(connection.sent[-1])
    connector_heartbeat = codec.decode_heartbeat_payload(
        connector_heartbeat_envelope.payload
    )
    assert connector_heartbeat_envelope.sequence == 0
    assert connector_heartbeat.next_outbound_sequence == 0
    assert connector_heartbeat.next_inbound_sequence == 0

    cloud_heartbeat = ConnectorHeartbeat(
        connection_id=CONNECTION_ID,
        sender_role="cloud",
        observed_at=NOW,
        next_outbound_sequence=0,
        next_inbound_sequence=1,
        session_state="active",
    )
    connection.inbound.append(
        _envelope(
            codec,
            message_type="connector.heartbeat",
            sequence=0,
            payload=json.loads(codec.encode_heartbeat(cloud_heartbeat)),
        )
    )
    await client.receive_one()

    assert storage.cursors["cloud.connector.outbound"] == 1
    assert storage.cursors["cloud.connector.inbound"] == 1
