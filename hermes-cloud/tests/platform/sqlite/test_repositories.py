from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, select
from sqlalchemy.orm import Session, sessionmaker

from hermes_cloud.modules.identity.domain import (
    PasswordCredential,
    RefreshSession,
    RefreshSessionUnavailable,
    WebSocketTicket,
    WebSocketTicketClaim,
    WebSocketTicketUnavailable,
)
from hermes_cloud.modules.projection.domain import (
    ProjectionConflict,
    ProjectionWriteResult,
    SessionMessageProjection,
    SessionProjection,
)
from hermes_cloud.platform.postgres.models import (
    AgentModel,
    RefreshSessionModel,
    RoleModel,
    SessionMessageProjectionModel,
    SessionProjectionCursorModel,
    SessionProjectionModel,
    TenantModel,
    UserModel,
    WebSocketTicketModel,
    WorkspaceMembershipModel,
    WorkspaceModel,
)
from hermes_cloud.platform.sqlalchemy.repositories.projection import (
    message_projection,
)
from hermes_cloud.platform.sqlite.engine import build_sqlite_engine
from hermes_cloud.platform.sqlite.repositories.identity import (
    SQLiteIdentityRepository,
)
from hermes_cloud.platform.sqlite.repositories.projection import (
    SQLiteSessionProjectionRepository,
)
from hermes_cloud.platform.sqlite.runtime import (
    SQLiteOperationScopedIdentityRepository,
    SQLiteOperationScopedSessionProjectionRepository,
)
from hermes_cloud.platform.sqlite.schema import build_sqlite_metadata

NOW = datetime(2026, 7, 31, 0, 0, tzinfo=UTC)


class _ScriptedResult:
    def __init__(
        self,
        *,
        rowcount: int | None = None,
        rows: tuple[object, ...] = (),
    ) -> None:
        self.rowcount = rowcount
        self._rows = rows

    def scalars(self) -> _ScriptedResult:
        return self

    def all(self) -> list[object]:
        return list(self._rows)


class _ScriptedSession:
    def __init__(self, *results: _ScriptedResult) -> None:
        self._results = list(results)
        self.statements: list[object] = []

    def execute(self, statement: object) -> _ScriptedResult:
        self.statements.append(statement)
        return self._results.pop(0)


def _database_url(path: Path) -> str:
    return f"sqlite+pysqlite:///{path}"


def _factory(tmp_path: Path) -> tuple[object, sessionmaker[Session]]:
    database = tmp_path / "database.sqlite3"
    engine = build_sqlite_engine(_database_url(database), allow_missing=True)
    build_sqlite_metadata().create_all(engine)
    database.chmod(0o660)
    return engine, sessionmaker(bind=engine, expire_on_commit=False)


def _seed_acl(factory: sessionmaker[Session]) -> tuple[UUID, UUID, UUID]:
    tenant_id = uuid4()
    user_id = uuid4()
    workspace_id = uuid4()
    role_id = uuid4()
    with factory.begin() as session:
        session.add(
            TenantModel(
                tenant_id=tenant_id,
                slug="sqlite",
                display_name="SQLite",
                status="active",
                created_at=NOW,
            )
        )
        session.flush()
        session.add(
            UserModel(
                tenant_id=tenant_id,
                user_id=user_id,
                subject="sqlite-user",
                display_name="SQLite User",
                email=None,
                status="active",
                created_at=NOW,
            )
        )
        session.add(
            RoleModel(
                tenant_id=tenant_id,
                role_id=role_id,
                role_key="workspace-member",
                display_name="Workspace Member",
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
                workspace_key="sqlite",
                display_name="SQLite",
                status="active",
                created_by=user_id,
                created_at=NOW,
            )
        )
        session.flush()
        session.add(
            WorkspaceMembershipModel(
                tenant_id=tenant_id,
                workspace_membership_id=uuid4(),
                workspace_id=workspace_id,
                user_id=user_id,
                role_id=role_id,
                status="active",
                joined_at=NOW,
                revoked_at=None,
            )
        )
    return tenant_id, user_id, workspace_id


def _refresh_session(tenant_id: UUID, user_id: UUID) -> RefreshSession:
    return RefreshSession(
        tenant_id=tenant_id,
        refresh_session_id=uuid4(),
        user_id=user_id,
        token_digest="a" * 64,
        rotation=0,
        created_at=NOW,
        rotated_at=None,
        revoked_at=None,
        expires_at=NOW + timedelta(hours=1),
        retention_until=NOW + timedelta(days=1),
    )


def test_agent_list_uses_active_workspace_membership_acl_and_stable_order(
    tmp_path: Path,
) -> None:
    engine, factory = _factory(tmp_path)
    tenant_id, user_id, workspace_id = _seed_acl(factory)
    hidden_workspace_id = uuid4()
    with factory.begin() as session:
        role_id = session.scalars(select(RoleModel.role_id)).one()
        session.add(
            WorkspaceModel(
                tenant_id=tenant_id,
                workspace_id=hidden_workspace_id,
                workspace_key="hidden",
                display_name="Hidden",
                status="active",
                created_by=user_id,
                created_at=NOW,
            )
        )
        session.flush()
        session.add(
            WorkspaceMembershipModel(
                tenant_id=tenant_id,
                workspace_membership_id=uuid4(),
                workspace_id=hidden_workspace_id,
                user_id=user_id,
                role_id=role_id,
                status="revoked",
                joined_at=NOW,
                revoked_at=NOW,
            )
        )
        session.add_all(
            (
                AgentModel(
                    tenant_id=tenant_id,
                    agent_id=uuid4(),
                    workspace_id=workspace_id,
                    agent_key="b-agent",
                    status="offline",
                    last_seen_at=None,
                    created_at=NOW,
                ),
                AgentModel(
                    tenant_id=tenant_id,
                    agent_id=uuid4(),
                    workspace_id=workspace_id,
                    agent_key="a-agent",
                    status="active",
                    last_seen_at=NOW,
                    created_at=NOW,
                ),
                AgentModel(
                    tenant_id=tenant_id,
                    agent_id=uuid4(),
                    workspace_id=hidden_workspace_id,
                    agent_key="hidden-agent",
                    status="active",
                    last_seen_at=NOW,
                    created_at=NOW,
                ),
            )
        )

    with factory.begin() as session:
        agents = SQLiteSessionProjectionRepository(session).list_agents(
            tenant_id=tenant_id,
            user_id=user_id,
        )
        filtered = SQLiteSessionProjectionRepository(session).list_agents(
            tenant_id=tenant_id,
            user_id=user_id,
            workspace_id=hidden_workspace_id,
        )

    assert [agent.agent_key for agent in agents] == ["a-agent", "b-agent"]
    assert filtered == ()
    engine.dispose()


def _refresh_session_model(refresh: RefreshSession) -> RefreshSessionModel:
    return RefreshSessionModel(
        tenant_id=refresh.tenant_id,
        refresh_session_id=refresh.refresh_session_id,
        user_id=refresh.user_id,
        token_digest=refresh.token_digest,
        rotation=refresh.rotation,
        created_at=refresh.created_at,
        rotated_at=refresh.rotated_at,
        revoked_at=refresh.revoked_at,
        expires_at=refresh.expires_at,
        retention_until=refresh.retention_until,
    )


def _websocket_ticket(
    refresh: RefreshSession,
    *,
    principal_id: UUID,
) -> WebSocketTicket:
    return WebSocketTicket(
        tenant_id=refresh.tenant_id,
        ticket_id=uuid4(),
        ticket_digest="c" * 64,
        principal_type="user",
        principal_id=principal_id,
        refresh_session_id=refresh.refresh_session_id,
        session_id=None,
        observer_scope=("session.observe",),
        issued_at=NOW,
        expires_at=NOW + timedelta(minutes=5),
        consumed_at=None,
        retention_until=NOW + timedelta(days=1),
    )


def _session_projection() -> SessionProjection:
    return SessionProjection(
        tenant_id=uuid4(),
        session_id=uuid4(),
        session_key="sqlite-session",
        workspace_id=uuid4(),
        agent_id=uuid4(),
        profile="default",
        title="SQLite Session",
        state="active",
        revision=1,
        lineage_tip_message_id=uuid4(),
        lineage_tip_sequence=1,
        started_at=NOW,
        updated_at=NOW,
        closed_at=None,
        retention_until=NOW + timedelta(days=1),
    )


def _message_projection(projection: SessionProjection) -> SessionMessageProjection:
    return SessionMessageProjection(
        tenant_id=projection.tenant_id,
        session_id=projection.session_id,
        message_id=projection.lineage_tip_message_id,
        sequence=1,
        role="assistant",
        content={"text": "persisted"},
        parent_message_id=None,
        created_at=NOW,
        retention_until=NOW + timedelta(days=1),
    )


def _durable_session_projection(
    *,
    tenant_id: UUID,
    workspace_id: UUID,
    agent_id: UUID,
    session_id: UUID,
    profile: str,
    session_key: str = "shared-session-key",
) -> SessionProjection:
    return SessionProjection(
        tenant_id=tenant_id,
        session_id=session_id,
        session_key=session_key,
        workspace_id=workspace_id,
        agent_id=agent_id,
        profile=profile,
        title=f"{profile} session",
        state="active",
        revision=1,
        lineage_tip_message_id=None,
        lineage_tip_sequence=0,
        started_at=NOW,
        updated_at=NOW,
        closed_at=None,
        retention_until=NOW + timedelta(days=1),
    )


def test_sqlite_projection_uses_agent_profile_session_durable_identity(
    tmp_path: Path,
) -> None:
    engine, factory = _factory(tmp_path)
    tenant_id, user_id, workspace_id = _seed_acl(factory)
    agent_a = uuid4()
    agent_b = uuid4()
    sessions = (
        _durable_session_projection(
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            agent_id=agent_a,
            session_id=uuid4(),
            profile="default",
        ),
        _durable_session_projection(
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            agent_id=agent_b,
            session_id=uuid4(),
            profile="default",
        ),
        _durable_session_projection(
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            agent_id=agent_a,
            session_id=uuid4(),
            profile="work",
        ),
    )
    try:
        with factory.begin() as session:
            session.add_all(
                AgentModel(
                    tenant_id=tenant_id,
                    agent_id=agent_id,
                    workspace_id=workspace_id,
                    agent_key=agent_key,
                    status="active",
                    last_seen_at=NOW,
                    created_at=NOW,
                )
                for agent_id, agent_key in ((agent_a, "agent-a"), (agent_b, "agent-b"))
            )
            session.flush()
            repository = SQLiteSessionProjectionRepository(session)
            assert tuple(repository.upsert_session(item) for item in sessions) == (
                ProjectionWriteResult.APPLIED,
                ProjectionWriteResult.APPLIED,
                ProjectionWriteResult.APPLIED,
            )

        with factory.begin() as session:
            repository = SQLiteSessionProjectionRepository(session)
            assert repository.session_detail(
                tenant_id=tenant_id,
                user_id=user_id,
                session_key="shared-session-key",
                agent_id=agent_a,
                profile="default",
            ) == sessions[0]
            assert repository.session_detail(
                tenant_id=tenant_id,
                user_id=user_id,
                session_key="shared-session-key",
                agent_id=agent_b,
                profile="default",
            ) == sessions[1]
            assert repository.session_detail(
                tenant_id=tenant_id,
                user_id=user_id,
                session_key="shared-session-key",
                agent_id=agent_a,
                profile="work",
            ) == sessions[2]
            assert repository.session_detail(
                tenant_id=tenant_id,
                user_id=user_id,
                session_key="shared-session-key",
                agent_id=uuid4(),
                profile="default",
            ) is None
            assert repository.session_detail(
                tenant_id=tenant_id,
                user_id=user_id,
                session_key="shared-session-key",
                agent_id=agent_a,
                profile="missing",
            ) is None
            assert repository.session_detail(
                tenant_id=tenant_id,
                user_id=user_id,
                session_key="shared-session-key",
            ) is None
    finally:
        engine.dispose()


@pytest.mark.parametrize("read_kind", ("messages", "event_head", "transcript"))
def test_sqlite_session_children_remain_bound_to_resolved_stable_identity(
    tmp_path: Path,
    read_kind: str,
) -> None:
    engine, factory = _factory(tmp_path)
    tenant_id, user_id, workspace_id = _seed_acl(factory)
    agent_a = uuid4()
    agent_b = uuid4()
    message_a = uuid4()
    message_b = uuid4()
    primary = replace(
        _durable_session_projection(
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            agent_id=agent_a,
            session_id=uuid4(),
            profile="default",
        ),
        lineage_tip_message_id=message_a,
        lineage_tip_sequence=1,
    )
    competing = replace(
        _durable_session_projection(
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            agent_id=agent_b,
            session_id=uuid4(),
            profile="default",
        ),
        lineage_tip_message_id=message_b,
        lineage_tip_sequence=1,
    )
    primary_message = SessionMessageProjection(
        tenant_id=tenant_id,
        session_id=primary.session_id,
        message_id=message_a,
        sequence=1,
        role="assistant",
        content={"text": "primary"},
        parent_message_id=None,
        created_at=NOW,
        retention_until=NOW + timedelta(days=1),
    )
    competing_message = SessionMessageProjection(
        tenant_id=tenant_id,
        session_id=competing.session_id,
        message_id=message_b,
        sequence=1,
        role="assistant",
        content={"text": "competing"},
        parent_message_id=None,
        created_at=NOW,
        retention_until=NOW + timedelta(days=1),
    )
    primary_cursor = SessionProjectionCursorModel(
        tenant_id=tenant_id,
        session_id=primary.session_id,
        stream="events",
        last_sequence=7,
        updated_at=NOW,
    )
    competing_cursor = SessionProjectionCursorModel(
        tenant_id=tenant_id,
        session_id=competing.session_id,
        stream="events",
        last_sequence=99,
        updated_at=NOW,
    )
    try:
        with factory.begin() as session:
            session.add_all(
                (
                    AgentModel(
                        tenant_id=tenant_id,
                        agent_id=agent_a,
                        workspace_id=workspace_id,
                        agent_key="stable-agent-a",
                        status="active",
                        last_seen_at=NOW,
                        created_at=NOW,
                    ),
                    AgentModel(
                        tenant_id=tenant_id,
                        agent_id=agent_b,
                        workspace_id=workspace_id,
                        agent_key="stable-agent-b",
                        status="active",
                        last_seen_at=NOW,
                        created_at=NOW,
                    ),
                )
            )
            session.flush()
            session.add(SessionProjectionModel(**primary.as_record()))
            session.flush()
            session.add_all(
                (
                    SessionMessageProjectionModel(**primary_message.as_record()),
                    primary_cursor,
                )
            )

        with factory.begin() as session:
            original_execute = session.execute
            execute_count = 0

            def interleaving_execute(
                statement: object,
                *args: object,
                **kwargs: object,
            ) -> object:
                nonlocal execute_count
                execute_count += 1
                if execute_count == 2:
                    session.add(SessionProjectionModel(**competing.as_record()))
                    session.flush()
                    session.add_all(
                        (
                            SessionMessageProjectionModel(
                                **competing_message.as_record()
                            ),
                            competing_cursor,
                        )
                    )
                    session.flush()
                return original_execute(  # type: ignore[call-overload]
                    statement,
                    *args,
                    **kwargs,
                )

            session.execute = interleaving_execute  # type: ignore[method-assign]
            repository = SQLiteSessionProjectionRepository(session)

            if read_kind == "messages":
                assert repository.session_messages(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    session_key="shared-session-key",
                    after_sequence=0,
                    limit=10,
                    offset=0,
                ) == (primary_message,)
            elif read_kind == "event_head":
                assert (
                    repository.session_event_head(
                        tenant_id=tenant_id,
                        user_id=user_id,
                        session_key="shared-session-key",
                    )
                    == 7
                )
            else:
                assert repository.session_transcript(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    session_key="shared-session-key",
                    after_sequence=0,
                    limit=10,
                    offset=0,
                ) == (primary, (primary_message,), 7)
    finally:
        engine.dispose()


def test_sqlite_applied_session_write_requires_exact_orm_reread() -> None:
    projection = _session_projection()
    stored = SessionProjectionModel(**projection.as_record())
    session = _ScriptedSession(
        _ScriptedResult(rowcount=1),
        _ScriptedResult(rows=(stored,)),
    )
    repository = SQLiteSessionProjectionRepository(session)  # type: ignore[arg-type]

    assert repository.upsert_session(projection) is ProjectionWriteResult.APPLIED
    assert len(session.statements) == 2


@pytest.mark.parametrize("row_count", (0, 2))
def test_sqlite_applied_session_write_fails_closed_on_missing_or_ambiguous_reread(
    row_count: int,
) -> None:
    projection = _session_projection()
    rows = tuple(
        SessionProjectionModel(**projection.as_record()) for _ in range(row_count)
    )
    session = _ScriptedSession(
        _ScriptedResult(rowcount=1),
        _ScriptedResult(rows=rows),
    )
    repository = SQLiteSessionProjectionRepository(session)  # type: ignore[arg-type]

    with pytest.raises(ProjectionConflict, match="outcome"):
        repository.upsert_session(projection)


def test_sqlite_applied_session_write_fails_closed_on_mismatched_reread() -> None:
    projection = _session_projection()
    values = projection.as_record()
    values["title"] = "Unexpected"
    session = _ScriptedSession(
        _ScriptedResult(rowcount=1),
        _ScriptedResult(rows=(SessionProjectionModel(**values),)),
    )
    repository = SQLiteSessionProjectionRepository(session)  # type: ignore[arg-type]

    with pytest.raises(ProjectionConflict, match="different content"):
        repository.upsert_session(projection)


def test_sqlite_claimed_cursor_requires_exact_orm_reread() -> None:
    projection = _session_projection()
    cursor = SessionProjectionCursorModel(
        tenant_id=projection.tenant_id,
        session_id=projection.session_id,
        stream="messages",
        last_sequence=1,
        updated_at=NOW,
    )
    session = _ScriptedSession(
        _ScriptedResult(rowcount=1),
        _ScriptedResult(rows=(cursor,)),
    )
    repository = SQLiteSessionProjectionRepository(session)  # type: ignore[arg-type]

    assert repository._claim_sequence(
        tenant_id=projection.tenant_id,
        session_id=projection.session_id,
        stream="messages",
        sequence=1,
        updated_at=NOW,
    )
    assert len(session.statements) == 2


def test_sqlite_claimed_cursor_fails_closed_on_incomplete_reread() -> None:
    projection = _session_projection()
    cursor = SessionProjectionCursorModel(
        tenant_id=projection.tenant_id,
        session_id=projection.session_id,
        stream="messages",
        last_sequence=1,
        updated_at=NOW + timedelta(seconds=1),
    )
    session = _ScriptedSession(
        _ScriptedResult(rowcount=1),
        _ScriptedResult(rows=(cursor,)),
    )
    repository = SQLiteSessionProjectionRepository(session)  # type: ignore[arg-type]

    with pytest.raises(ProjectionConflict, match="content"):
        repository._claim_sequence(
            tenant_id=projection.tenant_id,
            session_id=projection.session_id,
            stream="messages",
            sequence=1,
            updated_at=NOW,
        )


def test_sqlite_applied_projection_row_requires_exact_orm_reread() -> None:
    projection = _session_projection()
    message = _message_projection(projection)
    stored = SessionMessageProjectionModel(**message.as_record())
    session = _ScriptedSession(
        _ScriptedResult(rowcount=1),
        _ScriptedResult(rows=(stored,)),
    )
    repository = SQLiteSessionProjectionRepository(session)  # type: ignore[arg-type]

    result = repository._insert_or_compare(
        projection=message,
        model=SessionMessageProjectionModel,
        values=message.as_record(),
        identity=(
            SessionMessageProjectionModel.tenant_id == message.tenant_id,
            SessionMessageProjectionModel.session_id == message.session_id,
            SessionMessageProjectionModel.sequence == message.sequence,
        ),
        mapper=message_projection,
        claimed=True,
    )

    assert result is ProjectionWriteResult.APPLIED
    assert len(session.statements) == 2


def test_sqlite_applied_projection_row_fails_closed_on_missing_reread() -> None:
    projection = _session_projection()
    message = _message_projection(projection)
    session = _ScriptedSession(
        _ScriptedResult(rowcount=1),
        _ScriptedResult(),
    )
    repository = SQLiteSessionProjectionRepository(session)  # type: ignore[arg-type]

    with pytest.raises(ProjectionConflict, match="outcome"):
        repository._insert_or_compare(
            projection=message,
            model=SessionMessageProjectionModel,
            values=message.as_record(),
            identity=(
                SessionMessageProjectionModel.tenant_id == message.tenant_id,
                SessionMessageProjectionModel.session_id == message.session_id,
                SessionMessageProjectionModel.sequence == message.sequence,
            ),
            mapper=message_projection,
            claimed=True,
        )


def test_sqlite_session_reread_refreshes_a_loaded_identity_map_row(
    tmp_path: Path,
) -> None:
    engine, factory = _factory(tmp_path)
    tenant_id, _user_id, workspace_id = _seed_acl(factory)
    projection = replace(
        _session_projection(),
        tenant_id=tenant_id,
        workspace_id=workspace_id,
    )
    revised = replace(
        projection,
        title="Revised SQLite Session",
        revision=2,
        updated_at=NOW + timedelta(minutes=1),
    )
    try:
        with factory.begin() as session:
            session.add(
                AgentModel(
                    tenant_id=tenant_id,
                    agent_id=projection.agent_id,
                    workspace_id=workspace_id,
                    agent_key="projection-reread-agent",
                    status="active",
                    last_seen_at=NOW,
                    created_at=NOW,
                )
            )
            session.flush()
            repository = SQLiteSessionProjectionRepository(session)
            assert (
                repository.upsert_session(projection) is ProjectionWriteResult.APPLIED
            )
            loaded = session.scalar(
                select(SessionProjectionModel).where(
                    SessionProjectionModel.tenant_id == projection.tenant_id,
                    SessionProjectionModel.session_key == projection.session_key,
                )
            )
            assert loaded is not None
            assert loaded.revision == 1

            assert repository.upsert_session(revised) is ProjectionWriteResult.APPLIED
            assert loaded.revision == 2
            assert loaded.title == revised.title
    finally:
        engine.dispose()


def test_sqlite_identity_atomic_writes_refresh_preloaded_rows_and_classify_replays(
    tmp_path: Path,
) -> None:
    engine, factory = _factory(tmp_path)
    tenant_id, user_id, _workspace_id = _seed_acl(factory)
    refresh = _refresh_session(tenant_id, user_id)
    ticket = _websocket_ticket(refresh, principal_id=user_id)
    rotated_at = NOW + timedelta(minutes=1)
    revoked_at = NOW + timedelta(minutes=2)
    consumed_at = NOW + timedelta(minutes=1)
    try:
        with factory.begin() as session:
            repository = SQLiteIdentityRepository(session)
            assert repository.create_refresh_session(refresh) == refresh
            assert repository.issue_websocket_ticket(ticket) == ticket
            loaded_refresh = session.scalar(
                select(RefreshSessionModel).where(
                    RefreshSessionModel.tenant_id == tenant_id,
                    RefreshSessionModel.refresh_session_id
                    == refresh.refresh_session_id,
                )
            )
            loaded_ticket = session.scalar(
                select(WebSocketTicketModel).where(
                    WebSocketTicketModel.tenant_id == tenant_id,
                    WebSocketTicketModel.ticket_digest == ticket.ticket_digest,
                )
            )
            assert loaded_refresh is not None
            assert loaded_ticket is not None

            rotated = repository.rotate_refresh_session(
                tenant_id=tenant_id,
                refresh_session_id=refresh.refresh_session_id,
                expected_digest="a" * 64,
                replacement_digest="b" * 64,
                now=rotated_at,
            )
            assert rotated == replace(
                refresh,
                token_digest="b" * 64,
                rotation=1,
                rotated_at=rotated_at,
            )
            assert loaded_refresh.token_digest == "b" * 64
            assert loaded_refresh.rotation == 1
            assert loaded_refresh.rotated_at == rotated_at

            with pytest.raises(RefreshSessionUnavailable, match="already rotated"):
                repository.rotate_refresh_session(
                    tenant_id=tenant_id,
                    refresh_session_id=refresh.refresh_session_id,
                    expected_digest="a" * 64,
                    replacement_digest="b" * 64,
                    now=rotated_at,
                )
            with pytest.raises(RefreshSessionUnavailable, match="digest conflicts"):
                repository.rotate_refresh_session(
                    tenant_id=tenant_id,
                    refresh_session_id=refresh.refresh_session_id,
                    expected_digest="d" * 64,
                    replacement_digest="e" * 64,
                    now=rotated_at,
                )

            mismatched_claim = WebSocketTicketClaim(
                tenant_id=tenant_id,
                ticket_digest=ticket.ticket_digest,
                principal_type=ticket.principal_type,
                principal_id=uuid4(),
                refresh_session_id=ticket.refresh_session_id,
                session_id=ticket.session_id,
            )
            with pytest.raises(WebSocketTicketUnavailable, match="binding conflicts"):
                repository.consume_websocket_ticket(
                    mismatched_claim,
                    now=consumed_at,
                )

            claim = WebSocketTicketClaim(
                tenant_id=tenant_id,
                ticket_digest=ticket.ticket_digest,
                principal_type=ticket.principal_type,
                principal_id=ticket.principal_id,
                refresh_session_id=ticket.refresh_session_id,
                session_id=ticket.session_id,
            )
            consumed = repository.consume_websocket_ticket(claim, now=consumed_at)
            assert consumed == replace(ticket, consumed_at=consumed_at)
            assert loaded_ticket.consumed_at == consumed_at
            with pytest.raises(WebSocketTicketUnavailable, match="already consumed"):
                repository.consume_websocket_ticket(claim, now=consumed_at)

            revoked = repository.revoke_refresh_session(
                tenant_id=tenant_id,
                refresh_session_id=refresh.refresh_session_id,
                now=revoked_at,
            )
            assert revoked == replace(rotated, revoked_at=revoked_at)
            assert loaded_refresh.revoked_at == revoked_at
            with pytest.raises(RefreshSessionUnavailable, match="already revoked"):
                repository.revoke_refresh_session(
                    tenant_id=tenant_id,
                    refresh_session_id=refresh.refresh_session_id,
                    now=revoked_at,
                )
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("rows", "message"),
    (
        ((), "missing"),
        (
            (
                _refresh_session_model(_refresh_session(uuid4(), uuid4())),
                _refresh_session_model(_refresh_session(uuid4(), uuid4())),
            ),
            "ambiguous",
        ),
    ),
)
def test_sqlite_identity_rotation_fails_closed_on_missing_or_ambiguous_reread(
    rows: tuple[object, ...],
    message: str,
) -> None:
    session = _ScriptedSession(_ScriptedResult(rows=rows))
    repository = SQLiteIdentityRepository(session)  # type: ignore[arg-type]

    with pytest.raises(RefreshSessionUnavailable, match=message):
        repository.rotate_refresh_session(
            tenant_id=uuid4(),
            refresh_session_id=uuid4(),
            expected_digest="a" * 64,
            replacement_digest="b" * 64,
            now=NOW,
        )
    assert len(session.statements) == 1


def test_sqlite_identity_repository_round_trips_aware_credentials_and_refresh(
    tmp_path: Path,
) -> None:
    engine, factory = _factory(tmp_path)
    statements: list[str] = []
    event.listen(
        engine,
        "before_cursor_execute",
        lambda _connection, _cursor, statement, _parameters, _context, _many: (
            statements.append(statement)
        ),
    )
    tenant_id, user_id, _workspace_id = _seed_acl(factory)
    credential = PasswordCredential(
        tenant_id=tenant_id,
        credential_id=uuid4(),
        user_id=user_id,
        subject="sqlite-user",
        password_hash="$argon2id$synthetic",
        status="active",
        created_at=NOW,
        updated_at=NOW,
    )
    refresh = RefreshSession(
        tenant_id=tenant_id,
        refresh_session_id=uuid4(),
        user_id=user_id,
        token_digest="a" * 64,
        rotation=0,
        created_at=NOW,
        rotated_at=None,
        revoked_at=None,
        expires_at=NOW + timedelta(hours=1),
        retention_until=NOW + timedelta(days=1),
    )
    ticket = WebSocketTicket(
        tenant_id=tenant_id,
        ticket_id=uuid4(),
        ticket_digest="c" * 64,
        principal_type="user",
        principal_id=user_id,
        refresh_session_id=refresh.refresh_session_id,
        session_id=None,
        observer_scope=("session.observe",),
        issued_at=NOW,
        expires_at=NOW + timedelta(minutes=5),
        consumed_at=None,
        retention_until=NOW + timedelta(days=1),
    )
    try:
        scoped = SQLiteOperationScopedIdentityRepository(factory)
        assert scoped.store_password_credential(credential) == credential
        loaded = scoped.credential_by_subject(
            tenant_id=tenant_id,
            subject="sqlite-user",
        )
        assert loaded == credential
        assert loaded is not None
        assert loaded.created_at.utcoffset() == timedelta(0)

        assert scoped.create_refresh_session(refresh) == refresh
        rotated = scoped.rotate_refresh_session(
            tenant_id=tenant_id,
            refresh_session_id=refresh.refresh_session_id,
            expected_digest="a" * 64,
            replacement_digest="b" * 64,
            now=NOW + timedelta(minutes=1),
        )
        assert rotated.rotation == 1
        assert rotated.token_digest == "b" * 64
        assert rotated.rotated_at == NOW + timedelta(minutes=1)
        assert scoped.issue_websocket_ticket(ticket) == ticket
        consumed = scoped.consume_websocket_ticket(
            WebSocketTicketClaim(
                tenant_id=tenant_id,
                ticket_digest=ticket.ticket_digest,
                principal_type=ticket.principal_type,
                principal_id=ticket.principal_id,
                refresh_session_id=ticket.refresh_session_id,
                session_id=ticket.session_id,
            ),
            now=NOW + timedelta(minutes=1),
        )
        assert consumed.consumed_at == NOW + timedelta(minutes=1)
        revoked = scoped.revoke_refresh_session(
            tenant_id=tenant_id,
            refresh_session_id=refresh.refresh_session_id,
            now=NOW + timedelta(minutes=2),
        )
        assert revoked.revoked_at == NOW + timedelta(minutes=2)

        with factory.begin() as session:
            direct = SQLiteIdentityRepository(session).credential_by_subject(
                tenant_id=tenant_id,
                subject="sqlite-user",
            )
            assert direct == credential
        assert all("RETURNING" not in statement.upper() for statement in statements)
    finally:
        engine.dispose()


def test_sqlite_projection_repository_uses_native_upsert_and_acl_queries(
    tmp_path: Path,
) -> None:
    engine, factory = _factory(tmp_path)
    statements: list[str] = []
    event.listen(
        engine,
        "before_cursor_execute",
        lambda _connection, _cursor, statement, _parameters, _context, _many: (
            statements.append(statement)
        ),
    )
    tenant_id, user_id, workspace_id = _seed_acl(factory)
    session_id = uuid4()
    message_id = uuid4()
    projection = SessionProjection(
        tenant_id=tenant_id,
        session_id=session_id,
        session_key="sqlite-session",
        workspace_id=workspace_id,
        agent_id=uuid4(),
        profile="default",
        title="SQLite Session",
        state="active",
        revision=1,
        lineage_tip_message_id=message_id,
        lineage_tip_sequence=1,
        started_at=NOW,
        updated_at=NOW,
        closed_at=None,
        retention_until=NOW + timedelta(days=1),
    )
    message = SessionMessageProjection(
        tenant_id=tenant_id,
        session_id=session_id,
        message_id=message_id,
        sequence=1,
        role="assistant",
        content={"text": "persisted"},
        parent_message_id=None,
        created_at=NOW,
        retention_until=NOW + timedelta(days=1),
    )
    try:
        with factory.begin() as session:
            session.add(
                AgentModel(
                    tenant_id=tenant_id,
                    agent_id=projection.agent_id,
                    workspace_id=workspace_id,
                    agent_key="projection-operation-agent",
                    status="active",
                    last_seen_at=NOW,
                    created_at=NOW,
                )
            )
        scoped = SQLiteOperationScopedSessionProjectionRepository(factory)
        assert scoped.upsert_session(projection) is ProjectionWriteResult.APPLIED
        assert scoped.upsert_session(projection) is ProjectionWriteResult.IDEMPOTENT
        assert scoped.upsert_message(message) is ProjectionWriteResult.APPLIED
        assert scoped.upsert_message(message) is ProjectionWriteResult.IDEMPOTENT

        sessions, total = scoped.list_sessions(
            tenant_id=tenant_id,
            user_id=user_id,
            limit=10,
            offset=0,
            min_messages=0,
        )
        assert total == 1
        assert sessions == (projection,)
        assert (
            scoped.session_detail(
                tenant_id=tenant_id,
                user_id=user_id,
                session_key="sqlite-session",
            )
            == projection
        )
        assert scoped.session_messages(
            tenant_id=tenant_id,
            user_id=user_id,
            session_key="sqlite-session",
            after_sequence=0,
            limit=10,
            offset=0,
        ) == (message,)

        with factory.begin() as session:
            direct = SQLiteSessionProjectionRepository(session)
            assert (
                direct.session_detail(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    session_key="sqlite-session",
                )
                == projection
            )
        assert all("RETURNING" not in statement.upper() for statement in statements)
    finally:
        engine.dispose()
