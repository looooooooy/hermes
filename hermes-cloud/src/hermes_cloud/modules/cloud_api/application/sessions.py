"""Session projection queries for the external Cloud P0 representation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from hermes_cloud.modules.cloud_api.domain import Principal
from hermes_cloud.modules.projection.domain import (
    CatalogSessionProjection,
    ProjectionScopeAmbiguous,
    SessionMessageProjection,
    SessionProjection,
)
from hermes_cloud.modules.projection.ports import (
    ObserverProjectionRepositoryPort,
    SessionCatalogRepositoryPort,
    SessionProjectionRepositoryPort,
)


class SessionNotFound(RuntimeError):
    """The requested session is not visible to the authenticated principal."""


class SessionScopeAmbiguous(RuntimeError):
    """The request omitted fields needed to select one Agent/profile scope."""


@dataclass(frozen=True, slots=True)
class CatalogSessionBinding:
    session_id: UUID
    agent_id: UUID
    profile: str
    session_key: str


class SessionQueryService:
    def __init__(
        self,
        repository: SessionProjectionRepositoryPort,
        *,
        observer_repository: ObserverProjectionRepositoryPort | None = None,
        catalog_repository: SessionCatalogRepositoryPort | None = None,
    ) -> None:
        self._repository = repository
        self._observer_repository = observer_repository
        self._catalog_repository = catalog_repository

    def list_agents(
        self,
        *,
        principal: Principal,
        workspace_id: UUID | None = None,
    ) -> dict[str, object]:
        agents = self._repository.list_agents(
            tenant_id=principal.tenant_id,
            user_id=principal.user_id,
            workspace_id=workspace_id,
        )
        return {
            "agents": [
                {
                    "agent_id": str(agent.agent_id),
                    "workspace_id": str(agent.workspace_id),
                    "agent_key": agent.agent_key,
                    "status": agent.status,
                    "last_seen_at": (
                        agent.last_seen_at.isoformat()
                        if agent.last_seen_at is not None
                        else None
                    ),
                }
                for agent in agents
            ]
        }

    def list_sessions(
        self,
        *,
        principal: Principal,
        limit: int,
        offset: int,
        min_messages: int,
        agent_id: UUID | None = None,
        profile: str | None = None,
    ) -> dict[str, object]:
        try:
            projections, total = self._repository.list_sessions(
                tenant_id=principal.tenant_id,
                user_id=principal.user_id,
                limit=limit,
                offset=offset,
                min_messages=min_messages,
                **_agent_scope(agent_id),
                **_profile_scope(profile),
            )
        except ProjectionScopeAmbiguous:
            raise SessionScopeAmbiguous from None
        return {
            "sessions": [_session_response(projection) for projection in projections],
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    def session_detail(
        self,
        *,
        principal: Principal,
        session_key: str,
        agent_id: UUID | None = None,
        profile: str | None = None,
    ) -> dict[str, object]:
        projection = self._visible_session(
            principal=principal,
            session_key=session_key,
            agent_id=agent_id,
            profile=profile,
        )
        return _session_response(projection)

    def list_agent_catalog_sessions(
        self,
        *,
        principal: Principal,
        agent_id: UUID,
        limit: int,
        offset: int,
        min_messages: int = 0,
        profile: str | None = None,
    ) -> dict[str, object]:
        if min_messages not in {0, 1}:
            raise ValueError("min_messages is outside catalog bounds")
        if min_messages == 1:
            return {"sessions": [], "total": 0, "limit": limit, "offset": offset}
        if self._catalog_repository is None:
            return {"sessions": [], "total": 0, "limit": limit, "offset": offset}
        projections, total = self._catalog_repository.list_agent_sessions(
            tenant_id=principal.tenant_id,
            user_id=principal.user_id,
            agent_id=agent_id,
            profile=profile,
            limit=limit,
            offset=offset,
        )
        return {
            "sessions": [
                _catalog_session_response(projection) for projection in projections
            ],
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    def catalog_session_binding(
        self,
        *,
        principal: Principal,
        session_id: UUID,
        agent_id: UUID | None,
        profile: str | None,
    ) -> CatalogSessionBinding:
        if self._catalog_repository is None:
            raise SessionNotFound
        projection = self._catalog_repository.resolve_visible_session(
            tenant_id=principal.tenant_id,
            user_id=principal.user_id,
            session_id=session_id,
            agent_id=agent_id,
            profile=profile,
        )
        if projection is None:
            raise SessionNotFound
        return CatalogSessionBinding(
            session_id=projection.session_id,
            agent_id=projection.agent_id,
            profile=projection.profile,
            session_key=projection.session_key,
        )

    def catalog_session_detail(
        self,
        *,
        principal: Principal,
        session_id: UUID,
        agent_id: UUID,
        profile: str | None,
    ) -> dict[str, object]:
        binding = self.catalog_session_binding(
            principal=principal,
            session_id=session_id,
            agent_id=agent_id,
            profile=profile,
        )
        assert self._catalog_repository is not None
        projection = self._catalog_repository.resolve_visible_session(
            tenant_id=principal.tenant_id,
            user_id=principal.user_id,
            session_id=binding.session_id,
            agent_id=binding.agent_id,
            profile=binding.profile,
        )
        if projection is None:
            raise SessionNotFound
        return _catalog_session_response(projection)

    def catalog_session_messages(
        self,
        *,
        principal: Principal,
        session_id: UUID,
        agent_id: UUID,
        profile: str | None,
        limit: int,
        offset: int,
    ) -> dict[str, object]:
        binding = self.catalog_session_binding(
            principal=principal,
            session_id=session_id,
            agent_id=agent_id,
            profile=profile,
        )
        transcript = self._repository.session_transcript(
            tenant_id=principal.tenant_id,
            user_id=principal.user_id,
            session_key=binding.session_key,
            after_sequence=0,
            limit=limit,
            offset=offset,
            agent_id=binding.agent_id,
            profile=binding.profile,
        )
        if transcript is None or transcript[0].session_id != binding.session_id:
            raise SessionNotFound
        session, projections, _event_head = transcript
        return {
            "session_id": str(binding.session_id),
            "agent_id": str(binding.agent_id),
            "workspace_id": str(session.workspace_id),
            "messages": [_message_response(projection) for projection in projections],
            "pagination": {
                "limit": limit,
                "offset": offset,
                "returned": len(projections),
            },
        }

    def session_messages(
        self,
        *,
        principal: Principal,
        session_key: str,
        limit: int,
        offset: int,
        agent_id: UUID | None = None,
        profile: str | None = None,
    ) -> dict[str, object]:
        transcript = self._repository.session_transcript(
            tenant_id=principal.tenant_id,
            user_id=principal.user_id,
            session_key=session_key,
            after_sequence=0,
            limit=limit,
            offset=offset,
            **_agent_scope(agent_id),
            **_profile_scope(profile),
        )
        if transcript is None:
            raise SessionNotFound
        session, projections, _event_head = transcript
        return {
            "session_id": str(session.session_id),
            "agent_id": str(session.agent_id) if session.agent_id is not None else None,
            "workspace_id": str(session.workspace_id),
            "messages": [_message_response(projection) for projection in projections],
            "pagination": {
                "limit": limit,
                "offset": offset,
                "returned": len(projections),
            },
        }

    def observer_snapshot(
        self,
        *,
        principal: Principal,
        session_key: str,
        profile: str | None = None,
        agent_id: UUID | None = None,
    ) -> dict[str, object]:
        if self._observer_repository is not None:
            snapshot = self._observer_repository.observer_snapshot(
                tenant_id=principal.tenant_id,
                user_id=principal.user_id,
                session_key=session_key,
                profile=profile,
                **_agent_scope(agent_id),
            )
            if snapshot is None:
                raise SessionNotFound
            return dict(snapshot)
        transcript = self._repository.session_transcript(
            tenant_id=principal.tenant_id,
            user_id=principal.user_id,
            session_key=session_key,
            after_sequence=0,
            limit=500,
            offset=0,
            **_agent_scope(agent_id),
            **_profile_scope(profile),
        )
        if transcript is None:
            raise SessionNotFound
        session, messages, event_sequence = transcript
        return {
            "session_key": session.session_key,
            "runtime_session_id": str(session.session_id),
            "running": session.state in {"running", "working", "streaming"},
            "status": session.state,
            "event_sequence": event_sequence,
            "snapshot_event_sequence": event_sequence,
            "messages": [
                {
                    "role": message.role,
                    "content": _message_text(message.content),
                }
                for message in messages
            ],
            "inflight": {
                "user": None,
                "assistant": None,
                "streaming": False,
                "error": None,
            },
            "replay_events": [],
        }

    def _visible_session(
        self,
        *,
        principal: Principal,
        session_key: str,
        agent_id: UUID | None = None,
        profile: str | None = None,
    ) -> SessionProjection:
        projection = self._repository.session_detail(
            tenant_id=principal.tenant_id,
            user_id=principal.user_id,
            session_key=session_key,
            **_agent_scope(agent_id),
            **_profile_scope(profile),
        )
        if projection is None:
            raise SessionNotFound
        return projection


def _agent_scope(agent_id: UUID | None) -> dict[str, UUID]:
    return {} if agent_id is None else {"agent_id": agent_id}


def _profile_scope(profile: str | None) -> dict[str, str]:
    return {} if profile is None else {"profile": profile}


def _session_response(projection: SessionProjection) -> dict[str, object]:
    return {
        "id": str(projection.session_id),
        "agent_id": str(projection.agent_id) if projection.agent_id is not None else None,
        "workspace_id": str(projection.workspace_id),
        "_lineage_root_id": str(projection.session_id),
        "parent_session_id": None,
        "title": projection.title,
        "preview": None,
        "source": None,
        "model": None,
        "profile": projection.profile,
        "cwd": None,
        "git_branch": None,
        "started_at": projection.started_at.timestamp(),
        "ended_at": (
            projection.closed_at.timestamp()
            if projection.closed_at is not None
            else None
        ),
        "last_active": projection.updated_at.timestamp(),
        "message_count": projection.lineage_tip_sequence,
        "tool_call_count": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "is_active": projection.state in {"created", "active", "waiting"},
        "archived": False,
    }


def _catalog_session_response(
    projection: CatalogSessionProjection,
) -> dict[str, object]:
    return {
        "id": str(projection.session_id),
        "agent_id": str(projection.agent_id),
        "workspace_id": None,
        "_lineage_root_id": str(projection.session_id),
        "parent_session_id": None,
        "title": None,
        "preview": None,
        "source": None,
        "model": None,
        "profile": projection.profile,
        "cwd": None,
        "git_branch": None,
        "started_at": None,
        "ended_at": None,
        "last_active": None,
        "message_count": 0,
        "tool_call_count": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "is_active": projection.active,
        "archived": False,
        "directory_source": "host_catalog",
        "availability": "live" if projection.active else "offline",
        "runtime_generation": projection.runtime_generation,
        "surface": projection.surface,
        "authority_revision": projection.authority_revision,
        "available_actions": list(projection.available_actions),
        "transcript_available": False,
    }


def _message_response(
    projection: SessionMessageProjection,
) -> dict[str, Any]:
    return {
        "id": projection.sequence,
        "role": projection.role,
        "content": projection.content,
        "timestamp": projection.created_at.timestamp(),
        "reasoning": None,
        "reasoning_content": None,
        "reasoning_details": None,
        "tool_call_id": None,
        "tool_calls": None,
        "tool_name": None,
        "display_kind": None,
        "display_metadata": None,
    }


def _message_text(content: object) -> str | None:
    if content is None or isinstance(content, str):
        return content
    if isinstance(content, dict):
        text = content.get("text")
        if isinstance(text, str):
            return text
    return None
