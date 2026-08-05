from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from hermes_cloud.modules.cloud_api.application.sessions import (
    SessionNotFound,
    SessionQueryService,
)
from hermes_cloud.modules.cloud_api.domain import Principal
from hermes_cloud.modules.projection.domain import (
    CatalogSessionProjection,
    SessionMessageProjection,
    SessionProjection,
)

NOW = datetime(2026, 7, 31, 8, 0, tzinfo=UTC)
TENANT_ID = UUID("11111111-1111-4111-8111-111111111111")
USER_ID = UUID("22222222-2222-4222-8222-222222222222")
WORKSPACE_ID = UUID("33333333-3333-4333-8333-333333333333")
SESSION_ID = UUID("44444444-4444-4444-8444-444444444444")
MESSAGE_ID = UUID("55555555-5555-4555-8555-555555555555")
AGENT_ID = UUID("77777777-7777-4777-8777-777777777777")


def _principal() -> Principal:
    return Principal(
        tenant_id=TENANT_ID,
        user_id=USER_ID,
        provider="basic",
        refresh_session_id=UUID("66666666-6666-4666-8666-666666666666"),
    )


def _session() -> SessionProjection:
    return SessionProjection(
        tenant_id=TENANT_ID,
        session_id=SESSION_ID,
        session_key="session-root-1",
        workspace_id=WORKSPACE_ID,
        agent_id=AGENT_ID,
        profile="default",
        title="Paginated session",
        state="active",
        revision=1,
        lineage_tip_message_id=MESSAGE_ID,
        lineage_tip_sequence=1,
        started_at=NOW,
        updated_at=NOW,
        closed_at=None,
        retention_until=NOW + timedelta(days=30),
    )


def _message() -> SessionMessageProjection:
    return SessionMessageProjection(
        tenant_id=TENANT_ID,
        session_id=SESSION_ID,
        message_id=MESSAGE_ID,
        sequence=502,
        role="assistant",
        content={"text": "page after 500"},
        parent_message_id=None,
        created_at=NOW,
        retention_until=NOW + timedelta(days=30),
    )


class _Repository:
    def __init__(self) -> None:
        self.list_call: tuple[int, int, int] | None = None
        self.messages_call: tuple[int, int, int] | None = None
        self.event_head_call: tuple[UUID, UUID, str] | None = None

    def list_sessions(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        limit: int,
        offset: int = 0,
        min_messages: int = 1,
        agent_id: UUID | None = None,
    ) -> tuple[tuple[SessionProjection, ...], int]:
        assert (tenant_id, user_id) == (TENANT_ID, USER_ID)
        self.list_call = (limit, offset, min_messages, agent_id)  # type: ignore[assignment]
        return ((_session(),), 701)

    def session_detail(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        session_key: str,
        agent_id: UUID | None = None,
    ) -> SessionProjection | None:
        if (tenant_id, user_id, session_key, agent_id) == (
            TENANT_ID,
            USER_ID,
            "session-root-1",
            AGENT_ID,
        ):
            return _session()
        return None

    def session_messages(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        session_key: str,
        after_sequence: int,
        limit: int,
        offset: int = 0,
        agent_id: UUID | None = None,
    ) -> tuple[SessionMessageProjection, ...]:
        assert (tenant_id, user_id, session_key, agent_id) == (
            TENANT_ID,
            USER_ID,
            "session-root-1",
            AGENT_ID,
        )
        self.messages_call = (after_sequence, limit, offset)
        return (_message(),)

    def session_event_head(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        session_key: str,
        agent_id: UUID | None = None,
    ) -> int:
        self.event_head_call = (tenant_id, user_id, session_key, agent_id)  # type: ignore[assignment]
        return 37

    def session_transcript(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        session_key: str,
        after_sequence: int,
        limit: int,
        offset: int = 0,
        agent_id: UUID | None = None,
        profile: str | None = None,
    ):
        assert profile in {None, "default"}
        session = self.session_detail(
            tenant_id=tenant_id,
            user_id=user_id,
            session_key=session_key,
            agent_id=agent_id,
        )
        if session is None:
            return None
        messages = self.session_messages(
            tenant_id=tenant_id,
            user_id=user_id,
            session_key=session_key,
            after_sequence=after_sequence,
            limit=limit,
            offset=offset,
            agent_id=agent_id,
        )
        event_head = self.session_event_head(
            tenant_id=tenant_id,
            user_id=user_id,
            session_key=session_key,
            agent_id=agent_id,
        )
        return session, messages, event_head


class _ObserverRepository:
    def __init__(self) -> None:
        self.call: tuple[UUID, UUID, str, None] | None = None

    def observer_snapshot(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        session_key: str,
        profile: None = None,
    ) -> dict[str, object] | None:
        self.call = (tenant_id, user_id, session_key, profile)
        return {
            "session_key": session_key,
            "runtime_session_id": "runtime-real-1",
            "running": True,
            "status": "streaming",
            "event_sequence": 91,
            "snapshot_event_sequence": 88,
            "messages": [{"role": "assistant", "content": "real"}],
            "inflight": {"streaming": True},
            "replay_events": [],
        }


class _CatalogRepository:
    def list_agent_sessions(self, **scope: object):
        assert scope == {
            "tenant_id": TENANT_ID,
            "user_id": USER_ID,
            "agent_id": AGENT_ID,
            "profile": "default",
            "limit": 20,
            "offset": 0,
        }
        return (
            (
                CatalogSessionProjection(
                    session_id=SESSION_ID,
                    agent_id=AGENT_ID,
                    workspace_id=WORKSPACE_ID,
                    profile="default",
                    session_key="session-root-1",
                    runtime_generation="runtime-test",
                    surface="hermes-cli",
                    authority_revision=3,
                    available_actions=("prompt.submit", "session.interrupt"),
                    active=True,
                ),
            ),
            1,
        )

    def resolve_visible_session(self, **scope: object):
        expected = {
            "tenant_id": TENANT_ID,
            "user_id": USER_ID,
            "session_id": SESSION_ID,
            "agent_id": AGENT_ID,
        }
        if any(scope.get(key) != value for key, value in expected.items()) or scope.get(
            "profile"
        ) not in {None, "default"}:
            return None
        return CatalogSessionProjection(
            session_id=SESSION_ID,
            agent_id=AGENT_ID,
            workspace_id=WORKSPACE_ID,
            profile="default",
            session_key="session-root-1",
            runtime_generation="runtime-test",
            surface="hermes-cli",
            authority_revision=3,
            available_actions=("prompt.submit", "session.interrupt"),
            active=True,
        )


def test_session_list_uses_repository_pagination_and_authoritative_total() -> None:
    repository = _Repository()

    payload = SessionQueryService(repository).list_sessions(  # type: ignore[arg-type]
        principal=_principal(),
        limit=20,
        offset=600,
        min_messages=1,
        agent_id=AGENT_ID,
    )

    assert repository.list_call == (20, 600, 1, AGENT_ID)
    assert payload["total"] == 701
    assert len(payload["sessions"]) == 1  # type: ignore[arg-type]


def test_agent_session_catalog_list_never_fabricates_transcript_fields() -> None:
    payload = SessionQueryService(
        _Repository(),  # type: ignore[arg-type]
        catalog_repository=_CatalogRepository(),  # type: ignore[arg-type]
    ).list_agent_catalog_sessions(
        principal=_principal(),
        agent_id=AGENT_ID,
        profile="default",
        limit=20,
        offset=0,
    )

    assert payload == {
        "sessions": [
            {
                "id": str(SESSION_ID),
                "agent_id": str(AGENT_ID),
                "workspace_id": None,
                "_lineage_root_id": str(SESSION_ID),
                "parent_session_id": None,
                "title": None,
                "preview": None,
                "source": None,
                "model": None,
                "profile": "default",
                "cwd": None,
                "git_branch": None,
                "started_at": None,
                "ended_at": None,
                "last_active": None,
                "message_count": 0,
                "tool_call_count": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "is_active": True,
                "archived": False,
                "directory_source": "host_catalog",
                "availability": "live",
                "runtime_generation": "runtime-test",
                "surface": "hermes-cli",
                "authority_revision": 3,
                "available_actions": ["prompt.submit", "session.interrupt"],
                "transcript_available": False,
            }
        ],
        "total": 1,
        "limit": 20,
        "offset": 0,
    }


def test_catalog_detail_and_messages_resolve_stable_public_id_to_host_key() -> None:
    repository = _Repository()
    service = SessionQueryService(
        repository,  # type: ignore[arg-type]
        catalog_repository=_CatalogRepository(),  # type: ignore[arg-type]
    )

    detail = service.catalog_session_detail(
        principal=_principal(),
        session_id=SESSION_ID,
        agent_id=AGENT_ID,
        profile=None,
    )
    messages = service.catalog_session_messages(
        principal=_principal(),
        session_id=SESSION_ID,
        agent_id=AGENT_ID,
        profile=None,
        limit=200,
        offset=0,
    )

    assert detail["id"] == str(SESSION_ID)
    assert detail["_lineage_root_id"] == str(SESSION_ID)
    assert "session-root-1" not in repr(detail)
    assert messages["session_id"] == str(SESSION_ID)
    assert repository.messages_call == (0, 200, 0)


def test_message_page_applies_offset_in_repository_after_first_500_rows() -> None:
    repository = _Repository()

    payload = SessionQueryService(repository).session_messages(  # type: ignore[arg-type]
        principal=_principal(),
        session_key="session-root-1",
        limit=200,
        offset=501,
        agent_id=AGENT_ID,
    )

    assert repository.messages_call == (0, 200, 501)
    assert payload["pagination"] == {
        "limit": 200,
        "offset": 501,
        "returned": 1,
    }
    assert payload["agent_id"] == str(AGENT_ID)
    assert payload["workspace_id"] == str(WORKSPACE_ID)
    assert len(payload["messages"]) == 1  # type: ignore[arg-type]


def test_message_page_resolves_identity_and_reads_transcript_in_one_operation() -> None:
    class TranscriptRepository(_Repository):
        def __init__(self) -> None:
            super().__init__()
            self.transcript_scope: dict[str, object] | None = None

        def session_detail(self, **_scope: object) -> SessionProjection | None:
            raise AssertionError("message query must not split session resolution")

        def session_messages(self, **_scope: object) -> tuple[SessionMessageProjection, ...]:
            raise AssertionError("message query must not split transcript read")

        def session_transcript(self, **scope: object):
            self.transcript_scope = scope
            return _session(), (_message(),), 37

    repository = TranscriptRepository()

    payload = SessionQueryService(repository).session_messages(  # type: ignore[arg-type]
        principal=_principal(),
        session_key="session-root-1",
        profile="default",
        limit=200,
        offset=501,
        agent_id=AGENT_ID,
    )

    assert repository.transcript_scope == {
        "tenant_id": TENANT_ID,
        "user_id": USER_ID,
        "session_key": "session-root-1",
        "profile": "default",
        "after_sequence": 0,
        "limit": 200,
        "offset": 501,
        "agent_id": AGENT_ID,
    }
    assert payload["messages"][0]["id"] == 502  # type: ignore[index]


def test_observer_snapshot_uses_acl_event_head_not_message_lineage() -> None:
    repository = _Repository()

    payload = SessionQueryService(repository).observer_snapshot(  # type: ignore[arg-type]
        principal=_principal(),
        session_key="session-root-1",
        agent_id=AGENT_ID,
    )

    assert repository.event_head_call == (
        TENANT_ID,
        USER_ID,
        "session-root-1",
        AGENT_ID,
    )
    assert payload["event_sequence"] == 37
    assert payload["snapshot_event_sequence"] == 37
    assert payload["replay_events"] == []


def test_observer_snapshot_prefers_authoritative_connector_projection() -> None:
    repository = _Repository()
    observer = _ObserverRepository()

    payload = SessionQueryService(  # type: ignore[arg-type]
        repository,
        observer_repository=observer,
    ).observer_snapshot(
        principal=_principal(),
        session_key="session-root-1",
    )

    assert observer.call == (TENANT_ID, USER_ID, "session-root-1", None)
    assert payload["runtime_session_id"] == "runtime-real-1"
    assert payload["event_sequence"] == 91


def test_same_session_key_requires_agent_scope_and_selects_exact_instance() -> None:
    agent_b = UUID("88888888-8888-4888-8888-888888888888")
    session_b = SessionProjection(
        **{
            **_session().as_record(),
            "session_id": UUID("99999999-9999-4999-8999-999999999999"),
            "agent_id": agent_b,
            "title": "Agent B session",
        }
    )

    class AmbiguousRepository(_Repository):
        def session_detail(self, **scope: object) -> SessionProjection | None:
            selected = scope.get("agent_id")
            if selected == AGENT_ID:
                return _session()
            if selected == agent_b:
                return session_b
            return None

    service = SessionQueryService(AmbiguousRepository())  # type: ignore[arg-type]

    with pytest.raises(SessionNotFound):
        service.session_detail(principal=_principal(), session_key="session-root-1")
    assert service.session_detail(
        principal=_principal(),
        session_key="session-root-1",
        agent_id=agent_b,
    )["id"] == str(session_b.session_id)
