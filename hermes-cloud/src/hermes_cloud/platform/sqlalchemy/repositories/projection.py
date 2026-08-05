"""Dialect-neutral projection workflows, reads, and ORM mappers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from datetime import datetime
from uuid import UUID

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from hermes_cloud.modules.projection.domain import (
    AgentProjection,
    ProjectionScopeAmbiguous,
    ProjectionTenantMismatch,
    ProjectionWriteResult,
    SessionEventProjection,
    SessionMessageProjection,
    SessionProjection,
)
from hermes_cloud.platform.postgres.models import (
    AgentModel,
    SessionEventProjectionModel,
    SessionMessageProjectionModel,
    SessionProjectionCursorModel,
    SessionProjectionModel,
    WorkspaceMembershipModel,
)


class SqlAlchemySessionProjectionRepositoryBase(ABC):
    """Shared projection orchestration, ACL reads, validation, and mapping."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def list_agents(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        workspace_id: UUID | None = None,
    ) -> tuple[AgentProjection, ...]:
        membership_join = and_(
            WorkspaceMembershipModel.tenant_id == AgentModel.tenant_id,
            WorkspaceMembershipModel.workspace_id == AgentModel.workspace_id,
        )
        statement = (
            select(AgentModel)
            .join(WorkspaceMembershipModel, membership_join)
            .where(
                AgentModel.tenant_id == tenant_id,
                WorkspaceMembershipModel.tenant_id == tenant_id,
                WorkspaceMembershipModel.user_id == user_id,
                WorkspaceMembershipModel.status == "active",
            )
            .distinct()
            .order_by(AgentModel.workspace_id, AgentModel.agent_key, AgentModel.agent_id)
            .limit(1025)
        )
        if workspace_id is not None:
            statement = statement.where(AgentModel.workspace_id == workspace_id)
        rows = self._session.execute(statement).scalars().all()
        return tuple(
            AgentProjection(
                tenant_id=row.tenant_id,
                agent_id=row.agent_id,
                workspace_id=row.workspace_id,
                agent_key=row.agent_key,
                status=row.status,
                last_seen_at=row.last_seen_at,
            )
            for row in rows
        )

    @abstractmethod
    def upsert_session(
        self,
        projection: SessionProjection,
    ) -> ProjectionWriteResult:
        """Apply one provider-specific session upsert."""

    def upsert_message(
        self,
        projection: SessionMessageProjection,
    ) -> ProjectionWriteResult:
        self._require_tenant_session(
            projection.tenant_id,
            projection.session_id,
        )
        claimed = self._claim_sequence(
            tenant_id=projection.tenant_id,
            session_id=projection.session_id,
            stream="messages",
            sequence=projection.sequence,
            updated_at=projection.created_at,
        )
        return self._insert_or_compare(
            projection=projection,
            model=SessionMessageProjectionModel,
            values=projection.as_record(),
            identity=(
                SessionMessageProjectionModel.tenant_id == projection.tenant_id,
                SessionMessageProjectionModel.session_id == projection.session_id,
                SessionMessageProjectionModel.sequence == projection.sequence,
            ),
            mapper=message_projection,
            claimed=claimed,
        )

    def upsert_event(
        self,
        projection: SessionEventProjection,
    ) -> ProjectionWriteResult:
        self._require_tenant_session(
            projection.tenant_id,
            projection.session_id,
        )
        claimed = self._claim_sequence(
            tenant_id=projection.tenant_id,
            session_id=projection.session_id,
            stream="events",
            sequence=projection.sequence,
            updated_at=projection.occurred_at,
        )
        return self._insert_or_compare(
            projection=projection,
            model=SessionEventProjectionModel,
            values=projection.as_record(),
            identity=(
                SessionEventProjectionModel.tenant_id == projection.tenant_id,
                SessionEventProjectionModel.session_id == projection.session_id,
                SessionEventProjectionModel.sequence == projection.sequence,
            ),
            mapper=event_projection,
            claimed=claimed,
        )

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
    ) -> tuple[tuple[SessionProjection, ...], int]:
        require_limit(limit, maximum=500)
        require_nonnegative(offset, "offset")
        require_nonnegative(min_messages, "min_messages")
        membership_join = and_(
            WorkspaceMembershipModel.tenant_id == SessionProjectionModel.tenant_id,
            WorkspaceMembershipModel.workspace_id
            == SessionProjectionModel.workspace_id,
        )
        filters: tuple[object, ...] = (
            SessionProjectionModel.tenant_id == tenant_id,
            SessionProjectionModel.lineage_tip_sequence >= min_messages,
            WorkspaceMembershipModel.tenant_id == tenant_id,
            WorkspaceMembershipModel.user_id == user_id,
            WorkspaceMembershipModel.status == "active",
        )
        if agent_id is not None:
            filters = (*filters, SessionProjectionModel.agent_id == agent_id)
        if profile is not None:
            filters = (*filters, SessionProjectionModel.profile == profile)
        if agent_id is None or profile is None:
            scopes = self._session.execute(
                select(
                    SessionProjectionModel.agent_id,
                    SessionProjectionModel.profile,
                )
                .join(WorkspaceMembershipModel, membership_join)
                .where(*filters)
                .distinct()
                .order_by(
                    SessionProjectionModel.agent_id,
                    SessionProjectionModel.profile,
                )
                .limit(2)
            ).all()
            if len(scopes) > 1:
                raise ProjectionScopeAmbiguous
        total_statement = (
            select(func.count(func.distinct(SessionProjectionModel.session_id)))
            .select_from(SessionProjectionModel)
            .join(WorkspaceMembershipModel, membership_join)
            .where(*filters)
        )
        statement = (
            select(SessionProjectionModel)
            .join(WorkspaceMembershipModel, membership_join)
            .where(*filters)
            .distinct()
            .order_by(
                SessionProjectionModel.updated_at.desc(),
                SessionProjectionModel.session_id,
            )
            .limit(limit)
            .offset(offset)
        )
        total = int(self._session.execute(total_statement).scalar_one())
        rows = self._session.execute(statement).scalars().all()
        return tuple(session_projection(row) for row in rows), total

    def session_detail(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        session_key: str,
        agent_id: UUID | None = None,
        profile: str | None = None,
    ) -> SessionProjection | None:
        model = self._visible_session_model(
            tenant_id=tenant_id,
            user_id=user_id,
            session_key=session_key,
            agent_id=agent_id,
            profile=profile,
        )
        return None if model is None else session_projection(model)

    def _visible_session_model(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        session_key: str,
        agent_id: UUID | None = None,
        profile: str | None = None,
    ) -> SessionProjectionModel | None:
        filters: tuple[object, ...] = (
            SessionProjectionModel.tenant_id == tenant_id,
            SessionProjectionModel.session_key == session_key,
            WorkspaceMembershipModel.tenant_id == tenant_id,
            WorkspaceMembershipModel.user_id == user_id,
            WorkspaceMembershipModel.status == "active",
        )
        if agent_id is not None:
            filters = (*filters, SessionProjectionModel.agent_id == agent_id)
        if profile is not None:
            filters = (*filters, SessionProjectionModel.profile == profile)
        statement = (
            select(SessionProjectionModel)
            .join(
                WorkspaceMembershipModel,
                and_(
                    WorkspaceMembershipModel.tenant_id
                    == SessionProjectionModel.tenant_id,
                    WorkspaceMembershipModel.workspace_id
                    == SessionProjectionModel.workspace_id,
                ),
            )
            .where(*filters)
            .distinct()
            .order_by(SessionProjectionModel.session_id)
            .limit(2)
        )
        rows = self._session.execute(statement).scalars().all()
        return rows[0] if len(rows) == 1 else None

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
    ) -> tuple[SessionMessageProjection, ...]:
        require_nonnegative(after_sequence, "after_sequence")
        require_nonnegative(offset, "offset")
        require_limit(limit, maximum=500)
        session = self._visible_session_model(
            tenant_id=tenant_id,
            user_id=user_id,
            session_key=session_key,
            agent_id=agent_id,
            profile=profile,
        )
        if session is None:
            return ()
        resolved_tenant_id: UUID = session.tenant_id
        resolved_session_id: UUID = session.session_id
        filters: tuple[object, ...] = (
            SessionProjectionModel.tenant_id == resolved_tenant_id,
            SessionProjectionModel.session_id == resolved_session_id,
            SessionMessageProjectionModel.tenant_id == resolved_tenant_id,
            SessionMessageProjectionModel.session_id == resolved_session_id,
            WorkspaceMembershipModel.tenant_id == resolved_tenant_id,
            WorkspaceMembershipModel.user_id == user_id,
            WorkspaceMembershipModel.status == "active",
            SessionMessageProjectionModel.sequence > after_sequence,
        )
        rows = (
            self._session.execute(
                select(SessionMessageProjectionModel)
                .join(
                    SessionProjectionModel,
                    and_(
                        SessionProjectionModel.tenant_id
                        == SessionMessageProjectionModel.tenant_id,
                        SessionProjectionModel.session_id
                        == SessionMessageProjectionModel.session_id,
                    ),
                )
                .join(
                    WorkspaceMembershipModel,
                    and_(
                        WorkspaceMembershipModel.tenant_id
                        == SessionProjectionModel.tenant_id,
                        WorkspaceMembershipModel.workspace_id
                        == SessionProjectionModel.workspace_id,
                    ),
                )
                .where(*filters)
                .order_by(
                    SessionMessageProjectionModel.sequence,
                    SessionMessageProjectionModel.session_id,
                )
                .limit(limit)
                .offset(offset)
            )
            .scalars()
            .all()
        )
        return tuple(message_projection(row) for row in rows)

    def session_event_head(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        session_key: str,
        agent_id: UUID | None = None,
        profile: str | None = None,
    ) -> int:
        session = self._visible_session_model(
            tenant_id=tenant_id,
            user_id=user_id,
            session_key=session_key,
            agent_id=agent_id,
            profile=profile,
        )
        if session is None:
            return 0
        resolved_tenant_id: UUID = session.tenant_id
        resolved_session_id: UUID = session.session_id
        filters: tuple[object, ...] = (
            SessionProjectionModel.tenant_id == resolved_tenant_id,
            SessionProjectionModel.session_id == resolved_session_id,
            SessionProjectionCursorModel.tenant_id == resolved_tenant_id,
            SessionProjectionCursorModel.session_id == resolved_session_id,
            WorkspaceMembershipModel.tenant_id == resolved_tenant_id,
            WorkspaceMembershipModel.user_id == user_id,
            WorkspaceMembershipModel.status == "active",
            SessionProjectionCursorModel.stream == "events",
        )
        cursors = (
            self._session.execute(
                select(SessionProjectionCursorModel)
                .join(
                    SessionProjectionModel,
                    and_(
                        SessionProjectionModel.tenant_id
                        == SessionProjectionCursorModel.tenant_id,
                        SessionProjectionModel.session_id
                        == SessionProjectionCursorModel.session_id,
                    ),
                )
                .join(
                    WorkspaceMembershipModel,
                    and_(
                        WorkspaceMembershipModel.tenant_id
                        == SessionProjectionModel.tenant_id,
                        WorkspaceMembershipModel.workspace_id
                        == SessionProjectionModel.workspace_id,
                    ),
                )
                .where(*filters)
                .limit(2)
            )
            .scalars()
            .all()
        )
        return int(cursors[0].last_sequence) if len(cursors) == 1 else 0

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
    ) -> tuple[SessionProjection, tuple[SessionMessageProjection, ...], int] | None:
        require_nonnegative(after_sequence, "after_sequence")
        require_nonnegative(offset, "offset")
        require_limit(limit, maximum=500)
        model = self._visible_session_model(
            tenant_id=tenant_id,
            user_id=user_id,
            session_key=session_key,
            agent_id=agent_id,
            profile=profile,
        )
        if model is None:
            return None
        resolved_tenant_id: UUID = model.tenant_id
        resolved_session_id: UUID = model.session_id
        identity_filters: tuple[object, ...] = (
            SessionProjectionModel.tenant_id == resolved_tenant_id,
            SessionProjectionModel.session_id == resolved_session_id,
            WorkspaceMembershipModel.tenant_id == resolved_tenant_id,
            WorkspaceMembershipModel.user_id == user_id,
            WorkspaceMembershipModel.status == "active",
        )
        message_rows = (
            self._session.execute(
                select(SessionMessageProjectionModel)
                .join(
                    SessionProjectionModel,
                    and_(
                        SessionProjectionModel.tenant_id
                        == SessionMessageProjectionModel.tenant_id,
                        SessionProjectionModel.session_id
                        == SessionMessageProjectionModel.session_id,
                    ),
                )
                .join(
                    WorkspaceMembershipModel,
                    and_(
                        WorkspaceMembershipModel.tenant_id
                        == SessionProjectionModel.tenant_id,
                        WorkspaceMembershipModel.workspace_id
                        == SessionProjectionModel.workspace_id,
                    ),
                )
                .where(
                    *identity_filters,
                    SessionMessageProjectionModel.tenant_id == resolved_tenant_id,
                    SessionMessageProjectionModel.session_id == resolved_session_id,
                    SessionMessageProjectionModel.sequence > after_sequence,
                )
                .order_by(
                    SessionMessageProjectionModel.sequence,
                    SessionMessageProjectionModel.session_id,
                )
                .limit(limit)
                .offset(offset)
            )
            .scalars()
            .all()
        )
        cursor_rows = (
            self._session.execute(
                select(SessionProjectionCursorModel)
                .join(
                    SessionProjectionModel,
                    and_(
                        SessionProjectionModel.tenant_id
                        == SessionProjectionCursorModel.tenant_id,
                        SessionProjectionModel.session_id
                        == SessionProjectionCursorModel.session_id,
                    ),
                )
                .join(
                    WorkspaceMembershipModel,
                    and_(
                        WorkspaceMembershipModel.tenant_id
                        == SessionProjectionModel.tenant_id,
                        WorkspaceMembershipModel.workspace_id
                        == SessionProjectionModel.workspace_id,
                    ),
                )
                .where(
                    *identity_filters,
                    SessionProjectionCursorModel.tenant_id == resolved_tenant_id,
                    SessionProjectionCursorModel.session_id == resolved_session_id,
                    SessionProjectionCursorModel.stream == "events",
                )
                .limit(2)
            )
            .scalars()
            .all()
        )
        messages = tuple(message_projection(row) for row in message_rows)
        event_head = (
            int(cursor_rows[0].last_sequence) if len(cursor_rows) == 1 else 0
        )
        return session_projection(model), messages, event_head

    def _require_tenant_session(
        self,
        tenant_id: UUID,
        session_id: UUID,
    ) -> None:
        statement = (
            select(SessionProjectionModel)
            .where(
                SessionProjectionModel.tenant_id == tenant_id,
                SessionProjectionModel.session_id == session_id,
            )
            .limit(1)
        )
        if self._session.execute(statement).scalar_one_or_none() is None:
            raise ProjectionTenantMismatch(
                "projection child is not bound to the tenant session"
            )

    @abstractmethod
    def _claim_sequence(
        self,
        *,
        tenant_id: UUID,
        session_id: UUID,
        stream: str,
        sequence: int,
        updated_at: datetime,
    ) -> bool:
        """Claim one monotonically increasing stream sequence."""

    @abstractmethod
    def _insert_or_compare(
        self,
        *,
        projection: object,
        model: type[object],
        values: dict[str, object],
        identity: tuple[object, ...],
        mapper: Callable[[object], object],
        claimed: bool,
    ) -> ProjectionWriteResult:
        """Insert a claimed row or verify an exact idempotent retry."""


def require_limit(limit: int, *, maximum: int) -> None:
    if type(limit) is not int or not 1 <= limit <= maximum:
        raise ValueError("projection query limit is outside bounds")


def require_nonnegative(value: int, field: str) -> None:
    if type(value) is not int or value < 0:
        raise ValueError(f"{field} must not be negative")


def session_projection(model: SessionProjectionModel) -> SessionProjection:
    return SessionProjection(
        tenant_id=model.tenant_id,
        session_id=model.session_id,
        session_key=model.session_key,
        workspace_id=model.workspace_id,
        agent_id=model.agent_id,
        profile=model.profile,
        title=model.title,
        state=model.state,
        revision=model.revision,
        lineage_tip_message_id=model.lineage_tip_message_id,
        lineage_tip_sequence=model.lineage_tip_sequence,
        started_at=model.started_at,
        updated_at=model.updated_at,
        closed_at=model.closed_at,
        retention_until=model.retention_until,
    )


def message_projection(
    model: SessionMessageProjectionModel,
) -> SessionMessageProjection:
    return SessionMessageProjection(
        tenant_id=model.tenant_id,
        session_id=model.session_id,
        message_id=model.message_id,
        sequence=model.sequence,
        role=model.role,
        content=dict(model.content),
        parent_message_id=model.parent_message_id,
        created_at=model.created_at,
        retention_until=model.retention_until,
    )


def event_projection(
    model: SessionEventProjectionModel,
) -> SessionEventProjection:
    return SessionEventProjection(
        tenant_id=model.tenant_id,
        session_id=model.session_id,
        event_id=model.event_id,
        sequence=model.sequence,
        event_type=model.event_type,
        payload=dict(model.payload),
        occurred_at=model.occurred_at,
        retention_until=model.retention_until,
    )
