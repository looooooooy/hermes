from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from hermes_cloud.adapters.connector_contract_v1 import (
    CloudEnvelopeV1Adapter,
    ContractConformanceError,
)
from hermes_cloud.domain.connector_gateway import ConnectorIdentity
from hermes_cloud.domain.contract_models import CloudEnvelope
from hermes_cloud.platform.postgres.models import (
    AgentModel,
    ConnectorTransportCursorModel,
    ConnectorTransportHandshakeOwnershipModel,
    DeviceLifecycleModel,
    DeviceModel,
    SessionProjectionModel,
    TenantModel,
    WorkspaceMembershipModel,
    WorkspaceModel,
)
from hermes_cloud.platform.sqlalchemy.connector_transport_cursor import (
    SqlAlchemyConnectorTransportCursorAuthority,
)
from hermes_cloud.platform.sqlalchemy.session_catalog import (
    SqlAlchemySessionCatalogIngress,
    SqlAlchemySessionCatalogRepository,
)
from hermes_cloud.platform.sqlalchemy.session_catalog_models import (
    SessionCatalogAuthorityModel,
    SessionCatalogBase,
    SessionCatalogEntryModel,
    SessionCatalogInboxModel,
    SessionCatalogSnapshotPageModel,
)
from hermes_cloud.platform.sqlite.schema import (
    SQLITE_SCHEMA_TRANSLATE_MAP,
    build_sqlite_metadata,
)

TENANT_ID = UUID("10000000-0000-4000-8000-000000000001")
USER_ID = UUID("20000000-0000-4000-8000-000000000001")
WORKSPACE_ID = UUID("30000000-0000-4000-8000-000000000001")
AGENT_ID = UUID("50000000-0000-4000-8000-000000000001")
DEVICE_ID = UUID("60000000-0000-4000-8000-000000000001")
NOW = datetime(2026, 8, 3, 10, 0, tzinfo=UTC)


def _entry(key: str, revision: int = 1) -> dict[str, object]:
    return {
        "session_key": key,
        "surface": "hermes-cli",
        "authority_revision": revision,
        "available_actions": ["prompt.submit", "session.interrupt"],
    }


def _envelope(
    *,
    message_id: str,
    sequence: int,
    message_type: str,
    payload: dict[str, object],
) -> CloudEnvelope:
    return CloudEnvelope(
        contract_version=1,
        message_id=message_id,
        message_type=message_type,
        tenant_id=str(TENANT_ID),
        device_id=str(DEVICE_ID),
        sequence=sequence,
        sent_at="2026-08-03T02:00:00Z",
        payload=payload,
    )


@pytest.fixture
def catalog_store():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        execution_options={"schema_translate_map": SQLITE_SCHEMA_TRANSLATE_MAP},
    )
    build_sqlite_metadata().create_all(engine)
    SessionCatalogBase.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with Session(engine) as session, session.begin():
        session.add(
            TenantModel(
                tenant_id=TENANT_ID,
                slug="catalog-test",
                display_name="Catalog Test",
                status="active",
                created_at=NOW,
            )
        )
        session.add(
            WorkspaceModel(
                tenant_id=TENANT_ID,
                workspace_id=WORKSPACE_ID,
                workspace_key="catalog-workspace",
                display_name="Catalog Workspace",
                status="active",
                created_by=USER_ID,
                created_at=NOW,
            )
        )
        session.flush()
        session.add(
            WorkspaceMembershipModel(
                tenant_id=TENANT_ID,
                workspace_membership_id=UUID(
                    "40000000-0000-4000-8000-000000000001"
                ),
                workspace_id=WORKSPACE_ID,
                user_id=USER_ID,
                role_id=UUID("40000000-0000-4000-8000-000000000002"),
                status="active",
                joined_at=NOW,
                revoked_at=None,
            )
        )
        session.add(
            AgentModel(
                tenant_id=TENANT_ID,
                agent_id=AGENT_ID,
                workspace_id=WORKSPACE_ID,
                agent_key="catalog-agent",
                status="active",
                last_seen_at=NOW,
                created_at=NOW,
            )
        )
        session.flush()
        session.add(
            DeviceModel(
                tenant_id=TENANT_ID,
                device_id=DEVICE_ID,
                agent_id=AGENT_ID,
                workspace_id=WORKSPACE_ID,
                device_key="catalog-device",
                status="active",
                created_at=NOW,
            )
        )
        session.flush()
        session.add(
            DeviceLifecycleModel(
                tenant_id=TENANT_ID,
                device_id=DEVICE_ID,
                workspace_id=WORKSPACE_ID,
                agent_id=AGENT_ID,
                state="active",
                revision=3,
                updated_at=NOW,
            )
        )
    try:
        yield engine, factory
    finally:
        engine.dispose()


def _identity() -> ConnectorIdentity:
    return ConnectorIdentity(
        tenant_id=str(TENANT_ID),
        device_id=str(DEVICE_ID),
        agent_id=str(AGENT_ID),
        scopes=("session.observe",),
        legacy_seed=False,
    )


def test_codec_decodes_frozen_catalog_payloads_and_rejects_identity_leaks() -> None:
    codec = CloudEnvelopeV1Adapter()
    page = {
        "profile": "default",
        "runtime_generation": "runtime-a",
        "snapshot_id": "71000000-0000-4000-8000-000000000001",
        "catalog_revision": 7,
        "page_index": 0,
        "is_last": True,
        "sessions": [_entry("host-session-1")],
    }

    decoded = codec.decode_session_catalog_snapshot_page(page)
    assert decoded.profile == "default"
    assert decoded.sessions[0].session_key == "host-session-1"

    with pytest.raises(ContractConformanceError) as error:
        codec.decode_session_catalog_event(
            {
                "profile": "default",
                "runtime_generation": "runtime-a",
                "catalog_sequence": 8,
                "action": "upsert",
                "entry": {**_entry("host-session-1"), "agent_id": str(AGENT_ID)},
            }
        )
    assert error.value.category == "invalid_envelope"


@pytest.mark.asyncio
async def test_snapshot_is_invisible_until_terminal_page_then_commits_atomically(
    catalog_store,
) -> None:
    engine, factory = catalog_store
    ingress = SqlAlchemySessionCatalogIngress(factory, now=lambda: NOW)
    snapshot_id = "71000000-0000-4000-8000-000000000001"
    page0 = CloudEnvelopeV1Adapter().decode_session_catalog_snapshot_page(
        {
            "profile": "default",
            "runtime_generation": "runtime-a",
            "snapshot_id": snapshot_id,
            "catalog_revision": 7,
            "page_index": 0,
            "is_last": False,
            "sessions": [_entry("host-session-1")],
        }
    )
    result = await ingress.accept_snapshot_page(
        identity=_identity(),
        connection_id="72000000-0000-4000-8000-000000000001",
        connector_instance_id="73000000-0000-4000-8000-000000000001",
        runtime_generation="runtime-a",
        envelope=_envelope(
            message_id="74000000-0000-4000-8000-000000000001",
            sequence=2,
            message_type="session.catalog.snapshot.page",
            payload={
                "profile": "default",
                "runtime_generation": "runtime-a",
                "snapshot_id": snapshot_id,
                "catalog_revision": 7,
                "page_index": 0,
                "is_last": False,
                "sessions": [_entry("host-session-1")],
            },
        ),
        payload=page0,
    )
    assert result is None
    with Session(engine) as session:
        assert session.scalars(select(SessionCatalogEntryModel)).all() == []

    final_payload = {
        "profile": "default",
        "runtime_generation": "runtime-a",
        "snapshot_id": snapshot_id,
        "catalog_revision": 7,
        "page_index": 1,
        "is_last": True,
        "sessions": [_entry("host-session-2")],
    }
    receipt = await ingress.accept_snapshot_page(
        identity=_identity(),
        connection_id="72000000-0000-4000-8000-000000000001",
        connector_instance_id="73000000-0000-4000-8000-000000000001",
        runtime_generation="runtime-a",
        envelope=_envelope(
            message_id="74000000-0000-4000-8000-000000000002",
            sequence=3,
            message_type="session.catalog.snapshot.page",
            payload=final_payload,
        ),
        payload=CloudEnvelopeV1Adapter().decode_session_catalog_snapshot_page(
            final_payload
        ),
    )
    assert receipt is not None
    assert receipt.message_type == "session.catalog.ack"
    assert receipt.payload["ack_kind"] == "snapshot_committed"
    with Session(engine) as session:
        rows = session.scalars(
            select(SessionCatalogEntryModel).order_by(
                SessionCatalogEntryModel.session_key
            )
        ).all()
        assert [row.session_key for row in rows if row.active] == [
            "host-session-1",
            "host-session-2",
        ]
        assert all(isinstance(row.session_id, UUID) for row in rows)


@pytest.mark.asyncio
async def test_page_gap_and_event_gap_are_durable_contract_nacks(catalog_store) -> None:
    _engine, factory = catalog_store
    ingress = SqlAlchemySessionCatalogIngress(factory, now=lambda: NOW)
    payload = {
        "profile": "default",
        "runtime_generation": "runtime-a",
        "snapshot_id": "71000000-0000-4000-8000-000000000001",
        "catalog_revision": 7,
        "page_index": 1,
        "is_last": True,
        "sessions": [_entry("host-session-1")],
    }
    receipt = await ingress.accept_snapshot_page(
        identity=_identity(),
        connection_id="72000000-0000-4000-8000-000000000001",
        connector_instance_id="73000000-0000-4000-8000-000000000001",
        runtime_generation="runtime-a",
        envelope=_envelope(
            message_id="74000000-0000-4000-8000-000000000003",
            sequence=2,
            message_type="session.catalog.snapshot.page",
            payload=payload,
        ),
        payload=CloudEnvelopeV1Adapter().decode_session_catalog_snapshot_page(payload),
    )
    assert receipt is not None
    assert receipt.message_type == "session.catalog.nack"
    assert receipt.payload["reason"] == "page_gap"
    assert receipt.payload["expected_page_index"] == 0


@pytest.mark.asyncio
async def test_terminal_ack_and_event_gap_nack_replay_after_ingress_restart(
    catalog_store,
) -> None:
    engine, factory = catalog_store
    snapshot = {
        "profile": "default",
        "runtime_generation": "runtime-a",
        "snapshot_id": "71000000-0000-4000-8000-000000000010",
        "catalog_revision": 7,
        "page_index": 0,
        "is_last": True,
        "sessions": [_entry("host-session-1")],
    }
    snapshot_envelope = _envelope(
        message_id="74000000-0000-4000-8000-000000000010",
        sequence=10,
        message_type="session.catalog.snapshot.page",
        payload=snapshot,
    )
    first_ingress = SqlAlchemySessionCatalogIngress(factory, now=lambda: NOW)
    first_ack = await first_ingress.accept_snapshot_page(
        identity=_identity(),
        connection_id="72000000-0000-4000-8000-000000000001",
        connector_instance_id="73000000-0000-4000-8000-000000000001",
        runtime_generation="runtime-a",
        envelope=snapshot_envelope,
        payload=CloudEnvelopeV1Adapter().decode_session_catalog_snapshot_page(
            snapshot
        ),
    )

    restarted_ingress = SqlAlchemySessionCatalogIngress(factory, now=lambda: NOW)
    replayed_ack = await restarted_ingress.accept_snapshot_page(
        identity=_identity(),
        connection_id="72000000-0000-4000-8000-000000000099",
        connector_instance_id="73000000-0000-4000-8000-000000000001",
        runtime_generation="runtime-a",
        envelope=snapshot_envelope,
        payload=CloudEnvelopeV1Adapter().decode_session_catalog_snapshot_page(
            snapshot
        ),
    )
    assert replayed_ack == first_ack

    event = {
        "profile": "default",
        "runtime_generation": "runtime-a",
        "catalog_sequence": 9,
        "action": "upsert",
        "entry": _entry("host-session-2"),
    }
    event_envelope = _envelope(
        message_id="74000000-0000-4000-8000-000000000011",
        sequence=11,
        message_type="session.catalog.event",
        payload=event,
    )
    first_nack = await restarted_ingress.accept_event(
        identity=_identity(),
        connection_id="72000000-0000-4000-8000-000000000099",
        connector_instance_id="73000000-0000-4000-8000-000000000001",
        runtime_generation="runtime-a",
        envelope=event_envelope,
        payload=CloudEnvelopeV1Adapter().decode_session_catalog_event(event),
    )
    assert first_nack.message_type == "session.catalog.nack"
    assert first_nack.payload["reason"] == "event_gap"
    assert first_nack.payload["expected_catalog_sequence"] == 8

    replayed_nack = await SqlAlchemySessionCatalogIngress(
        factory, now=lambda: NOW
    ).accept_event(
        identity=_identity(),
        connection_id="72000000-0000-4000-8000-000000000100",
        connector_instance_id="73000000-0000-4000-8000-000000000001",
        runtime_generation="runtime-a",
        envelope=event_envelope,
        payload=CloudEnvelopeV1Adapter().decode_session_catalog_event(event),
    )
    assert replayed_nack == first_nack
    with Session(engine) as session:
        assert len(session.scalars(select(SessionCatalogInboxModel)).all()) == 2


@pytest.mark.asyncio
async def test_message_or_sequence_collision_returns_contract_nack(catalog_store) -> None:
    _engine, factory = catalog_store
    ingress = SqlAlchemySessionCatalogIngress(factory, now=lambda: NOW)
    original = {
        "profile": "default",
        "runtime_generation": "runtime-a",
        "snapshot_id": "71000000-0000-4000-8000-000000000020",
        "catalog_revision": 1,
        "page_index": 0,
        "is_last": True,
        "sessions": [_entry("host-session-1")],
    }
    original_envelope = _envelope(
        message_id="74000000-0000-4000-8000-000000000020",
        sequence=20,
        message_type="session.catalog.snapshot.page",
        payload=original,
    )
    await ingress.accept_snapshot_page(
        identity=_identity(),
        connection_id="72000000-0000-4000-8000-000000000001",
        connector_instance_id="73000000-0000-4000-8000-000000000001",
        runtime_generation="runtime-a",
        envelope=original_envelope,
        payload=CloudEnvelopeV1Adapter().decode_session_catalog_snapshot_page(
            original
        ),
    )

    mutated = {**original, "catalog_revision": 2}
    collision = await ingress.accept_snapshot_page(
        identity=_identity(),
        connection_id="72000000-0000-4000-8000-000000000001",
        connector_instance_id="73000000-0000-4000-8000-000000000001",
        runtime_generation="runtime-a",
        envelope=_envelope(
            message_id=original_envelope.message_id,
            sequence=20,
            message_type="session.catalog.snapshot.page",
            payload=mutated,
        ),
        payload=CloudEnvelopeV1Adapter().decode_session_catalog_snapshot_page(
            mutated
        ),
    )
    assert collision is not None
    assert collision.message_type == "session.catalog.nack"
    assert collision.payload["reason"] == "contract_mismatch"


@pytest.mark.asyncio
async def test_atomic_contract_collision_advances_cursor_and_requires_snapshot(
    catalog_store,
) -> None:
    engine, factory = catalog_store
    connection_id = UUID("72000000-0000-4000-8000-000000000021")
    connector_instance_id = UUID("73000000-0000-4000-8000-000000000021")
    with Session(engine) as session, session.begin():
        session.add(
            ConnectorTransportCursorModel(
                tenant_id=TENANT_ID,
                device_id=DEVICE_ID,
                connector_instance_id=connector_instance_id,
                runtime_generation="runtime-a",
                connection_id=connection_id,
                state="active",
                next_connector_sequence=1,
                next_cloud_sequence=1,
                revision=1,
                connected_at=NOW,
                updated_at=NOW,
            )
        )
        session.add(
            ConnectorTransportHandshakeOwnershipModel(
                tenant_id=TENANT_ID,
                device_id=DEVICE_ID,
                connector_instance_id=connector_instance_id,
                runtime_generation="runtime-a",
                connection_id=connection_id,
                previous_connection_id=None,
                resume_decision="fresh",
                handshake_disposition="advance",
                state="active",
                expected_next_connector_sequence=0,
                expected_next_cloud_sequence=0,
                next_connector_sequence=1,
                next_cloud_sequence=1,
                revision=1,
                lease_expires_at=NOW + timedelta(minutes=5),
                prepared_at=NOW,
                updated_at=NOW,
            )
        )

    ingress = SqlAlchemySessionCatalogIngress(factory, now=lambda: NOW)
    original = {
        "profile": "default",
        "runtime_generation": "runtime-a",
        "snapshot_id": "71000000-0000-4000-8000-000000000021",
        "catalog_revision": 1,
        "page_index": 0,
        "is_last": True,
        "sessions": [_entry("host-session-1")],
    }
    message_id = "74000000-0000-4000-8000-000000000021"
    first = await ingress.accept_snapshot_page_and_advance(
        identity=_identity(),
        connection_id=str(connection_id),
        connector_instance_id=str(connector_instance_id),
        runtime_generation="runtime-a",
        envelope=_envelope(
            message_id=message_id,
            sequence=1,
            message_type="session.catalog.snapshot.page",
            payload=original,
        ),
        payload=CloudEnvelopeV1Adapter().decode_session_catalog_snapshot_page(
            original
        ),
        expected_next_connector_sequence=1,
        expected_next_cloud_sequence=1,
    )
    assert first is not None and first.message_type == "session.catalog.ack"

    conflicting = {**original, "catalog_revision": 2}
    collision = await ingress.accept_snapshot_page_and_advance(
        identity=_identity(),
        connection_id=str(connection_id),
        connector_instance_id=str(connector_instance_id),
        runtime_generation="runtime-a",
        envelope=_envelope(
            message_id=message_id,
            sequence=2,
            message_type="session.catalog.snapshot.page",
            payload=conflicting,
        ),
        payload=CloudEnvelopeV1Adapter().decode_session_catalog_snapshot_page(
            conflicting
        ),
        expected_next_connector_sequence=2,
        expected_next_cloud_sequence=2,
    )
    assert collision is not None
    assert collision.message_type == "session.catalog.nack"
    assert collision.payload["reason"] == "contract_mismatch"
    with Session(engine) as session:
        cursor = session.scalar(
            select(ConnectorTransportCursorModel).where(
                ConnectorTransportCursorModel.tenant_id == TENANT_ID,
                ConnectorTransportCursorModel.device_id == DEVICE_ID,
                ConnectorTransportCursorModel.connector_instance_id
                == connector_instance_id,
                ConnectorTransportCursorModel.runtime_generation == "runtime-a",
            )
        )
        authority = session.scalars(select(SessionCatalogAuthorityModel)).one()
        assert cursor is not None
        assert cursor.next_connector_sequence == 3
        assert cursor.next_cloud_sequence == 3
        assert authority.require_full_snapshot is True


@pytest.mark.asyncio
async def test_stable_id_is_reused_across_remove_and_generation_rollover(
    catalog_store,
) -> None:
    engine, factory = catalog_store
    ingress = SqlAlchemySessionCatalogIngress(factory, now=lambda: NOW)

    async def commit_snapshot(generation: str, snapshot_id: str, sequence: int) -> None:
        raw = {
            "profile": "default",
            "runtime_generation": generation,
            "snapshot_id": snapshot_id,
            "catalog_revision": 1,
            "page_index": 0,
            "is_last": True,
            "sessions": [_entry("host-session-1")],
        }
        await ingress.accept_snapshot_page(
            identity=_identity(),
            connection_id="72000000-0000-4000-8000-000000000001",
            connector_instance_id="73000000-0000-4000-8000-000000000001",
            runtime_generation=generation,
            envelope=_envelope(
                message_id=f"74000000-0000-4000-8000-{sequence:012d}",
                sequence=sequence,
                message_type="session.catalog.snapshot.page",
                payload=raw,
            ),
            payload=CloudEnvelopeV1Adapter().decode_session_catalog_snapshot_page(raw),
        )

    await commit_snapshot(
        "runtime-a", "71000000-0000-4000-8000-000000000001", 2
    )
    with Session(engine) as session:
        first_id = session.scalar(
            select(SessionCatalogEntryModel.session_id).where(
                SessionCatalogEntryModel.session_key == "host-session-1"
            )
        )
    await commit_snapshot(
        "runtime-b", "71000000-0000-4000-8000-000000000002", 3
    )
    with Session(engine) as session:
        second_id = session.scalar(
            select(SessionCatalogEntryModel.session_id).where(
                SessionCatalogEntryModel.session_key == "host-session-1"
            )
        )
    assert second_id == first_id


def test_catalog_repository_lists_only_active_catalog_rows(catalog_store) -> None:
    _engine, factory = catalog_store
    repository = SqlAlchemySessionCatalogRepository(factory)
    assert repository.list_agent_sessions(
        tenant_id=TENANT_ID,
        user_id=USER_ID,
        agent_id=AGENT_ID,
        profile=None,
        limit=50,
        offset=0,
    ) == ((), 0)


@pytest.mark.asyncio
async def test_stable_catalog_identity_survives_repository_restart_and_acl_scope(
    catalog_store,
) -> None:
    _engine, factory = catalog_store
    ingress = SqlAlchemySessionCatalogIngress(
        factory,
        now=lambda: NOW,
        id_factory=lambda: UUID("81000000-0000-4000-8000-000000000001"),
    )
    raw = {
        "profile": "default",
        "runtime_generation": "runtime-a",
        "snapshot_id": "71000000-0000-4000-8000-000000000020",
        "catalog_revision": 1,
        "page_index": 0,
        "is_last": True,
        "sessions": [_entry("host-session-1")],
    }
    await ingress.accept_snapshot_page(
        identity=_identity(),
        connection_id="72000000-0000-4000-8000-000000000001",
        connector_instance_id="73000000-0000-4000-8000-000000000001",
        runtime_generation="runtime-a",
        envelope=_envelope(
            message_id="74000000-0000-4000-8000-000000000020",
            sequence=20,
            message_type="session.catalog.snapshot.page",
            payload=raw,
        ),
        payload=CloudEnvelopeV1Adapter().decode_session_catalog_snapshot_page(raw),
    )

    first_repository = SqlAlchemySessionCatalogRepository(factory)
    listed, total = first_repository.list_agent_sessions(
        tenant_id=TENANT_ID,
        user_id=USER_ID,
        agent_id=AGENT_ID,
        profile="default",
        limit=20,
        offset=0,
    )
    assert total == 1
    stable_id = listed[0].session_id
    assert stable_id == UUID("81000000-0000-4000-8000-000000000001")

    restarted_repository = SqlAlchemySessionCatalogRepository(factory)
    resolved = restarted_repository.resolve_visible_session(
        tenant_id=TENANT_ID,
        user_id=USER_ID,
        session_id=stable_id,
        agent_id=AGENT_ID,
        profile="default",
    )
    assert resolved is not None
    assert resolved.session_id == stable_id
    assert resolved.session_key == "host-session-1"
    assert restarted_repository.resolve_visible_session(
        tenant_id=TENANT_ID,
        user_id=UUID("20000000-0000-4000-8000-000000000099"),
        session_id=stable_id,
        agent_id=AGENT_ID,
        profile="default",
    ) is None
    assert restarted_repository.resolve_visible_session(
        tenant_id=TENANT_ID,
        user_id=USER_ID,
        session_id=stable_id,
        agent_id=UUID("50000000-0000-4000-8000-000000000099"),
        profile="default",
    ) is None


async def _accept_snapshot_for_recovery_test(
    ingress: SqlAlchemySessionCatalogIngress,
    *,
    identity: ConnectorIdentity,
    message_id: str,
    connector_sequence: int,
    snapshot_id: str,
    runtime_generation: str = "runtime-a",
    catalog_revision: int = 1,
    page_index: int = 0,
    is_last: bool = True,
    session_keys: tuple[str, ...] = ("host-session-1",),
    connector_instance_id: str = "73000000-0000-4000-8000-000000000001",
):
    payload = {
        "profile": "default",
        "runtime_generation": runtime_generation,
        "snapshot_id": snapshot_id,
        "catalog_revision": catalog_revision,
        "page_index": page_index,
        "is_last": is_last,
        "sessions": [_entry(key, catalog_revision) for key in session_keys],
    }
    return await ingress.accept_snapshot_page(
        identity=identity,
        connection_id="72000000-0000-4000-8000-000000000001",
        connector_instance_id=connector_instance_id,
        runtime_generation=runtime_generation,
        envelope=_envelope(
            message_id=message_id,
            sequence=connector_sequence,
            message_type="session.catalog.snapshot.page",
            payload=payload,
        ),
        payload=CloudEnvelopeV1Adapter().decode_session_catalog_snapshot_page(payload),
    )


@pytest.mark.asyncio
async def test_failed_terminal_page_rolls_back_and_new_full_snapshot_recovers(
    catalog_store,
) -> None:
    engine, factory = catalog_store
    ingress = SqlAlchemySessionCatalogIngress(factory, now=lambda: NOW)
    identity = _identity()
    first_snapshot = "71000000-0000-4000-8000-000000000101"

    assert await _accept_snapshot_for_recovery_test(
        ingress,
        identity=identity,
        message_id="74000000-0000-4000-8000-000000000101",
        connector_sequence=1,
        snapshot_id=first_snapshot,
        is_last=False,
        session_keys=("host-duplicate",),
    ) is None
    rejected = await _accept_snapshot_for_recovery_test(
        ingress,
        identity=identity,
        message_id="74000000-0000-4000-8000-000000000102",
        connector_sequence=2,
        snapshot_id=first_snapshot,
        page_index=1,
        session_keys=("host-duplicate",),
    )

    assert rejected is not None
    assert rejected.message_type == "session.catalog.nack"
    with Session(engine) as session:
        authority = session.scalars(select(SessionCatalogAuthorityModel)).one()
        assert authority.expected_page_index == 0
        assert authority.staging_snapshot_id is None
        assert authority.require_full_snapshot is True
        assert session.scalar(
            select(func.count()).select_from(SessionCatalogSnapshotPageModel)
        ) == 0

    recovered = await _accept_snapshot_for_recovery_test(
        ingress,
        identity=identity,
        message_id="74000000-0000-4000-8000-000000000103",
        connector_sequence=3,
        snapshot_id="71000000-0000-4000-8000-000000000102",
        session_keys=("host-recovered",),
    )
    assert recovered is not None
    assert recovered.message_type == "session.catalog.ack"
    with Session(engine) as session:
        active = session.scalars(
            select(SessionCatalogEntryModel).where(SessionCatalogEntryModel.active)
        ).all()
        assert [row.session_key for row in active] == ["host-recovered"]


@pytest.mark.asyncio
async def test_event_gap_requires_full_snapshot_before_more_events(
    catalog_store,
) -> None:
    _engine, factory = catalog_store
    ingress = SqlAlchemySessionCatalogIngress(factory, now=lambda: NOW)
    identity = _identity()
    committed = await _accept_snapshot_for_recovery_test(
        ingress,
        identity=identity,
        message_id="74000000-0000-4000-8000-000000000111",
        connector_sequence=1,
        snapshot_id="71000000-0000-4000-8000-000000000111",
    )
    assert committed is not None

    async def accept_event(message_id: str, sequence: int, catalog_sequence: int):
        payload = {
            "profile": "default",
            "runtime_generation": "runtime-a",
            "catalog_sequence": catalog_sequence,
            "action": "upsert",
            "entry": _entry(f"host-event-{catalog_sequence}", catalog_sequence),
        }
        return await ingress.accept_event(
            identity=identity,
            connection_id="72000000-0000-4000-8000-000000000001",
            connector_instance_id="73000000-0000-4000-8000-000000000001",
            runtime_generation="runtime-a",
            envelope=_envelope(
                message_id=message_id,
                sequence=sequence,
                message_type="session.catalog.event",
                payload=payload,
            ),
            payload=CloudEnvelopeV1Adapter().decode_session_catalog_event(payload),
        )

    gap = await accept_event("74000000-0000-4000-8000-000000000112", 2, 3)
    blocked = await accept_event("74000000-0000-4000-8000-000000000113", 3, 2)
    assert gap is not None and gap.payload["reason"] == "event_gap"
    assert blocked is not None
    assert blocked.message_type == "session.catalog.nack"
    assert blocked.payload["reset_required"] is True

    reset = await _accept_snapshot_for_recovery_test(
        ingress,
        identity=identity,
        message_id="74000000-0000-4000-8000-000000000114",
        connector_sequence=4,
        snapshot_id="71000000-0000-4000-8000-000000000112",
        catalog_revision=2,
        session_keys=("host-reset",),
    )
    assert reset is not None and reset.message_type == "session.catalog.ack"
    resumed = await accept_event("74000000-0000-4000-8000-000000000115", 5, 3)
    assert resumed is not None and resumed.message_type == "session.catalog.ack"


@pytest.mark.asyncio
async def test_current_active_device_takes_over_writer_with_monotonic_fence(
    catalog_store,
) -> None:
    engine, factory = catalog_store
    ingress = SqlAlchemySessionCatalogIngress(factory, now=lambda: NOW)
    old_identity = _identity()
    first = await _accept_snapshot_for_recovery_test(
        ingress,
        identity=old_identity,
        message_id="74000000-0000-4000-8000-000000000121",
        connector_sequence=1,
        snapshot_id="71000000-0000-4000-8000-000000000121",
    )
    assert first is not None and first.message_type == "session.catalog.ack"

    new_device_id = UUID("60000000-0000-4000-8000-000000000002")
    with Session(engine) as session, session.begin():
        old_lifecycle = session.get(
            DeviceLifecycleModel,
            {"tenant_id": TENANT_ID, "device_id": DEVICE_ID},
        )
        assert old_lifecycle is not None
        old_lifecycle.state = "revoked"
        old_lifecycle.revision += 1
        session.add(
            DeviceModel(
                tenant_id=TENANT_ID,
                device_id=new_device_id,
                agent_id=AGENT_ID,
                workspace_id=WORKSPACE_ID,
                device_key="device-b",
                status="active",
            )
        )
        session.add(
            DeviceLifecycleModel(
                tenant_id=TENANT_ID,
                device_id=new_device_id,
                workspace_id=WORKSPACE_ID,
                agent_id=AGENT_ID,
                state="active",
                revision=1,
                updated_at=NOW,
            )
        )

    new_identity = replace(old_identity, device_id=str(new_device_id))
    takeover = await _accept_snapshot_for_recovery_test(
        ingress,
        identity=new_identity,
        message_id="74000000-0000-4000-8000-000000000122",
        connector_sequence=1,
        connector_instance_id="73000000-0000-4000-8000-000000000002",
        snapshot_id="71000000-0000-4000-8000-000000000122",
        runtime_generation="runtime-b",
        catalog_revision=2,
    )
    assert takeover is not None and takeover.message_type == "session.catalog.ack"
    with Session(engine) as session:
        authority = session.scalars(select(SessionCatalogAuthorityModel)).one()
        assert authority.writer_id == new_device_id
        assert authority.writer_fence == 2

    stale = await _accept_snapshot_for_recovery_test(
        ingress,
        identity=old_identity,
        message_id="74000000-0000-4000-8000-000000000123",
        connector_sequence=2,
        snapshot_id="71000000-0000-4000-8000-000000000123",
        runtime_generation="runtime-c",
        catalog_revision=3,
    )
    assert stale is not None
    assert stale.message_type == "session.catalog.nack"
    assert stale.payload["reason"] == "stale_writer"


@pytest.mark.asyncio
async def test_projection_anchor_conflict_rolls_back_catalog_entry(
    catalog_store,
) -> None:
    engine, factory = catalog_store
    with Session(engine) as session, session.begin():
        session.add(
            SessionProjectionModel(
                tenant_id=TENANT_ID,
                session_id=UUID("75000000-0000-4000-8000-000000000001"),
                session_key="host-seeded",
                workspace_id=WORKSPACE_ID,
                agent_id=AGENT_ID,
                profile="default",
                title="seed",
                state="active",
                revision=1,
                lineage_tip_message_id=None,
                lineage_tip_sequence=0,
                started_at=NOW,
                updated_at=NOW,
                closed_at=None,
                retention_until=NOW + timedelta(days=30),
            )
        )
    ingress = SqlAlchemySessionCatalogIngress(factory, now=lambda: NOW)
    receipt = await _accept_snapshot_for_recovery_test(
        ingress,
        identity=_identity(),
        message_id="74000000-0000-4000-8000-000000000131",
        connector_sequence=1,
        snapshot_id="71000000-0000-4000-8000-000000000131",
        session_keys=("host-seeded",),
    )
    assert receipt is not None and receipt.message_type == "session.catalog.nack"
    with Session(engine) as session:
        assert session.scalar(
            select(func.count()).select_from(SessionCatalogEntryModel)
        ) == 0


@pytest.mark.asyncio
async def test_catalog_mutation_rejects_changed_transport_ownership_atomically(
    catalog_store,
) -> None:
    engine, factory = catalog_store
    stale_connection_id = UUID("72000000-0000-4000-8000-000000000140")
    with Session(engine) as session, session.begin():
        session.add(
            ConnectorTransportCursorModel(
                tenant_id=TENANT_ID,
                device_id=DEVICE_ID,
                connector_instance_id=UUID(
                    "73000000-0000-4000-8000-000000000140"
                ),
                runtime_generation="runtime-a",
                connection_id=stale_connection_id,
                state="active",
                next_connector_sequence=1,
                next_cloud_sequence=1,
                revision=1,
                connected_at=NOW,
                updated_at=NOW,
            )
        )
        session.add(
            ConnectorTransportHandshakeOwnershipModel(
                tenant_id=TENANT_ID,
                device_id=DEVICE_ID,
                connector_instance_id=UUID(
                    "73000000-0000-4000-8000-000000000140"
                ),
                runtime_generation="runtime-a",
                connection_id=stale_connection_id,
                previous_connection_id=None,
                resume_decision="fresh",
                handshake_disposition="advance",
                state="active",
                expected_next_connector_sequence=0,
                expected_next_cloud_sequence=0,
                next_connector_sequence=1,
                next_cloud_sequence=1,
                revision=1,
                lease_expires_at=NOW + timedelta(minutes=5),
                prepared_at=NOW,
                updated_at=NOW,
            )
        )

    ingress = SqlAlchemySessionCatalogIngress(factory, now=lambda: NOW)
    payload = {
        "profile": "default",
        "runtime_generation": "runtime-a",
        "snapshot_id": "71000000-0000-4000-8000-000000000140",
        "catalog_revision": 1,
        "page_index": 0,
        "is_last": True,
        "sessions": [_entry("host-ownership-race")],
    }

    async def mutate() -> object:
        atomic = getattr(ingress, "accept_snapshot_page_and_advance", None)
        if atomic is None:
            return await ingress.accept_snapshot_page(
                identity=_identity(),
                connection_id="72000000-0000-4000-8000-000000000141",
                connector_instance_id="73000000-0000-4000-8000-000000000141",
                runtime_generation="runtime-a",
                envelope=_envelope(
                    message_id="74000000-0000-4000-8000-000000000140",
                    sequence=1,
                    message_type="session.catalog.snapshot.page",
                    payload=payload,
                ),
                payload=CloudEnvelopeV1Adapter().decode_session_catalog_snapshot_page(
                    payload
                ),
            )
        return await atomic(
            identity=_identity(),
            connection_id="72000000-0000-4000-8000-000000000141",
            connector_instance_id="73000000-0000-4000-8000-000000000141",
            runtime_generation="runtime-a",
            envelope=_envelope(
                message_id="74000000-0000-4000-8000-000000000140",
                sequence=1,
                message_type="session.catalog.snapshot.page",
                payload=payload,
            ),
            payload=CloudEnvelopeV1Adapter().decode_session_catalog_snapshot_page(
                payload
            ),
            expected_next_connector_sequence=1,
            expected_next_cloud_sequence=1,
        )

    with pytest.raises(RuntimeError, match="ownership changed"):
        await mutate()
    with Session(engine) as session:
        assert session.scalar(
            select(func.count()).select_from(SessionCatalogEntryModel)
        ) == 0
        assert session.scalar(
            select(func.count()).select_from(SessionCatalogInboxModel)
        ) == 0


@pytest.mark.asyncio
async def test_stale_writer_nack_advances_transport_in_same_unit_of_work(
    catalog_store,
) -> None:
    engine, factory = catalog_store
    current_writer = UUID("60000000-0000-4000-8000-000000000171")
    connection_id = UUID("72000000-0000-4000-8000-000000000171")
    connector_instance_id = UUID("73000000-0000-4000-8000-000000000171")
    with Session(engine) as session, session.begin():
        old_lifecycle = session.get(
            DeviceLifecycleModel,
            {"tenant_id": TENANT_ID, "device_id": DEVICE_ID},
        )
        assert old_lifecycle is not None
        old_lifecycle.state = "revoked"
        old_lifecycle.revision += 1
        session.add(
            DeviceModel(
                tenant_id=TENANT_ID,
                device_id=current_writer,
                agent_id=AGENT_ID,
                workspace_id=WORKSPACE_ID,
                device_key="current-writer",
                status="active",
                created_at=NOW,
            )
        )
        session.add(
            DeviceLifecycleModel(
                tenant_id=TENANT_ID,
                device_id=current_writer,
                workspace_id=WORKSPACE_ID,
                agent_id=AGENT_ID,
                state="active",
                revision=1,
                updated_at=NOW,
            )
        )
        session.add(
            SessionCatalogAuthorityModel(
                tenant_id=TENANT_ID,
                agent_id=AGENT_ID,
                profile="default",
                workspace_id=WORKSPACE_ID,
                writer_id=current_writer,
                writer_fence=2,
                runtime_generation="runtime-b",
                catalog_revision=1,
                catalog_sequence=0,
                staging_snapshot_id=None,
                staging_runtime_generation=None,
                staging_catalog_revision=None,
                staging_deadline=None,
                require_full_snapshot=False,
                expected_page_index=0,
                updated_at=NOW,
            )
        )
        session.add(
            ConnectorTransportCursorModel(
                tenant_id=TENANT_ID,
                device_id=DEVICE_ID,
                connector_instance_id=connector_instance_id,
                runtime_generation="runtime-a",
                connection_id=connection_id,
                state="active",
                next_connector_sequence=1,
                next_cloud_sequence=1,
                revision=1,
                connected_at=NOW,
                updated_at=NOW,
            )
        )
        session.add(
            ConnectorTransportHandshakeOwnershipModel(
                tenant_id=TENANT_ID,
                device_id=DEVICE_ID,
                connector_instance_id=connector_instance_id,
                runtime_generation="runtime-a",
                connection_id=connection_id,
                previous_connection_id=None,
                resume_decision="fresh",
                handshake_disposition="advance",
                state="active",
                expected_next_connector_sequence=0,
                expected_next_cloud_sequence=0,
                next_connector_sequence=1,
                next_cloud_sequence=1,
                revision=1,
                lease_expires_at=NOW + timedelta(minutes=5),
                prepared_at=NOW,
                updated_at=NOW,
            )
        )

    raw = {
        "profile": "default",
        "runtime_generation": "runtime-a",
        "snapshot_id": "71000000-0000-4000-8000-000000000171",
        "catalog_revision": 2,
        "page_index": 0,
        "is_last": True,
        "sessions": [_entry("stale-writer-session", 2)],
    }
    ingress = SqlAlchemySessionCatalogIngress(
        factory,
        now=lambda: NOW,
    )
    receipt = await ingress.accept_snapshot_page_and_advance(
        identity=_identity(),
        connection_id=str(connection_id),
        connector_instance_id=str(connector_instance_id),
        runtime_generation="runtime-a",
        envelope=_envelope(
            message_id="74000000-0000-4000-8000-000000000171",
            sequence=1,
            message_type="session.catalog.snapshot.page",
            payload=raw,
        ),
        payload=CloudEnvelopeV1Adapter().decode_session_catalog_snapshot_page(raw),
        expected_next_connector_sequence=1,
        expected_next_cloud_sequence=1,
    )
    assert receipt is not None
    assert receipt.message_type == "session.catalog.nack"
    assert receipt.payload["reason"] == "stale_writer"
    first_dispatch_message_id = receipt.message_id
    with Session(engine) as session:
        cursor = session.scalar(
            select(ConnectorTransportCursorModel).where(
                ConnectorTransportCursorModel.tenant_id == TENANT_ID,
                ConnectorTransportCursorModel.device_id == DEVICE_ID,
            )
        )
        ownership = session.scalar(
            select(ConnectorTransportHandshakeOwnershipModel).where(
                ConnectorTransportHandshakeOwnershipModel.tenant_id == TENANT_ID,
                ConnectorTransportHandshakeOwnershipModel.device_id == DEVICE_ID,
            )
        )
        assert cursor is not None and ownership is not None
        assert cursor.next_connector_sequence == 2
        assert cursor.next_cloud_sequence == 2
        assert ownership.revision == 2
        assert ownership.lease_expires_at.replace(tzinfo=UTC) == NOW + timedelta(
            seconds=90
        )
        assert session.scalar(
            select(func.count()).select_from(SessionCatalogInboxModel)
        ) == 1
        inbox = session.scalars(select(SessionCatalogInboxModel)).one()
        assert inbox.receipt_state == "pending"
        assert inbox.dispatch_connection_id == connection_id
        assert inbox.dispatch_message_id == UUID(first_dispatch_message_id)
        assert inbox.dispatch_sequence == 1
        assert inbox.dispatch_attempts == 1
        assert inbox.receipt_sent_at is None

    replacement_connection_id = UUID("72000000-0000-4000-8000-000000000172")
    with Session(engine) as session, session.begin():
        cursor = session.scalars(select(ConnectorTransportCursorModel)).one()
        ownership = session.scalars(
            select(ConnectorTransportHandshakeOwnershipModel)
        ).one()
        cursor.connection_id = replacement_connection_id
        ownership.connection_id = replacement_connection_id
        ownership.previous_connection_id = connection_id
        ownership.resume_decision = "resumed"

    with pytest.raises(RuntimeError, match="dispatch ownership changed"):
        await ingress.mark_receipt_sent(
            identity=_identity(),
            connection_id=str(connection_id),
            connector_instance_id=str(connector_instance_id),
            runtime_generation="runtime-a",
            catalog_message_id=receipt.catalog_message_id,
            message_id=first_dispatch_message_id,
            receipt_sequence=1,
        )
    assert await ingress.confirm_receipts_through_cursor(
        identity=_identity(),
        connection_id=str(connection_id),
        durable_next_inbound_sequence=2,
    ) == 0
    with Session(engine) as session:
        pending = session.scalars(select(SessionCatalogInboxModel)).one()
        assert pending.receipt_state == "pending"
        assert pending.receipt_sent_at is None

    assert await ingress.next_pending_receipt(
        identity=_identity(),
        connection_id=str(replacement_connection_id),
    ) == receipt.catalog_message_id
    reserved = await ingress.reserve_pending_receipt_and_advance(
        identity=_identity(),
        connection_id=str(replacement_connection_id),
        connector_instance_id=str(connector_instance_id),
        runtime_generation="runtime-a",
        catalog_message_id=receipt.catalog_message_id,
        expected_next_connector_sequence=2,
        expected_next_cloud_sequence=2,
    )
    assert reserved.catalog_message_id == receipt.catalog_message_id
    assert reserved.message_id != first_dispatch_message_id
    assert reserved.sequence == 2
    with Session(engine) as session:
        cursor = session.scalar(select(ConnectorTransportCursorModel))
        inbox = session.scalars(select(SessionCatalogInboxModel)).one()
        assert cursor is not None
        assert cursor.next_connector_sequence == 2
        assert cursor.next_cloud_sequence == 3
        assert inbox.dispatch_connection_id == replacement_connection_id
        assert inbox.dispatch_message_id == UUID(reserved.message_id)
        assert inbox.dispatch_sequence == 2
        assert inbox.dispatch_attempts == 2
        assert inbox.receipt_state == "pending"

    with pytest.raises(RuntimeError, match="dispatch ownership changed"):
        await ingress.mark_receipt_sent(
            identity=_identity(),
            connection_id=str(connection_id),
            connector_instance_id=str(connector_instance_id),
            runtime_generation="runtime-a",
            catalog_message_id=receipt.catalog_message_id,
            message_id=first_dispatch_message_id,
            receipt_sequence=1,
        )
    assert await ingress.confirm_receipts_through_cursor(
        identity=_identity(),
        connection_id=str(connection_id),
        durable_next_inbound_sequence=2,
    ) == 0
    await ingress.mark_receipt_sent(
        identity=_identity(),
        connection_id=str(replacement_connection_id),
        connector_instance_id=str(connector_instance_id),
        runtime_generation="runtime-a",
        catalog_message_id=receipt.catalog_message_id,
        message_id=reserved.message_id,
        receipt_sequence=2,
    )
    assert await ingress.confirm_receipts_through_cursor(
        identity=_identity(),
        connection_id=str(replacement_connection_id),
        durable_next_inbound_sequence=3,
    ) == 1
    with Session(engine) as session:
        inbox = session.scalars(select(SessionCatalogInboxModel)).one()
        assert inbox.receipt_state == "settled"
        assert inbox.receipt_sent_at is not None
        assert inbox.receipt_settled_at is not None


@pytest.mark.asyncio
async def test_fresh_connector_epoch_retires_old_pending_receipt_with_audit_reason(
    catalog_store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import hermes_cloud.platform.sqlalchemy.session_catalog as catalog_module

    engine, factory = catalog_store
    old_connection_id = UUID("72000000-0000-4000-8000-000000000191")
    new_connection_id = UUID("72000000-0000-4000-8000-000000000192")
    old_instance_id = UUID("73000000-0000-4000-8000-000000000191")
    new_instance_id = UUID("73000000-0000-4000-8000-000000000192")
    catalog_message_id = UUID("74000000-0000-4000-8000-000000000191")
    with Session(engine) as session, session.begin():
        session.add(
            SessionCatalogInboxModel(
                tenant_id=TENANT_ID,
                message_id=catalog_message_id,
                workspace_id=WORKSPACE_ID,
                agent_id=AGENT_ID,
                device_id=DEVICE_ID,
                connector_instance_id=old_instance_id,
                runtime_generation="runtime-a",
                connector_sequence=1,
                message_type="session.catalog.event",
                payload_digest="a" * 64,
                receipt_type="session.catalog.ack",
                receipt_payload={"acked_message_id": str(catalog_message_id)},
                receipt_state="pending",
                dispatch_connection_id=old_connection_id,
                dispatch_message_id=UUID(
                    "75000000-0000-4000-8000-000000000191"
                ),
                dispatch_sequence=1,
                dispatch_attempts=1,
                received_at=NOW,
                updated_at=NOW,
                receipt_sent_at=NOW,
                receipt_settled_at=None,
                retention_until=NOW + timedelta(days=7),
            )
        )

    monkeypatch.setattr(catalog_module, "_CATALOG_PENDING_RECEIPT_CAPACITY", 1)
    transport = SqlAlchemyConnectorTransportCursorAuthority(
        factory,
        now=lambda: NOW,
    )
    await transport.prepare_session(
        identity=_identity(),
        connection_id=str(new_connection_id),
        connector_instance_id=str(new_instance_id),
        runtime_generation="runtime-b",
        resume_decision="fresh",
        handshake_disposition="advance",
        previous_connection_id=None,
        expected_next_connector_sequence=0,
        expected_next_cloud_sequence=0,
        next_connector_sequence=1,
        next_cloud_sequence=1,
    )
    await transport.confirm_session(
        identity=_identity(),
        connection_id=str(new_connection_id),
        connector_instance_id=str(new_instance_id),
        runtime_generation="runtime-b",
    )

    # Fresh activation retires the old business epoch before any new catalog frame.
    with Session(engine) as session:
        retired = session.get(
            SessionCatalogInboxModel,
            (TENANT_ID, catalog_message_id),
        )
        assert retired is not None
        assert retired.receipt_state == "retired"
        assert retired.receipt_retired_at.replace(tzinfo=UTC) == NOW
        assert retired.receipt_retirement_reason == "connector_epoch_replaced"

    ingress = SqlAlchemySessionCatalogIngress(factory, now=lambda: NOW)
    assert await ingress.next_pending_receipt(
        identity=_identity(),
        connection_id=str(new_connection_id),
    ) is None
    raw = {
        "profile": "default",
        "runtime_generation": "runtime-b",
        "snapshot_id": "71000000-0000-4000-8000-000000000192",
        "catalog_revision": 1,
        "page_index": 0,
        "is_last": True,
        "sessions": [_entry("fresh-epoch-session")],
    }
    receipt = await ingress.accept_snapshot_page_and_advance(
        identity=_identity(),
        connection_id=str(new_connection_id),
        connector_instance_id=str(new_instance_id),
        runtime_generation="runtime-b",
        envelope=_envelope(
            message_id="74000000-0000-4000-8000-000000000192",
            sequence=1,
            message_type="session.catalog.snapshot.page",
            payload=raw,
        ),
        payload=CloudEnvelopeV1Adapter().decode_session_catalog_snapshot_page(raw),
        expected_next_connector_sequence=1,
        expected_next_cloud_sequence=1,
    )
    assert receipt is not None
    await ingress.mark_receipt_sent(
        identity=_identity(),
        connection_id=str(new_connection_id),
        connector_instance_id=str(new_instance_id),
        runtime_generation="runtime-b",
        catalog_message_id=receipt.catalog_message_id,
        message_id=receipt.message_id,
        receipt_sequence=receipt.sequence,
    )
    assert await ingress.confirm_receipts_through_cursor(
        identity=_identity(),
        connection_id=str(new_connection_id),
        durable_next_inbound_sequence=2,
    ) == 1


@pytest.mark.asyncio
async def test_multiple_catalog_receipts_partially_confirm_and_redeliver_in_order(
    catalog_store,
) -> None:
    engine, factory = catalog_store
    connection_id = UUID("72000000-0000-4000-8000-000000000181")
    replacement_connection_id = UUID("72000000-0000-4000-8000-000000000182")
    connector_instance_id = UUID("73000000-0000-4000-8000-000000000181")
    catalog_ids = tuple(UUID(int=300 + index) for index in range(3))
    with Session(engine) as session, session.begin():
        session.add(
            ConnectorTransportCursorModel(
                tenant_id=TENANT_ID,
                device_id=DEVICE_ID,
                connector_instance_id=connector_instance_id,
                runtime_generation="runtime-a",
                connection_id=connection_id,
                state="active",
                next_connector_sequence=1,
                next_cloud_sequence=4,
                revision=1,
                connected_at=NOW,
                updated_at=NOW,
            )
        )
        session.add(
            ConnectorTransportHandshakeOwnershipModel(
                tenant_id=TENANT_ID,
                device_id=DEVICE_ID,
                connector_instance_id=connector_instance_id,
                runtime_generation="runtime-a",
                connection_id=connection_id,
                previous_connection_id=None,
                resume_decision="fresh",
                handshake_disposition="advance",
                state="active",
                expected_next_connector_sequence=0,
                expected_next_cloud_sequence=0,
                next_connector_sequence=1,
                next_cloud_sequence=4,
                revision=1,
                lease_expires_at=NOW + timedelta(minutes=5),
                prepared_at=NOW,
                updated_at=NOW,
            )
        )
        for index, catalog_id in enumerate(catalog_ids, start=1):
            observed_at = NOW + timedelta(seconds=index)
            session.add(
                SessionCatalogInboxModel(
                    tenant_id=TENANT_ID,
                    message_id=catalog_id,
                    workspace_id=WORKSPACE_ID,
                    agent_id=AGENT_ID,
                    device_id=DEVICE_ID,
                    connector_instance_id=connector_instance_id,
                    runtime_generation="runtime-a",
                    connector_sequence=index,
                    message_type="session.catalog.event",
                    payload_digest=f"{index:064x}",
                    receipt_type="session.catalog.ack",
                    receipt_payload={
                        "acked_message_id": str(catalog_id),
                        "acked_connector_sequence": index,
                    },
                    receipt_state="pending",
                    dispatch_connection_id=connection_id,
                    dispatch_message_id=UUID(int=400 + index),
                    dispatch_sequence=index,
                    dispatch_attempts=1,
                    received_at=observed_at,
                    updated_at=observed_at,
                    receipt_sent_at=observed_at,
                    receipt_settled_at=None,
                    retention_until=observed_at + timedelta(days=7),
                )
            )

    ingress = SqlAlchemySessionCatalogIngress(factory, now=lambda: NOW)
    assert await ingress.confirm_receipts_through_cursor(
        identity=_identity(),
        connection_id=str(connection_id),
        durable_next_inbound_sequence=2,
    ) == 1
    with Session(engine) as session, session.begin():
        cursor = session.scalars(select(ConnectorTransportCursorModel)).one()
        ownership = session.scalars(
            select(ConnectorTransportHandshakeOwnershipModel)
        ).one()
        cursor.connection_id = replacement_connection_id
        ownership.connection_id = replacement_connection_id
        ownership.previous_connection_id = connection_id
        ownership.resume_decision = "resumed"

    redeliveries = []
    for expected_catalog_id, cloud_sequence in zip(
        catalog_ids[1:],
        (4, 5),
        strict=True,
    ):
        pending_id = await ingress.next_pending_receipt(
            identity=_identity(),
            connection_id=str(replacement_connection_id),
        )
        assert pending_id == str(expected_catalog_id)
        delivery = await ingress.reserve_pending_receipt_and_advance(
            identity=_identity(),
            connection_id=str(replacement_connection_id),
            connector_instance_id=str(connector_instance_id),
            runtime_generation="runtime-a",
            catalog_message_id=pending_id,
            expected_next_connector_sequence=1,
            expected_next_cloud_sequence=cloud_sequence,
        )
        redeliveries.append(delivery)

    assert [delivery.catalog_message_id for delivery in redeliveries] == [
        str(catalog_ids[1]),
        str(catalog_ids[2]),
    ]
    assert [delivery.sequence for delivery in redeliveries] == [4, 5]
    assert await ingress.confirm_receipts_through_cursor(
        identity=_identity(),
        connection_id=str(connection_id),
        durable_next_inbound_sequence=4,
    ) == 0
    for delivery in redeliveries:
        await ingress.mark_receipt_sent(
            identity=_identity(),
            connection_id=str(replacement_connection_id),
            connector_instance_id=str(connector_instance_id),
            runtime_generation="runtime-a",
            catalog_message_id=delivery.catalog_message_id,
            message_id=delivery.message_id,
            receipt_sequence=delivery.sequence,
        )
    assert await ingress.confirm_receipts_through_cursor(
        identity=_identity(),
        connection_id=str(replacement_connection_id),
        durable_next_inbound_sequence=5,
    ) == 1
    assert await ingress.confirm_receipts_through_cursor(
        identity=_identity(),
        connection_id=str(replacement_connection_id),
        durable_next_inbound_sequence=6,
    ) == 1
    with Session(engine) as session:
        states = session.scalars(
            select(SessionCatalogInboxModel.receipt_state).order_by(
                SessionCatalogInboxModel.message_id
            )
        ).all()
        assert states == ["settled", "settled", "settled"]


def test_catalog_retention_metadata_and_bounded_cleaner_are_published() -> None:
    import hermes_cloud.platform.sqlalchemy.session_catalog as catalog_module

    assert "staging_deadline" in SessionCatalogAuthorityModel.__table__.columns
    assert "require_full_snapshot" in SessionCatalogAuthorityModel.__table__.columns
    assert "retention_until" in SessionCatalogInboxModel.__table__.columns
    cleaner = getattr(catalog_module, "SqlAlchemySessionCatalogRetentionCleaner", None)
    assert cleaner is not None


@pytest.mark.asyncio
async def test_catalog_retention_cleaner_deletes_bounded_expired_batches(
    catalog_store,
) -> None:
    import hermes_cloud.platform.sqlalchemy.session_catalog as catalog_module

    engine, factory = catalog_store
    snapshot_id = UUID("71000000-0000-4000-8000-000000000150")
    with Session(engine) as session, session.begin():
        session.add(
            SessionCatalogAuthorityModel(
                tenant_id=TENANT_ID,
                agent_id=AGENT_ID,
                profile="default",
                workspace_id=WORKSPACE_ID,
                writer_id=DEVICE_ID,
                writer_fence=1,
                runtime_generation=None,
                catalog_revision=0,
                catalog_sequence=0,
                staging_snapshot_id=snapshot_id,
                staging_runtime_generation="runtime-a",
                staging_catalog_revision=1,
                staging_deadline=NOW - timedelta(seconds=1),
                require_full_snapshot=False,
                expected_page_index=3,
                updated_at=NOW - timedelta(minutes=20),
            )
        )
        for index in range(3):
            session.add(
                SessionCatalogSnapshotPageModel(
                    tenant_id=TENANT_ID,
                    agent_id=AGENT_ID,
                    profile="default",
                    snapshot_id=snapshot_id,
                    page_index=index,
                    runtime_generation="runtime-a",
                    catalog_revision=1,
                    is_last=False,
                    sessions=[],
                    payload_digest=f"{index + 1:064x}",
                    created_at=NOW - timedelta(minutes=20),
                )
            )
            session.add(
                SessionCatalogInboxModel(
                    tenant_id=TENANT_ID,
                    message_id=UUID(int=200 + index),
                    workspace_id=WORKSPACE_ID,
                    agent_id=AGENT_ID,
                    device_id=DEVICE_ID,
                    connector_instance_id=UUID(
                        "73000000-0000-4000-8000-000000000150"
                    ),
                    runtime_generation="runtime-a",
                    connector_sequence=index + 1,
                    message_type="session.catalog.snapshot.page",
                    payload_digest=f"{index + 10:064x}",
                    receipt_type=None,
                    receipt_payload=None,
                    receipt_state=None,
                    dispatch_connection_id=None,
                    dispatch_message_id=None,
                    dispatch_sequence=None,
                    dispatch_attempts=0,
                    received_at=NOW - timedelta(days=8),
                    updated_at=NOW - timedelta(days=8),
                    receipt_sent_at=None,
                    receipt_settled_at=None,
                    retention_until=NOW - timedelta(seconds=1),
                )
            )

    cleaner_type = getattr(
        catalog_module,
        "SqlAlchemySessionCatalogRetentionCleaner",
        None,
    )
    assert cleaner_type is not None
    cleaner = cleaner_type(factory, now=lambda: NOW, batch_size=2)

    first = await cleaner.cleanup_once()
    assert first.inbox_deleted == 2
    assert first.snapshot_pages_deleted == 2
    assert first.authorities_reset == 1
    with Session(engine) as session:
        authority = session.scalars(select(SessionCatalogAuthorityModel)).one()
        assert authority.require_full_snapshot is True
        assert authority.staging_snapshot_id is None
        assert session.scalar(
            select(func.count()).select_from(SessionCatalogInboxModel)
        ) == 1
        assert session.scalar(
            select(func.count()).select_from(SessionCatalogSnapshotPageModel)
        ) == 1

    second = await cleaner.cleanup_once()
    assert second.inbox_deleted == 1
    assert second.snapshot_pages_deleted == 1


@pytest.mark.asyncio
async def test_catalog_retention_cleaner_bounds_retired_deletes_and_protects_audit(
    catalog_store,
) -> None:
    import hermes_cloud.platform.sqlalchemy.session_catalog as catalog_module

    engine, factory = catalog_store
    connector_instance_id = UUID("73000000-0000-4000-8000-000000000155")
    states = (
        ("retired", NOW - timedelta(seconds=1)),
        ("retired", NOW - timedelta(seconds=1)),
        ("retired", NOW - timedelta(seconds=1)),
        ("retired", NOW + timedelta(days=1)),
        ("pending", NOW - timedelta(seconds=1)),
    )
    with Session(engine) as session, session.begin():
        for index, (state, retention_until) in enumerate(states, start=1):
            message_id = UUID(int=500 + index)
            session.add(
                SessionCatalogInboxModel(
                    tenant_id=TENANT_ID,
                    message_id=message_id,
                    workspace_id=WORKSPACE_ID,
                    agent_id=AGENT_ID,
                    device_id=DEVICE_ID,
                    connector_instance_id=connector_instance_id,
                    runtime_generation="runtime-retired",
                    connector_sequence=index,
                    message_type="session.catalog.event",
                    payload_digest=f"{index:064x}",
                    receipt_type="session.catalog.ack",
                    receipt_payload={"acked_message_id": str(message_id)},
                    receipt_state=state,
                    dispatch_connection_id=UUID(int=600 + index),
                    dispatch_message_id=UUID(int=700 + index),
                    dispatch_sequence=index,
                    dispatch_attempts=1,
                    received_at=NOW - timedelta(days=8),
                    updated_at=NOW - timedelta(days=8),
                    receipt_sent_at=NOW - timedelta(days=8),
                    receipt_settled_at=None,
                    receipt_retired_at=(
                        NOW - timedelta(days=8) if state == "retired" else None
                    ),
                    receipt_retirement_reason=(
                        "connector_epoch_replaced"
                        if state == "retired"
                        else None
                    ),
                    retention_until=retention_until,
                )
            )

    cleaner_type = catalog_module.SqlAlchemySessionCatalogRetentionCleaner
    cleaner = cleaner_type(factory, now=lambda: NOW, batch_size=2)

    assert (await cleaner.cleanup_once()).inbox_deleted == 2
    assert (await cleaner.cleanup_once()).inbox_deleted == 1
    assert (await cleaner.cleanup_once()).inbox_deleted == 0
    with Session(engine) as session:
        remaining = session.scalars(
            select(SessionCatalogInboxModel).order_by(
                SessionCatalogInboxModel.connector_sequence
            )
        ).all()
        assert [row.receipt_state for row in remaining] == ["retired", "pending"]
        assert remaining[0].receipt_retirement_reason == (
            "connector_epoch_replaced"
        )
        assert remaining[0].receipt_retired_at is not None


@pytest.mark.asyncio
async def test_terminal_snapshot_rejects_page_deleted_by_cross_authority_cleanup(
    catalog_store,
) -> None:
    import hermes_cloud.platform.sqlalchemy.session_catalog as catalog_module

    engine, factory = catalog_store
    snapshot_a = UUID("71000000-0000-4000-8000-000000000160")
    snapshot_b = UUID("71000000-0000-4000-8000-000000000161")
    with Session(engine) as session, session.begin():
        for profile, snapshot_id in (("a", snapshot_a), ("b", snapshot_b)):
            session.add(
                SessionCatalogAuthorityModel(
                    tenant_id=TENANT_ID,
                    agent_id=AGENT_ID,
                    profile=profile,
                    workspace_id=WORKSPACE_ID,
                    writer_id=DEVICE_ID,
                    writer_fence=1,
                    runtime_generation=None,
                    catalog_revision=0,
                    catalog_sequence=0,
                    staging_snapshot_id=snapshot_id,
                    staging_runtime_generation="runtime-a",
                    staging_catalog_revision=1,
                    staging_deadline=NOW - timedelta(seconds=1),
                    require_full_snapshot=False,
                    expected_page_index=1,
                    updated_at=NOW - timedelta(minutes=30),
                )
            )
        session.add(
            SessionCatalogSnapshotPageModel(
                tenant_id=TENANT_ID,
                agent_id=AGENT_ID,
                profile="a",
                snapshot_id=snapshot_a,
                page_index=0,
                runtime_generation="runtime-a",
                catalog_revision=1,
                is_last=False,
                sessions=[_entry("page-a")],
                payload_digest="a" * 64,
                created_at=NOW - timedelta(minutes=15),
            )
        )
        session.add(
            SessionCatalogSnapshotPageModel(
                tenant_id=TENANT_ID,
                agent_id=AGENT_ID,
                profile="b",
                snapshot_id=snapshot_b,
                page_index=0,
                runtime_generation="runtime-a",
                catalog_revision=1,
                is_last=False,
                sessions=[_entry("missing-page-b")],
                payload_digest="b" * 64,
                created_at=NOW - timedelta(minutes=20),
            )
        )

    cleaner = catalog_module.SqlAlchemySessionCatalogRetentionCleaner(
        factory,
        now=lambda: NOW,
        batch_size=1,
    )
    cleaned = await cleaner.cleanup_once()
    assert cleaned.authorities_reset == 1
    assert cleaned.snapshot_pages_deleted == 1
    with Session(engine) as session:
        authority_b = session.get(
            SessionCatalogAuthorityModel,
            (TENANT_ID, AGENT_ID, "b"),
        )
        assert authority_b is not None
        assert authority_b.expected_page_index == 1
        assert session.get(
            SessionCatalogSnapshotPageModel,
            (TENANT_ID, AGENT_ID, "b", snapshot_b, 0),
        ) is None

    terminal = {
        "profile": "b",
        "runtime_generation": "runtime-a",
        "snapshot_id": str(snapshot_b),
        "catalog_revision": 1,
        "page_index": 1,
        "is_last": True,
        "sessions": [_entry("terminal-page-b")],
    }
    receipt = await SqlAlchemySessionCatalogIngress(
        factory,
        now=lambda: NOW,
    ).accept_snapshot_page(
        identity=_identity(),
        connection_id="72000000-0000-4000-8000-000000000161",
        connector_instance_id="73000000-0000-4000-8000-000000000161",
        runtime_generation="runtime-a",
        envelope=_envelope(
            message_id="74000000-0000-4000-8000-000000000161",
            sequence=2,
            message_type="session.catalog.snapshot.page",
            payload=terminal,
        ),
        payload=CloudEnvelopeV1Adapter().decode_session_catalog_snapshot_page(
            terminal
        ),
    )
    assert receipt is not None
    assert receipt.message_type == "session.catalog.nack"
    assert receipt.payload["reason"] == "page_gap"
    with Session(engine) as session:
        assert session.scalar(
            select(func.count()).select_from(SessionCatalogEntryModel)
        ) == 0
        authority_b = session.get(
            SessionCatalogAuthorityModel,
            (TENANT_ID, AGENT_ID, "b"),
        )
        assert authority_b is not None
        assert authority_b.require_full_snapshot is True
