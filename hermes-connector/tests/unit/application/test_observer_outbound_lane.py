from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from uuid import UUID

import pytest

from hermes_connector.adapters.cloud.codec import (
    ConnectorProtocolCodec,
    InvalidCloudFrame,
)
from hermes_connector.adapters.sqlite_storage import SQLiteStorageComponent
from hermes_connector.application.observer_outbound_lane import ObserverOutboundLane
from hermes_connector.bootstrap.config import ConnectorConfig
from hermes_connector.domain.canonical_json import canonical_json_bytes

CONTRACTS = Path(__file__).parents[4] / "contracts"
VALID = CONTRACTS / "fixtures" / "valid"
MESSAGE_ID = UUID("83000000-0000-4000-8000-000000000001")
GENERATED_DIGEST_VECTOR = (
    Path(__file__).parents[3]
    / "src/hermes_connector/contracts/generated/canonical-payload-digest-v1.json"
)

_CREDENTIAL_LIKE_VALUES = (
    "Authorization: Basic dXNlcjpwYXNz",
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.c2ln",
    "password=hunter2",
    "secret: swordfish",
    "token=opaque-token-value",
    "api-key: abcdefghijklmnop",
    "AKIAABCDEFGHIJKLMNOP",
    "AIzaSyDUMMYKEY012345678901234567890123",
    "ya29.a0AfH6SMB0123456789abcdefghijklmnopqrstuvwxyz",
    "github_pat_11AA0123456789abcdefghijklmnopqrstuvwxyz",
    "glpat-0123456789abcdefghijklmnop",
    "sk-ant-api03-0123456789abcdefghijklmnop",
    "hf_0123456789abcdefghijklmnop",
)

_SAFE_CREDENTIAL_ADJACENT_VALUES = (
    "Basic authentication is disabled.",
    "Basic YWJjZA== is not a user-password credential.",
    "release v1.2.3 is available from api.example.com.",
    "tokenizer/pathology analysis complete",
)


async def _start(
    path: Path,
) -> tuple[SQLiteStorageComponent, asyncio.Task[None]]:
    storage = SQLiteStorageComponent(path, ConnectorConfig())
    await storage.start()
    runner = asyncio.create_task(storage.run())
    assert await storage.ready()
    return storage, runner


async def _stop(
    storage: SQLiteStorageComponent,
    runner: asyncio.Task[None],
) -> None:
    await storage.drain()
    await storage.stop()
    await runner


@pytest.mark.asyncio
async def test_stage_event_persists_canonical_envelope_before_transport_use(
    tmp_path: Path,
) -> None:
    storage, runner = await _start(tmp_path / "connector.sqlite3")
    codec = ConnectorProtocolCodec()
    lane = ObserverOutboundLane(
        storage=storage,
        codec=codec,
        tenant_id="tenant-test",
        device_id="device-test",
        utc_now=lambda: datetime(2026, 7, 31, 9, 0, tzinfo=UTC),
        message_id_factory=lambda: MESSAGE_ID,
    )
    event = codec.decode_session_event(
        (VALID / "session-event-payload.json").read_bytes()
    )

    staged = await lane.stage_event(event, connector_sequence=41)

    assert staged.message_id == str(MESSAGE_ID)
    assert staged.state == "pending"
    envelope = codec.decode_envelope(staged.frame)
    assert envelope.message_id == MESSAGE_ID
    assert envelope.sequence == 41
    assert envelope.message_type == "session.event"
    assert envelope.idempotency_key == str(MESSAGE_ID)
    assert codec.decode_session_event_payload(envelope.payload) == event
    await _stop(storage, runner)


@pytest.mark.asyncio
async def test_stage_v2_event_uses_versioned_message_type_without_downgrade(
    tmp_path: Path,
) -> None:
    storage, runner = await _start(tmp_path / "connector.sqlite3")
    codec = ConnectorProtocolCodec()
    lane = ObserverOutboundLane(
        storage=storage,
        codec=codec,
        tenant_id="tenant-test",
        device_id="device-test",
        utc_now=lambda: datetime(2026, 7, 31, 9, 0, tzinfo=UTC),
        message_id_factory=lambda: MESSAGE_ID,
    )
    event = codec.decode_session_event_v2(
        (VALID / "session-event-v2-tool-upsert.json").read_bytes()
    )

    staged = await lane.stage_event(event, connector_sequence=41)

    envelope = codec.decode_envelope(staged.frame)
    assert staged.message_type == "session.event.v2"
    assert envelope.message_type == "session.event.v2"
    assert codec.decode_session_event_v2_payload(envelope.payload) == event
    await _stop(storage, runner)


@pytest.mark.asyncio
async def test_unsafe_v2_extension_never_reaches_observer_outbox(
    tmp_path: Path,
) -> None:
    storage, runner = await _start(tmp_path / "connector.sqlite3")
    codec = ConnectorProtocolCodec()
    lane = ObserverOutboundLane(
        storage=storage,
        codec=codec,
        tenant_id="tenant-test",
        device_id="device-test",
        message_id_factory=lambda: MESSAGE_ID,
    )
    event = codec.decode_session_event_v2(
        (VALID / "session-event-v2-tool-upsert.json").read_bytes()
    )
    unsafe = replace(
        event,
        extensions=MappingProxyType(
            {"vendor.private": MappingProxyType({"tool_args": "--secret"})}
        ),
    )

    with pytest.raises(InvalidCloudFrame):
        await lane.stage_event(unsafe, connector_sequence=41)

    assert await storage.pending_observer_outbox(limit=10) == ()
    await _stop(storage, runner)


@pytest.mark.parametrize("credential_like_value", _CREDENTIAL_LIKE_VALUES)
@pytest.mark.parametrize("fact_kind", ("snapshot", "event"))
@pytest.mark.asyncio
async def test_credential_like_v2_values_fail_before_orm_outbox_without_echo(
    tmp_path: Path,
    fact_kind: str,
    credential_like_value: str,
) -> None:
    storage, runner = await _start(tmp_path / "connector.sqlite3")
    codec = ConnectorProtocolCodec()
    lane = ObserverOutboundLane(
        storage=storage,
        codec=codec,
        tenant_id="tenant-test",
        device_id="device-test",
        message_id_factory=lambda: MESSAGE_ID,
    )
    if fact_kind == "snapshot":
        fact = replace(
            codec.decode_session_snapshot_v2(
                (VALID / "session-snapshot-v2-payload.json").read_bytes()
            ),
            extensions=MappingProxyType(
                {
                    "vendor.display": MappingProxyType(
                        {"note": credential_like_value}
                    )
                }
            ),
        )
        stage = lane.stage_snapshot(fact, connector_sequence=41)
    else:
        fact = replace(
            codec.decode_session_event_v2(
                (VALID / "session-event-v2-tool-upsert.json").read_bytes()
            ),
            extensions=MappingProxyType(
                {
                    "vendor.display": MappingProxyType(
                        {"note": credential_like_value}
                    )
                }
            ),
        )
        stage = lane.stage_event(fact, connector_sequence=41)

    with pytest.raises(InvalidCloudFrame) as raised:
        await stage

    assert credential_like_value not in str(raised.value)
    assert await storage.pending_observer_outbox(limit=10) == ()
    assert await storage.get_observer_outbox(str(MESSAGE_ID)) is None
    await _stop(storage, runner)


@pytest.mark.parametrize("safe_value", _SAFE_CREDENTIAL_ADJACENT_VALUES)
@pytest.mark.asyncio
async def test_safe_v2_text_and_nonnegative_token_counts_reach_orm_outbox(
    tmp_path: Path,
    safe_value: str,
) -> None:
    storage, runner = await _start(tmp_path / "connector.sqlite3")
    codec = ConnectorProtocolCodec()
    lane = ObserverOutboundLane(
        storage=storage,
        codec=codec,
        tenant_id="tenant-test",
        device_id="device-test",
        message_id_factory=lambda: MESSAGE_ID,
    )
    event = replace(
        codec.decode_session_event_v2(
            (VALID / "session-event-v2-tool-upsert.json").read_bytes()
        ),
        extensions=MappingProxyType(
            {
                "vendor.metrics": MappingProxyType(
                    {
                        "note": safe_value,
                        "token_counts": MappingProxyType(
                            {"input": 10, "output": 2, "reasoning": 1}
                        ),
                    }
                )
            }
        ),
    )

    staged = await lane.stage_event(event, connector_sequence=41)

    assert staged.state == "pending"
    assert len(await storage.pending_observer_outbox(limit=10)) == 1
    await _stop(storage, runner)


@pytest.mark.asyncio
async def test_same_observer_fact_reuses_first_message_id_sequence_and_frame(
    tmp_path: Path,
) -> None:
    storage, runner = await _start(tmp_path / "connector.sqlite3")
    codec = ConnectorProtocolCodec()
    event = codec.decode_session_event(
        (VALID / "session-event-payload.json").read_bytes()
    )
    first_lane = ObserverOutboundLane(
        storage=storage,
        codec=codec,
        tenant_id="tenant-test",
        device_id="device-test",
        message_id_factory=lambda: MESSAGE_ID,
    )
    first = await first_lane.stage_event(event, connector_sequence=41)
    second_lane = ObserverOutboundLane(
        storage=storage,
        codec=codec,
        tenant_id="tenant-test",
        device_id="device-test",
        message_id_factory=lambda: UUID("83000000-0000-4000-8000-000000000099"),
    )

    second = await second_lane.stage_event(event, connector_sequence=99)

    assert second.message_id == first.message_id
    assert second.connector_sequence == first.connector_sequence
    assert second.frame == first.frame
    await _stop(storage, runner)


@pytest.mark.asyncio
async def test_transport_write_never_acknowledges_business_fact(
    tmp_path: Path,
) -> None:
    storage, runner = await _start(tmp_path / "connector.sqlite3")
    codec = ConnectorProtocolCodec()
    lane = ObserverOutboundLane(
        storage=storage,
        codec=codec,
        tenant_id="tenant-test",
        device_id="device-test",
        message_id_factory=lambda: MESSAGE_ID,
    )
    event = codec.decode_session_event(
        (VALID / "session-event-payload.json").read_bytes()
    )
    staged = await lane.stage_event(event, connector_sequence=41)

    await lane.transport_sent(staged)

    assert (await storage.get_observer_outbox(str(MESSAGE_ID))).state == "pending"
    ack = codec.decode_stream_ack((VALID / "stream-ack-payload.json").read_bytes())
    ack = replace(
        ack,
        payload_digest=staged.payload_digest,
        event_sequence=event.event_sequence,
    )
    committed = await lane.acknowledge(ack)
    assert committed.state == "acked"
    await _stop(storage, runner)


@pytest.mark.asyncio
async def test_rejected_fact_stages_new_message_sequence_and_frame_attempt(
    tmp_path: Path,
) -> None:
    storage, runner = await _start(tmp_path / "connector.sqlite3")
    codec = ConnectorProtocolCodec()
    ids = iter(
        (
            MESSAGE_ID,
            UUID("83000000-0000-4000-8000-000000000002"),
        )
    )
    lane = ObserverOutboundLane(
        storage=storage,
        codec=codec,
        tenant_id="tenant-test",
        device_id="device-test",
        message_id_factory=lambda: next(ids),
    )
    event = codec.decode_session_event(
        (VALID / "session-event-payload.json").read_bytes()
    )
    first = await lane.stage_event(event, connector_sequence=41)
    nack = codec.decode_stream_nack((VALID / "stream-nack-payload.json").read_bytes())
    nack = replace(
        nack,
        observer_message_id=UUID(first.message_id),
        payload_digest=first.payload_digest,
        connector_sequence=first.connector_sequence,
        observer_message_type="session.event",
        event_sequence=event.event_sequence,
    )
    await lane.reject(nack)

    replacement = await lane.stage_event(event, connector_sequence=42)

    assert replacement.message_id != first.message_id
    assert replacement.connector_sequence == 42
    assert replacement.frame != first.frame
    assert replacement.state == "pending"
    assert (await storage.get_observer_outbox(first.message_id)).state == "rejected"
    await _stop(storage, runner)


@pytest.mark.asyncio
async def test_recovery_snapshot_forces_new_attempt_even_after_prior_ack(
    tmp_path: Path,
) -> None:
    storage, runner = await _start(tmp_path / "connector.sqlite3")
    codec = ConnectorProtocolCodec()
    ids = iter(
        (
            MESSAGE_ID,
            UUID("83000000-0000-4000-8000-000000000002"),
        )
    )
    lane = ObserverOutboundLane(
        storage=storage,
        codec=codec,
        tenant_id="tenant-test",
        device_id="device-test",
        message_id_factory=lambda: next(ids),
    )
    snapshot = codec.decode_session_snapshot(
        (VALID / "session-snapshot-payload.json").read_bytes()
    )
    first = await lane.stage_snapshot(snapshot, connector_sequence=41)
    ack = codec.decode_stream_ack((VALID / "stream-ack-payload.json").read_bytes())
    ack = replace(
        ack,
        observer_message_id=UUID(first.message_id),
        payload_digest=first.payload_digest,
        connector_sequence=first.connector_sequence,
        observer_message_type="session.snapshot",
        event_sequence=snapshot.event_sequence,
    )
    await lane.acknowledge(ack)

    replacement = await lane.stage_snapshot(
        snapshot,
        connector_sequence=42,
        force_new_attempt=True,
    )

    assert replacement.message_id != first.message_id
    assert replacement.connector_sequence == 42
    assert replacement.frame != first.frame
    await _stop(storage, runner)


@pytest.mark.asyncio
async def test_actual_orm_outbox_digest_consumes_generated_unicode_vector(
    tmp_path: Path,
) -> None:
    contract = json.loads(GENERATED_DIGEST_VECTOR.read_text(encoding="utf-8"))
    vector = contract["vectors"][0]
    payload = canonical_json_bytes(vector["payload"])
    real_codec = ConnectorProtocolCodec()
    event = real_codec.decode_session_event(
        (VALID / "session-event-payload.json").read_bytes()
    )

    class _VectorCodec:
        def encode_session_event(self, _message: object) -> bytes:
            return payload

        def session_event_payload(self, _message: object) -> dict[str, object]:
            return vector["payload"]

        def encode_envelope(self, _message: object) -> bytes:
            return b"{}"

    storage, runner = await _start(tmp_path / "connector.sqlite3")
    lane = ObserverOutboundLane(
        storage=storage,
        codec=_VectorCodec(),  # type: ignore[arg-type]
        tenant_id="tenant-test",
        device_id="device-test",
        message_id_factory=lambda: MESSAGE_ID,
    )

    staged = await lane.stage_event(event, connector_sequence=41)

    assert staged.payload == payload
    assert staged.payload_digest == vector["sha256"]
    assert b"\\u" not in staged.payload
    await _stop(storage, runner)
