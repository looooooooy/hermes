from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import event, func, inspect, select
from sqlalchemy.orm import Session, sessionmaker

from hermes_cloud.adapters.connector_contract_v1 import CloudEnvelopeV1Adapter
from hermes_cloud.domain.connector_gateway import (
    ConnectorIdentity,
    ConnectorObserverSnapshot,
)
from hermes_cloud.domain.contract_models import CloudEnvelope
from hermes_cloud.platform.postgres.models import (
    AgentModel,
    AuditEventModel,
    DeviceLifecycleModel,
    DeviceModel,
    InboxMessageModel,
    OutboxEventModel,
    RoleModel,
    TenantModel,
    UserModel,
    WorkspaceMembershipModel,
    WorkspaceModel,
)
from hermes_cloud.platform.sqlalchemy.observer_encryption import (
    AesGcmTenantEnvelopeCipher,
    MappingTenantKekResolver,
    ObserverEncryptionContext,
    ObserverEncryptionError,
)
from hermes_cloud.platform.sqlalchemy.observer_projection import (
    ObserverProjectionConflict,
    ObserverProjectionEventSource,
    SqlAlchemyObserverIngress,
    SqlAlchemyObserverProjectionRepository,
    SqlAlchemyObserverRetentionCleaner,
)
from hermes_cloud.platform.sqlalchemy.observer_projection_models import (
    ObserverDeletionLedgerModel,
    ObserverEventModel,
    ObserverInboxModel,
    ObserverProjectionBase,
    ObserverSessionModel,
    ObserverV2StateModel,
)
from hermes_cloud.platform.sqlalchemy.session_catalog_models import (
    SessionCatalogBase,
    SessionCatalogEntryModel,
)
from hermes_cloud.platform.sqlite.engine import build_sqlite_engine
from hermes_cloud.platform.sqlite.migrations import (
    PUBLISHED_SQLITE_MIGRATIONS,
    SQLiteMigrationHistoryConflict,
    SQLiteSchemaMigration,
    upgrade_sqlite_schema,
)
from hermes_cloud.platform.sqlite.schema import build_sqlite_metadata

TENANT_ID = UUID("10000000-0000-4000-8000-000000000001")
USER_ID = UUID("20000000-0000-4000-8000-000000000001")
WORKSPACE_ID = UUID("30000000-0000-4000-8000-000000000001")
ROLE_ID = UUID("40000000-0000-4000-8000-000000000001")
AGENT_ID = UUID("50000000-0000-4000-8000-000000000001")
DEVICE_ID = UUID("60000000-0000-4000-8000-000000000001")
CATALOG_SESSION_ID = UUID("61000000-0000-4000-8000-000000000001")
CATALOG_V2_SESSION_ID = UUID("61000000-0000-4000-8000-000000000002")
NOW = datetime(2026, 7, 31, 9, 0, tzinfo=UTC)
CONTRACT_ROOT = Path(__file__).resolve().parents[2] / "fixtures/repository_contracts"
CIPHER = AesGcmTenantEnvelopeCipher(
    MappingTenantKekResolver(
        keys={TENANT_ID: {"test-v1": b"k" * 32}},
        current_versions={TENANT_ID: "test-v1"},
    )
)


@pytest.fixture
def observer_store(tmp_path: Path):
    engine = build_sqlite_engine(
        f"sqlite+pysqlite:///{tmp_path / 'observer.sqlite3'}",
        allow_missing=True,
    )
    build_sqlite_metadata().create_all(engine)
    ObserverProjectionBase.metadata.create_all(engine)
    SessionCatalogBase.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with Session(engine) as session, session.begin():
        session.add(
            TenantModel(
                tenant_id=TENANT_ID,
                slug="observer-test",
                display_name="Observer Test",
                status="active",
                created_at=NOW,
            )
        )
        session.flush()
        session.add(
            UserModel(
                tenant_id=TENANT_ID,
                user_id=USER_ID,
                subject="observer-user",
                display_name="Observer User",
                email=None,
                status="active",
                created_at=NOW,
            )
        )
        session.flush()
        session.add(
            RoleModel(
                tenant_id=TENANT_ID,
                role_id=ROLE_ID,
                role_key="observer",
                display_name="Observer",
                scope_type="workspace",
                permissions=["session.read"],
                status="active",
                version=1,
                created_at=NOW,
            )
        )
        session.add(
            WorkspaceModel(
                tenant_id=TENANT_ID,
                workspace_id=WORKSPACE_ID,
                workspace_key="observer-workspace",
                display_name="Observer Workspace",
                status="active",
                created_by=USER_ID,
                created_at=NOW,
            )
        )
        session.flush()
        session.add(
            WorkspaceMembershipModel(
                tenant_id=TENANT_ID,
                workspace_membership_id=uuid4(),
                workspace_id=WORKSPACE_ID,
                user_id=USER_ID,
                role_id=ROLE_ID,
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
                agent_key="observer-agent",
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
                device_key="observer-device",
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
                revision=1,
                updated_at=NOW,
            )
        )
        for session_id, session_key in (
            (CATALOG_SESSION_ID, "session-root-1"),
            (CATALOG_V2_SESSION_ID, "session-root-v2"),
        ):
            session.add(
                SessionCatalogEntryModel(
                    tenant_id=TENANT_ID,
                    session_id=session_id,
                    workspace_id=WORKSPACE_ID,
                    agent_id=AGENT_ID,
                    profile="default",
                    session_key=session_key,
                    surface="hermes-cli",
                    authority_revision=1,
                    available_actions=["prompt.submit"],
                    runtime_generation="runtime-generation-v2",
                    writer_id=DEVICE_ID,
                    writer_fence=1,
                    content_digest="a" * 64,
                    active=True,
                    updated_at=NOW,
                )
            )
    try:
        yield engine, factory
    finally:
        engine.dispose()


def _fixture(name: str) -> dict[str, object]:
    return json.loads(
        (CONTRACT_ROOT / "fixtures" / "valid" / name).read_text(encoding="utf-8")
    )


def _seed_versioned_v2_plaintext_observer(engine) -> None:
    session_id = UUID("75000000-0000-4000-8000-000000000001")
    with engine.begin() as connection:
        operations = Operations(MigrationContext.configure(connection))
        for migration in PUBLISHED_SQLITE_MIGRATIONS[:2]:
            migration.upgrade(operations)
        SQLiteSchemaMigration.__table__.create(connection)
        with Session(
            bind=connection, join_transaction_mode="create_savepoint"
        ) as session:
            session.add_all(
                [
                    SQLiteSchemaMigration(
                        version=migration.version,
                        name=migration.name,
                        checksum=migration.checksum,
                        applied_at=NOW,
                    )
                    for migration in PUBLISHED_SQLITE_MIGRATIONS[:2]
                ]
            )
            session.add(
                ObserverSessionModel(
                    tenant_id=TENANT_ID,
                    session_id=session_id,
                    workspace_id=WORKSPACE_ID,
                    agent_id=AGENT_ID,
                    device_id=DEVICE_ID,
                    profile="default",
                    session_key="session-root-1",
                    runtime_session_id="runtime-session-1",
                    runtime_generation="runtime-20260731-01",
                    connector_instance_id="74000000-0000-4000-8000-000000000001",
                    connection_id="73000000-0000-4000-8000-000000000001",
                    running=True,
                    status="running",
                    event_sequence=1,
                    snapshot_event_sequence=0,
                    snapshot_head_sequence=0,
                    messages=[{"role": "assistant", "content": "v2 plaintext"}],
                    inflight={
                        "user": None,
                        "assistant": None,
                        "streaming": False,
                        "error": None,
                    },
                    replay_events=[],
                    payload_digest="d" * 64,
                    updated_at=NOW,
                    retention_until=NOW + timedelta(days=30),
                )
            )
            session.add(
                ObserverEventModel(
                    tenant_id=TENANT_ID,
                    session_id=session_id,
                    event_sequence=1,
                    event_sequence_start=1,
                    session_key="session-root-1",
                    runtime_session_id="runtime-session-1",
                    event_type="message.delta",
                    payload={"text": "v2 event plaintext"},
                    payload_digest="e" * 64,
                    occurred_at=NOW,
                    retention_until=NOW + timedelta(days=30),
                )
            )
            session.commit()


def _identity() -> ConnectorIdentity:
    return ConnectorIdentity(
        tenant_id=str(TENANT_ID),
        device_id=str(DEVICE_ID),
        agent_id=str(AGENT_ID),
        scopes=("session.observe",),
        legacy_seed=False,
    )


def _snapshot_call(
    message_id: str = "71000000-0000-4000-8000-000000000001",
    *,
    connector_sequence: int = 1,
    sent_at: str = "2026-07-31T09:00:00Z",
    overrides: dict[str, object] | None = None,
):
    codec = CloudEnvelopeV1Adapter()
    raw = _fixture("session-snapshot-payload.json")
    if overrides is not None:
        raw.update(overrides)
    payload = codec.decode_session_snapshot(raw)
    envelope = codec.decode_connector_frame(
        json.dumps(
            {
                "contract_version": 1,
                "message_id": message_id,
                "message_type": "session.snapshot",
                "tenant_id": str(TENANT_ID),
                "device_id": str(DEVICE_ID),
                "sequence": connector_sequence,
                "sent_at": sent_at,
                "payload": raw,
            }
        )
    )
    return envelope, payload


def _event_call(
    message_id: str = "72000000-0000-4000-8000-000000000001",
    *,
    sequence: int = 6,
    connector_sequence: int = 2,
    sent_at: str = "2026-07-31T09:00:01Z",
):
    codec = CloudEnvelopeV1Adapter()
    raw = _fixture("session-event-payload.json")
    raw["event_sequence"] = sequence
    payload = codec.decode_session_event(raw)
    envelope = codec.decode_connector_frame(
        json.dumps(
            {
                "contract_version": 1,
                "message_id": message_id,
                "message_type": "session.event",
                "tenant_id": str(TENANT_ID),
                "device_id": str(DEVICE_ID),
                "sequence": connector_sequence,
                "sent_at": sent_at,
                "payload": raw,
            }
        )
    )
    return envelope, payload


def _snapshot_v2_call(
    message_id: str = "71000000-0000-4000-8000-000000000201",
    *,
    runtime_generation: str = "runtime-generation-v2",
    overrides: dict[str, object] | None = None,
):
    codec = CloudEnvelopeV1Adapter()
    raw = {
        "observer_contract": 2,
        "profile": "default",
        "runtime_generation": runtime_generation,
        "session_key": "session-root-v2",
        "runtime_session_id": "runtime-session-v2",
        "running": True,
        "status": "running",
        "event_sequence": 4,
        "snapshot_event_sequence": 4,
        "messages": [],
        "inflight": {
            "user": None,
            "assistant": None,
            "streaming": False,
            "error": None,
        },
        "todo_sections": [
            {
                "turn_id": "turn-1",
                "section_id": "todo-1",
                "revision": 1,
                "first_event_sequence": 1,
                "status": "in_progress",
                "items": [
                    {"id": "item-1", "label": "Run tests", "status": "in_progress"}
                ],
            }
        ],
        "subagents": [],
        "tools": [],
        "terminals": [],
        "replay_events": [],
    }
    if overrides is not None:
        raw.update(overrides)
    payload = codec.decode_session_snapshot(raw)
    envelope = codec.decode_connector_frame(
        json.dumps(
            {
                "contract_version": 1,
                "message_id": message_id,
                "message_type": "session.snapshot.v2",
                "tenant_id": str(TENANT_ID),
                "device_id": str(DEVICE_ID),
                "sequence": 1,
                "sent_at": "2026-07-31T09:00:00Z",
                "payload": raw,
            }
        )
    )
    return envelope, payload


def _event_v2_call(
    *,
    revision: int,
    first_event_sequence: int = 4,
    sequence: int = 5,
):
    codec = CloudEnvelopeV1Adapter()
    raw = {
        "observer_contract": 2,
        "profile": "default",
        "runtime_generation": "runtime-generation-v2",
        "session_key": "session-root-v2",
        "session_id": "runtime-session-v2",
        "type": "tool.update",
        "event_sequence": sequence,
        "payload": {
            "turn_id": "turn-1",
            "tool_call_id": "tool-1",
            "revision": revision,
            "first_event_sequence": first_event_sequence,
            "operation": "upsert",
            "status": "completed",
            "name": "Contract tests",
        },
    }
    payload = codec.decode_session_event(raw)
    envelope = codec.decode_connector_frame(
        json.dumps(
            {
                "contract_version": 1,
                "message_id": "72000000-0000-4000-8000-000000000201",
                "message_type": "session.event.v2",
                "tenant_id": str(TENANT_ID),
                "device_id": str(DEVICE_ID),
                "sequence": 2,
                "sent_at": "2026-07-31T09:00:01Z",
                "payload": raw,
            }
        )
    )
    return envelope, payload


def test_snapshot_and_event_are_one_orm_projection_with_metadata_ledgers(
    observer_store,
) -> None:
    engine, factory = observer_store
    ingress = SqlAlchemyObserverIngress(factory, cipher=CIPHER, now=lambda: NOW)
    snapshot_envelope, snapshot = _snapshot_call()
    event_envelope, event = _event_call()

    async def scenario() -> None:
        await ingress.accept_snapshot(
            identity=_identity(),
            connection_id="73000000-0000-4000-8000-000000000001",
            connector_instance_id="74000000-0000-4000-8000-000000000001",
            runtime_generation=snapshot.runtime_generation,
            envelope=snapshot_envelope,
            payload=snapshot,
        )
        await ingress.accept_event(
            identity=_identity(),
            connection_id="73000000-0000-4000-8000-000000000001",
            connector_instance_id="74000000-0000-4000-8000-000000000001",
            runtime_generation=event.runtime_generation,
            envelope=event_envelope,
            payload=event,
        )

    asyncio.run(scenario())

    with Session(engine) as session:
        stored = session.scalar(select(ObserverSessionModel))
        assert stored is not None
        assert stored.tenant_id == TENANT_ID
        assert stored.workspace_id == WORKSPACE_ID
        assert stored.agent_id == AGENT_ID
        assert stored.profile == "default"
        assert stored.session_id == CATALOG_SESSION_ID
        assert stored.event_sequence == 6
        assert stored.snapshot_event_sequence == 4
        assert "Fixture snapshot" not in json.dumps(stored.messages)
        assert "ciphertext" in stored.messages
        assert session.scalar(select(func.count()).select_from(ObserverEventModel)) == 2
        assert session.scalar(select(func.count()).select_from(InboxMessageModel)) == 0
        assert session.scalar(select(func.count()).select_from(ObserverInboxModel)) == 2
        assert session.scalar(select(func.count()).select_from(OutboxEventModel)) == 2
        assert session.scalar(select(func.count()).select_from(AuditEventModel)) == 2
        outbox_payloads = session.scalars(select(OutboxEventModel.payload)).all()
        audit_details = session.scalars(select(AuditEventModel.details)).all()
        serialized_metadata = json.dumps([*outbox_payloads, *audit_details])
        assert "Fixture snapshot" not in serialized_metadata
        assert "Fixture live delta" not in serialized_metadata

    repository = SqlAlchemyObserverProjectionRepository(factory, cipher=CIPHER)
    projected = repository.observer_snapshot(
        tenant_id=TENANT_ID,
        user_id=USER_ID,
        session_key="session-root-1",
    )
    assert projected is not None
    assert projected["runtime_session_id"] == "runtime-session-1"
    assert projected["event_sequence"] == 6
    assert [item["event_sequence"] for item in projected["replay_events"]] == [5, 6]
    wrong_cipher = AesGcmTenantEnvelopeCipher(
        MappingTenantKekResolver(
            keys={TENANT_ID: {"test-v1": b"x" * 32}},
            current_versions={TENANT_ID: "test-v1"},
        )
    )
    with pytest.raises(ObserverEncryptionError):
        SqlAlchemyObserverProjectionRepository(
            factory,
            cipher=wrong_cipher,
        ).observer_snapshot(
            tenant_id=TENANT_ID,
            user_id=USER_ID,
            session_key="session-root-1",
        )


def test_v2_snapshot_atomically_persists_encrypted_lifecycle_projection(
    observer_store,
) -> None:
    engine, factory = observer_store
    ingress = SqlAlchemyObserverIngress(factory, cipher=CIPHER, now=lambda: NOW)
    envelope, snapshot = _snapshot_v2_call()

    asyncio.run(
        ingress.accept_snapshot(
            identity=_identity(),
            connection_id="73000000-0000-4000-8000-000000000001",
            connector_instance_id="74000000-0000-4000-8000-000000000001",
            runtime_generation=snapshot.runtime_generation,
            envelope=envelope,
            payload=snapshot,
        )
    )

    with Session(engine) as session:
        stored = session.scalar(select(ObserverSessionModel))
        stored_v2_state = session.scalar(select(ObserverV2StateModel))
        assert stored is not None
        assert stored_v2_state is not None
        assert stored_v2_state.session_id == stored.session_id
        assert stored_v2_state.observer_contract == 2
        assert "Run tests" not in json.dumps(stored_v2_state.lifecycle_projection)
        assert "ciphertext" in stored_v2_state.lifecycle_projection

    projected = SqlAlchemyObserverProjectionRepository(
        factory,
        cipher=CIPHER,
    ).observer_snapshot(
        tenant_id=TENANT_ID,
        user_id=USER_ID,
        session_key="session-root-v2",
        profile="default",
    )
    assert projected is not None
    assert projected["observer_contract"] == 2
    assert projected["runtime_generation"] == "runtime-generation-v2"
    assert projected["session_id"] == str(CATALOG_V2_SESSION_ID)
    assert projected["todo_sections"] == [
        {
            "turn_id": "turn-1",
            "section_id": "todo-1",
            "revision": 1,
            "first_event_sequence": 1,
            "status": "in_progress",
            "items": [{"id": "item-1", "label": "Run tests", "status": "in_progress"}],
        }
    ]
    assert projected["subagents"] == []
    assert projected["tools"] == []
    assert projected["terminals"] == []


def test_snapshot_and_event_ambiguity_queries_are_bounded_to_two_candidates(
    observer_store,
) -> None:
    engine, factory = observer_store
    with factory.begin() as session:
        for position, profile in enumerate(
            ("default", "work", "review"),
            start=1,
        ):
            session.add(
                ObserverSessionModel(
                    tenant_id=TENANT_ID,
                    session_id=UUID(
                        f"75000000-0000-4000-8000-{position:012d}"
                    ),
                    workspace_id=WORKSPACE_ID,
                    agent_id=AGENT_ID,
                    device_id=DEVICE_ID,
                    profile=profile,
                    session_key="ambiguous-session-root",
                    runtime_session_id=f"runtime-{profile}",
                    runtime_generation="runtime-ambiguous-1",
                    connector_instance_id=(
                        "74000000-0000-4000-8000-000000000001"
                    ),
                    connection_id="73000000-0000-4000-8000-000000000001",
                    running=True,
                    status="running",
                    event_sequence=0,
                    snapshot_event_sequence=0,
                    snapshot_head_sequence=0,
                    messages={},
                    inflight={},
                    replay_events={},
                    payload_digest=f"{position}" * 64,
                    updated_at=NOW,
                    retention_until=NOW + timedelta(days=30),
                )
            )

    candidate_queries: list[tuple[str, object]] = []

    def capture_candidate_query(
        _connection: object,
        _cursor: object,
        statement: str,
        parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        if "observer_sessions" in statement and "workspace_memberships" in statement:
            candidate_queries.append((statement, parameters))

    repository = SqlAlchemyObserverProjectionRepository(factory, cipher=CIPHER)
    event.listen(engine, "before_cursor_execute", capture_candidate_query)
    try:
        assert (
            repository.observer_snapshot(
                tenant_id=TENANT_ID,
                user_id=USER_ID,
                session_key="ambiguous-session-root",
            )
            is None
        )
        assert (
            repository.event_batch(
                tenant_id=TENANT_ID,
                user_id=USER_ID,
                session_key="ambiguous-session-root",
                profile=None,
                after_sequence=0,
                limit=100,
            )
            == ()
        )
    finally:
        event.remove(engine, "before_cursor_execute", capture_candidate_query)

    assert len(candidate_queries) == 2
    for statement, parameters in candidate_queries:
        assert "LIMIT ? OFFSET ?" in statement
        assert 2 in parameters


def test_v2_snapshot_semantic_conflict_rolls_back_the_whole_ingress(
    observer_store,
) -> None:
    engine, factory = observer_store
    ingress = SqlAlchemyObserverIngress(factory, cipher=CIPHER, now=lambda: NOW)
    envelope, snapshot = _snapshot_v2_call(
        overrides={
            "subagents": [
                {
                    "turn_id": "turn-1",
                    "subagent_id": "orphan",
                    "revision": 1,
                    "first_event_sequence": 1,
                    "parent_subagent_id": "missing",
                    "name": "Orphan",
                    "goal": "",
                    "summary": None,
                    "status": "running",
                }
            ]
        }
    )

    with pytest.raises(ObserverProjectionConflict, match="lifecycle"):
        asyncio.run(
            ingress.accept_snapshot(
                identity=_identity(),
                connection_id="73000000-0000-4000-8000-000000000001",
                connector_instance_id="74000000-0000-4000-8000-000000000001",
                runtime_generation=snapshot.runtime_generation,
                envelope=envelope,
                payload=snapshot,
            )
        )

    with Session(engine) as session:
        assert (
            session.scalar(select(func.count()).select_from(ObserverSessionModel)) == 0
        )
        assert (
            session.scalar(select(func.count()).select_from(ObserverV2StateModel)) == 0
        )
        assert session.scalar(select(func.count()).select_from(ObserverInboxModel)) == 0
        assert session.scalar(select(func.count()).select_from(OutboxEventModel)) == 0


def test_v2_private_extensions_fail_before_any_orm_projection(observer_store) -> None:
    engine, factory = observer_store
    ingress = SqlAlchemyObserverIngress(factory, cipher=CIPHER, now=lambda: NOW)
    envelope, snapshot = _snapshot_v2_call()
    unsafe_envelope = replace(
        envelope,
        payload={
            **envelope.payload,
            "extensions": {
                "vendor.private": {
                    "nested": [{"deeper": {"client_secret": "must-not-cross"}}]
                }
            },
        },
    )

    with pytest.raises(ObserverProjectionConflict, match="display-safe"):
        asyncio.run(
            ingress.accept_snapshot(
                identity=_identity(),
                connection_id="73000000-0000-4000-8000-000000000001",
                connector_instance_id="74000000-0000-4000-8000-000000000001",
                runtime_generation=snapshot.runtime_generation,
                envelope=unsafe_envelope,
                payload=snapshot,
            )
        )

    with Session(engine) as session:
        assert (
            session.scalar(select(func.count()).select_from(ObserverSessionModel)) == 0
        )
        assert (
            session.scalar(select(func.count()).select_from(ObserverV2StateModel)) == 0
        )
        assert session.scalar(select(func.count()).select_from(ObserverInboxModel)) == 0
        assert session.scalar(select(func.count()).select_from(OutboxEventModel)) == 0


@pytest.mark.parametrize(
    "credential",
    (
        "Authorization: Basic dXNlcjpwYXNzd29yZA==",
        "password=hunter2",
    ),
)
def test_v2_credential_in_message_rolls_back_before_encryption_and_acl_read(
    observer_store,
    credential: str,
) -> None:
    engine, factory = observer_store
    ingress = SqlAlchemyObserverIngress(factory, cipher=CIPHER, now=lambda: NOW)
    envelope, snapshot = _snapshot_v2_call()
    unsafe_envelope = replace(
        envelope,
        payload={
            **envelope.payload,
            "messages": [{"role": "assistant", "content": credential}],
        },
    )
    unsafe_snapshot = replace(
        snapshot,
        messages=({"role": "assistant", "content": credential},),
    )

    with pytest.raises(ObserverProjectionConflict, match="display-safe") as rejected:
        asyncio.run(
            ingress.accept_snapshot(
                identity=_identity(),
                connection_id="73000000-0000-4000-8000-000000000001",
                connector_instance_id="74000000-0000-4000-8000-000000000001",
                runtime_generation=unsafe_snapshot.runtime_generation,
                envelope=unsafe_envelope,
                payload=unsafe_snapshot,
            )
        )

    assert credential not in str(rejected.value)
    with Session(engine) as session:
        for model in (
            ObserverSessionModel,
            ObserverV2StateModel,
            ObserverEventModel,
            ObserverInboxModel,
            OutboxEventModel,
        ):
            assert session.scalar(select(func.count()).select_from(model)) == 0
    assert (
        SqlAlchemyObserverProjectionRepository(
            factory,
            cipher=CIPHER,
        ).observer_snapshot(
            tenant_id=TENANT_ID,
            user_id=USER_ID,
            session_key="session-root-v2",
            profile="default",
        )
        is None
    )


@pytest.mark.parametrize(
    "credential",
    (
        "token=provider-token-value",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2ln",
    ),
)
def test_v2_credential_in_event_text_rolls_back_without_acl_or_outbox_leak(
    observer_store,
    credential: str,
) -> None:
    engine, factory = observer_store
    ingress = SqlAlchemyObserverIngress(factory, cipher=CIPHER, now=lambda: NOW)
    snapshot_envelope, snapshot = _snapshot_v2_call()
    event_envelope, event = _event_v2_call(revision=1)
    unsafe_envelope = replace(
        event_envelope,
        payload={
            **event_envelope.payload,
            "payload": {**event_envelope.payload["payload"], "text": credential},
        },
    )
    unsafe_event = replace(event, payload={**event.payload, "text": credential})

    asyncio.run(
        ingress.accept_snapshot(
            identity=_identity(),
            connection_id="73000000-0000-4000-8000-000000000001",
            connector_instance_id="74000000-0000-4000-8000-000000000001",
            runtime_generation=snapshot.runtime_generation,
            envelope=snapshot_envelope,
            payload=snapshot,
        )
    )
    with pytest.raises(ObserverProjectionConflict, match="display-safe") as rejected:
        asyncio.run(
            ingress.accept_event(
                identity=_identity(),
                connection_id="73000000-0000-4000-8000-000000000001",
                connector_instance_id="74000000-0000-4000-8000-000000000001",
                runtime_generation=unsafe_event.runtime_generation,
                envelope=unsafe_envelope,
                payload=unsafe_event,
            )
        )

    assert credential not in str(rejected.value)
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(ObserverEventModel)) == 0
        assert session.scalar(select(func.count()).select_from(ObserverInboxModel)) == 1
        assert session.scalar(select(func.count()).select_from(OutboxEventModel)) == 1
    projected = SqlAlchemyObserverProjectionRepository(
        factory,
        cipher=CIPHER,
    ).observer_snapshot(
        tenant_id=TENANT_ID,
        user_id=USER_ID,
        session_key="session-root-v2",
        profile="default",
    )
    assert projected is not None
    assert credential not in json.dumps(projected)


def test_v2_live_revision_conflict_rolls_back_event_and_transport_inbox(
    observer_store,
) -> None:
    engine, factory = observer_store
    ingress = SqlAlchemyObserverIngress(factory, cipher=CIPHER, now=lambda: NOW)
    snapshot_envelope, snapshot = _snapshot_v2_call(
        overrides={
            "tools": [
                {
                    "turn_id": "turn-1",
                    "tool_call_id": "tool-1",
                    "revision": 1,
                    "first_event_sequence": 4,
                    "status": "running",
                    "name": "Contract tests",
                }
            ]
        }
    )
    event_envelope, event = _event_v2_call(revision=1)

    asyncio.run(
        ingress.accept_snapshot(
            identity=_identity(),
            connection_id="73000000-0000-4000-8000-000000000001",
            connector_instance_id="74000000-0000-4000-8000-000000000001",
            runtime_generation=snapshot.runtime_generation,
            envelope=snapshot_envelope,
            payload=snapshot,
        )
    )
    with pytest.raises(ObserverProjectionConflict, match="lifecycle"):
        asyncio.run(
            ingress.accept_event(
                identity=_identity(),
                connection_id="73000000-0000-4000-8000-000000000001",
                connector_instance_id="74000000-0000-4000-8000-000000000001",
                runtime_generation=event.runtime_generation,
                envelope=event_envelope,
                payload=event,
            )
        )

    with Session(engine) as session:
        stored = session.scalar(select(ObserverSessionModel))
        assert stored is not None
        assert stored.event_sequence == 4
        assert session.scalar(select(func.count()).select_from(ObserverEventModel)) == 0
        assert session.scalar(select(func.count()).select_from(ObserverInboxModel)) == 1


def test_event_gap_rolls_back_inbox_projection_outbox_and_audit(observer_store) -> None:
    engine, factory = observer_store
    ingress = SqlAlchemyObserverIngress(factory, cipher=CIPHER, now=lambda: NOW)
    snapshot_envelope, snapshot = _snapshot_call()
    gap_envelope, gap = _event_call(
        "72000000-0000-4000-8000-000000000099",
        sequence=8,
    )

    async def scenario() -> None:
        await ingress.accept_snapshot(
            identity=_identity(),
            connection_id="73000000-0000-4000-8000-000000000001",
            connector_instance_id="74000000-0000-4000-8000-000000000001",
            runtime_generation=snapshot.runtime_generation,
            envelope=snapshot_envelope,
            payload=snapshot,
        )
        with pytest.raises(
            ObserverProjectionConflict,
            match="contiguous",
        ) as rejected:
            await ingress.accept_event(
                identity=_identity(),
                connection_id="73000000-0000-4000-8000-000000000001",
                connector_instance_id="74000000-0000-4000-8000-000000000001",
                runtime_generation=gap.runtime_generation,
                envelope=gap_envelope,
                payload=gap,
            )
        assert rejected.value.reason == "event_gap"
        assert rejected.value.expected_event_sequence == 6
        assert rejected.value.recovery == "send_snapshot"

    asyncio.run(scenario())

    with Session(engine) as session:
        stored = session.scalar(select(ObserverSessionModel))
        assert stored is not None
        assert stored.event_sequence == 5
        assert session.scalar(select(func.count()).select_from(InboxMessageModel)) == 0
        assert session.scalar(select(func.count()).select_from(ObserverInboxModel)) == 1
        assert session.scalar(select(func.count()).select_from(OutboxEventModel)) == 1
        assert session.scalar(select(func.count()).select_from(AuditEventModel)) == 1


def test_retention_uses_trusted_source_time_without_future_extension(
    observer_store,
) -> None:
    engine, factory = observer_store
    ingress = SqlAlchemyObserverIngress(factory, cipher=CIPHER, now=lambda: NOW)
    envelope, snapshot = _snapshot_call(sent_at="2026-06-01T09:00:00Z")

    asyncio.run(
        ingress.accept_snapshot(
            identity=_identity(),
            connection_id="73000000-0000-4000-8000-000000000001",
            connector_instance_id="74000000-0000-4000-8000-000000000001",
            runtime_generation=snapshot.runtime_generation,
            envelope=envelope,
            payload=snapshot,
        )
    )

    with Session(engine) as session:
        stored = session.scalar(select(ObserverSessionModel))
        assert stored is not None
        assert stored.retention_until.replace(tzinfo=UTC) == datetime(
            2026,
            7,
            1,
            9,
            0,
            tzinfo=UTC,
        )


def test_retention_rejects_source_timestamp_beyond_allowed_future_skew(
    observer_store,
) -> None:
    _engine, factory = observer_store
    ingress = SqlAlchemyObserverIngress(factory, cipher=CIPHER, now=lambda: NOW)
    envelope, snapshot = _snapshot_call(sent_at="2026-07-31T09:05:01Z")

    with pytest.raises(ObserverProjectionConflict, match="future skew"):
        asyncio.run(
            ingress.accept_snapshot(
                identity=_identity(),
                connection_id="73000000-0000-4000-8000-000000000001",
                connector_instance_id="74000000-0000-4000-8000-000000000001",
                runtime_generation=snapshot.runtime_generation,
                envelope=envelope,
                payload=snapshot,
            )
        )


@pytest.mark.parametrize(
    ("event_sent_at", "ingress_now", "expected_retention"),
    (
        (
            "2026-08-20T09:00:00Z",
            NOW + timedelta(days=20),
            NOW + timedelta(days=50),
        ),
        (
            "2026-07-11T09:00:00Z",
            NOW + timedelta(days=1),
            NOW + timedelta(days=30),
        ),
    ),
)
def test_events_extend_but_never_shrink_session_retention(
    observer_store,
    event_sent_at: str,
    ingress_now: datetime,
    expected_retention: datetime,
) -> None:
    engine, factory = observer_store
    current_time = NOW
    ingress = SqlAlchemyObserverIngress(
        factory,
        cipher=CIPHER,
        now=lambda: current_time,
    )
    snapshot_envelope, snapshot = _snapshot_call()
    event_envelope, event = _event_call(sent_at=event_sent_at)

    async def scenario() -> None:
        nonlocal current_time
        await ingress.accept_snapshot(
            identity=_identity(),
            connection_id="73000000-0000-4000-8000-000000000001",
            connector_instance_id="74000000-0000-4000-8000-000000000001",
            runtime_generation=snapshot.runtime_generation,
            envelope=snapshot_envelope,
            payload=snapshot,
        )
        current_time = ingress_now
        await ingress.accept_event(
            identity=_identity(),
            connection_id="73000000-0000-4000-8000-000000000001",
            connector_instance_id="74000000-0000-4000-8000-000000000001",
            runtime_generation=event.runtime_generation,
            envelope=event_envelope,
            payload=event,
        )

    asyncio.run(scenario())

    with Session(engine) as session:
        stored = session.scalar(select(ObserverSessionModel))
        assert stored is not None
        assert stored.retention_until.replace(tzinfo=UTC) == expected_retention


@pytest.mark.parametrize(
    ("field", "plaintext"),
    (
        ("messages", [{"role": "assistant", "content": "x" * 131_073}]),
        ("inflight", {"user": None}),
        (
            "replay_events",
            [
                {
                    "type": "unknown.event",
                    "session_id": "runtime-session-1",
                    "session_key": "session-root-1",
                    "event_sequence": 5,
                    "payload": {},
                }
            ],
        ),
    ),
)
def test_repository_revalidates_decrypted_snapshot_plaintext(
    observer_store,
    field: str,
    plaintext: object,
) -> None:
    engine, factory = observer_store
    ingress = SqlAlchemyObserverIngress(factory, cipher=CIPHER, now=lambda: NOW)
    envelope, snapshot = _snapshot_call()
    asyncio.run(
        ingress.accept_snapshot(
            identity=_identity(),
            connection_id="73000000-0000-4000-8000-000000000001",
            connector_instance_id="74000000-0000-4000-8000-000000000001",
            runtime_generation=snapshot.runtime_generation,
            envelope=envelope,
            payload=snapshot,
        )
    )
    encrypted = CIPHER.encrypt_json(
        plaintext,
        context=ObserverEncryptionContext(
            tenant_id=TENANT_ID,
            agent_id=AGENT_ID,
            profile="default",
            session_key="session-root-1",
            field=field,
            schema_version=1,
        ),
    )
    with Session(engine) as session, session.begin():
        stored = session.scalar(select(ObserverSessionModel))
        assert stored is not None
        setattr(stored, field, encrypted)

    repository = SqlAlchemyObserverProjectionRepository(factory, cipher=CIPHER)
    with pytest.raises(ObserverProjectionConflict, match="plaintext"):
        repository.observer_snapshot(
            tenant_id=TENANT_ID,
            user_id=USER_ID,
            session_key="session-root-1",
        )


def test_retention_cleaner_deletes_bodies_with_metadata_only_audit(
    observer_store,
) -> None:
    engine, factory = observer_store
    ingress = SqlAlchemyObserverIngress(factory, cipher=CIPHER, now=lambda: NOW)
    envelope, snapshot = _snapshot_call()
    asyncio.run(
        ingress.accept_snapshot(
            identity=_identity(),
            connection_id="73000000-0000-4000-8000-000000000001",
            connector_instance_id="74000000-0000-4000-8000-000000000001",
            runtime_generation=snapshot.runtime_generation,
            envelope=envelope,
            payload=snapshot,
        )
    )
    cleaner = SqlAlchemyObserverRetentionCleaner(
        factory,
        tenant_id=TENANT_ID,
        now=lambda: NOW + timedelta(days=31),
        retry_delay=timedelta(minutes=1),
    )

    result = cleaner.run_once()

    assert result.deleted == 1
    assert result.failed == 0
    with Session(engine) as session:
        assert (
            session.scalar(select(func.count()).select_from(ObserverSessionModel)) == 0
        )
        assert session.scalar(select(func.count()).select_from(ObserverEventModel)) == 0
        assert session.scalar(select(func.count()).select_from(ObserverInboxModel)) == 0
        ledger = session.scalar(select(ObserverDeletionLedgerModel))
        assert ledger is not None
        assert ledger.state == "deleted"
        assert ledger.attempts == 1
        audits = session.scalars(
            select(AuditEventModel).where(AuditEventModel.action == "retention.delete")
        ).all()
        assert len(audits) == 1
        assert set(audits[0].details) == {"profile", "session_key"}
        assert "Fixture snapshot" not in json.dumps(audits[0].details)


def test_retention_cleaner_records_failure_and_retries_after_backoff(
    observer_store,
) -> None:
    engine, factory = observer_store
    ingress = SqlAlchemyObserverIngress(factory, cipher=CIPHER, now=lambda: NOW)
    envelope, snapshot = _snapshot_call()
    asyncio.run(
        ingress.accept_snapshot(
            identity=_identity(),
            connection_id="73000000-0000-4000-8000-000000000001",
            connector_instance_id="74000000-0000-4000-8000-000000000001",
            runtime_generation=snapshot.runtime_generation,
            envelope=envelope,
            payload=snapshot,
        )
    )
    current_time = NOW + timedelta(days=31)
    calls = 0

    def fail_once(_tenant_id: UUID, _session_id: UUID) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("sensitive body must not enter the ledger")

    cleaner = SqlAlchemyObserverRetentionCleaner(
        factory,
        tenant_id=TENANT_ID,
        now=lambda: current_time,
        retry_delay=timedelta(minutes=1),
        before_delete=fail_once,
    )

    first = cleaner.run_once()

    assert first.failed == 1
    with Session(engine) as session:
        assert (
            session.scalar(select(func.count()).select_from(ObserverSessionModel)) == 1
        )
        ledger = session.scalar(select(ObserverDeletionLedgerModel))
        assert ledger is not None
        assert ledger.state == "failed"
        assert ledger.attempts == 1
        assert ledger.last_error_code == "observer_retention_delete_failed"
        assert "sensitive" not in repr(ledger.__dict__)

    assert cleaner.run_once().selected == 0
    current_time += timedelta(minutes=2)
    second = cleaner.run_once()

    assert second.deleted == 1
    with Session(engine) as session:
        ledger = session.scalar(select(ObserverDeletionLedgerModel))
        assert ledger is not None
        assert ledger.state == "deleted"
        assert ledger.attempts == 2


def test_retention_prunes_old_epoch_inbox_without_deleting_active_session(
    observer_store,
) -> None:
    engine, factory = observer_store
    current_time = NOW
    ingress = SqlAlchemyObserverIngress(
        factory,
        cipher=CIPHER,
        now=lambda: current_time,
    )
    old_envelope, old_snapshot = _snapshot_call()
    new_envelope, new_snapshot = _snapshot_call(
        "71000000-0000-4000-8000-000000000002",
        sent_at="2026-08-31T09:00:00Z",
        overrides={
            "runtime_generation": "runtime-20260831-02",
            "runtime_session_id": "runtime-session-2",
            "event_sequence": 0,
            "snapshot_event_sequence": 0,
            "messages": [],
            "replay_events": [],
        },
    )

    async def scenario() -> None:
        nonlocal current_time
        await ingress.accept_snapshot(
            identity=_identity(),
            connection_id="73000000-0000-4000-8000-000000000001",
            connector_instance_id="74000000-0000-4000-8000-000000000001",
            runtime_generation=old_snapshot.runtime_generation,
            envelope=old_envelope,
            payload=old_snapshot,
        )
        current_time = NOW + timedelta(days=31)
        await ingress.accept_snapshot(
            identity=_identity(),
            connection_id="73000000-0000-4000-8000-000000000002",
            connector_instance_id="74000000-0000-4000-8000-000000000001",
            runtime_generation=new_snapshot.runtime_generation,
            envelope=new_envelope,
            payload=new_snapshot,
        )
        # The current epoch remains idempotent before its retention boundary.
        await ingress.accept_snapshot(
            identity=_identity(),
            connection_id="73000000-0000-4000-8000-000000000003",
            connector_instance_id="74000000-0000-4000-8000-000000000001",
            runtime_generation=new_snapshot.runtime_generation,
            envelope=new_envelope,
            payload=new_snapshot,
        )

    asyncio.run(scenario())
    cleaner = SqlAlchemyObserverRetentionCleaner(
        factory,
        tenant_id=TENANT_ID,
        now=lambda: current_time,
    )

    result = cleaner.run_once()

    assert result.deleted == 0
    assert result.inbox_selected == 1
    assert result.inbox_deleted == 1
    with Session(engine) as session:
        assert (
            session.scalar(select(func.count()).select_from(ObserverSessionModel)) == 1
        )
        inbox = session.scalars(select(ObserverInboxModel)).all()
        assert [row.runtime_generation for row in inbox] == ["runtime-20260831-02"]


def test_inbox_retention_cleanup_is_tenant_scoped_bounded_and_retryable(
    observer_store,
) -> None:
    engine, factory = observer_store
    other_tenant_id = UUID("10000000-0000-4000-8000-000000000099")
    target_ids = (
        UUID("71000000-0000-4000-8000-000000000011"),
        UUID("71000000-0000-4000-8000-000000000012"),
    )
    other_id = UUID("71000000-0000-4000-8000-000000000099")

    def inbox_row(
        *, tenant_id: UUID, message_id: UUID, sequence: int
    ) -> ObserverInboxModel:
        return ObserverInboxModel(
            tenant_id=tenant_id,
            message_id=message_id,
            workspace_id=WORKSPACE_ID,
            agent_id=AGENT_ID,
            device_id=DEVICE_ID,
            connector_instance_id="74000000-0000-4000-8000-000000000001",
            runtime_generation=f"expired-epoch-{sequence}",
            connector_sequence=sequence,
            message_type="session.snapshot",
            payload_digest="a" * 64,
            binding_digest="b" * 64,
            received_at=NOW - timedelta(days=31),
            retention_until=NOW - timedelta(days=1),
        )

    with Session(engine) as session, session.begin():
        session.add_all(
            [
                inbox_row(tenant_id=TENANT_ID, message_id=target_ids[0], sequence=11),
                inbox_row(tenant_id=TENANT_ID, message_id=target_ids[1], sequence=12),
                inbox_row(tenant_id=other_tenant_id, message_id=other_id, sequence=99),
            ]
        )

    def fail_delete(_tenant_id: UUID, _message_id: UUID) -> None:
        raise RuntimeError("synthetic inbox cleanup failure")

    failing = SqlAlchemyObserverRetentionCleaner(
        factory,
        tenant_id=TENANT_ID,
        now=lambda: NOW,
        batch_size=1,
        before_inbox_delete=fail_delete,
    )
    with pytest.raises(RuntimeError, match="synthetic inbox cleanup failure"):
        failing.run_once()

    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(ObserverInboxModel)) == 3

    cleaner = SqlAlchemyObserverRetentionCleaner(
        factory,
        tenant_id=TENANT_ID,
        now=lambda: NOW,
        batch_size=1,
    )
    first = cleaner.run_once()
    second = cleaner.run_once()
    third = cleaner.run_once()

    assert (first.inbox_selected, first.inbox_deleted) == (1, 1)
    assert (second.inbox_selected, second.inbox_deleted) == (1, 1)
    assert (third.inbox_selected, third.inbox_deleted) == (0, 0)
    with Session(engine) as session:
        remaining = session.scalars(select(ObserverInboxModel)).all()
        assert [row.tenant_id for row in remaining] == [other_tenant_id]


def test_observer_inbox_binds_authority_and_connection_is_not_idempotency(
    observer_store,
) -> None:
    engine, factory = observer_store
    ingress = SqlAlchemyObserverIngress(factory, cipher=CIPHER, now=lambda: NOW)
    envelope, snapshot = _snapshot_call()

    async def scenario() -> None:
        for connection_id in (
            "73000000-0000-4000-8000-000000000001",
            "73000000-0000-4000-8000-000000000002",
        ):
            await ingress.accept_snapshot(
                identity=_identity(),
                connection_id=connection_id,
                connector_instance_id="74000000-0000-4000-8000-000000000001",
                runtime_generation=snapshot.runtime_generation,
                envelope=envelope,
                payload=snapshot,
            )

    asyncio.run(scenario())

    with Session(engine) as session:
        inbox = session.scalar(select(ObserverInboxModel))
        assert inbox is not None
        assert inbox.workspace_id == WORKSPACE_ID
        assert inbox.agent_id == AGENT_ID
        assert inbox.device_id == DEVICE_ID
        assert inbox.connector_sequence == 1
        assert inbox.message_type == "session.snapshot"
        assert inbox.retention_until.replace(tzinfo=UTC) == NOW + timedelta(days=30)
        assert not hasattr(inbox, "connection_id")
        assert inbox.binding_digest != inbox.payload_digest
        assert session.scalar(select(func.count()).select_from(ObserverInboxModel)) == 1
        assert session.scalar(select(func.count()).select_from(OutboxEventModel)) == 1


def test_observer_inbox_scopes_transport_sequence_idempotency_to_runtime_epoch(
    observer_store,
) -> None:
    engine, factory = observer_store
    ingress = SqlAlchemyObserverIngress(factory, cipher=CIPHER, now=lambda: NOW)
    epoch_a_envelope, epoch_a = _snapshot_call()
    epoch_a_conflict_envelope, epoch_a_conflict = _snapshot_call(
        "71000000-0000-4000-8000-000000000099",
        overrides={"messages": [{"role": "assistant", "content": "changed"}]},
    )
    epoch_b_envelope, epoch_b = _snapshot_call(
        "71000000-0000-4000-8000-000000000002",
        overrides={
            "runtime_generation": "runtime-20260731-02",
            "runtime_session_id": "runtime-session-2",
            "event_sequence": 0,
            "snapshot_event_sequence": 0,
            "messages": [],
            "replay_events": [],
        },
    )

    async def accept(
        connection_id: str,
        envelope: CloudEnvelope,
        snapshot: ConnectorObserverSnapshot,
    ) -> None:
        await ingress.accept_snapshot(
            identity=_identity(),
            connection_id=connection_id,
            connector_instance_id="74000000-0000-4000-8000-000000000001",
            runtime_generation=snapshot.runtime_generation,
            envelope=envelope,
            payload=snapshot,
        )

    async def scenario() -> None:
        await accept(
            "73000000-0000-4000-8000-000000000001",
            epoch_a_envelope,
            epoch_a,
        )
        await accept(
            "73000000-0000-4000-8000-000000000002",
            epoch_a_envelope,
            epoch_a,
        )
        with pytest.raises(ObserverProjectionConflict, match="transport binding"):
            await accept(
                "73000000-0000-4000-8000-000000000003",
                epoch_a_conflict_envelope,
                epoch_a_conflict,
            )
        await accept(
            "73000000-0000-4000-8000-000000000004",
            epoch_b_envelope,
            epoch_b,
        )

    asyncio.run(scenario())

    with Session(engine) as session:
        inbox = session.scalars(
            select(ObserverInboxModel).order_by(ObserverInboxModel.received_at)
        ).all()
        assert len(inbox) == 2
        assert {row.runtime_generation for row in inbox} == {
            "runtime-20260731-01",
            "runtime-20260731-02",
        }
        assert {row.connector_sequence for row in inbox} == {1}


def test_observer_inbox_rejects_message_and_sequence_binding_collisions(
    observer_store,
) -> None:
    _engine, factory = observer_store
    ingress = SqlAlchemyObserverIngress(factory, cipher=CIPHER, now=lambda: NOW)
    envelope, snapshot = _snapshot_call()
    changed_sequence_envelope, changed_sequence_snapshot = _snapshot_call(
        envelope.message_id,
        connector_sequence=2,
    )
    changed_message_envelope, changed_message_snapshot = _snapshot_call(
        "71000000-0000-4000-8000-000000000099",
        connector_sequence=1,
    )

    async def scenario() -> None:
        await ingress.accept_snapshot(
            identity=_identity(),
            connection_id="73000000-0000-4000-8000-000000000001",
            connector_instance_id="74000000-0000-4000-8000-000000000001",
            runtime_generation=snapshot.runtime_generation,
            envelope=envelope,
            payload=snapshot,
        )
        for candidate_envelope, candidate_snapshot in (
            (changed_sequence_envelope, changed_sequence_snapshot),
            (changed_message_envelope, changed_message_snapshot),
        ):
            with pytest.raises(ObserverProjectionConflict, match="binding"):
                await ingress.accept_snapshot(
                    identity=_identity(),
                    connection_id="73000000-0000-4000-8000-000000000002",
                    connector_instance_id="74000000-0000-4000-8000-000000000001",
                    runtime_generation=candidate_snapshot.runtime_generation,
                    envelope=candidate_envelope,
                    payload=candidate_snapshot,
                )

    asyncio.run(scenario())


def test_snapshot_same_cursor_is_idempotent_or_conflict_and_new_generation_resets(
    observer_store,
) -> None:
    engine, factory = observer_store
    ingress = SqlAlchemyObserverIngress(factory, cipher=CIPHER, now=lambda: NOW)
    first_envelope, first = _snapshot_call()
    replay_envelope, replay = _snapshot_call(
        "71000000-0000-4000-8000-000000000002",
        connector_sequence=2,
    )
    conflict_envelope, conflict = _snapshot_call(
        "71000000-0000-4000-8000-000000000003",
        connector_sequence=3,
        overrides={"messages": [{"role": "assistant", "content": "changed"}]},
    )
    next_generation_envelope, next_generation = _snapshot_call(
        "71000000-0000-4000-8000-000000000004",
        connector_sequence=4,
        overrides={
            "runtime_generation": "runtime-20260731-02",
            "runtime_session_id": "runtime-session-2",
            "event_sequence": 0,
            "snapshot_event_sequence": 0,
            "messages": [],
            "replay_events": [],
        },
    )

    async def scenario() -> None:
        for envelope, snapshot in (
            (first_envelope, first),
            (replay_envelope, replay),
        ):
            await ingress.accept_snapshot(
                identity=_identity(),
                connection_id="73000000-0000-4000-8000-000000000001",
                connector_instance_id="74000000-0000-4000-8000-000000000001",
                runtime_generation=snapshot.runtime_generation,
                envelope=envelope,
                payload=snapshot,
            )
        with pytest.raises(ObserverProjectionConflict, match="conflicting"):
            await ingress.accept_snapshot(
                identity=_identity(),
                connection_id="73000000-0000-4000-8000-000000000001",
                connector_instance_id="74000000-0000-4000-8000-000000000001",
                runtime_generation=conflict.runtime_generation,
                envelope=conflict_envelope,
                payload=conflict,
            )
        await ingress.accept_snapshot(
            identity=_identity(),
            connection_id="73000000-0000-4000-8000-000000000001",
            connector_instance_id="74000000-0000-4000-8000-000000000001",
            runtime_generation=next_generation.runtime_generation,
            envelope=next_generation_envelope,
            payload=next_generation,
        )

    asyncio.run(scenario())

    with Session(engine) as session:
        stored = session.scalar(select(ObserverSessionModel))
        assert stored is not None
        assert stored.runtime_generation == "runtime-20260731-02"
        assert stored.runtime_session_id == "runtime-session-2"
        assert stored.event_sequence == 0
        assert session.scalar(select(func.count()).select_from(ObserverEventModel)) == 0
        assert session.scalar(select(func.count()).select_from(OutboxEventModel)) == 2


def test_event_source_reads_committed_rows_in_order_and_is_cancellable(
    observer_store,
) -> None:
    _engine, factory = observer_store
    ingress = SqlAlchemyObserverIngress(factory, cipher=CIPHER, now=lambda: NOW)
    snapshot_envelope, snapshot = _snapshot_call()
    event_envelope, event = _event_call()
    source = ObserverProjectionEventSource(
        factory,
        cipher=CIPHER,
        poll_interval_seconds=0.01,
        batch_size=10,
    )

    async def scenario() -> None:
        await ingress.accept_snapshot(
            identity=_identity(),
            connection_id="73000000-0000-4000-8000-000000000001",
            connector_instance_id="74000000-0000-4000-8000-000000000001",
            runtime_generation=snapshot.runtime_generation,
            envelope=snapshot_envelope,
            payload=snapshot,
        )
        stream = source.events(
            tenant_id=TENANT_ID,
            user_id=USER_ID,
            session_key="session-root-1",
            profile="default",
            after_sequence=5,
        )
        pending = asyncio.create_task(anext(stream))
        await asyncio.sleep(0)
        assert not pending.done()
        await ingress.accept_event(
            identity=_identity(),
            connection_id="73000000-0000-4000-8000-000000000001",
            connector_instance_id="74000000-0000-4000-8000-000000000001",
            runtime_generation=event.runtime_generation,
            envelope=event_envelope,
            payload=event,
        )
        received = await asyncio.wait_for(pending, timeout=1)
        assert received["event_sequence"] == 6
        assert received["type"] == "message.delta"
        await stream.aclose()

    asyncio.run(scenario())


def test_published_sqlite_migrations_create_observer_projection_tables(
    tmp_path: Path,
) -> None:
    engine = build_sqlite_engine(
        f"sqlite+pysqlite:///{tmp_path / 'migrated.sqlite3'}",
        allow_missing=True,
    )
    try:
        result = upgrade_sqlite_schema(engine)

        assert result.schema_version == 13
        assert {
            "observer_sessions",
            "observer_events",
            "observer_inbox_messages",
            "observer_deletion_ledger",
            "observer_v2_states",
            "observer_subscription_targets",
            "observer_subscription_leases",
            "observer_subscription_intents",
            "observer_connector_routes",
        } <= set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def test_v3_atomically_encrypts_versioned_v2_observer_plaintext(
    tmp_path: Path,
) -> None:
    engine = build_sqlite_engine(
        f"sqlite+pysqlite:///{tmp_path / 'v2-plaintext.sqlite3'}",
        allow_missing=True,
    )
    try:
        _seed_versioned_v2_plaintext_observer(engine)

        result = upgrade_sqlite_schema(engine, observer_cipher=CIPHER)

        assert result.source == "versioned-2"
        with Session(engine) as session:
            stored = session.scalar(select(ObserverSessionModel))
            event = session.scalar(select(ObserverEventModel))
            assert stored is not None
            assert event is not None
            assert stored.messages["algorithm"] == "A256GCM"
            assert event.payload["algorithm"] == "A256GCM"
            assert "v2 plaintext" not in json.dumps(stored.messages)
            assert "v2 event plaintext" not in json.dumps(event.payload)
            assert session.get(SQLiteSchemaMigration, 3) is not None
    finally:
        engine.dispose()


def test_v3_rejects_plaintext_without_key_and_rolls_back_schema_and_ledger(
    tmp_path: Path,
) -> None:
    engine = build_sqlite_engine(
        f"sqlite+pysqlite:///{tmp_path / 'v2-missing-key.sqlite3'}",
        allow_missing=True,
    )
    try:
        _seed_versioned_v2_plaintext_observer(engine)

        with pytest.raises(SQLiteMigrationHistoryConflict, match="tenant key"):
            upgrade_sqlite_schema(engine)

        assert "observer_inbox_messages" not in inspect(engine).get_table_names()
        with Session(engine) as session:
            stored = session.scalar(select(ObserverSessionModel))
            assert stored is not None
            assert isinstance(stored.messages, list)
            assert session.get(SQLiteSchemaMigration, 3) is None
    finally:
        engine.dispose()
