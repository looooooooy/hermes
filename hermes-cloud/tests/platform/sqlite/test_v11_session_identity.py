from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4, uuid5

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import func, inspect, select
from sqlalchemy.orm import Session

from hermes_cloud.platform.postgres.models import (
    AgentModel,
    RefreshSessionModel,
    RoleModel,
    SessionEventProjectionModel,
    SessionMessageProjectionModel,
    SessionProjectionCursorModel,
    SessionProjectionModel,
    TenantModel,
    UserModel,
    WebSocketTicketModel,
    WorkspaceMembershipModel,
    WorkspaceModel,
)
from hermes_cloud.platform.sqlalchemy.observer_projection_models import (
    ObserverSessionModel,
)
from hermes_cloud.platform.sqlalchemy.session_projection_migration_models import (
    SessionEventProjectionV10Model,
    SessionMessageProjectionV10Model,
    SessionProjectionCursorV10Model,
    SessionProjectionV10Model,
    WebSocketTicketV10Model,
)
from hermes_cloud.platform.sqlite.engine import build_sqlite_engine
from hermes_cloud.platform.sqlite.migrations import (
    PUBLISHED_SQLITE_MIGRATIONS,
    SQLiteMigrationHistoryConflict,
    SQLiteSchemaMigration,
    sqlite_schema_fingerprint,
    upgrade_sqlite_schema,
)

NOW = datetime(2026, 7, 31, tzinfo=UTC)
RETENTION = datetime(2027, 7, 31, tzinfo=UTC)
NAMESPACE = UUID("ba84c827-b174-47f8-bbbd-52cbaf7232b9")


def _stable_id(kind: str, *parts: str) -> UUID:
    return uuid5(NAMESPACE, "\x1f".join((kind, *parts)))


def _engine(tmp_path: Path, name: str):
    return build_sqlite_engine(
        f"sqlite+pysqlite:///{tmp_path / name}",
        allow_missing=True,
    )


def _apply_through_v10(engine: object) -> None:
    with engine.begin() as connection:  # type: ignore[union-attr]
        operations = Operations(MigrationContext.configure(connection))
        SQLiteSchemaMigration.__table__.create(connection)
        for migration in PUBLISHED_SQLITE_MIGRATIONS[:10]:
            migration.upgrade(operations)
    with Session(engine) as session, session.begin():
        session.add_all(
            SQLiteSchemaMigration(
                version=migration.version,
                name=migration.name,
                checksum=migration.checksum,
                applied_at=NOW,
            )
            for migration in PUBLISHED_SQLITE_MIGRATIONS[:10]
        )


def _seed_v10_identity(
    engine: object,
    *,
    exact_seed: bool,
    authoritative_profile: bool = False,
) -> dict[str, UUID]:
    tenant_slug = "migration"
    workspace_key = "workspace"
    tenant_id = _stable_id("tenant", tenant_slug)
    user_id = _stable_id("user", tenant_slug, "user")
    role_id = _stable_id("role", tenant_slug, workspace_key, "test-user")
    workspace_id = _stable_id("workspace", tenant_slug, workspace_key)
    membership_id = _stable_id(
        "workspace-membership",
        str(tenant_id),
        str(workspace_id),
        str(user_id),
        str(role_id),
    )
    session_id = (
        _stable_id("session", tenant_slug, workspace_key, "android-bootstrap")
        if exact_seed
        else uuid4()
    )
    session_key = "android-bootstrap" if exact_seed else "unproven-session"
    message_id = (
        _stable_id("session-message", str(session_id), "1")
        if exact_seed
        else uuid4()
    )
    refresh_session_id = uuid4()
    control_ticket_id = uuid4()
    observer_ticket_id = uuid4()
    agent_id = uuid4() if authoritative_profile else None
    profile = "work" if authoritative_profile else "default"
    with Session(engine) as session, session.begin():
        session.add(
            TenantModel(
                tenant_id=tenant_id,
                slug=tenant_slug,
                display_name="Migration",
                status="active",
                created_at=NOW,
            )
        )
        session.flush()
        if agent_id is not None:
            session.add(
                AgentModel(
                    tenant_id=tenant_id,
                    agent_id=agent_id,
                    workspace_id=workspace_id,
                    agent_key="observed-agent",
                    status="active",
                    last_seen_at=NOW,
                    created_at=NOW,
                )
            )
            session.flush()
        session.add(
            UserModel(
                tenant_id=tenant_id,
                user_id=user_id,
                subject="user",
                display_name="User",
                email=None,
                status="active",
                created_at=NOW,
            )
        )
        session.add(
            RoleModel(
                tenant_id=tenant_id,
                role_id=role_id,
                role_key="test-user",
                display_name="Test user",
                scope_type="workspace",
                permissions=[],
                status="active",
                version=1,
                created_at=NOW,
            )
        )
        session.flush()
        session.add(
            WorkspaceModel(
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                workspace_key=workspace_key,
                display_name="Workspace",
                status="active",
                created_by=user_id,
                created_at=NOW,
            )
        )
        session.flush()
        session.add(
            WorkspaceMembershipModel(
                tenant_id=tenant_id,
                workspace_membership_id=membership_id,
                workspace_id=workspace_id,
                user_id=user_id,
                role_id=role_id,
                status="active",
                joined_at=NOW,
                revoked_at=None,
            )
        )
        session.add(
            RefreshSessionModel(
                tenant_id=tenant_id,
                refresh_session_id=refresh_session_id,
                user_id=user_id,
                token_digest="a" * 64,
                rotation=0,
                created_at=NOW,
                rotated_at=None,
                revoked_at=None,
                expires_at=NOW + timedelta(hours=1),
                retention_until=NOW + timedelta(days=1),
            )
        )
        session.flush()
        session.add(
            SessionProjectionV10Model(
                tenant_id=tenant_id,
                session_id=session_id,
                session_key=session_key,
                workspace_id=workspace_id,
                agent_id=agent_id,
                title=(
                    "Hermes Cloud test session" if exact_seed else "Unproven session"
                ),
                state="active",
                revision=1,
                lineage_tip_message_id=message_id,
                lineage_tip_sequence=1,
                started_at=NOW,
                updated_at=NOW,
                closed_at=None,
                retention_until=RETENTION,
            )
        )
        session.flush()
        if agent_id is not None:
            session.add(
                ObserverSessionModel(
                    tenant_id=tenant_id,
                    session_id=uuid4(),
                    workspace_id=workspace_id,
                    agent_id=agent_id,
                    device_id=uuid4(),
                    profile=profile,
                    session_key=session_key,
                    runtime_session_id="runtime-observed-session",
                    runtime_generation="generation-1",
                    connector_instance_id=str(uuid4()),
                    connection_id=str(uuid4()),
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
                    retention_until=RETENTION,
                )
            )
        session.add_all(
            (
                SessionMessageProjectionV10Model(
                    tenant_id=tenant_id,
                    session_id=session_id,
                    message_id=message_id,
                    sequence=1,
                    role="assistant",
                    content={"text": "preserved"},
                    parent_message_id=None,
                    created_at=NOW,
                    retention_until=RETENTION,
                ),
                SessionEventProjectionV10Model(
                    tenant_id=tenant_id,
                    session_id=session_id,
                    event_id=uuid4(),
                    sequence=1,
                    event_type="message.complete",
                    payload={"message_id": str(message_id)},
                    occurred_at=NOW,
                    retention_until=RETENTION,
                ),
                SessionProjectionCursorV10Model(
                    tenant_id=tenant_id,
                    session_id=session_id,
                    stream="events",
                    last_sequence=1,
                    updated_at=NOW,
                ),
                WebSocketTicketV10Model(
                    tenant_id=tenant_id,
                    ticket_id=control_ticket_id,
                    ticket_digest="b" * 64,
                    principal_type="user",
                    principal_id=user_id,
                    refresh_session_id=refresh_session_id,
                    session_key=session_key,
                    observer_scope=[
                        "session.control",
                        f"profile={profile}",
                        f"agent_id={agent_id}",
                    ],
                    issued_at=NOW,
                    expires_at=NOW + timedelta(minutes=1),
                    consumed_at=None,
                    retention_until=NOW + timedelta(days=1),
                ),
                WebSocketTicketV10Model(
                    tenant_id=tenant_id,
                    ticket_id=observer_ticket_id,
                    ticket_digest="c" * 64,
                    principal_type="user",
                    principal_id=user_id,
                    refresh_session_id=refresh_session_id,
                    session_key=None,
                    observer_scope=["session.observe"],
                    issued_at=NOW,
                    expires_at=NOW + timedelta(minutes=1),
                    consumed_at=None,
                    retention_until=NOW + timedelta(days=1),
                ),
            )
        )
    return {
        "tenant_id": tenant_id,
        "session_id": session_id,
        "message_id": message_id,
        "control_ticket_id": control_ticket_id,
        "observer_ticket_id": observer_ticket_id,
        "agent_id": agent_id,
    }


def test_v10_to_v11_uses_authoritative_observer_profile_and_preserves_children(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path, "preserve.sqlite3")
    _apply_through_v10(engine)
    identity = _seed_v10_identity(
        engine,
        exact_seed=False,
        authoritative_profile=True,
    )

    assert upgrade_sqlite_schema(engine).schema_version == 13

    with Session(engine) as session:
        stored = session.get(
            SessionProjectionModel,
            (identity["tenant_id"], identity["session_id"]),
        )
        assert stored is not None
        assert stored.profile == "work"
        assert stored.agent_id == identity["agent_id"]
        assert session.scalar(select(func.count()).select_from(SessionMessageProjectionModel)) == 1
        assert session.scalar(select(func.count()).select_from(SessionEventProjectionModel)) == 1
        assert session.scalar(select(func.count()).select_from(SessionProjectionCursorModel)) == 1
        control = session.get(
            WebSocketTicketModel,
            (identity["tenant_id"], identity["control_ticket_id"]),
        )
        observer = session.get(
            WebSocketTicketModel,
            (identity["tenant_id"], identity["observer_ticket_id"]),
        )
        assert control is not None and control.session_id == identity["session_id"]
        assert observer is not None and observer.session_id is None
    engine.dispose()


def test_v11_rejects_named_test_seed_without_authoritative_profile_evidence(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path, "named-test-seed-rejected.sqlite3")
    _apply_through_v10(engine)
    _seed_v10_identity(engine, exact_seed=True)
    before = sqlite_schema_fingerprint(engine)

    with pytest.raises(SQLiteMigrationHistoryConflict, match="Agent identity"):
        upgrade_sqlite_schema(engine)

    assert sqlite_schema_fingerprint(engine) == before
    with Session(engine) as session:
        assert session.scalar(select(func.max(SQLiteSchemaMigration.version))) == 10
    engine.dispose()


def test_v11_rejects_unproven_profile_and_rolls_back_schema_and_ledger(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path, "rollback.sqlite3")
    _apply_through_v10(engine)
    _seed_v10_identity(engine, exact_seed=False)
    before = sqlite_schema_fingerprint(engine)

    with pytest.raises(SQLiteMigrationHistoryConflict, match="Agent identity"):
        upgrade_sqlite_schema(engine)

    assert sqlite_schema_fingerprint(engine) == before
    assert "profile" not in {
        column["name"] for column in inspect(engine).get_columns("sessions")
    }
    with Session(engine) as session:
        assert session.scalar(select(func.max(SQLiteSchemaMigration.version))) == 10
    engine.dispose()


@pytest.mark.parametrize("mutation", ("missing_binding", "wrong_scope"))
def test_v11_rejects_invalid_control_ticket_and_rolls_back(
    tmp_path: Path,
    mutation: str,
) -> None:
    engine = _engine(tmp_path, f"invalid-ticket-{mutation}.sqlite3")
    _apply_through_v10(engine)
    identity = _seed_v10_identity(
        engine,
        exact_seed=False,
        authoritative_profile=True,
    )
    with Session(engine) as session, session.begin():
        ticket = session.get(
            WebSocketTicketV10Model,
            (identity["tenant_id"], identity["control_ticket_id"]),
        )
        assert ticket is not None
        if mutation == "missing_binding":
            ticket.session_key = None
        else:
            ticket.observer_scope = [
                "session.control",
                "profile=wrong-profile",
                f"agent_id={identity['agent_id']}",
            ]
    before = sqlite_schema_fingerprint(engine)

    with pytest.raises(SQLiteMigrationHistoryConflict, match="control ticket"):
        upgrade_sqlite_schema(engine)

    assert sqlite_schema_fingerprint(engine) == before
    assert "profile" not in {
        column["name"] for column in inspect(engine).get_columns("sessions")
    }
    with Session(engine) as session:
        assert session.scalar(select(func.max(SQLiteSchemaMigration.version))) == 10
    engine.dispose()
