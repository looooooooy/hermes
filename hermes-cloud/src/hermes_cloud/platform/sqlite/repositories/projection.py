"""SQLite 3.24-compatible projection upserts without RETURNING."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from uuid import UUID

from sqlalchemy import and_, select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session
from sqlalchemy.sql import Select

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


def _rowcount(result: object, *, operation: str) -> int:
    value = getattr(result, "rowcount", None)
    if value not in {0, 1}:
        raise ProjectionConflict(f"{operation} write outcome is unavailable")
    return int(value)


def _read_unique_model(
    session: Session,
    statement: Select[tuple[object]],
    *,
    operation: str,
) -> object:
    rows = (
        session.execute(statement.execution_options(populate_existing=True))
        .scalars()
        .all()
    )
    if not rows:
        raise ProjectionConflict(f"{operation} write outcome is missing")
    if len(rows) != 1:
        raise ProjectionConflict(f"{operation} write outcome is ambiguous")
    return rows[0]


class SQLiteSessionProjectionRepository(SqlAlchemySessionProjectionRepositoryBase):
    """Use row counts and ORM comparisons for SQLite projection writes."""

    def upsert_session(
        self,
        projection: SessionProjection,
    ) -> ProjectionWriteResult:
        values = projection.as_record()
        session_insert = insert(SessionProjectionModel)
        excluded = session_insert.excluded
        statement = session_insert.values(**values).on_conflict_do_update(
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
        result = self._session.execute(statement)
        rowcount = _rowcount(result, operation="session projection")
        existing = _read_unique_model(
            self._session,
            select(SessionProjectionModel)
            .where(
                SessionProjectionModel.tenant_id == projection.tenant_id,
                SessionProjectionModel.agent_id == projection.agent_id,
                SessionProjectionModel.profile == projection.profile,
                SessionProjectionModel.session_key == projection.session_key,
            )
            .limit(2),
            operation="session projection",
        )
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
        if rowcount == 1:
            return ProjectionWriteResult.APPLIED
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
        statement = cursor_insert.values(
            tenant_id=tenant_id,
            session_id=session_id,
            stream=stream,
            last_sequence=sequence,
            updated_at=updated_at,
        ).on_conflict_do_update(
            index_elements=(
                SessionProjectionCursorModel.tenant_id,
                SessionProjectionCursorModel.session_id,
                SessionProjectionCursorModel.stream,
            ),
            set_={
                "last_sequence": excluded.last_sequence,
                "updated_at": excluded.updated_at,
            },
            where=SessionProjectionCursorModel.last_sequence < excluded.last_sequence,
        )
        result = self._session.execute(statement)
        rowcount = _rowcount(result, operation="projection cursor")
        existing = _read_unique_model(
            self._session,
            select(SessionProjectionCursorModel)
            .where(
                SessionProjectionCursorModel.tenant_id == tenant_id,
                SessionProjectionCursorModel.session_id == session_id,
                SessionProjectionCursorModel.stream == stream,
            )
            .limit(2),
            operation="projection cursor",
        )
        if existing.last_sequence > sequence:
            raise ProjectionRegression("projection sequence regressed")
        if existing.last_sequence < sequence:
            raise ProjectionConflict("projection cursor did not advance")
        if (
            existing.tenant_id != tenant_id
            or existing.session_id != session_id
            or existing.stream != stream
            or existing.updated_at != updated_at
        ):
            raise ProjectionConflict(
                "projection cursor sequence was reused with different content"
            )
        return rowcount == 1

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
        rowcount = 0
        if claimed:
            statement = insert(model).values(**values).on_conflict_do_nothing()
            result = self._session.execute(statement)
            rowcount = _rowcount(result, operation="projection row")

        existing = _read_unique_model(
            self._session,
            select(model).where(*identity).limit(2),
            operation="projection row",
        )
        if mapper(existing) != projection:
            raise ProjectionConflict(
                "projection sequence was reused with different content"
            )
        if rowcount == 1:
            return ProjectionWriteResult.APPLIED
        return ProjectionWriteResult.IDEMPOTENT
