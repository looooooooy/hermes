"""SQLite 3.24-compatible atomic identity updates without RETURNING."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.orm import Session

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


def _load_refresh_session_model(
    session: Session,
    *,
    tenant_id: UUID,
    refresh_session_id: UUID,
    operation: str,
) -> RefreshSessionModel:
    rows = (
        session.execute(
            select(RefreshSessionModel)
            .where(
                RefreshSessionModel.tenant_id == tenant_id,
                RefreshSessionModel.refresh_session_id == refresh_session_id,
            )
            .limit(2)
            .execution_options(populate_existing=True)
        )
        .scalars()
        .all()
    )
    if not rows:
        raise RefreshSessionUnavailable(
            f"refresh session is missing during {operation}"
        )
    if len(rows) != 1:
        raise RefreshSessionUnavailable(
            f"refresh session is ambiguous during {operation}"
        )
    return rows[0]


def _load_websocket_ticket_model(
    session: Session,
    *,
    tenant_id: UUID,
    ticket_digest: str,
    operation: str,
) -> WebSocketTicketModel:
    rows = (
        session.execute(
            select(WebSocketTicketModel)
            .where(
                WebSocketTicketModel.tenant_id == tenant_id,
                WebSocketTicketModel.ticket_digest == ticket_digest,
            )
            .limit(2)
            .execution_options(populate_existing=True)
        )
        .scalars()
        .all()
    )
    if not rows:
        raise WebSocketTicketUnavailable(
            f"WebSocket ticket is missing during {operation}"
        )
    if len(rows) != 1:
        raise WebSocketTicketUnavailable(
            f"WebSocket ticket is ambiguous during {operation}"
        )
    return rows[0]


def _ticket_matches_claim(
    ticket: WebSocketTicket,
    claim: WebSocketTicketClaim,
) -> bool:
    return (
        ticket.tenant_id == claim.tenant_id
        and ticket.ticket_digest == claim.ticket_digest
        and ticket.principal_type == claim.principal_type
        and ticket.principal_id == claim.principal_id
        and ticket.refresh_session_id == claim.refresh_session_id
        and ticket.session_id == claim.session_id
    )


class SQLiteIdentityRepository(SqlAlchemyIdentityRepositoryBase):
    """Use guarded row counts followed by same-transaction ORM verification."""

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
        before = refresh_session_value(
            _load_refresh_session_model(
                self._session,
                tenant_id=tenant_id,
                refresh_session_id=refresh_session_id,
                operation="rotation",
            )
        )
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
            .execution_options(synchronize_session=False)
        )
        result = self._session.execute(statement)
        model = _load_refresh_session_model(
            self._session,
            tenant_id=tenant_id,
            refresh_session_id=refresh_session_id,
            operation="rotation verification",
        )
        current = refresh_session_value(model)
        rowcount = getattr(result, "rowcount", None)
        if rowcount not in (0, 1):
            raise RefreshSessionUnavailable(
                "refresh session rotation outcome is unavailable"
            )
        if rowcount == 0:
            if current.revoked_at is not None:
                raise RefreshSessionUnavailable("refresh session is already revoked")
            if current.expires_at <= now:
                raise RefreshSessionUnavailable("refresh session is expired")
            if (
                current.token_digest == replacement_digest
                and current.rotated_at is not None
            ):
                raise RefreshSessionUnavailable("refresh session is already rotated")
            if current.token_digest != expected_digest:
                raise RefreshSessionUnavailable(
                    "refresh session digest conflicts with rotation"
                )
            raise RefreshSessionUnavailable(
                "refresh session rotation outcome conflicts"
            )
        expected = replace(
            before,
            token_digest=replacement_digest,
            rotation=before.rotation + 1,
            rotated_at=now,
        )
        if (
            before.token_digest != expected_digest
            or before.revoked_at is not None
            or before.expires_at <= now
            or current != expected
        ):
            raise RefreshSessionUnavailable(
                "refresh session rotation could not be verified"
            )
        return current

    def revoke_refresh_session(
        self,
        *,
        tenant_id: UUID,
        refresh_session_id: UUID,
        now: datetime,
    ) -> RefreshSession:
        require_aware_identity_time(now, "now")
        before = refresh_session_value(
            _load_refresh_session_model(
                self._session,
                tenant_id=tenant_id,
                refresh_session_id=refresh_session_id,
                operation="revocation",
            )
        )
        statement = (
            update(RefreshSessionModel)
            .where(
                RefreshSessionModel.tenant_id == tenant_id,
                RefreshSessionModel.refresh_session_id == refresh_session_id,
                RefreshSessionModel.revoked_at.is_(None),
            )
            .values(revoked_at=now)
            .execution_options(synchronize_session=False)
        )
        result = self._session.execute(statement)
        model = _load_refresh_session_model(
            self._session,
            tenant_id=tenant_id,
            refresh_session_id=refresh_session_id,
            operation="revocation verification",
        )
        current = refresh_session_value(model)
        rowcount = getattr(result, "rowcount", None)
        if rowcount not in (0, 1):
            raise RefreshSessionUnavailable(
                "refresh session revocation outcome is unavailable"
            )
        if rowcount == 0:
            if current.revoked_at is not None:
                raise RefreshSessionUnavailable("refresh session is already revoked")
            raise RefreshSessionUnavailable(
                "refresh session revocation outcome conflicts"
            )
        expected = replace(before, revoked_at=now)
        if before.revoked_at is not None or current != expected:
            raise RefreshSessionUnavailable(
                "refresh session revocation could not be verified"
            )
        return current

    def consume_websocket_ticket(
        self,
        claim: WebSocketTicketClaim,
        *,
        now: datetime,
    ) -> WebSocketTicket:
        require_aware_identity_time(now, "now")
        before = websocket_ticket(
            _load_websocket_ticket_model(
                self._session,
                tenant_id=claim.tenant_id,
                ticket_digest=claim.ticket_digest,
                operation="consumption",
            )
        )
        statement = (
            update(WebSocketTicketModel)
            .where(ticket_consumption_scope(claim, now=now))
            .values(consumed_at=now)
            .execution_options(synchronize_session=False)
        )
        result = self._session.execute(statement)
        model = _load_websocket_ticket_model(
            self._session,
            tenant_id=claim.tenant_id,
            ticket_digest=claim.ticket_digest,
            operation="consumption verification",
        )
        current = websocket_ticket(model)
        rowcount = getattr(result, "rowcount", None)
        if rowcount not in (0, 1):
            raise WebSocketTicketUnavailable(
                "WebSocket ticket consumption outcome is unavailable"
            )
        if rowcount == 0:
            if not _ticket_matches_claim(current, claim):
                raise WebSocketTicketUnavailable(
                    "WebSocket ticket binding conflicts with claim"
                )
            if current.consumed_at is not None:
                raise WebSocketTicketUnavailable("WebSocket ticket is already consumed")
            if current.expires_at <= now:
                raise WebSocketTicketUnavailable("WebSocket ticket is expired")
            raise WebSocketTicketUnavailable(
                "WebSocket ticket consumption outcome conflicts"
            )
        expected = replace(before, consumed_at=now)
        if (
            not _ticket_matches_claim(before, claim)
            or before.consumed_at is not None
            or before.expires_at <= now
            or current != expected
        ):
            raise WebSocketTicketUnavailable(
                "WebSocket ticket consumption could not be verified"
            )
        return current
