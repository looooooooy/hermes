"""Session projection repository contracts without SQLAlchemy dependencies."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol
from uuid import UUID

from hermes_cloud.modules.projection.domain import (
    AgentProjection,
    CatalogSessionProjection,
    ProjectionWriteResult,
    SessionEventProjection,
    SessionMessageProjection,
    SessionProjection,
)


class SessionCatalogRepositoryPort(Protocol):
    def list_agent_sessions(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        agent_id: UUID,
        profile: str | None,
        limit: int,
        offset: int,
    ) -> tuple[tuple[CatalogSessionProjection, ...], int]: ...

    def resolve_visible_session(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        session_id: UUID,
        agent_id: UUID | None,
        profile: str | None,
    ) -> CatalogSessionProjection | None: ...


class SessionProjectionRepositoryPort(Protocol):
    def list_agents(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        workspace_id: UUID | None = None,
    ) -> tuple[AgentProjection, ...]: ...

    def upsert_session(
        self,
        projection: SessionProjection,
    ) -> ProjectionWriteResult: ...

    def upsert_message(
        self,
        projection: SessionMessageProjection,
    ) -> ProjectionWriteResult: ...

    def upsert_event(
        self,
        projection: SessionEventProjection,
    ) -> ProjectionWriteResult: ...

    def list_sessions(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        limit: int,
        offset: int,
        min_messages: int,
        agent_id: UUID | None = None,
        profile: str | None = None,
    ) -> tuple[tuple[SessionProjection, ...], int]: ...

    def session_detail(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        session_key: str,
        agent_id: UUID | None = None,
        profile: str | None = None,
    ) -> SessionProjection | None: ...

    def session_messages(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        session_key: str,
        after_sequence: int,
        limit: int,
        offset: int,
        agent_id: UUID | None = None,
        profile: str | None = None,
    ) -> tuple[SessionMessageProjection, ...]: ...

    def session_event_head(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        session_key: str,
        agent_id: UUID | None = None,
        profile: str | None = None,
    ) -> int: ...

    def session_transcript(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        session_key: str,
        after_sequence: int,
        limit: int,
        offset: int,
        agent_id: UUID | None = None,
        profile: str | None = None,
    ) -> tuple[SessionProjection, tuple[SessionMessageProjection, ...], int] | None: ...


class ObserverProjectionRepositoryPort(Protocol):
    def observer_snapshot(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        session_key: str,
        profile: str | None = None,
        agent_id: UUID | None = None,
    ) -> Mapping[str, object] | None: ...
