from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Column, Integer, String, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from hermes_cloud.domain.canonical_json import canonical_payload_digest
from hermes_cloud.platform.postgres.models import OutboxEventModel, TenantModel
from hermes_cloud.platform.sqlalchemy.observer_subscription_migration_models import (
    ObserverConnectorRouteV7Model,
    ObserverSubscriptionIntentV7Model,
    ObserverSubscriptionTargetV7Model,
)
from hermes_cloud.platform.sqlalchemy.observer_subscription_models import (
    ObserverConnectorRouteModel,
    ObserverSubscriptionIntentModel,
    ObserverSubscriptionTargetModel,
)
from hermes_cloud.platform.sqlite.engine import build_sqlite_engine
from hermes_cloud.platform.sqlite.migrations import (
    CURRENT_SQLITE_SCHEMA_VERSION,
    PUBLISHED_SQLITE_MIGRATIONS,
    SQLiteSchemaMigration,
    upgrade_sqlite_schema,
)

V8_APPLIED_AT = datetime(2026, 8, 1, 12, tzinfo=UTC)
BEFORE_V8 = V8_APPLIED_AT - timedelta(hours=1)
AFTER_V8 = V8_APPLIED_AT + timedelta(hours=1)
CONNECTION_ID = "91000000-0000-4000-8000-000000000001"
CONNECTOR_INSTANCE_ID = "92000000-0000-4000-8000-000000000001"


def _database_url(path: object) -> str:
    return f"sqlite+pysqlite:///{path}"


def test_v10_is_a_distinct_data_revision_with_the_unchanged_v9_schema_shape() -> None:
    v9 = PUBLISHED_SQLITE_MIGRATIONS[8]
    v10 = PUBLISHED_SQLITE_MIGRATIONS[9]

    assert v9.name == "0009_observer_inbox_retention"
    assert v10.name == "0010_observer_subscription_legacy_wire_repair"
    assert v10.version == 10
    assert v10.checksum == v9.checksum


def _apply_through_v7(engine: Engine) -> None:
    with engine.begin() as connection:
        operations = Operations(MigrationContext.configure(connection))
        SQLiteSchemaMigration.__table__.create(connection)
        for migration in PUBLISHED_SQLITE_MIGRATIONS[:7]:
            migration.upgrade(operations)
    with Session(engine) as session, session.begin():
        for migration in PUBLISHED_SQLITE_MIGRATIONS[:7]:
            session.add(
                SQLiteSchemaMigration(
                    version=migration.version,
                    name=migration.name,
                    checksum=migration.checksum,
                    applied_at=BEFORE_V8 - timedelta(days=1),
                )
            )


def _apply_old_guessing_v8(engine: Engine) -> None:
    migration = PUBLISHED_SQLITE_MIGRATIONS[7]
    with engine.begin() as connection:
        operations = Operations(MigrationContext.configure(connection))
        operations.add_column(
            ObserverSubscriptionIntentModel.__table__.name,
            Column("observer_contract", Integer(), nullable=True),
        )
        operations.add_column(
            ObserverSubscriptionIntentModel.__table__.name,
            Column("wire_message_type", String(35), nullable=True),
        )
        operations.add_column(
            ObserverSubscriptionIntentModel.__table__.name,
            Column("wire_payload_digest", String(64), nullable=True),
        )
    with Session(engine) as session, session.begin():
        for intent in session.scalars(select(ObserverSubscriptionIntentModel)):
            intent.observer_contract = 1
            intent.wire_message_type = intent.message_type
            intent.wire_payload_digest = canonical_payload_digest(intent.payload)
        session.add(
            SQLiteSchemaMigration(
                version=migration.version,
                name=migration.name,
                checksum=migration.checksum,
                applied_at=V8_APPLIED_AT,
            )
        )


def _apply_current_revision(engine: Engine, version: int) -> None:
    migration = PUBLISHED_SQLITE_MIGRATIONS[version - 1]
    with engine.begin() as connection:
        migration.upgrade(Operations(MigrationContext.configure(connection)))
    with Session(engine) as session, session.begin():
        session.add(
            SQLiteSchemaMigration(
                version=migration.version,
                name=migration.name,
                checksum=migration.checksum,
                applied_at=V8_APPLIED_AT + timedelta(minutes=version),
            )
        )


def _payload(request_id: UUID, target_id: UUID, message_type: str) -> dict[str, object]:
    payload: dict[str, object] = {
        "request_id": str(request_id),
        "subscription_id": str(target_id),
        "profile": "default",
        "session_key": f"session-{target_id}",
        "target_source": "cloud_authorized_binding",
    }
    if message_type == "session.observe.open":
        payload["requested_at"] = "2026-07-31T11:00:00Z"
    else:
        payload["reason"] = "client_unsubscribe"
        payload["closed_at"] = "2026-07-31T11:00:00Z"
    return payload


def _seed_tenant(session: Session, tenant_id: UUID) -> None:
    session.add(
        TenantModel(
            tenant_id=tenant_id,
            slug=f"v10-repair-{tenant_id.hex}",
            display_name="V10 repair",
            status="active",
            created_at=BEFORE_V8,
        )
    )
    session.flush()


def _seed_target(
    session: Session,
    *,
    tenant_id: UUID,
    target_id: UUID,
    workspace_id: UUID,
    agent_id: UUID,
    device_id: UUID,
    state: str,
    active_ref_count: int,
    next_intent_sequence: int,
) -> None:
    session.add(
        ObserverSubscriptionTargetV7Model(
            tenant_id=tenant_id,
            target_subscription_id=target_id,
            workspace_id=workspace_id,
            agent_id=agent_id,
            device_id=device_id,
            profile="default",
            session_key=f"session-{target_id}",
            state=state,
            active_ref_count=active_ref_count,
            next_intent_sequence=next_intent_sequence,
            revision=1,
            created_at=BEFORE_V8,
            updated_at=BEFORE_V8,
        )
    )


def _seed_intent(
    session: Session,
    *,
    tenant_id: UUID,
    request_id: UUID,
    target_id: UUID,
    workspace_id: UUID,
    agent_id: UUID,
    device_id: UUID,
    intent_sequence: int,
    message_type: str,
    state: str,
    dispatch_attempts: int,
    dispatched_at: datetime | None,
    settled_at: datetime | None,
) -> None:
    payload = _payload(request_id, target_id, message_type)
    session.add(
        ObserverSubscriptionIntentV7Model(
            tenant_id=tenant_id,
            request_id=request_id,
            supersedes_request_id=None,
            target_subscription_id=target_id,
            intent_sequence=intent_sequence,
            workspace_id=workspace_id,
            agent_id=agent_id,
            device_id=device_id,
            message_type=message_type,
            payload=payload,
            state=state,
            dispatch_connection_id=(CONNECTION_ID if dispatch_attempts else None),
            dispatch_sequence=(intent_sequence + 4 if dispatch_attempts else None),
            dispatch_attempts=dispatch_attempts,
            dispatched_at=dispatched_at,
            settled_at=settled_at,
            created_at=BEFORE_V8,
            updated_at=settled_at or dispatched_at or BEFORE_V8,
        )
    )
    session.add(
        OutboxEventModel(
            tenant_id=tenant_id,
            event_id=request_id,
            workspace_id=workspace_id,
            aggregate_type="observer_subscription",
            aggregate_id=target_id,
            event_type=message_type,
            payload={"request_id": str(request_id)},
            state=(
                "published"
                if state == "settled"
                else "publishing"
                if dispatch_attempts
                else "pending"
            ),
            publish_attempts=dispatch_attempts,
            available_at=settled_at or dispatched_at or BEFORE_V8,
            published_at=settled_at,
            created_at=BEFORE_V8,
        )
    )


@pytest.mark.parametrize("source_version", (8, 9))
def test_v10_repairs_old_v8_unknown_bindings_once_without_authority_drift(
    tmp_path: object,
    source_version: int,
) -> None:
    engine = build_sqlite_engine(
        _database_url(tmp_path / f"old-v8-source-{source_version}.sqlite3"),
        allow_missing=True,
    )
    tenant_id = uuid4()
    workspace_id = uuid4()
    active_target_id = uuid4()
    active_agent_id = uuid4()
    active_device_id = uuid4()
    closed_target_id = uuid4()
    closed_agent_id = uuid4()
    closed_device_id = uuid4()
    active_request_ids = (uuid4(), uuid4())
    closed_request_id = uuid4()
    try:
        _apply_through_v7(engine)
        with Session(engine) as session, session.begin():
            _seed_tenant(session, tenant_id)
            _seed_target(
                session,
                tenant_id=tenant_id,
                target_id=active_target_id,
                workspace_id=workspace_id,
                agent_id=active_agent_id,
                device_id=active_device_id,
                state="active",
                active_ref_count=2,
                next_intent_sequence=2,
            )
            _seed_target(
                session,
                tenant_id=tenant_id,
                target_id=closed_target_id,
                workspace_id=workspace_id,
                agent_id=closed_agent_id,
                device_id=closed_device_id,
                state="closed",
                active_ref_count=0,
                next_intent_sequence=1,
            )
            session.flush()
            session.add(
                ObserverConnectorRouteV7Model(
                    tenant_id=tenant_id,
                    device_id=active_device_id,
                    agent_id=active_agent_id,
                    connector_instance_id=CONNECTOR_INSTANCE_ID,
                    connection_id=CONNECTION_ID,
                    runtime_generation="legacy-runtime",
                    state="active",
                    next_connector_sequence=7,
                    next_cloud_sequence=5,
                    revision=1,
                    connected_at=BEFORE_V8,
                    updated_at=BEFORE_V8,
                )
            )
            for sequence, (request_id, state) in enumerate(
                zip(active_request_ids, ("dispatching", "settled"), strict=True)
            ):
                _seed_intent(
                    session,
                    tenant_id=tenant_id,
                    request_id=request_id,
                    target_id=active_target_id,
                    workspace_id=workspace_id,
                    agent_id=active_agent_id,
                    device_id=active_device_id,
                    intent_sequence=sequence,
                    message_type="session.observe.open",
                    state=state,
                    dispatch_attempts=1,
                    dispatched_at=BEFORE_V8,
                    settled_at=(
                        BEFORE_V8 + timedelta(minutes=1) if state == "settled" else None
                    ),
                )
            _seed_intent(
                session,
                tenant_id=tenant_id,
                request_id=closed_request_id,
                target_id=closed_target_id,
                workspace_id=workspace_id,
                agent_id=closed_agent_id,
                device_id=closed_device_id,
                intent_sequence=0,
                message_type="session.observe.close",
                state="settled",
                dispatch_attempts=1,
                dispatched_at=BEFORE_V8,
                settled_at=BEFORE_V8 + timedelta(minutes=1),
            )

        _apply_old_guessing_v8(engine)
        if source_version == 9:
            _apply_current_revision(engine, 9)

        result = upgrade_sqlite_schema(engine)

        assert result.source == f"versioned-{source_version}"
        assert result.schema_version == 13
        assert CURRENT_SQLITE_SCHEMA_VERSION == 13
        with Session(engine) as session:
            for request_id in (*active_request_ids, closed_request_id):
                old = session.get(
                    ObserverSubscriptionIntentModel,
                    (tenant_id, request_id),
                )
                assert old is not None
                assert old.state == "cancelled"
                assert old.observer_contract is None
                assert old.wire_message_type is None
                assert old.wire_payload_digest is None
                outbox = session.get(OutboxEventModel, (tenant_id, request_id))
                assert outbox is not None
                assert outbox.state == "dead"

            active_intents = session.scalars(
                select(ObserverSubscriptionIntentModel).where(
                    ObserverSubscriptionIntentModel.tenant_id == tenant_id,
                    ObserverSubscriptionIntentModel.target_subscription_id
                    == active_target_id,
                )
            ).all()
            assert len(active_intents) == 3
            replacement = next(
                intent
                for intent in active_intents
                if intent.request_id not in active_request_ids
            )
            assert replacement.intent_sequence == 2
            assert replacement.supersedes_request_id == active_request_ids[1]
            assert replacement.state == "pending"
            assert replacement.observer_contract is None
            assert replacement.dispatch_sequence is None

            active_target = session.get(
                ObserverSubscriptionTargetModel,
                (tenant_id, active_target_id),
            )
            assert active_target is not None
            assert active_target.state == "active"
            assert active_target.active_ref_count == 2
            assert active_target.next_intent_sequence == 3
            closed_target = session.get(
                ObserverSubscriptionTargetModel,
                (tenant_id, closed_target_id),
            )
            assert closed_target is not None
            assert closed_target.state == "closed"
            assert closed_target.active_ref_count == 0
            assert closed_target.next_intent_sequence == 1
            route = session.get(
                ObserverConnectorRouteModel,
                (tenant_id, active_device_id),
            )
            assert route is not None
            assert route.next_connector_sequence == 7
            assert route.next_cloud_sequence == 5

        assert upgrade_sqlite_schema(engine).source == "current"
        with Session(engine) as session:
            assert (
                len(
                    session.scalars(
                        select(ObserverSubscriptionIntentModel).where(
                            ObserverSubscriptionIntentModel.tenant_id == tenant_id
                        )
                    ).all()
                )
                == 4
            )
    finally:
        engine.dispose()


def test_v10_repairs_old_v8_guess_when_created_at_clock_is_ahead(
    tmp_path: object,
) -> None:
    engine = build_sqlite_engine(
        _database_url(tmp_path / "v10-old-v8-clock-skew.sqlite3"),
        allow_missing=True,
    )
    tenant_id = uuid4()
    workspace_id = uuid4()
    target_id = uuid4()
    agent_id = uuid4()
    device_id = uuid4()
    request_ids = (uuid4(), uuid4())
    try:
        _apply_through_v7(engine)
        with Session(engine) as session, session.begin():
            _seed_tenant(session, tenant_id)
            _seed_target(
                session,
                tenant_id=tenant_id,
                target_id=target_id,
                workspace_id=workspace_id,
                agent_id=agent_id,
                device_id=device_id,
                state="active",
                active_ref_count=1,
                next_intent_sequence=2,
            )
            session.flush()
            session.add(
                ObserverConnectorRouteV7Model(
                    tenant_id=tenant_id,
                    device_id=device_id,
                    agent_id=agent_id,
                    connector_instance_id=CONNECTOR_INSTANCE_ID,
                    connection_id=CONNECTION_ID,
                    runtime_generation="clock-skew-runtime",
                    state="active",
                    next_connector_sequence=7,
                    next_cloud_sequence=5,
                    revision=1,
                    connected_at=BEFORE_V8,
                    updated_at=BEFORE_V8,
                )
            )
            for sequence, (request_id, dispatched_at) in enumerate(
                zip(
                    request_ids,
                    (BEFORE_V8, V8_APPLIED_AT + timedelta(minutes=30)),
                    strict=True,
                )
            ):
                _seed_intent(
                    session,
                    tenant_id=tenant_id,
                    request_id=request_id,
                    target_id=target_id,
                    workspace_id=workspace_id,
                    agent_id=agent_id,
                    device_id=device_id,
                    intent_sequence=sequence,
                    message_type="session.observe.open",
                    state="dispatching",
                    dispatch_attempts=1,
                    dispatched_at=dispatched_at,
                    settled_at=None,
                )
            session.flush()
            for request_id in request_ids:
                skewed = session.get(
                    ObserverSubscriptionIntentV7Model,
                    (tenant_id, request_id),
                )
                assert skewed is not None
                skewed.created_at = AFTER_V8

        _apply_old_guessing_v8(engine)

        assert upgrade_sqlite_schema(engine).source == "versioned-8"

        with Session(engine) as session:
            for request_id in request_ids:
                old = session.get(
                    ObserverSubscriptionIntentModel,
                    (tenant_id, request_id),
                )
                assert old is not None
                assert old.state == "cancelled"
                assert old.observer_contract is None
                outbox = session.get(OutboxEventModel, (tenant_id, request_id))
                assert outbox is not None
                assert outbox.state == "dead"
            intents = session.scalars(
                select(ObserverSubscriptionIntentModel).where(
                    ObserverSubscriptionIntentModel.tenant_id == tenant_id
                )
            ).all()
            assert len(intents) == 3
            replacement = next(
                intent for intent in intents if intent.request_id not in request_ids
            )
            assert replacement.state == "pending"
            assert replacement.observer_contract is None
            target = session.get(
                ObserverSubscriptionTargetModel,
                (tenant_id, target_id),
            )
            assert target is not None
            assert target.state == "active"
            assert target.active_ref_count == 1
            assert target.next_intent_sequence == 3
            route = session.get(
                ObserverConnectorRouteModel,
                (tenant_id, device_id),
            )
            assert route is not None
            assert route.next_connector_sequence == 7
            assert route.next_cloud_sequence == 5

        assert upgrade_sqlite_schema(engine).source == "current"
        with Session(engine) as session:
            assert (
                len(
                    session.scalars(
                        select(ObserverSubscriptionIntentModel).where(
                            ObserverSubscriptionIntentModel.tenant_id == tenant_id
                        )
                    ).all()
                )
                == 3
            )
    finally:
        engine.dispose()


@pytest.mark.parametrize("proven_state", ("dispatching", "settled"))
def test_v10_preserves_only_proven_post_v8_v1_and_new_v2(
    tmp_path: object,
    proven_state: str,
) -> None:
    engine = build_sqlite_engine(
        _database_url(tmp_path / "v10-proven-boundaries.sqlite3"),
        allow_missing=True,
    )
    tenant_id = uuid4()
    workspace_id = uuid4()
    target_id = uuid4()
    agent_id = uuid4()
    device_id = uuid4()
    pending_id = uuid4()
    proven_v1_id = uuid4()
    cancelled_id = uuid4()
    post_v8_v2_id = uuid4()
    post_v8_v1_id = uuid4()
    try:
        _apply_through_v7(engine)
        with Session(engine) as session, session.begin():
            _seed_tenant(session, tenant_id)
            _seed_target(
                session,
                tenant_id=tenant_id,
                target_id=target_id,
                workspace_id=workspace_id,
                agent_id=agent_id,
                device_id=device_id,
                state="active",
                active_ref_count=3,
                next_intent_sequence=3,
            )
            session.flush()
            for sequence, request_id in enumerate((pending_id, proven_v1_id)):
                _seed_intent(
                    session,
                    tenant_id=tenant_id,
                    request_id=request_id,
                    target_id=target_id,
                    workspace_id=workspace_id,
                    agent_id=agent_id,
                    device_id=device_id,
                    intent_sequence=sequence,
                    message_type="session.observe.open",
                    state="pending",
                    dispatch_attempts=0,
                    dispatched_at=None,
                    settled_at=None,
                )
            _seed_intent(
                session,
                tenant_id=tenant_id,
                request_id=cancelled_id,
                target_id=target_id,
                workspace_id=workspace_id,
                agent_id=agent_id,
                device_id=device_id,
                intent_sequence=2,
                message_type="session.observe.open",
                state="cancelled",
                dispatch_attempts=1,
                dispatched_at=BEFORE_V8,
                settled_at=None,
            )

        _apply_old_guessing_v8(engine)
        with Session(engine) as session, session.begin():
            proven = session.get(
                ObserverSubscriptionIntentModel,
                (tenant_id, proven_v1_id),
            )
            assert proven is not None
            proven.state = proven_state
            proven.dispatch_connection_id = CONNECTION_ID
            proven.dispatch_sequence = 5
            proven.dispatch_attempts = 1
            proven.dispatched_at = AFTER_V8
            proven.settled_at = (
                AFTER_V8 + timedelta(minutes=1) if proven_state == "settled" else None
            )
            proven.updated_at = AFTER_V8
            proven_outbox = session.get(OutboxEventModel, (tenant_id, proven_v1_id))
            assert proven_outbox is not None
            proven_outbox.state = (
                "published" if proven_state == "settled" else "publishing"
            )
            proven_outbox.publish_attempts = 1
            proven_outbox.available_at = AFTER_V8
            proven_outbox.published_at = (
                AFTER_V8 + timedelta(minutes=1) if proven_state == "settled" else None
            )

            target = session.get(
                ObserverSubscriptionTargetModel,
                (tenant_id, target_id),
            )
            assert target is not None
            target.next_intent_sequence = 5
            payload = _payload(post_v8_v2_id, target_id, "session.observe.open")
            session.add(
                ObserverSubscriptionIntentModel(
                    tenant_id=tenant_id,
                    request_id=post_v8_v2_id,
                    supersedes_request_id=None,
                    target_subscription_id=target_id,
                    intent_sequence=3,
                    workspace_id=workspace_id,
                    agent_id=agent_id,
                    device_id=device_id,
                    message_type="session.observe.open",
                    payload=payload,
                    state="dispatching",
                    dispatch_connection_id=CONNECTION_ID,
                    dispatch_sequence=6,
                    dispatch_attempts=1,
                    dispatched_at=AFTER_V8 + timedelta(minutes=1),
                    settled_at=None,
                    created_at=AFTER_V8,
                    updated_at=AFTER_V8 + timedelta(minutes=1),
                    observer_contract=2,
                    wire_message_type="session.observe.open.v2",
                    wire_payload_digest=canonical_payload_digest(
                        {**payload, "observer_contract": 2}
                    ),
                )
            )
            session.add(
                OutboxEventModel(
                    tenant_id=tenant_id,
                    event_id=post_v8_v2_id,
                    workspace_id=workspace_id,
                    aggregate_type="observer_subscription",
                    aggregate_id=target_id,
                    event_type="session.observe.open",
                    payload={"request_id": str(post_v8_v2_id)},
                    state="publishing",
                    publish_attempts=1,
                    available_at=AFTER_V8 + timedelta(minutes=1),
                    published_at=None,
                    created_at=AFTER_V8,
                )
            )
            v1_payload = _payload(post_v8_v1_id, target_id, "session.observe.open")
            v1_dispatched_at = AFTER_V8 + timedelta(minutes=2)
            v1_settled_at = (
                AFTER_V8 + timedelta(minutes=3) if proven_state == "settled" else None
            )
            session.add(
                ObserverSubscriptionIntentModel(
                    tenant_id=tenant_id,
                    request_id=post_v8_v1_id,
                    supersedes_request_id=None,
                    target_subscription_id=target_id,
                    intent_sequence=4,
                    workspace_id=workspace_id,
                    agent_id=agent_id,
                    device_id=device_id,
                    message_type="session.observe.open",
                    payload=v1_payload,
                    state=proven_state,
                    dispatch_connection_id=CONNECTION_ID,
                    dispatch_sequence=7,
                    dispatch_attempts=1,
                    dispatched_at=v1_dispatched_at,
                    settled_at=v1_settled_at,
                    created_at=AFTER_V8,
                    updated_at=v1_settled_at or v1_dispatched_at,
                    observer_contract=1,
                    wire_message_type="session.observe.open",
                    wire_payload_digest=canonical_payload_digest(v1_payload),
                )
            )
            session.add(
                OutboxEventModel(
                    tenant_id=tenant_id,
                    event_id=post_v8_v1_id,
                    workspace_id=workspace_id,
                    aggregate_type="observer_subscription",
                    aggregate_id=target_id,
                    event_type="session.observe.open",
                    payload={"request_id": str(post_v8_v1_id)},
                    state=("published" if proven_state == "settled" else "publishing"),
                    publish_attempts=1,
                    available_at=v1_dispatched_at,
                    published_at=v1_settled_at,
                    created_at=AFTER_V8,
                )
            )

        assert upgrade_sqlite_schema(engine).source == "versioned-8"

        with Session(engine) as session:
            pending = session.get(
                ObserverSubscriptionIntentModel,
                (tenant_id, pending_id),
            )
            assert pending is not None
            assert pending.state == "pending"
            assert pending.observer_contract is None
            assert pending.wire_message_type is None
            assert pending.wire_payload_digest is None
            proven = session.get(
                ObserverSubscriptionIntentModel,
                (tenant_id, proven_v1_id),
            )
            assert proven is not None
            assert proven.state == "cancelled"
            assert proven.observer_contract is None
            proven_outbox = session.get(OutboxEventModel, (tenant_id, proven_v1_id))
            assert proven_outbox is not None
            assert proven_outbox.state == "dead"
            cancelled = session.get(
                ObserverSubscriptionIntentModel,
                (tenant_id, cancelled_id),
            )
            assert cancelled is not None
            assert cancelled.state == "cancelled"
            assert cancelled.observer_contract is None
            cancelled_outbox = session.get(
                OutboxEventModel,
                (tenant_id, cancelled_id),
            )
            assert cancelled_outbox is not None
            assert cancelled_outbox.state == "dead"
            post_v8 = session.get(
                ObserverSubscriptionIntentModel,
                (tenant_id, post_v8_v2_id),
            )
            assert post_v8 is not None
            assert post_v8.state == "dispatching"
            assert post_v8.observer_contract == 2
            post_v8_v1 = session.get(
                ObserverSubscriptionIntentModel,
                (tenant_id, post_v8_v1_id),
            )
            assert post_v8_v1 is not None
            assert post_v8_v1.state == proven_state
            assert post_v8_v1.observer_contract == 1
            assert (
                len(
                    session.scalars(
                        select(ObserverSubscriptionIntentModel).where(
                            ObserverSubscriptionIntentModel.tenant_id == tenant_id
                        )
                    ).all()
                )
                == 5
            )
            target = session.get(
                ObserverSubscriptionTargetModel,
                (tenant_id, target_id),
            )
            assert target is not None
            assert target.state == "active"
            assert target.active_ref_count == 3
            assert target.next_intent_sequence == 5
    finally:
        engine.dispose()


def test_v10_is_noop_for_database_already_repaired_by_current_v8(
    tmp_path: object,
) -> None:
    engine = build_sqlite_engine(
        _database_url(tmp_path / "v10-current-v8-noop.sqlite3"),
        allow_missing=True,
    )
    tenant_id = uuid4()
    workspace_id = uuid4()
    target_id = uuid4()
    agent_id = uuid4()
    device_id = uuid4()
    request_id = uuid4()
    try:
        _apply_through_v7(engine)
        with Session(engine) as session, session.begin():
            _seed_tenant(session, tenant_id)
            _seed_target(
                session,
                tenant_id=tenant_id,
                target_id=target_id,
                workspace_id=workspace_id,
                agent_id=agent_id,
                device_id=device_id,
                state="active",
                active_ref_count=1,
                next_intent_sequence=1,
            )
            session.flush()
            _seed_intent(
                session,
                tenant_id=tenant_id,
                request_id=request_id,
                target_id=target_id,
                workspace_id=workspace_id,
                agent_id=agent_id,
                device_id=device_id,
                intent_sequence=0,
                message_type="session.observe.open",
                state="dispatching",
                dispatch_attempts=1,
                dispatched_at=BEFORE_V8,
                settled_at=None,
            )

        _apply_current_revision(engine, 8)
        _apply_current_revision(engine, 9)
        with Session(engine) as session:
            before = session.scalars(
                select(ObserverSubscriptionIntentModel).where(
                    ObserverSubscriptionIntentModel.tenant_id == tenant_id
                )
            ).all()
            before_facts = tuple(
                (
                    intent.request_id,
                    intent.state,
                    intent.supersedes_request_id,
                    intent.observer_contract,
                )
                for intent in before
            )
            assert len(before_facts) == 2

        assert upgrade_sqlite_schema(engine).source == "versioned-9"

        with Session(engine) as session:
            after = session.scalars(
                select(ObserverSubscriptionIntentModel).where(
                    ObserverSubscriptionIntentModel.tenant_id == tenant_id
                )
            ).all()
            assert (
                tuple(
                    (
                        intent.request_id,
                        intent.state,
                        intent.supersedes_request_id,
                        intent.observer_contract,
                    )
                    for intent in after
                )
                == before_facts
            )
            target = session.get(
                ObserverSubscriptionTargetModel,
                (tenant_id, target_id),
            )
            assert target is not None
            assert target.active_ref_count == 1
            assert target.next_intent_sequence == 2
    finally:
        engine.dispose()
