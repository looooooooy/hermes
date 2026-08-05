from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from importlib.util import find_spec
from pathlib import Path
from threading import Barrier, Event, local
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, select
from sqlalchemy.orm import Session, sessionmaker

from hermes_cloud.domain.canonical_json import canonical_payload_digest
from hermes_cloud.domain.connector_gateway import ConnectorIdentity
from hermes_cloud.modules.cloud_api.domain import Principal
from hermes_cloud.platform.postgres.models import (
    AgentModel,
    DeviceLifecycleModel,
    DeviceModel,
    RoleModel,
    SessionProjectionModel,
    TenantModel,
    UserModel,
    WorkspaceMembershipModel,
    WorkspaceModel,
)
from hermes_cloud.platform.sqlite.engine import build_sqlite_engine
from hermes_cloud.platform.sqlite.schema import build_sqlite_metadata

TENANT_ID = UUID("10000000-0000-4000-8000-000000000001")
USER_ID = UUID("20000000-0000-4000-8000-000000000001")
WORKSPACE_ID = UUID("30000000-0000-4000-8000-000000000001")
ROLE_ID = UUID("40000000-0000-4000-8000-000000000001")
AGENT_ID = UUID("50000000-0000-4000-8000-000000000001")
DEVICE_ID = UUID("60000000-0000-4000-8000-000000000001")
SESSION_ID = UUID("70000000-0000-4000-8000-000000000001")
NOW = datetime(2026, 7, 31, 9, 0, tzinfo=UTC)


def test_subscription_router_is_a_real_cloud_orm_adapter() -> None:
    assert (
        find_spec("hermes_cloud.platform.sqlalchemy.observer_subscription") is not None
    )


@pytest.fixture
def subscription_store(tmp_path: Path):
    from hermes_cloud.platform.sqlalchemy.observer_projection_models import (
        ObserverProjectionBase,
    )
    from hermes_cloud.platform.sqlalchemy.observer_subscription_models import (
        ObserverSubscriptionBase,
    )

    engine = build_sqlite_engine(
        f"sqlite+pysqlite:///{tmp_path / 'subscriptions.sqlite3'}",
        allow_missing=True,
    )
    build_sqlite_metadata().create_all(engine)
    ObserverProjectionBase.metadata.create_all(engine)
    ObserverSubscriptionBase.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with Session(engine) as session, session.begin():
        session.add(
            TenantModel(
                tenant_id=TENANT_ID,
                slug="subscription-test",
                display_name="Subscription Test",
                status="active",
                created_at=NOW,
            )
        )
        session.flush()
        session.add(
            UserModel(
                tenant_id=TENANT_ID,
                user_id=USER_ID,
                subject="subscription-user",
                display_name="Subscription User",
                email=None,
                status="active",
                created_at=NOW,
            )
        )
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
                workspace_key="subscription-workspace",
                display_name="Subscription Workspace",
                status="active",
                created_by=USER_ID,
                created_at=NOW,
            )
        )
        session.flush()
        session.add(
            WorkspaceMembershipModel(
                tenant_id=TENANT_ID,
                workspace_membership_id=UUID("41000000-0000-4000-8000-000000000001"),
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
                agent_key="subscription-agent",
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
                device_key="subscription-device",
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
        session.add(
            SessionProjectionModel(
                tenant_id=TENANT_ID,
                session_id=SESSION_ID,
                session_key="session-root-1",
                workspace_id=WORKSPACE_ID,
                agent_id=AGENT_ID,
                profile="default",
                title="Active Hermes Session",
                state="active",
                revision=1,
                lineage_tip_message_id=None,
                lineage_tip_sequence=0,
                started_at=NOW,
                updated_at=NOW,
                closed_at=None,
                retention_until=datetime(2026, 8, 30, 9, 0, tzinfo=UTC),
            )
        )
    try:
        yield engine, factory
    finally:
        engine.dispose()


def _principal() -> Principal:
    return Principal(
        tenant_id=TENANT_ID,
        user_id=USER_ID,
        provider="basic",
        refresh_session_id=UUID("21000000-0000-4000-8000-000000000001"),
    )


def _observer_session(*, profile: str, session_id: UUID):
    from hermes_cloud.platform.sqlalchemy.observer_projection_models import (
        ObserverSessionModel,
    )

    return ObserverSessionModel(
        tenant_id=TENANT_ID,
        session_id=session_id,
        workspace_id=WORKSPACE_ID,
        agent_id=AGENT_ID,
        device_id=DEVICE_ID,
        profile=profile,
        session_key="session-root-1",
        runtime_session_id=f"runtime-{profile}",
        runtime_generation="runtime-generation-1",
        connector_instance_id="81000000-0000-4000-8000-000000000001",
        connection_id="82000000-0000-4000-8000-000000000001",
        running=True,
        status="running",
        event_sequence=0,
        snapshot_event_sequence=0,
        snapshot_head_sequence=0,
        messages={},
        inflight={},
        replay_events={},
        payload_digest="d" * 64,
        updated_at=NOW,
        retention_until=NOW + timedelta(days=30),
    )


def test_omitted_profile_resolves_only_one_authoritative_observer_session(
    subscription_store,
) -> None:
    from hermes_cloud.platform.sqlalchemy.observer_subscription import (
        SqlAlchemyObserverSubscriptionRouter,
    )

    _engine, factory = subscription_store
    with factory.begin() as session:
        session.add(
            _observer_session(
                profile="resolved-profile",
                session_id=UUID("71000000-0000-4000-8000-000000000001"),
            )
        )

    handle = SqlAlchemyObserverSubscriptionRouter(
        factory, now=lambda: NOW
    ).open_subscription(
        principal=_principal(),
        session_key="session-root-1",
        profile=None,
    )

    assert handle.profile == "resolved-profile"


def test_omitted_profile_fails_closed_for_missing_or_ambiguous_authority(
    subscription_store,
) -> None:
    from hermes_cloud.platform.sqlalchemy.observer_subscription import (
        ObserverSubscriptionUnauthorized,
        SqlAlchemyObserverSubscriptionRouter,
    )

    _engine, factory = subscription_store
    router = SqlAlchemyObserverSubscriptionRouter(factory, now=lambda: NOW)
    with pytest.raises(ObserverSubscriptionUnauthorized, match="not found"):
        router.open_subscription(
            principal=_principal(), session_key="session-root-1", profile=None
        )

    with factory.begin() as session:
        session.add_all(
            (
                _observer_session(
                    profile="first-profile",
                    session_id=UUID("71000000-0000-4000-8000-000000000001"),
                ),
                _observer_session(
                    profile="second-profile",
                    session_id=UUID("71000000-0000-4000-8000-000000000002"),
                ),
            )
        )
    with pytest.raises(ObserverSubscriptionUnauthorized, match="ambiguous"):
        router.open_subscription(
            principal=_principal(), session_key="session-root-1", profile=None
        )


def test_projection_ambiguity_query_is_bounded_to_two_candidates(
    subscription_store,
) -> None:
    from hermes_cloud.platform.sqlalchemy.observer_subscription import (
        ObserverSubscriptionUnauthorized,
        SqlAlchemyObserverSubscriptionRouter,
    )

    engine, factory = subscription_store
    with factory.begin() as session:
        for position, profile in enumerate(("work", "review"), start=2):
            session.add(
                SessionProjectionModel(
                    tenant_id=TENANT_ID,
                    session_id=UUID(
                        f"70000000-0000-4000-8000-{position:012d}"
                    ),
                    session_key="session-root-1",
                    workspace_id=WORKSPACE_ID,
                    agent_id=AGENT_ID,
                    profile=profile,
                    title=f"{profile.title()} Hermes Session",
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

    projection_queries: list[tuple[str, object]] = []

    def capture_projection_query(
        _connection: object,
        _cursor: object,
        statement: str,
        parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        if "workspace_memberships" in statement:
            projection_queries.append((statement, parameters))

    event.listen(engine, "before_cursor_execute", capture_projection_query)
    try:
        with pytest.raises(ObserverSubscriptionUnauthorized):
            SqlAlchemyObserverSubscriptionRouter(
                factory,
                now=lambda: NOW,
            ).open_subscription(
                principal=_principal(),
                session_key="session-root-1",
                profile=None,
            )
    finally:
        event.remove(engine, "before_cursor_execute", capture_projection_query)

    assert len(projection_queries) == 1
    statement, parameters = projection_queries[0]
    assert "LIMIT ? OFFSET ?" in statement
    assert 2 in parameters


def test_supplied_profile_authorizes_exact_same_agent_session_projection(
    subscription_store,
) -> None:
    from hermes_cloud.platform.sqlalchemy.observer_subscription import (
        ObserverSubscriptionUnauthorized,
        SqlAlchemyObserverSubscriptionRouter,
    )

    _engine, factory = subscription_store
    with factory.begin() as session:
        session.add(
            SessionProjectionModel(
                tenant_id=TENANT_ID,
                session_id=UUID("70000000-0000-4000-8000-000000000002"),
                session_key="session-root-1",
                workspace_id=WORKSPACE_ID,
                agent_id=AGENT_ID,
                profile="work",
                title="Work Hermes Session",
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

    router = SqlAlchemyObserverSubscriptionRouter(factory, now=lambda: NOW)
    handle = router.open_subscription(
        principal=_principal(),
        session_key="session-root-1",
        profile="default",
    )

    assert handle.profile == "default"
    with pytest.raises(ObserverSubscriptionUnauthorized):
        router.open_subscription(
            principal=_principal(),
            session_key="session-root-1",
            profile="missing",
        )


def test_only_first_authorized_ref_creates_open_intent(subscription_store) -> None:
    from hermes_cloud.platform.sqlalchemy.observer_subscription import (
        SqlAlchemyObserverSubscriptionRouter,
    )
    from hermes_cloud.platform.sqlalchemy.observer_subscription_models import (
        ObserverSubscriptionIntentModel,
        ObserverSubscriptionLeaseModel,
        ObserverSubscriptionTargetModel,
    )

    _engine, factory = subscription_store
    router = SqlAlchemyObserverSubscriptionRouter(
        factory,
        now=lambda: NOW,
        lease_ttl_seconds=90,
    )

    first = router.open_subscription(
        principal=_principal(),
        session_key="session-root-1",
        profile="default",
    )
    second = router.open_subscription(
        principal=_principal(),
        session_key="session-root-1",
        profile="default",
    )

    assert first.subscription_id != second.subscription_id
    assert first.target_subscription_id == second.target_subscription_id
    assert first.requires_initial_snapshot is True
    assert second.requires_initial_snapshot is False
    with factory.begin() as session:
        targets = session.scalars(select(ObserverSubscriptionTargetModel)).all()
        leases = session.scalars(select(ObserverSubscriptionLeaseModel)).all()
        intents = session.scalars(select(ObserverSubscriptionIntentModel)).all()
    assert len(targets) == 1
    assert targets[0].active_ref_count == 2
    assert len(leases) == 2
    assert len(intents) == 1
    assert intents[0].message_type == "session.observe.open"
    assert intents[0].intent_sequence == 0
    assert intents[0].payload == {
        "request_id": str(intents[0].request_id),
        "subscription_id": str(targets[0].target_subscription_id),
        "profile": "default",
        "session_key": "session-root-1",
        "target_source": "cloud_authorized_binding",
        "requested_at": "2026-07-31T09:00:00Z",
    }


def test_only_last_ref_creates_close_intent(subscription_store) -> None:
    from hermes_cloud.platform.sqlalchemy.observer_subscription import (
        SqlAlchemyObserverSubscriptionRouter,
    )
    from hermes_cloud.platform.sqlalchemy.observer_subscription_models import (
        ObserverSubscriptionIntentModel,
        ObserverSubscriptionTargetModel,
    )

    _engine, factory = subscription_store
    router = SqlAlchemyObserverSubscriptionRouter(factory, now=lambda: NOW)
    first = router.open_subscription(
        principal=_principal(), session_key="session-root-1", profile="default"
    )
    second = router.open_subscription(
        principal=_principal(), session_key="session-root-1", profile="default"
    )

    router.close_subscription(
        principal=_principal(),
        subscription_id=first.subscription_id,
        reason="client_unsubscribe",
    )
    with factory.begin() as session:
        target = session.scalars(select(ObserverSubscriptionTargetModel)).one()
        intents = session.scalars(select(ObserverSubscriptionIntentModel)).all()
    assert target.active_ref_count == 1
    assert [intent.message_type for intent in intents] == ["session.observe.open"]

    router.close_subscription(
        principal=_principal(),
        subscription_id=second.subscription_id,
        reason="client_unsubscribe",
    )
    with factory.begin() as session:
        target = session.scalars(select(ObserverSubscriptionTargetModel)).one()
        intents = session.scalars(
            select(ObserverSubscriptionIntentModel).order_by(
                ObserverSubscriptionIntentModel.intent_sequence,
            )
        ).all()
    assert target.active_ref_count == 0
    assert target.state == "closing"
    assert target.next_intent_sequence == 2
    assert [intent.message_type for intent in intents] == [
        "session.observe.open",
        "session.observe.close",
    ]
    close_intent = next(
        intent for intent in intents if intent.message_type == "session.observe.close"
    )
    assert close_intent.payload["subscription_id"] == str(first.target_subscription_id)


@pytest.mark.asyncio
async def test_dispatch_settles_only_after_heartbeat_and_replays_on_reconnect(
    subscription_store,
) -> None:
    from hermes_cloud.platform.sqlalchemy.observer_subscription import (
        SqlAlchemyObserverSubscriptionRouter,
    )
    from hermes_cloud.platform.sqlalchemy.observer_subscription_models import (
        ObserverSubscriptionIntentModel,
    )

    _engine, factory = subscription_store
    router = SqlAlchemyObserverSubscriptionRouter(
        factory,
        now=lambda: NOW,
        poll_interval_seconds=0.001,
    )
    router.open_subscription(
        principal=_principal(), session_key="session-root-1", profile="default"
    )
    identity = ConnectorIdentity(
        tenant_id=str(TENANT_ID),
        device_id=str(DEVICE_ID),
        agent_id=str(AGENT_ID),
        scopes=("session.observe",),
        legacy_seed=False,
    )

    await router.connector_connected(
        identity=identity,
        connection_id="91000000-0000-4000-8000-000000000001",
        connector_instance_id="92000000-0000-4000-8000-000000000001",
        runtime_generation="runtime-generation-1",
    )
    delivery = await router.wait_for_subscription_intent(
        identity=identity,
        connection_id="91000000-0000-4000-8000-000000000001",
        connector_instance_id="92000000-0000-4000-8000-000000000001",
        runtime_generation="runtime-generation-1",
    )
    reserved = await router.reserve_subscription_intent(
        identity=identity,
        connection_id="91000000-0000-4000-8000-000000000001",
        connector_instance_id="92000000-0000-4000-8000-000000000001",
        request_id=delivery.request_id,
        message_id=delivery.message_id,
        sequence=4,
        observer_contract=1,
        wire_message_type=delivery.message_type,
        wire_payload_digest=canonical_payload_digest(delivery.payload),
    )
    assert reserved.request_id == delivery.request_id
    with factory.begin() as session:
        stored = session.scalars(select(ObserverSubscriptionIntentModel)).one()
        assert stored.state == "dispatching"
        assert stored.settled_at is None

    await router.connector_heartbeat(
        identity=identity,
        connection_id="91000000-0000-4000-8000-000000000001",
        connector_instance_id="92000000-0000-4000-8000-000000000001",
        runtime_generation="runtime-generation-1",
        next_connector_sequence=1,
        next_cloud_sequence=5,
    )
    with factory.begin() as session:
        stored = session.scalars(select(ObserverSubscriptionIntentModel)).one()
        assert stored.state == "settled"
        assert stored.settled_at == NOW

    assert (
        router._next_subscription_intent(
            identity,
            "91000000-0000-4000-8000-000000000001",
            "92000000-0000-4000-8000-000000000001",
            "runtime-generation-1",
        )
        is None
    )

    await router.connector_disconnected(
        identity=identity,
        connection_id="91000000-0000-4000-8000-000000000001",
        connector_instance_id="92000000-0000-4000-8000-000000000001",
    )
    await router.connector_connected(
        identity=identity,
        connection_id="91000000-0000-4000-8000-000000000002",
        connector_instance_id="92000000-0000-4000-8000-000000000001",
        runtime_generation="runtime-generation-1",
    )
    replay = await router.wait_for_subscription_intent(
        identity=identity,
        connection_id="91000000-0000-4000-8000-000000000002",
        connector_instance_id="92000000-0000-4000-8000-000000000001",
        runtime_generation="runtime-generation-1",
    )
    assert replay.request_id == delivery.request_id
    assert replay.payload["subscription_id"] == delivery.payload["subscription_id"]
    reconciled = await router.reserve_subscription_intent(
        identity=identity,
        connection_id="91000000-0000-4000-8000-000000000002",
        connector_instance_id="92000000-0000-4000-8000-000000000001",
        request_id=replay.request_id,
        message_id=replay.message_id,
        sequence=9,
        observer_contract=1,
        wire_message_type=replay.message_type,
        wire_payload_digest=canonical_payload_digest(replay.payload),
    )
    assert reconciled.request_id != delivery.request_id
    assert reconciled.message_id == reconciled.request_id
    assert reconciled.payload["subscription_id"] == delivery.payload["subscription_id"]
    with factory.begin() as session:
        attempts = session.scalars(
            select(ObserverSubscriptionIntentModel).order_by(
                ObserverSubscriptionIntentModel.created_at,
                ObserverSubscriptionIntentModel.request_id,
            )
        ).all()
    assert len(attempts) == 2
    replacement = next(
        item for item in attempts if item.request_id == UUID(reconciled.request_id)
    )
    assert replacement.supersedes_request_id == UUID(delivery.request_id)
    assert replacement.dispatch_sequence == 9


@pytest.mark.parametrize(
    "message_type", ("session.observe.open", "session.observe.close")
)
@pytest.mark.parametrize("observer_contract", (1, 2))
@pytest.mark.asyncio
async def test_durable_intent_freezes_exact_wire_contract_across_reconnects(
    subscription_store,
    message_type: str,
    observer_contract: int,
) -> None:
    from hermes_cloud.platform.sqlalchemy.observer_subscription import (
        SqlAlchemyObserverSubscriptionRouter,
    )
    from hermes_cloud.platform.sqlalchemy.observer_subscription_models import (
        ObserverSubscriptionIntentModel,
    )

    _engine, factory = subscription_store
    router = SqlAlchemyObserverSubscriptionRouter(
        factory,
        now=lambda: NOW,
        poll_interval_seconds=0.001,
    )
    subscription = router.open_subscription(
        principal=_principal(), session_key="session-root-1", profile="default"
    )
    identity = ConnectorIdentity(
        tenant_id=str(TENANT_ID),
        device_id=str(DEVICE_ID),
        agent_id=str(AGENT_ID),
        scopes=("session.observe",),
        legacy_seed=False,
    )
    connector_instance_id = "92000000-0000-4000-8000-000000000001"
    first_connection_id = "91000000-0000-4000-8000-000000000011"
    second_connection_id = "91000000-0000-4000-8000-000000000012"
    third_connection_id = "91000000-0000-4000-8000-000000000013"

    await router.connector_connected(
        identity=identity,
        connection_id=first_connection_id,
        connector_instance_id=connector_instance_id,
        runtime_generation="runtime-generation-1",
    )
    open_delivery = await router.wait_for_subscription_intent(
        identity=identity,
        connection_id=first_connection_id,
        connector_instance_id=connector_instance_id,
        runtime_generation="runtime-generation-1",
    )
    if message_type == "session.observe.close":
        open_payload = dict(open_delivery.payload)
        await router.reserve_subscription_intent(
            identity=identity,
            connection_id=first_connection_id,
            connector_instance_id=connector_instance_id,
            request_id=open_delivery.request_id,
            message_id=open_delivery.message_id,
            sequence=4,
            observer_contract=1,
            wire_message_type="session.observe.open",
            wire_payload_digest=canonical_payload_digest(open_payload),
        )
        await router.connector_heartbeat(
            identity=identity,
            connection_id=first_connection_id,
            connector_instance_id=connector_instance_id,
            runtime_generation="runtime-generation-1",
            next_connector_sequence=1,
            next_cloud_sequence=5,
        )
        router.close_subscription(
            principal=_principal(),
            subscription_id=subscription.subscription_id,
            reason="client_unsubscribe",
        )
        delivery = await router.wait_for_subscription_intent(
            identity=identity,
            connection_id=first_connection_id,
            connector_instance_id=connector_instance_id,
            runtime_generation="runtime-generation-1",
        )
        sequence = 5
    else:
        delivery = open_delivery
        sequence = 4

    wire_message_type = (
        f"{delivery.message_type}.v2"
        if observer_contract == 2
        else delivery.message_type
    )
    wire_payload = {
        **dict(delivery.payload),
        **({"observer_contract": 2} if observer_contract == 2 else {}),
    }
    wire_payload_digest = canonical_payload_digest(wire_payload)
    first = await router.reserve_subscription_intent(
        identity=identity,
        connection_id=first_connection_id,
        connector_instance_id=connector_instance_id,
        request_id=delivery.request_id,
        message_id=delivery.message_id,
        sequence=sequence,
        observer_contract=observer_contract,
        wire_message_type=wire_message_type,
        wire_payload_digest=wire_payload_digest,
    )

    await router.connector_disconnected(
        identity=identity,
        connection_id=first_connection_id,
        connector_instance_id=connector_instance_id,
    )
    await router.connector_connected(
        identity=identity,
        connection_id=second_connection_id,
        connector_instance_id=connector_instance_id,
        runtime_generation="runtime-generation-1",
    )
    replay = await router.wait_for_subscription_intent(
        identity=identity,
        connection_id=second_connection_id,
        connector_instance_id=connector_instance_id,
        runtime_generation="runtime-generation-1",
    )
    exact = await router.reserve_subscription_intent(
        identity=identity,
        connection_id=second_connection_id,
        connector_instance_id=connector_instance_id,
        request_id=replay.request_id,
        message_id=replay.message_id,
        sequence=sequence,
        observer_contract=observer_contract,
        wire_message_type=wire_message_type,
        wire_payload_digest=wire_payload_digest,
    )
    assert exact.request_id == first.request_id

    await router.connector_disconnected(
        identity=identity,
        connection_id=second_connection_id,
        connector_instance_id=connector_instance_id,
    )
    await router.connector_connected(
        identity=identity,
        connection_id=third_connection_id,
        connector_instance_id=connector_instance_id,
        runtime_generation="runtime-generation-1",
    )
    changed_contract = 2 if observer_contract == 1 else 1
    changed_message_type = (
        f"{delivery.message_type}.v2"
        if changed_contract == 2
        else delivery.message_type
    )
    changed_payload = {
        **dict(delivery.payload),
        **({"observer_contract": 2} if changed_contract == 2 else {}),
    }
    with pytest.raises(RuntimeError, match="wire contract changed"):
        await router.reserve_subscription_intent(
            identity=identity,
            connection_id=third_connection_id,
            connector_instance_id=connector_instance_id,
            request_id=replay.request_id,
            message_id=replay.message_id,
            sequence=sequence,
            observer_contract=changed_contract,
            wire_message_type=changed_message_type,
            wire_payload_digest=canonical_payload_digest(changed_payload),
        )

    with factory.begin() as session:
        stored = session.get(
            ObserverSubscriptionIntentModel,
            (TENANT_ID, UUID(first.request_id)),
        )
        assert stored is not None
        assert stored.observer_contract == observer_contract
        assert stored.wire_message_type == wire_message_type
        assert stored.wire_payload_digest == wire_payload_digest
        assert stored.dispatch_connection_id == second_connection_id
        assert stored.dispatch_sequence == sequence
        assert stored.dispatch_attempts == 2


@pytest.mark.asyncio
async def test_gateway_reconnect_cannot_change_reserved_v1_open_into_v2(
    subscription_store,
) -> None:
    from hermes_cloud.entrypoints.connector_gateway import create_app
    from hermes_cloud.platform.sqlalchemy.observer_subscription import (
        SqlAlchemyObserverSubscriptionRouter,
    )

    _engine, factory = subscription_store
    router = SqlAlchemyObserverSubscriptionRouter(
        factory,
        now=lambda: NOW,
        poll_interval_seconds=0.001,
    )
    router.open_subscription(
        principal=_principal(), session_key="session-root-1", profile="default"
    )
    identity = ConnectorIdentity(
        tenant_id=str(TENANT_ID),
        device_id=str(DEVICE_ID),
        agent_id=str(AGENT_ID),
        scopes=("session.observe",),
        legacy_seed=False,
    )

    class Authenticator:
        async def authenticate(self, _token: str) -> ConnectorIdentity:
            return identity

        async def revalidate(self, _identity: ConnectorIdentity) -> None:
            return None

    class FrozenCursorAuthority:
        def __init__(self) -> None:
            self.commit_attempts: list[dict[str, object]] = []

        async def resolve(self, *_args: object, **_kwargs: object) -> object:
            return SimpleNamespace(
                decision="resumed",
                next_connector_sequence=0,
                next_cloud_sequence=3,
                handshake_disposition="advance",
            )

        async def prepare_session(self, **_binding: object) -> None:
            return None

        async def confirm_session(self, **_binding: object) -> None:
            return None

        async def abort_session(self, **_binding: object) -> None:
            return None

        async def commit_cursors(self, **binding: object) -> None:
            self.commit_attempts.append(dict(binding))
            raise RuntimeError("synthetic post-send pre-commit disconnect")

        async def disconnect_session(self, **_binding: object) -> None:
            return None

    authority = FrozenCursorAuthority()

    def hello(*, observer_contract: int) -> str:
        required = ["session.observe"]
        if observer_contract == 2:
            required.append("session.observe.output-parity.v1")
        return json.dumps(
            {
                "contract_version": 1,
                "message_id": str(uuid4()),
                "message_type": "connector.hello",
                "tenant_id": str(TENANT_ID),
                "device_id": str(DEVICE_ID),
                "sequence": 0,
                "sent_at": "2026-07-31T09:00:00Z",
                "payload": {
                    "connector_instance_id": ("92000000-0000-4000-8000-000000000001"),
                    "connector_version": "1.0.0",
                    "runtime_generation": "runtime-generation-1",
                    "required_capabilities": required,
                    "optional_capabilities": [],
                    "resume": {
                        "mode": "resume",
                        "previous_connection_id": (
                            "93000000-0000-4000-8000-000000000001"
                        ),
                        "next_outbound_sequence": 0,
                        "next_inbound_sequence": 3,
                    },
                },
            },
            separators=(",", ":"),
        )

    async def exchange(observer_contract: int) -> list[dict[str, Any]]:
        incoming: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        outgoing: list[dict[str, Any]] = []
        await incoming.put({"type": "websocket.connect"})
        await incoming.put(
            {
                "type": "websocket.receive",
                "text": hello(observer_contract=observer_contract),
            }
        )

        async def receive() -> dict[str, Any]:
            return await incoming.get()

        async def send(message: dict[str, Any]) -> None:
            outgoing.append(message)

        try:
            await app(
                {
                    "type": "websocket",
                    "path": "/api/ws",
                    "headers": [(b"authorization", b"Bearer test-token")],
                    "subprotocols": ["hermes.connector.v1"],
                },
                receive,
                send,
            )
        except RuntimeError:
            pass
        return outgoing

    app = create_app(
        authenticator=Authenticator(),
        transport_cursor_authority=authority,
        observer_subscription_router=router,
        available_capabilities=(
            "session.observe",
            "session.observe.output-parity.v1",
        ),
    )
    await app.startup()
    try:
        first = await asyncio.wait_for(exchange(1), timeout=3)
        second = await asyncio.wait_for(exchange(2), timeout=3)
    finally:
        await app.shutdown()

    first_frames = [
        json.loads(message["text"])
        for message in first
        if message["type"] == "websocket.send"
    ]
    second_frames = [
        json.loads(message["text"])
        for message in second
        if message["type"] == "websocket.send"
    ]
    assert any(
        frame["message_type"] == "session.observe.open" and frame["sequence"] == 4
        for frame in first_frames
    ), first
    assert all(
        frame["message_type"] != "session.observe.open.v2" for frame in second_frames
    )
    assert len(authority.commit_attempts) == 1
    assert authority.commit_attempts[0]["next_cloud_sequence"] == 5


@pytest.mark.asyncio
async def test_replayed_open_cannot_be_reserved_after_last_lease_closes(
    subscription_store,
) -> None:
    from hermes_cloud.platform.sqlalchemy.observer_subscription import (
        SqlAlchemyObserverSubscriptionRouter,
    )
    from hermes_cloud.platform.sqlalchemy.observer_subscription_models import (
        ObserverSubscriptionIntentModel,
        ObserverSubscriptionTargetModel,
    )

    _engine, factory = subscription_store
    router = SqlAlchemyObserverSubscriptionRouter(
        factory,
        now=lambda: NOW,
        poll_interval_seconds=0.001,
    )
    subscription = router.open_subscription(
        principal=_principal(), session_key="session-root-1", profile="default"
    )
    identity = ConnectorIdentity(
        tenant_id=str(TENANT_ID),
        device_id=str(DEVICE_ID),
        agent_id=str(AGENT_ID),
        scopes=("session.observe",),
        legacy_seed=False,
    )
    first_connection = "91000000-0000-4000-8000-000000000001"
    second_connection = "91000000-0000-4000-8000-000000000002"
    third_connection = "91000000-0000-4000-8000-000000000003"
    connector_instance = "92000000-0000-4000-8000-000000000001"
    await router.connector_connected(
        identity=identity,
        connection_id=first_connection,
        connector_instance_id=connector_instance,
        runtime_generation="runtime-generation-1",
    )
    delivery = await router.wait_for_subscription_intent(
        identity=identity,
        connection_id=first_connection,
        connector_instance_id=connector_instance,
        runtime_generation="runtime-generation-1",
    )
    await router.reserve_subscription_intent(
        identity=identity,
        connection_id=first_connection,
        connector_instance_id=connector_instance,
        request_id=delivery.request_id,
        message_id=delivery.message_id,
        sequence=4,
        observer_contract=1,
        wire_message_type=delivery.message_type,
        wire_payload_digest=canonical_payload_digest(delivery.payload),
    )
    await router.connector_disconnected(
        identity=identity,
        connection_id=first_connection,
        connector_instance_id=connector_instance,
    )
    await router.connector_connected(
        identity=identity,
        connection_id=second_connection,
        connector_instance_id=connector_instance,
        runtime_generation="runtime-generation-1",
    )
    same_sequence_replay = await router.wait_for_subscription_intent(
        identity=identity,
        connection_id=second_connection,
        connector_instance_id=connector_instance,
        runtime_generation="runtime-generation-1",
    )
    same_sequence_reservation = await router.reserve_subscription_intent(
        identity=identity,
        connection_id=second_connection,
        connector_instance_id=connector_instance,
        request_id=same_sequence_replay.request_id,
        message_id=same_sequence_replay.message_id,
        sequence=4,
        observer_contract=1,
        wire_message_type=same_sequence_replay.message_type,
        wire_payload_digest=canonical_payload_digest(same_sequence_replay.payload),
    )
    assert same_sequence_reservation.request_id == delivery.request_id
    with factory.begin() as session:
        same_intent = session.scalars(select(ObserverSubscriptionIntentModel)).one()
    assert same_intent.state == "dispatching"
    assert same_intent.dispatch_connection_id == second_connection
    assert same_intent.dispatch_sequence == 4
    assert same_intent.dispatch_attempts == 2

    await router.connector_heartbeat(
        identity=identity,
        connection_id=second_connection,
        connector_instance_id=connector_instance,
        runtime_generation="runtime-generation-1",
        next_connector_sequence=1,
        next_cloud_sequence=5,
    )
    await router.connector_disconnected(
        identity=identity,
        connection_id=second_connection,
        connector_instance_id=connector_instance,
    )
    await router.connector_connected(
        identity=identity,
        connection_id=third_connection,
        connector_instance_id=connector_instance,
        runtime_generation="runtime-generation-1",
    )
    stale_open = await router.wait_for_subscription_intent(
        identity=identity,
        connection_id=third_connection,
        connector_instance_id=connector_instance,
        runtime_generation="runtime-generation-1",
    )
    assert stale_open.request_id == delivery.request_id

    router.close_subscription(
        principal=_principal(),
        subscription_id=subscription.subscription_id,
        reason="client_unsubscribe",
    )

    with pytest.raises(RuntimeError, match="reservation target changed"):
        await router.reserve_subscription_intent(
            identity=identity,
            connection_id=third_connection,
            connector_instance_id=connector_instance,
            request_id=stale_open.request_id,
            message_id=stale_open.message_id,
            sequence=9,
            observer_contract=1,
            wire_message_type=stale_open.message_type,
            wire_payload_digest=canonical_payload_digest(stale_open.payload),
        )
    with factory.begin() as session:
        target = session.scalars(select(ObserverSubscriptionTargetModel)).one()
        intents = session.scalars(
            select(ObserverSubscriptionIntentModel).order_by(
                ObserverSubscriptionIntentModel.intent_sequence
            )
        ).all()
    assert target.state == "closing"
    assert target.active_ref_count == 0
    assert [intent.message_type for intent in intents] == [
        "session.observe.open",
        "session.observe.close",
    ]


@pytest.mark.asyncio
async def test_dispatch_query_bounds_and_filters_settled_intents_in_sql(
    subscription_store,
) -> None:
    from hermes_cloud.platform.sqlalchemy.observer_subscription import (
        SqlAlchemyObserverSubscriptionRouter,
    )
    from hermes_cloud.platform.sqlalchemy.observer_subscription_models import (
        ObserverSubscriptionIntentModel,
        ObserverSubscriptionTargetModel,
    )

    engine, factory = subscription_store
    router = SqlAlchemyObserverSubscriptionRouter(factory, now=lambda: NOW)
    router.open_subscription(
        principal=_principal(), session_key="session-root-1", profile="default"
    )
    identity = ConnectorIdentity(
        tenant_id=str(TENANT_ID),
        device_id=str(DEVICE_ID),
        agent_id=str(AGENT_ID),
        scopes=("session.observe",),
        legacy_seed=False,
    )
    connection_id = "91000000-0000-4000-8000-000000000003"
    connector_instance_id = "92000000-0000-4000-8000-000000000001"
    await router.connector_connected(
        identity=identity,
        connection_id=connection_id,
        connector_instance_id=connector_instance_id,
        runtime_generation="runtime-generation-1",
    )

    inactive_target_id = UUID("85000000-0000-4000-8000-000000000001")
    invalid_created_at = NOW - timedelta(minutes=1)
    with factory.begin() as session:
        active_target = session.scalars(select(ObserverSubscriptionTargetModel)).one()
        pending = session.scalars(select(ObserverSubscriptionIntentModel)).one()
        session.add(
            ObserverSubscriptionTargetModel(
                tenant_id=TENANT_ID,
                target_subscription_id=inactive_target_id,
                workspace_id=WORKSPACE_ID,
                agent_id=AGENT_ID,
                device_id=DEVICE_ID,
                profile="inactive-profile",
                session_key="inactive-session",
                state="closed",
                active_ref_count=0,
                next_intent_sequence=128,
                revision=1,
                created_at=invalid_created_at,
                updated_at=invalid_created_at,
            )
        )
        session.flush()
        invalid_intents = []
        for index in range(128):
            inactive_request_id = UUID(f"83000000-0000-4000-8000-{index + 1:012d}")
            close_request_id = UUID(f"84000000-0000-4000-8000-{index + 1:012d}")
            invalid_intents.extend(
                (
                    ObserverSubscriptionIntentModel(
                        tenant_id=TENANT_ID,
                        request_id=inactive_request_id,
                        supersedes_request_id=None,
                        target_subscription_id=inactive_target_id,
                        intent_sequence=index,
                        workspace_id=WORKSPACE_ID,
                        agent_id=AGENT_ID,
                        device_id=DEVICE_ID,
                        message_type="session.observe.open",
                        payload={"request_id": str(inactive_request_id)},
                        state="settled",
                        dispatch_connection_id=("91000000-0000-4000-8000-000000000001"),
                        dispatch_sequence=index,
                        dispatch_attempts=1,
                        dispatched_at=invalid_created_at,
                        settled_at=invalid_created_at,
                        created_at=invalid_created_at,
                        updated_at=invalid_created_at,
                    ),
                    ObserverSubscriptionIntentModel(
                        tenant_id=TENANT_ID,
                        request_id=close_request_id,
                        supersedes_request_id=None,
                        target_subscription_id=active_target.target_subscription_id,
                        intent_sequence=index + 1,
                        workspace_id=WORKSPACE_ID,
                        agent_id=AGENT_ID,
                        device_id=DEVICE_ID,
                        message_type="session.observe.close",
                        payload={"request_id": str(close_request_id)},
                        state="settled",
                        dispatch_connection_id=("91000000-0000-4000-8000-000000000001"),
                        dispatch_sequence=index,
                        dispatch_attempts=1,
                        dispatched_at=invalid_created_at,
                        settled_at=invalid_created_at,
                        created_at=invalid_created_at,
                        updated_at=invalid_created_at,
                    ),
                )
            )
        session.add_all(invalid_intents)

    intent_queries: list[tuple[str, tuple[object, ...]]] = []
    loaded_request_ids: list[UUID] = []

    def capture_intent_query(
        _connection,
        _cursor,
        statement,
        parameters,
        _context,
        _executemany,
    ) -> None:
        if (
            statement.lstrip().upper().startswith("SELECT")
            and "observer_subscription_intents" in statement
        ):
            intent_queries.append((statement, tuple(parameters)))

    def capture_intent_load(intent, _context) -> None:
        loaded_request_ids.append(intent.request_id)

    event.listen(engine, "before_cursor_execute", capture_intent_query)
    event.listen(ObserverSubscriptionIntentModel, "load", capture_intent_load)
    try:
        delivery = router._next_subscription_intent(
            identity,
            connection_id,
            connector_instance_id,
            "runtime-generation-1",
        )
    finally:
        event.remove(engine, "before_cursor_execute", capture_intent_query)
        event.remove(ObserverSubscriptionIntentModel, "load", capture_intent_load)

    assert delivery is not None
    assert delivery.request_id == str(pending.request_id)
    assert loaded_request_ids == [pending.request_id]
    assert len(intent_queries) == 1
    statement, parameters = intent_queries[0]
    normalized = " ".join(statement.lower().split())
    assert "exists" in normalized
    assert "observer_subscription_targets.state" in normalized
    assert "observer_subscription_targets.active_ref_count" in normalized
    assert "order by" in normalized
    assert " limit " in normalized
    ordering = normalized.split(" order by ", maxsplit=1)[1].split(
        " limit ", maxsplit=1
    )[0]
    assert ordering.index(".created_at") < ordering.index(".intent_sequence")
    assert ordering.index(".intent_sequence") < ordering.index(".request_id")
    assert {"pending", "dispatching", "settled"}.issubset(set(parameters))
    assert connection_id in parameters
    assert 1 in parameters


def test_active_target_limit_rejects_new_target_not_shared_ref(
    subscription_store,
) -> None:
    from hermes_cloud.platform.sqlalchemy.observer_subscription import (
        ObserverSubscriptionCapacityExceeded,
        SqlAlchemyObserverSubscriptionRouter,
    )

    _engine, factory = subscription_store
    with factory.begin() as session:
        session.add(
            SessionProjectionModel(
                tenant_id=TENANT_ID,
                session_id=UUID("70000000-0000-4000-8000-000000000002"),
                session_key="session-root-2",
                workspace_id=WORKSPACE_ID,
                agent_id=AGENT_ID,
                profile="default",
                title="Second Hermes Session",
                state="active",
                revision=1,
                lineage_tip_message_id=None,
                lineage_tip_sequence=0,
                started_at=NOW,
                updated_at=NOW,
                closed_at=None,
                retention_until=datetime(2026, 8, 30, 9, 0, tzinfo=UTC),
            )
        )
    router = SqlAlchemyObserverSubscriptionRouter(
        factory,
        now=lambda: NOW,
        max_active_targets_per_connector=1,
    )

    first = router.open_subscription(
        principal=_principal(), session_key="session-root-1", profile="default"
    )
    shared = router.open_subscription(
        principal=_principal(), session_key="session-root-1", profile="default"
    )
    assert shared.target_subscription_id == first.target_subscription_id

    with pytest.raises(ObserverSubscriptionCapacityExceeded):
        router.open_subscription(
            principal=_principal(), session_key="session-root-2", profile="default"
        )


def test_closed_target_reopen_obeys_capacity_and_close_releases_it(
    subscription_store,
) -> None:
    from hermes_cloud.platform.sqlalchemy.observer_subscription import (
        ObserverSubscriptionCapacityExceeded,
        SqlAlchemyObserverSubscriptionRouter,
    )

    _engine, factory = subscription_store
    with factory.begin() as session:
        session.add(
            SessionProjectionModel(
                tenant_id=TENANT_ID,
                session_id=UUID("70000000-0000-4000-8000-000000000002"),
                session_key="session-root-2",
                workspace_id=WORKSPACE_ID,
                agent_id=AGENT_ID,
                profile="default",
                title="Second Hermes Session",
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
    router = SqlAlchemyObserverSubscriptionRouter(
        factory,
        now=lambda: NOW,
        max_active_targets_per_connector=1,
    )
    historical = router.open_subscription(
        principal=_principal(), session_key="session-root-1", profile="default"
    )
    router.close_subscription(
        principal=_principal(),
        subscription_id=historical.subscription_id,
        reason="client_unsubscribe",
    )
    occupying = router.open_subscription(
        principal=_principal(), session_key="session-root-2", profile="default"
    )

    with pytest.raises(ObserverSubscriptionCapacityExceeded):
        router.open_subscription(
            principal=_principal(), session_key="session-root-1", profile="default"
        )

    router.close_subscription(
        principal=_principal(),
        subscription_id=occupying.subscription_id,
        reason="client_unsubscribe",
    )
    reopened = router.open_subscription(
        principal=_principal(), session_key="session-root-1", profile="default"
    )
    assert reopened.target_subscription_id == historical.target_subscription_id


def test_concurrent_reopen_and_new_target_never_exceed_capacity(
    subscription_store,
) -> None:
    from hermes_cloud.platform.sqlalchemy.observer_subscription import (
        ObserverSubscriptionCapacityExceeded,
        SqlAlchemyObserverSubscriptionRouter,
    )
    from hermes_cloud.platform.sqlalchemy.observer_subscription_models import (
        ObserverSubscriptionTargetModel,
    )

    _engine, factory = subscription_store
    with factory.begin() as session:
        session.add(
            SessionProjectionModel(
                tenant_id=TENANT_ID,
                session_id=UUID("70000000-0000-4000-8000-000000000002"),
                session_key="session-root-2",
                workspace_id=WORKSPACE_ID,
                agent_id=AGENT_ID,
                profile="default",
                title="Second Hermes Session",
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
    setup = SqlAlchemyObserverSubscriptionRouter(
        factory, now=lambda: NOW, max_active_targets_per_connector=1
    )
    historical = setup.open_subscription(
        principal=_principal(), session_key="session-root-1", profile="default"
    )
    setup.close_subscription(
        principal=_principal(),
        subscription_id=historical.subscription_id,
        reason="client_unsubscribe",
    )

    barrier = Barrier(2)
    calls = local()

    def synchronized_id() -> UUID:
        count = getattr(calls, "count", 0)
        calls.count = count + 1
        if count == 0:
            barrier.wait(timeout=2)
        return uuid4()

    routers = (
        SqlAlchemyObserverSubscriptionRouter(
            factory,
            now=lambda: NOW,
            id_factory=synchronized_id,
            max_active_targets_per_connector=1,
        ),
        SqlAlchemyObserverSubscriptionRouter(
            factory,
            now=lambda: NOW,
            id_factory=synchronized_id,
            max_active_targets_per_connector=1,
        ),
    )

    def open_target(arguments):
        router, session_key = arguments
        try:
            return router.open_subscription(
                principal=_principal(), session_key=session_key, profile="default"
            )
        except ObserverSubscriptionCapacityExceeded:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = tuple(
            executor.map(
                open_target,
                zip(routers, ("session-root-1", "session-root-2"), strict=True),
            )
        )

    assert sum(outcome is not None for outcome in outcomes) == 1
    with factory.begin() as session:
        active = session.scalars(
            select(ObserverSubscriptionTargetModel).where(
                ObserverSubscriptionTargetModel.state == "active",
                ObserverSubscriptionTargetModel.active_ref_count > 0,
            )
        ).all()
    assert len(active) == 1


def test_connector_capacity_locks_reclaim_churn_and_exception_paths() -> None:
    from hermes_cloud.platform.sqlalchemy import observer_subscription

    lock_registry = getattr(
        observer_subscription,
        "_CONNECTOR_CAPACITY_LOCKS",
        None,
    )
    connector_lock = getattr(
        observer_subscription,
        "_connector_capacity_lock",
        None,
    )
    assert lock_registry is not None
    assert connector_lock is not None
    assert lock_registry == {}

    for index in range(256):
        tenant_id = UUID(int=index + 1)
        device_id = UUID(int=index + 1000)
        with connector_lock(tenant_id, device_id):
            pass

    with (
        pytest.raises(RuntimeError, match="synthetic lock failure"),
        connector_lock(UUID(int=9999), UUID(int=10000)),
    ):
        raise RuntimeError("synthetic lock failure")

    assert lock_registry == {}


def test_connector_capacity_locks_do_not_serialize_distinct_devices() -> None:
    from hermes_cloud.platform.sqlalchemy import observer_subscription

    lock_registry = observer_subscription._CONNECTOR_CAPACITY_LOCKS
    connector_lock = observer_subscription._connector_capacity_lock
    first_entered = Event()
    release_first = Event()
    second_entered = Event()

    def hold_first_device() -> None:
        with connector_lock(TENANT_ID, DEVICE_ID):
            first_entered.set()
            assert release_first.wait(timeout=2)

    def enter_second_device() -> None:
        assert first_entered.wait(timeout=2)
        with connector_lock(TENANT_ID, UUID(int=DEVICE_ID.int + 1)):
            second_entered.set()

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(hold_first_device)
        second = executor.submit(enter_second_device)
        try:
            assert second_entered.wait(timeout=2)
        finally:
            release_first.set()
        first.result(timeout=2)
        second.result(timeout=2)

    assert lock_registry == {}


def test_concurrent_shared_refs_do_not_lose_count_or_duplicate_open(
    subscription_store,
) -> None:
    from hermes_cloud.platform.sqlalchemy.observer_subscription import (
        SqlAlchemyObserverSubscriptionRouter,
    )
    from hermes_cloud.platform.sqlalchemy.observer_subscription_models import (
        ObserverSubscriptionIntentModel,
        ObserverSubscriptionTargetModel,
    )

    _engine, factory = subscription_store
    barrier = Barrier(2)
    calls = local()

    def synchronized_id() -> UUID:
        count = getattr(calls, "count", 0)
        calls.count = count + 1
        if count == 0:
            barrier.wait(timeout=2)
        return uuid4()

    routers = (
        SqlAlchemyObserverSubscriptionRouter(
            factory, now=lambda: NOW, id_factory=synchronized_id
        ),
        SqlAlchemyObserverSubscriptionRouter(
            factory, now=lambda: NOW, id_factory=synchronized_id
        ),
    )

    def open_ref(router: SqlAlchemyObserverSubscriptionRouter):
        return router.open_subscription(
            principal=_principal(), session_key="session-root-1", profile="default"
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        handles = tuple(executor.map(open_ref, routers))

    assert handles[0].target_subscription_id == handles[1].target_subscription_id
    with factory.begin() as session:
        target = session.scalars(select(ObserverSubscriptionTargetModel)).one()
        intents = session.scalars(select(ObserverSubscriptionIntentModel)).all()
    assert target.active_ref_count == 2
    assert len(intents) == 1


def test_lease_renewal_prevents_early_close_and_sweeper_closes_last_ref(
    subscription_store,
) -> None:
    from hermes_cloud.platform.sqlalchemy.observer_subscription import (
        SqlAlchemyObserverSubscriptionRouter,
    )
    from hermes_cloud.platform.sqlalchemy.observer_subscription_models import (
        ObserverSubscriptionIntentModel,
        ObserverSubscriptionTargetModel,
    )

    _engine, factory = subscription_store
    clock = [NOW]
    router = SqlAlchemyObserverSubscriptionRouter(
        factory,
        now=lambda: clock[0],
        lease_ttl_seconds=30,
    )
    renewed = router.open_subscription(
        principal=_principal(), session_key="session-root-1", profile="default"
    )
    router.open_subscription(
        principal=_principal(), session_key="session-root-1", profile="default"
    )
    clock[0] = NOW + timedelta(seconds=20)
    router.renew_subscription(
        principal=_principal(), subscription_id=renewed.subscription_id
    )
    clock[0] = NOW + timedelta(seconds=31)
    router.expire_stale_leases()
    with factory.begin() as session:
        target = session.scalars(select(ObserverSubscriptionTargetModel)).one()
        intents = session.scalars(select(ObserverSubscriptionIntentModel)).all()
    assert target.active_ref_count == 1
    assert [intent.message_type for intent in intents] == ["session.observe.open"]

    clock[0] = NOW + timedelta(seconds=51)
    router.expire_stale_leases()
    with factory.begin() as session:
        target = session.scalars(select(ObserverSubscriptionTargetModel)).one()
        intents = session.scalars(select(ObserverSubscriptionIntentModel)).all()
    assert target.active_ref_count == 0
    assert target.state == "closing"
    assert {intent.message_type for intent in intents} == {
        "session.observe.open",
        "session.observe.close",
    }


def test_concurrent_lease_renew_wins_before_stale_sweep_mutation(
    subscription_store,
) -> None:
    from hermes_cloud.platform.sqlalchemy.observer_subscription import (
        SqlAlchemyObserverSubscriptionRouter,
    )
    from hermes_cloud.platform.sqlalchemy.observer_subscription_models import (
        ObserverSubscriptionIntentModel,
        ObserverSubscriptionLeaseModel,
        ObserverSubscriptionTargetModel,
    )

    engine, factory = subscription_store
    opened = SqlAlchemyObserverSubscriptionRouter(
        factory,
        now=lambda: NOW,
        lease_ttl_seconds=30,
    ).open_subscription(
        principal=_principal(),
        session_key="session-root-1",
        profile="default",
    )
    race_now = NOW + timedelta(seconds=31)
    sweeper = SqlAlchemyObserverSubscriptionRouter(
        factory,
        now=lambda: race_now,
        lease_ttl_seconds=30,
    )
    renewer = SqlAlchemyObserverSubscriptionRouter(
        factory,
        now=lambda: race_now,
        lease_ttl_seconds=30,
    )
    sweep_thread = local()
    stale_scan_ready = Event()
    allow_sweep_mutation = Event()

    def pause_sweep_before_lease_mutation(
        _connection,
        _cursor,
        statement: str,
        _parameters,
        _context,
        _executemany,
    ) -> None:
        if (
            getattr(sweep_thread, "active", False)
            and not stale_scan_ready.is_set()
            and statement.lstrip().upper().startswith("UPDATE")
            and "observer_subscription_leases" in statement
        ):
            stale_scan_ready.set()
            assert allow_sweep_mutation.wait(timeout=2)

    event.listen(engine, "before_cursor_execute", pause_sweep_before_lease_mutation)

    def run_sweep() -> None:
        sweep_thread.active = True
        sweeper.expire_stale_leases()

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            sweep = executor.submit(run_sweep)
            assert stale_scan_ready.wait(timeout=2)
            try:
                renewer.renew_subscription(
                    principal=_principal(),
                    subscription_id=opened.subscription_id,
                )
            finally:
                allow_sweep_mutation.set()
            sweep.result(timeout=2)
    finally:
        allow_sweep_mutation.set()
        event.remove(
            engine,
            "before_cursor_execute",
            pause_sweep_before_lease_mutation,
        )

    with factory.begin() as session:
        lease = session.get(
            ObserverSubscriptionLeaseModel,
            (TENANT_ID, opened.subscription_id),
        )
        target = session.scalars(select(ObserverSubscriptionTargetModel)).one()
        intents = session.scalars(select(ObserverSubscriptionIntentModel)).all()
    assert lease is not None
    assert lease.state == "active"
    assert lease.expires_at == race_now + timedelta(seconds=30)
    assert target.state == "active"
    assert target.active_ref_count == 1
    assert [intent.message_type for intent in intents] == ["session.observe.open"]
