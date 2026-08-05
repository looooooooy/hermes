"""PostgreSQL projection writes using INSERT ON CONFLICT RETURNING."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from uuid import UUID

from sqlalchemy import and_, select
from sqlalchemy.dialects.postgresql import insert

from hermes_cloud.modules.projection.domain import (
    ProjectionConflict,
    ProjectionRegression,
    ProjectionWriteResult,
    SessionProjection,
)
from hermes_cloud.platform.postgres.models import (
    SessionProjectionCursorModel,
    SessionProjectionModel,
)
from hermes_cloud.platform.sqlalchemy.repositories.projection import (
    SqlAlchemySessionProjectionRepositoryBase,
    session_projection,
)


class SqlAlchemySessionProjectionRepository(SqlAlchemySessionProjectionRepositoryBase):
    """Bind shared projection behavior to PostgreSQL atomic writes."""

    def upsert_session(
        self,
        projection: SessionProjection,
    ) -> ProjectionWriteResult:
        values = projection.as_record()
        excluded = insert(SessionProjectionModel).excluded
        statement = (
            insert(SessionProjectionModel)
            .values(**values)
            .on_conflict_do_update(
                index_elements=(
                    SessionProjectionModel.tenant_id,
                    SessionProjectionModel.agent_id,
                    SessionProjectionModel.profile,
                    SessionProjectionModel.session_key,
                ),
                set_={
                    "workspace_id": excluded.workspace_id,
                    "title": excluded.title,
                    "state": excluded.state,
                    "revision": excluded.revision,
                    "lineage_tip_message_id": excluded.lineage_tip_message_id,
                    "lineage_tip_sequence": excluded.lineage_tip_sequence,
                    "started_at": excluded.started_at,
                    "updated_at": excluded.updated_at,
                    "closed_at": excluded.closed_at,
                    "retention_until": excluded.retention_until,
                },
                where=and_(
                    SessionProjectionModel.session_id == excluded.session_id,
                    SessionProjectionModel.revision < excluded.revision,
                ),
            )
            .returning(SessionProjectionModel)
        )
        applied = self._session.execute(statement).scalar_one_or_none()
        if applied is not None:
            return ProjectionWriteResult.APPLIED

        existing = self._session.execute(
            select(SessionProjectionModel)
            .where(
                SessionProjectionModel.tenant_id == projection.tenant_id,
                SessionProjectionModel.agent_id == projection.agent_id,
                SessionProjectionModel.profile == projection.profile,
                SessionProjectionModel.session_key == projection.session_key,
            )
            .limit(1)
        ).scalar_one_or_none()
        if existing is None:
            raise ProjectionConflict("session projection write outcome is missing")
        if existing.session_id != projection.session_id:
            raise ProjectionConflict(
                "session projection session_id cannot change for a durable identity"
            )
        if existing.revision > projection.revision:
            raise ProjectionRegression("session projection revision regressed")
        if session_projection(existing) != projection:
            raise ProjectionConflict(
                "session projection revision was reused with different content"
            )
        return ProjectionWriteResult.IDEMPOTENT

    def _claim_sequence(
        self,
        *,
        tenant_id: UUID,
        session_id: UUID,
        stream: str,
        sequence: int,
        updated_at: datetime,
    ) -> bool:
        cursor_insert = insert(SessionProjectionCursorModel)
        excluded = cursor_insert.excluded
        statement = (
            cursor_insert.values(
                tenant_id=tenant_id,
                session_id=session_id,
                stream=stream,
                last_sequence=sequence,
                updated_at=updated_at,
            )
            .on_conflict_do_update(
                index_elements=(
                    SessionProjectionCursorModel.tenant_id,
                    SessionProjectionCursorModel.session_id,
                    SessionProjectionCursorModel.stream,
                ),
                set_={
                    "last_sequence": excluded.last_sequence,
                    "updated_at": excluded.updated_at,
                },
                where=SessionProjectionCursorModel.last_sequence
                < excluded.last_sequence,
            )
            .returning(SessionProjectionCursorModel)
        )
        if self._session.execute(statement).scalar_one_or_none() is not None:
            return True

        existing = self._session.execute(
            select(SessionProjectionCursorModel)
            .where(
                SessionProjectionCursorModel.tenant_id == tenant_id,
                SessionProjectionCursorModel.session_id == session_id,
                SessionProjectionCursorModel.stream == stream,
            )
            .limit(1)
        ).scalar_one_or_none()
        if existing is None:
            raise ProjectionConflict("projection cursor write outcome is missing")
        if existing.last_sequence > sequence:
            raise ProjectionRegression("projection sequence regressed")
        if existing.last_sequence < sequence:
            raise ProjectionConflict("projection cursor did not advance")
        return False

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
        if claimed:
            statement = (
                insert(model).values(**values).on_conflict_do_nothing().returning(model)
            )
            inserted = self._session.execute(statement).scalar_one_or_none()
            if inserted is not None:
                return ProjectionWriteResult.APPLIED

        existing = self._session.execute(
            select(model).where(*identity).limit(1)
        ).scalar_one_or_none()
        if existing is None or mapper(existing) != projection:
            raise ProjectionConflict(
                "projection sequence was reused with different content"
            )
        return ProjectionWriteResult.IDEMPOTENT


# Compatibility for older imports; neutral consumers use session_projection directly.
_session_projection = session_projection
