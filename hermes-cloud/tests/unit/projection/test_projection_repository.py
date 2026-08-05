from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql.dml import Insert
from sqlalchemy.sql.selectable import Select

from hermes_cloud.modules.projection.domain import (
    ProjectionConflict,
    ProjectionRegression,
    ProjectionScopeAmbiguous,
    ProjectionTenantMismatch,
    ProjectionWriteResult,
    SessionEventProjection,
    SessionMessageProjection,
    SessionProjection,
)
from hermes_cloud.platform.postgres.models import (
    SessionEventProjectionModel,
    SessionMessageProjectionModel,
    SessionProjectionCursorModel,
    SessionProjectionModel,
)
from hermes_cloud.platform.postgres.repositories.projection import (
    SqlAlchemySessionProjectionRepository,
)

NOW = datetime(2026, 7, 30, 11, 0, tzinfo=UTC)
TENANT_ID = UUID("11111111-1111-4111-8111-111111111111")
OTHER_TENANT_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
USER_ID = UUID("22222222-2222-4222-8222-222222222222")
WORKSPACE_ID = UUID("33333333-3333-4333-8333-333333333333")
SESSION_ID = UUID("44444444-4444-4444-8444-444444444444")
MESSAGE_ID = UUID("55555555-5555-4555-8555-555555555555")
EVENT_ID = UUID("66666666-6666-4666-8666-666666666666")
AGENT_ID = UUID("77777777-7777-4777-8777-777777777777")


class _Result:
    def __init__(self, value: object | None) -> None:
        self.value = value

    def scalar_one_or_none(self) -> object | None:
        return self.value

    def scalar_one(self) -> object:
        assert self.value is not None
        return self.value

    def scalars(self) -> _Result:
        return self

    def all(self) -> list[object]:
        if self.value is None:
            return []
        if isinstance(self.value, list):
            return self.value
        return [self.value]


class _Session:
    def __init__(self, values: list[object | None]) -> None:
        self.values = values
        self.statements: list[object] = []

    def execute(self, statement: object) -> _Result:
        self.statements.append(statement)
        return _Result(self.values.pop(0))


def _compiled(statement: object) -> str:
    return str(statement.compile(dialect=postgresql.dialect()))


def _projection(
    *,
    revision: int = 2,
    session_id: UUID = SESSION_ID,
) -> SessionProjection:
    return SessionProjection(
        tenant_id=TENANT_ID,
        session_id=session_id,
        session_key="stable-session-key",
        workspace_id=WORKSPACE_ID,
        agent_id=AGENT_ID,
        profile="default",
        title="Hermes session",
        state="active",
        revision=revision,
        lineage_tip_message_id=MESSAGE_ID,
        lineage_tip_sequence=1,
        started_at=NOW - timedelta(minutes=5),
        updated_at=NOW,
        closed_at=None,
        retention_until=NOW + timedelta(days=30),
    )


def _projection_model(
    *,
    revision: int = 2,
    session_id: UUID = SESSION_ID,
) -> SessionProjectionModel:
    value = _projection(revision=revision, session_id=session_id)
    return SessionProjectionModel(**value.as_record())


def _message() -> SessionMessageProjection:
    return SessionMessageProjection(
        tenant_id=TENANT_ID,
        session_id=SESSION_ID,
        message_id=MESSAGE_ID,
        sequence=1,
        role="assistant",
        content={"text": "ready"},
        parent_message_id=None,
        created_at=NOW,
        retention_until=NOW + timedelta(days=30),
    )


def _message_model() -> SessionMessageProjectionModel:
    return SessionMessageProjectionModel(**_message().as_record())


def test_session_upsert_is_revision_guarded_and_exact_retry_is_idempotent() -> None:
    inserted = _projection_model()
    session = _Session([inserted])
    repository = SqlAlchemySessionProjectionRepository(
        session  # type: ignore[arg-type]
    )

    assert repository.upsert_session(_projection()) is ProjectionWriteResult.APPLIED
    statement = session.statements[0]
    assert isinstance(statement, Insert)
    compiled = _compiled(statement)
    assert (
        "ON CONFLICT (tenant_id, agent_id, profile, session_key) DO UPDATE"
        in compiled
    )
    assert "revision < excluded.revision" in compiled
    assert "session_id = excluded.session_id" in compiled
    assert "RETURNING" in compiled

    retry_session = _Session([None, inserted])
    retry_repository = SqlAlchemySessionProjectionRepository(
        retry_session  # type: ignore[arg-type]
    )
    assert (
        retry_repository.upsert_session(_projection())
        is ProjectionWriteResult.IDEMPOTENT
    )


def test_session_upsert_rejects_revision_regression() -> None:
    session = _Session([None, _projection_model(revision=2)])
    repository = SqlAlchemySessionProjectionRepository(
        session  # type: ignore[arg-type]
    )

    with pytest.raises(ProjectionRegression):
        repository.upsert_session(_projection(revision=1))


@pytest.mark.parametrize("revision", [1, 2, 3])
def test_session_upsert_rejects_session_id_rebinding_at_any_revision(
    revision: int,
) -> None:
    replacement_session_id = UUID("77777777-7777-4777-8777-777777777777")
    session = _Session([None, _projection_model(revision=2)])
    repository = SqlAlchemySessionProjectionRepository(
        session  # type: ignore[arg-type]
    )

    with pytest.raises(ProjectionConflict, match="session_id"):
        repository.upsert_session(
            _projection(
                revision=revision,
                session_id=replacement_session_id,
            )
        )

    assert len(session.statements) == 2


def test_projection_object_fields_reject_non_mapping_values_with_type_error() -> None:
    message = _message()
    with pytest.raises(TypeError, match="content"):
        SessionMessageProjection(
            **{
                **message.as_record(),
                "content": [],  # type: ignore[dict-item]
            }
        )

    event = SessionEventProjection(
        tenant_id=TENANT_ID,
        session_id=SESSION_ID,
        event_id=EVENT_ID,
        sequence=1,
        event_type="session.updated",
        payload={"state": "active"},
        occurred_at=NOW,
        retention_until=NOW + timedelta(days=30),
    )
    with pytest.raises(TypeError, match="payload"):
        SessionEventProjection(
            **{
                **event.as_record(),
                "payload": [],  # type: ignore[dict-item]
            }
        )


def test_message_sequence_claim_is_atomic_idempotent_and_rejects_regression() -> None:
    session_model = _projection_model()
    cursor = SessionProjectionCursorModel(
        tenant_id=TENANT_ID,
        session_id=SESSION_ID,
        stream="messages",
        last_sequence=1,
        updated_at=NOW,
    )
    message_model = _message_model()
    session = _Session([session_model, cursor, message_model])
    repository = SqlAlchemySessionProjectionRepository(
        session  # type: ignore[arg-type]
    )

    assert repository.upsert_message(_message()) is ProjectionWriteResult.APPLIED
    assert isinstance(session.statements[0], Select)
    assert isinstance(session.statements[1], Insert)
    cursor_sql = _compiled(session.statements[1])
    assert "projection.session_cursors" in cursor_sql
    assert "ON CONFLICT (tenant_id, session_id, stream) DO UPDATE" in cursor_sql
    assert "last_sequence < excluded.last_sequence" in cursor_sql
    assert isinstance(session.statements[2], Insert)

    retry_session = _Session([session_model, None, cursor, message_model])
    retry_repository = SqlAlchemySessionProjectionRepository(
        retry_session  # type: ignore[arg-type]
    )
    assert (
        retry_repository.upsert_message(_message()) is ProjectionWriteResult.IDEMPOTENT
    )

    newer_cursor = SessionProjectionCursorModel(
        tenant_id=TENANT_ID,
        session_id=SESSION_ID,
        stream="messages",
        last_sequence=2,
        updated_at=NOW,
    )
    regression_session = _Session([session_model, None, newer_cursor])
    regression_repository = SqlAlchemySessionProjectionRepository(
        regression_session  # type: ignore[arg-type]
    )
    with pytest.raises(ProjectionRegression):
        regression_repository.upsert_message(_message())


def test_event_upsert_uses_separate_event_cursor_and_typed_insert() -> None:
    event = SessionEventProjection(
        tenant_id=TENANT_ID,
        session_id=SESSION_ID,
        event_id=EVENT_ID,
        sequence=1,
        event_type="session.updated",
        payload={"state": "active"},
        occurred_at=NOW,
        retention_until=NOW + timedelta(days=30),
    )
    event_model = SessionEventProjectionModel(**event.as_record())
    cursor = SessionProjectionCursorModel(
        tenant_id=TENANT_ID,
        session_id=SESSION_ID,
        stream="events",
        last_sequence=1,
        updated_at=NOW,
    )
    session = _Session([_projection_model(), cursor, event_model])
    repository = SqlAlchemySessionProjectionRepository(
        session  # type: ignore[arg-type]
    )

    assert repository.upsert_event(event) is ProjectionWriteResult.APPLIED
    assert "events" in str(session.statements[1].compile().params.values())
    assert "projection.session_events" in _compiled(session.statements[2])


def test_cross_tenant_projection_child_write_fails_before_insert() -> None:
    message = _message()
    cross_tenant = SessionMessageProjection(
        **{
            **message.as_record(),
            "tenant_id": OTHER_TENANT_ID,
        }
    )
    session = _Session([None])
    repository = SqlAlchemySessionProjectionRepository(
        session  # type: ignore[arg-type]
    )

    with pytest.raises(ProjectionTenantMismatch):
        repository.upsert_message(cross_tenant)

    assert len(session.statements) == 1
    compiled = _compiled(session.statements[0])
    assert "projection.sessions.tenant_id" in compiled
    assert "projection.sessions.session_id" in compiled


def test_acl_reads_join_active_membership_and_scope_every_query() -> None:
    session = _Session(
        [
            [(AGENT_ID, "default")],
            1,
            [_projection_model()],
            _projection_model(),
            _projection_model(),
            [_message_model()],
        ]
    )
    repository = SqlAlchemySessionProjectionRepository(
        session  # type: ignore[arg-type]
    )

    sessions, total = repository.list_sessions(
        tenant_id=TENANT_ID,
        user_id=USER_ID,
        limit=20,
        offset=0,
        min_messages=1,
    )
    assert len(sessions) == 1
    assert total == 1
    assert (
        repository.session_detail(
            tenant_id=TENANT_ID,
            user_id=USER_ID,
            session_key="stable-session-key",
        )
        is not None
    )
    assert (
        len(
            repository.session_messages(
                tenant_id=TENANT_ID,
                user_id=USER_ID,
                session_key="stable-session-key",
                after_sequence=0,
                limit=50,
                offset=0,
            )
        )
        == 1
    )

    for statement in session.statements[:4]:
        compiled = _compiled(statement)
        assert "workspace.workspace_memberships" in compiled
        assert "workspace_memberships.tenant_id" in compiled
        assert "workspace_memberships.user_id" in compiled
        assert "workspace_memberships.status" in compiled
    child_sql = _compiled(session.statements[5])
    assert "projection.session_messages.session_id" in child_sql


def test_acl_list_supports_contract_maximum_offset_and_authoritative_total() -> None:
    session = _Session([[(AGENT_ID, "default")], 701, [_projection_model()]])
    repository = SqlAlchemySessionProjectionRepository(
        session  # type: ignore[arg-type]
    )

    projections, total = repository.list_sessions(
        tenant_id=TENANT_ID,
        user_id=USER_ID,
        limit=500,
        offset=600,
        min_messages=1,
    )

    assert projections == (_projection(),)
    assert total == 701
    assert len(session.statements) == 3
    count_sql = _compiled(session.statements[1])
    page_sql = _compiled(session.statements[2])
    assert "count(" in count_sql.lower()
    assert "lineage_tip_sequence" in count_sql
    assert "LIMIT" in page_sql
    assert "OFFSET" in page_sql
    assert "lineage_tip_sequence" in page_sql


def test_acl_list_fails_closed_before_pagination_when_scope_is_ambiguous() -> None:
    session = _Session(
        [
            [
                (AGENT_ID, "default"),
                (UUID("88888888-8888-4888-8888-888888888888"), "work"),
            ]
        ]
    )
    repository = SqlAlchemySessionProjectionRepository(
        session  # type: ignore[arg-type]
    )

    with pytest.raises(ProjectionScopeAmbiguous):
        repository.list_sessions(
            tenant_id=TENANT_ID,
            user_id=USER_ID,
            limit=20,
            offset=0,
            min_messages=1,
        )

    assert len(session.statements) == 1
    compiled = _compiled(session.statements[0])
    assert "projection.sessions.agent_id" in compiled
    assert "projection.sessions.profile" in compiled
    assert "LIMIT" in compiled


def test_acl_message_query_applies_offset_in_database() -> None:
    session = _Session([_projection_model(), [_message_model()]])
    repository = SqlAlchemySessionProjectionRepository(
        session  # type: ignore[arg-type]
    )

    projections = repository.session_messages(
        tenant_id=TENANT_ID,
        user_id=USER_ID,
        session_key="stable-session-key",
        after_sequence=0,
        limit=500,
        offset=501,
    )

    assert projections == (_message(),)
    compiled = _compiled(session.statements[1])
    assert "LIMIT" in compiled
    assert "OFFSET" in compiled


def test_acl_event_head_reads_the_events_cursor_for_visible_session() -> None:
    cursor = SessionProjectionCursorModel(
        tenant_id=TENANT_ID,
        session_id=SESSION_ID,
        stream="events",
        last_sequence=37,
        updated_at=NOW,
    )
    session = _Session([_projection_model(), cursor])
    repository = SqlAlchemySessionProjectionRepository(
        session  # type: ignore[arg-type]
    )

    assert (
        repository.session_event_head(
            tenant_id=TENANT_ID,
            user_id=USER_ID,
            session_key="stable-session-key",
        )
        == 37
    )
    resolver_sql = _compiled(session.statements[0])
    compiled = _compiled(session.statements[1])
    assert "projection.session_cursors" in compiled
    assert "projection.session_cursors.session_id" in compiled
    assert "projection.sessions" in resolver_sql
    assert "workspace.workspace_memberships" in resolver_sql
    assert "workspace_memberships.tenant_id" in resolver_sql
    assert "workspace_memberships.user_id" in resolver_sql
    assert "workspace_memberships.status" in resolver_sql
    assert "events" in str(session.statements[1].compile().params.values())


def test_acl_event_head_is_zero_when_visible_session_has_no_events() -> None:
    session = _Session([None])
    repository = SqlAlchemySessionProjectionRepository(
        session  # type: ignore[arg-type]
    )

    assert (
        repository.session_event_head(
            tenant_id=TENANT_ID,
            user_id=USER_ID,
            session_key="stable-session-key",
        )
        == 0
    )
