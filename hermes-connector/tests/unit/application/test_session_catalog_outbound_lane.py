from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest

from hermes_connector.adapters.cloud.codec import ConnectorProtocolCodec
from hermes_connector.application.session_catalog_outbound_lane import (
    SessionCatalogOutboundLane,
)
from hermes_connector.domain.session_catalog import (
    SessionCatalogEntry,
    SessionCatalogEvent,
    SessionCatalogSnapshotPage,
)
from hermes_connector.domain.storage import SessionCatalogOutboxRecord


class _Storage:
    def __init__(self) -> None:
        self.records: list[SessionCatalogOutboxRecord] = []
        self.retired = 0

    async def get_session_catalog_fact(self, **values: object):
        for record in reversed(self.records):
            if all(getattr(record, key) == value for key, value in values.items()):
                return record
        return None

    async def append_session_catalog_outbox(self, **values: object):
        from hashlib import sha256

        record = SessionCatalogOutboxRecord(
            message_id=str(values["message_id"]),
            payload_digest=sha256(values["payload"]).hexdigest(),
            connector_sequence=int(values["connector_sequence"]),
            message_type=str(values["message_type"]),
            profile=str(values["profile"]),
            runtime_generation=str(values["runtime_generation"]),
            snapshot_id=values["snapshot_id"],
            catalog_revision=values["catalog_revision"],
            page_index=values["page_index"],
            is_last=values["is_last"],
            catalog_sequence=values["catalog_sequence"],
            payload=values["payload"],
            frame=values["frame"],
            state="pending",
            transport_epoch_id=values["transport_epoch_id"],
        )
        self.records.append(record)
        return record

    async def pending_session_catalog_outbox(self, **_values: object):
        return tuple(record for record in self.records if record.state == "pending")

    async def ack_session_catalog_outbox(self, **_values: object):
        return self.records[-1]

    async def nack_session_catalog_outbox(self, **_values: object):
        return self.records[-1]

    async def retire_session_catalog_outbox(self) -> None:
        self.retired += 1


def _page() -> SessionCatalogSnapshotPage:
    return SessionCatalogSnapshotPage(
        profile="default",
        runtime_generation="runtime-generation-1",
        snapshot_id=UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
        catalog_revision=7,
        page_index=0,
        is_last=True,
        sessions=(
            SessionCatalogEntry(
                session_key="durable-session-real",
                surface="gateway",
                authority_revision=3,
                available_actions=("prompt.submit",),
            ),
        ),
    )


@pytest.mark.asyncio
async def test_catalog_page_is_enveloped_and_transport_send_does_not_settle() -> None:
    storage = _Storage()
    codec = ConnectorProtocolCodec()
    lane = SessionCatalogOutboundLane(
        storage=storage,
        codec=codec,
        tenant_id="tenant-1",
        device_id="device-1",
        utc_now=lambda: datetime(2026, 8, 3, tzinfo=UTC),
        message_id_factory=lambda: UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
    )

    record = await lane.stage_snapshot_page(
        _page(),
        connector_sequence=12,
        transport_epoch_id="cccccccc-cccc-4ccc-8ccc-cccccccccccc",
    )
    await lane.transport_sent(record)

    envelope = codec.decode_envelope(record.frame)
    assert envelope.message_type == "session.catalog.snapshot.page"
    assert envelope.payload["snapshot_id"] == "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    assert "session_id" not in envelope.payload
    assert record.state == "pending"


@pytest.mark.asyncio
async def test_catalog_event_uses_host_session_key_without_cloud_uuid_mapping() -> None:
    storage = _Storage()
    codec = ConnectorProtocolCodec()
    lane = SessionCatalogOutboundLane(
        storage=storage,
        codec=codec,
        tenant_id="tenant-1",
        device_id="device-1",
        message_id_factory=lambda: UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd"),
    )
    event = SessionCatalogEvent(
        profile="default",
        runtime_generation="runtime-generation-1",
        catalog_sequence=8,
        action="upsert",
        entry=_page().sessions[0],
    )

    record = await lane.stage_event(event, connector_sequence=13)
    payload = codec.decode_envelope(record.frame).payload

    assert payload["entry"]["session_key"] == "durable-session-real"
    assert set(payload) == {
        "profile",
        "runtime_generation",
        "catalog_sequence",
        "action",
        "entry",
    }


@pytest.mark.asyncio
async def test_catalog_lane_retires_pending_attempts_on_explicit_capability_loss() -> None:
    storage = _Storage()
    lane = SessionCatalogOutboundLane(
        storage=storage,
        codec=ConnectorProtocolCodec(),
        tenant_id="tenant-1",
        device_id="device-1",
    )

    await lane.retire_pending()

    assert storage.retired == 1
