"""Dialect-neutral identity persistence and ORM mappers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from uuid import UUID

from sqlalchemy import and_, exists, select
from sqlalchemy.orm import Session

from hermes_cloud.modules.identity.domain import (
    PasswordCredential,
    RefreshSession,
    WebSocketTicket,
    WebSocketTicketClaim,
)
from hermes_cloud.platform.postgres.models import (
    PasswordCredentialModel,
    RefreshSessionModel,
    TenantModel,
    UserModel,
    WebSocketTicketModel,
)


class SqlAlchemyIdentityRepositoryBase(ABC):
    """Shared identity inserts, reads, and domain mapping."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def store_password_credential(
        self,
        credential: PasswordCredential,
    ) -> PasswordCredential:
        model = PasswordCredentialModel(
            tenant_id=credential.tenant_id,
            credential_id=credential.credential_id,
            user_id=credential.user_id,
            subject=credential.subject,
            password_hash=credential.password_hash,
            status=credential.status,
            created_at=credential.created_at,
            updated_at=credential.updated_at,
        )
        self._session.add(model)
        self._session.flush()
        return password_credential(model)

    def credential_by_subject(
        self,
        *,
        tenant_id: UUID,
        subject: str,
    ) -> PasswordCredential | None:
        statement = (
            select(PasswordCredentialModel)
            .join(
                UserModel,
                and_(
                    UserModel.tenant_id == PasswordCredentialModel.tenant_id,
                    UserModel.user_id == PasswordCredentialModel.user_id,
                ),
            )
            .join(
                TenantModel,
                TenantModel.tenant_id == PasswordCredentialModel.tenant_id,
            )
            .where(
                PasswordCredentialModel.tenant_id == tenant_id,
                PasswordCredentialModel.subject == subject,
                PasswordCredentialModel.status == "active",
                UserModel.status == "active",
                TenantModel.status == "active",
            )
            .limit(1)
        )
        model = self._session.execute(statement).scalar_one_or_none()
        return None if model is None else password_credential(model)

    def create_refresh_session(
        self,
        refresh_session: RefreshSession,
    ) -> RefreshSession:
        model = RefreshSessionModel(
            tenant_id=refresh_session.tenant_id,
            refresh_session_id=refresh_session.refresh_session_id,
            user_id=refresh_session.user_id,
            token_digest=refresh_session.token_digest,
            rotation=refresh_session.rotation,
            created_at=refresh_session.created_at,
            rotated_at=refresh_session.rotated_at,
            revoked_at=refresh_session.revoked_at,
            expires_at=refresh_session.expires_at,
            retention_until=refresh_session.retention_until,
        )
        self._session.add(model)
        self._session.flush()
        return refresh_session_value(model)

    def refresh_session_by_id(
        self,
        *,
        tenant_id: UUID,
        refresh_session_id: UUID,
    ) -> RefreshSession | None:
        statement = select(RefreshSessionModel).where(
            RefreshSessionModel.tenant_id == tenant_id,
            RefreshSessionModel.refresh_session_id == refresh_session_id,
        )
        model = self._session.execute(statement).scalar_one_or_none()
        return None if model is None else refresh_session_value(model)

    @abstractmethod
    def rotate_refresh_session(
        self,
        *,
        tenant_id: UUID,
        refresh_session_id: UUID,
        expected_digest: str,
        replacement_digest: str,
        now: datetime,
    ) -> RefreshSession:
        """Atomically rotate one matching refresh session."""

    @abstractmethod
    def revoke_refresh_session(
        self,
        *,
        tenant_id: UUID,
        refresh_session_id: UUID,
        now: datetime,
    ) -> RefreshSession:
        """Atomically revoke one active refresh session."""

    def issue_websocket_ticket(
        self,
        ticket: WebSocketTicket,
    ) -> WebSocketTicket:
        model = WebSocketTicketModel(
            tenant_id=ticket.tenant_id,
            ticket_id=ticket.ticket_id,
            ticket_digest=ticket.ticket_digest,
            principal_type=ticket.principal_type,
            principal_id=ticket.principal_id,
            refresh_session_id=ticket.refresh_session_id,
            session_id=ticket.session_id,
            observer_scope=list(ticket.observer_scope),
            issued_at=ticket.issued_at,
            expires_at=ticket.expires_at,
            consumed_at=ticket.consumed_at,
            retention_until=ticket.retention_until,
        )
        self._session.add(model)
        self._session.flush()
        return websocket_ticket(model)

    @abstractmethod
    def consume_websocket_ticket(
        self,
        claim: WebSocketTicketClaim,
        *,
        now: datetime,
    ) -> WebSocketTicket:
        """Atomically consume one matching unexpired ticket."""


def ticket_session_scope(
    claim: WebSocketTicketClaim,
) -> object:
    return (
        WebSocketTicketModel.session_id.is_(None)
        if claim.session_id is None
        else WebSocketTicketModel.session_id == claim.session_id
    )


def ticket_consumption_scope(
    claim: WebSocketTicketClaim,
    *,
    now: datetime,
) -> object:
    """One mapped ORM authority predicate shared by every SQL dialect."""

    return and_(
        WebSocketTicketModel.tenant_id == claim.tenant_id,
        WebSocketTicketModel.ticket_digest == claim.ticket_digest,
        WebSocketTicketModel.principal_type == claim.principal_type,
        WebSocketTicketModel.principal_id == claim.principal_id,
        WebSocketTicketModel.refresh_session_id == claim.refresh_session_id,
        ticket_session_scope(claim),
        WebSocketTicketModel.consumed_at.is_(None),
        WebSocketTicketModel.expires_at > now,
        exists().where(
            RefreshSessionModel.tenant_id == claim.tenant_id,
            RefreshSessionModel.refresh_session_id == claim.refresh_session_id,
            RefreshSessionModel.user_id == claim.principal_id,
            RefreshSessionModel.revoked_at.is_(None),
            RefreshSessionModel.expires_at > now,
        ),
    )


def password_credential(
    model: PasswordCredentialModel,
) -> PasswordCredential:
    return PasswordCredential(
        tenant_id=model.tenant_id,
        credential_id=model.credential_id,
        user_id=model.user_id,
        subject=model.subject,
        password_hash=model.password_hash,
        status=model.status,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


def refresh_session_value(model: RefreshSessionModel) -> RefreshSession:
    return RefreshSession(
        tenant_id=model.tenant_id,
        refresh_session_id=model.refresh_session_id,
        user_id=model.user_id,
        token_digest=model.token_digest,
        rotation=model.rotation,
        created_at=model.created_at,
        rotated_at=model.rotated_at,
        revoked_at=model.revoked_at,
        expires_at=model.expires_at,
        retention_until=model.retention_until,
    )


def websocket_ticket(model: WebSocketTicketModel) -> WebSocketTicket:
    return WebSocketTicket(
        tenant_id=model.tenant_id,
        ticket_id=model.ticket_id,
        ticket_digest=model.ticket_digest,
        principal_type=model.principal_type,
        principal_id=model.principal_id,
        refresh_session_id=model.refresh_session_id,
        session_id=model.session_id,
        observer_scope=tuple(model.observer_scope),
        issued_at=model.issued_at,
        expires_at=model.expires_at,
        consumed_at=model.consumed_at,
        retention_until=model.retention_until,
    )
