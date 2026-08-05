"""PostgreSQL atomic identity updates using UPDATE RETURNING."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import update

from hermes_cloud.modules.identity.domain import (
    RefreshSession,
    RefreshSessionUnavailable,
    WebSocketTicket,
    WebSocketTicketClaim,
    WebSocketTicketUnavailable,
    require_aware_identity_time,
    require_sha256_digest,
)
from hermes_cloud.platform.postgres.models import (
    RefreshSessionModel,
    WebSocketTicketModel,
)
from hermes_cloud.platform.sqlalchemy.repositories.identity import (
    SqlAlchemyIdentityRepositoryBase,
    refresh_session_value,
    ticket_consumption_scope,
    websocket_ticket,
)


class SqlAlchemyIdentityRepository(SqlAlchemyIdentityRepositoryBase):
    """Bind shared identity behavior to PostgreSQL atomic writes."""

    def rotate_refresh_session(
        self,
        *,
        tenant_id: UUID,
        refresh_session_id: UUID,
        expected_digest: str,
        replacement_digest: str,
        now: datetime,
    ) -> RefreshSession:
        require_sha256_digest(expected_digest, "expected_digest")
        require_sha256_digest(replacement_digest, "replacement_digest")
        require_aware_identity_time(now, "now")
        statement = (
            update(RefreshSessionModel)
            .where(
                RefreshSessionModel.tenant_id == tenant_id,
                RefreshSessionModel.refresh_session_id == refresh_session_id,
                RefreshSessionModel.token_digest == expected_digest,
                RefreshSessionModel.revoked_at.is_(None),
                RefreshSessionModel.expires_at > now,
            )
            .values(
                token_digest=replacement_digest,
                rotation=RefreshSessionModel.rotation + 1,
                rotated_at=now,
            )
            .returning(RefreshSessionModel)
        )
        model = self._session.execute(statement).scalar_one_or_none()
        if model is None:
            raise RefreshSessionUnavailable(
                "refresh session is unavailable for rotation"
            )
        return refresh_session_value(model)

    def revoke_refresh_session(
        self,
        *,
        tenant_id: UUID,
        refresh_session_id: UUID,
        now: datetime,
    ) -> RefreshSession:
        require_aware_identity_time(now, "now")
        statement = (
            update(RefreshSessionModel)
            .where(
                RefreshSessionModel.tenant_id == tenant_id,
                RefreshSessionModel.refresh_session_id == refresh_session_id,
                RefreshSessionModel.revoked_at.is_(None),
            )
            .values(revoked_at=now)
            .returning(RefreshSessionModel)
        )
        model = self._session.execute(statement).scalar_one_or_none()
        if model is None:
            raise RefreshSessionUnavailable(
                "refresh session is unavailable for revocation"
            )
        return refresh_session_value(model)

    def consume_websocket_ticket(
        self,
        claim: WebSocketTicketClaim,
        *,
        now: datetime,
    ) -> WebSocketTicket:
        require_aware_identity_time(now, "now")
        statement = (
            update(WebSocketTicketModel)
            .where(ticket_consumption_scope(claim, now=now))
            .values(consumed_at=now)
            .returning(WebSocketTicketModel)
        )
        model = self._session.execute(statement).scalar_one_or_none()
        if model is None:
            raise WebSocketTicketUnavailable(
                "WebSocket ticket is unavailable for consumption"
            )
        return websocket_ticket(model)
