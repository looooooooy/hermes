"""Identity repository contracts without database implementation types."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol
from uuid import UUID

from hermes_cloud.modules.identity.domain import (
    PasswordCredential,
    RefreshSession,
    WebSocketTicket,
    WebSocketTicketClaim,
)


class IdentityRepositoryFailure(RuntimeError):
    """A persistence operation failed before its transaction committed."""


class IdentityRepositoryPort(Protocol):
    def store_password_credential(
        self,
        credential: PasswordCredential,
    ) -> PasswordCredential: ...

    def credential_by_subject(
        self,
        *,
        tenant_id: UUID,
        subject: str,
    ) -> PasswordCredential | None: ...

    def create_refresh_session(
        self,
        refresh_session: RefreshSession,
    ) -> RefreshSession: ...

    def refresh_session_by_id(
        self,
        *,
        tenant_id: UUID,
        refresh_session_id: UUID,
    ) -> RefreshSession | None: ...

    def rotate_refresh_session(
        self,
        *,
        tenant_id: UUID,
        refresh_session_id: UUID,
        expected_digest: str,
        replacement_digest: str,
        now: datetime,
    ) -> RefreshSession: ...

    def revoke_refresh_session(
        self,
        *,
        tenant_id: UUID,
        refresh_session_id: UUID,
        now: datetime,
    ) -> RefreshSession: ...

    def issue_websocket_ticket(
        self,
        ticket: WebSocketTicket,
    ) -> WebSocketTicket: ...

    def consume_websocket_ticket(
        self,
        claim: WebSocketTicketClaim,
        *,
        now: datetime,
    ) -> WebSocketTicket: ...
