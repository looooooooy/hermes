"""Provider-neutral operation scopes, login resolution, and database probing."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import datetime
from typing import Protocol, TypeVar
from uuid import UUID

from sqlalchemy import and_, select
from sqlalchemy.exc import IntegrityError, OperationalError, SQLAlchemyError
from sqlalchemy.orm import Session

from hermes_cloud.modules.device.domain import (
    DeviceAuthenticationSnapshot,
    PairingOffer,
    PairingSnapshot,
)
from hermes_cloud.modules.device.ports import (
    ActivatePairingCommand,
    CancelPairingCommand,
    ClaimPairingCommand,
    ConfirmPairingCommand,
    ConsumeDeviceChallengeCommand,
    CreateDeviceChallengeCommand,
    PairingMutation,
    PairingStateConflict,
    RevokeDeviceCommand,
)
from hermes_cloud.modules.identity.domain import (
    PasswordCredential,
    RefreshSession,
    WebSocketTicket,
    WebSocketTicketClaim,
)
from hermes_cloud.modules.identity.ports import IdentityRepositoryFailure
from hermes_cloud.modules.projection.domain import (
    AgentProjection,
    ProjectionWriteResult,
    SessionEventProjection,
    SessionMessageProjection,
    SessionProjection,
)
from hermes_cloud.platform.postgres.models import (
    PasswordCredentialModel,
    TenantModel,
    UserModel,
)
from hermes_cloud.platform.sqlalchemy.repositories.device import (
    PairingFailure,
    SqlAlchemyPairingRepositoryBase,
)
from hermes_cloud.platform.sqlalchemy.repositories.identity import (
    SqlAlchemyIdentityRepositoryBase,
)
from hermes_cloud.platform.sqlalchemy.repositories.projection import (
    SqlAlchemySessionProjectionRepositoryBase,
)

ResultT = TypeVar("ResultT")
_MUTATION_RETRY_DELAYS = (0.0, 0.005, 0.01, 0.02, 0.04)


class SessionFactory(Protocol):
    def begin(self) -> AbstractContextManager[Session]: ...


class IdentityRepositoryFactory(Protocol):
    def __call__(self, session: Session) -> SqlAlchemyIdentityRepositoryBase: ...


class ProjectionRepositoryFactory(Protocol):
    def __call__(
        self,
        session: Session,
    ) -> SqlAlchemySessionProjectionRepositoryBase: ...


class PairingRepositoryFactory(Protocol):
    def __call__(self, session: Session) -> SqlAlchemyPairingRepositoryBase: ...


class OperationScopedPairingRepository:
    """Create one transaction and Session for every pairing operation."""

    def __init__(
        self,
        session_factory: SessionFactory,
        repository_factory: PairingRepositoryFactory,
    ) -> None:
        self._session_factory = session_factory
        self._repository_factory = repository_factory

    def create_offer(
        self,
        offer: PairingOffer,
        *,
        mutation: PairingMutation,
    ) -> PairingOffer:
        return self._run_idempotent_mutation(
            lambda repository: repository.create_offer(
                offer,
                mutation=mutation,
            )
        )

    def _run_idempotent_mutation(
        self,
        operation: Callable[[SqlAlchemyPairingRepositoryBase], ResultT],
    ) -> ResultT:
        last_error: IntegrityError | OperationalError | PairingStateConflict | None = (
            None
        )
        for delay in _MUTATION_RETRY_DELAYS:
            if delay:
                time.sleep(delay)
            try:
                with self._session_factory.begin() as session:
                    return operation(self._repository_factory(session))
            except (IntegrityError, OperationalError, PairingStateConflict) as error:
                if not _is_idempotency_race(error):
                    raise
                last_error = error
        assert last_error is not None
        raise last_error

    def _run_outcome(
        self,
        operation: Callable[
            [SqlAlchemyPairingRepositoryBase],
            PairingSnapshot | PairingFailure,
        ],
    ) -> PairingSnapshot:
        outcome = self._run_idempotent_mutation(operation)
        if isinstance(outcome, PairingFailure):
            raise outcome.error
        return outcome

    def claim_offer(
        self,
        command: ClaimPairingCommand,
        *,
        mutation: PairingMutation,
    ) -> PairingSnapshot:
        return self._run_outcome(
            lambda repository: repository.claim_offer(
                command,
                mutation=mutation,
            )
        )

    def confirm_owner(
        self,
        command: ConfirmPairingCommand,
        *,
        mutation: PairingMutation,
    ) -> PairingSnapshot:
        return self._run_outcome(
            lambda repository: repository.confirm_owner(
                command,
                mutation=mutation,
            )
        )

    def activate_verified_credential(
        self,
        command: ActivatePairingCommand,
        *,
        mutation: PairingMutation,
    ) -> PairingSnapshot:
        return self._run_outcome(
            lambda repository: repository.activate_verified_credential(
                command,
                mutation=mutation,
            )
        )

    def cancel_pairing(
        self,
        command: CancelPairingCommand,
        *,
        mutation: PairingMutation,
    ) -> PairingSnapshot:
        return self._run_outcome(
            lambda repository: repository.cancel_pairing(
                command,
                mutation=mutation,
            )
        )

    def expire_offer(
        self,
        pairing_offer_id: UUID,
        *,
        now: datetime,
    ) -> PairingOffer:
        with self._session_factory.begin() as session:
            return self._repository_factory(session).expire_offer(
                pairing_offer_id,
                now=now,
            )

    def revoke_device(
        self,
        command: RevokeDeviceCommand,
        *,
        mutation: PairingMutation,
    ) -> PairingSnapshot:
        return self._run_outcome(
            lambda repository: repository.revoke_device(
                command,
                mutation=mutation,
            )
        )

    def replay_pairing_mutation(
        self,
        mutation: PairingMutation,
    ) -> PairingSnapshot | None:
        with self._session_factory.begin() as session:
            return self._repository_factory(session).replay_pairing_mutation(mutation)

    def get_offer(
        self,
        pairing_offer_id: UUID,
        *,
        bootstrap_secret_digest: str,
        now: datetime,
    ) -> PairingSnapshot:
        with self._session_factory.begin() as session:
            return self._repository_factory(session).get_offer(
                pairing_offer_id,
                bootstrap_secret_digest=bootstrap_secret_digest,
                now=now,
            )

    def get_pairing_for_proof(
        self,
        pairing_session_id: UUID,
        *,
        bootstrap_secret_digest: str,
        now: datetime,
    ) -> PairingSnapshot:
        with self._session_factory.begin() as session:
            return self._repository_factory(session).get_pairing_for_proof(
                pairing_session_id,
                bootstrap_secret_digest=bootstrap_secret_digest,
                now=now,
            )

    def get_pairing_for_proof_history(
        self,
        pairing_session_id: UUID,
        *,
        bootstrap_secret_digest: str,
    ) -> PairingSnapshot:
        with self._session_factory.begin() as session:
            return self._repository_factory(session).get_pairing_for_proof_history(
                pairing_session_id,
                bootstrap_secret_digest=bootstrap_secret_digest,
            )

    def get_owner_pairing(
        self,
        *,
        tenant_id: UUID,
        owner_user_id: UUID,
        pairing_session_id: UUID,
        now: datetime,
    ) -> PairingSnapshot:
        with self._session_factory.begin() as session:
            return self._repository_factory(session).get_owner_pairing(
                tenant_id=tenant_id,
                owner_user_id=owner_user_id,
                pairing_session_id=pairing_session_id,
                now=now,
            )

    def get_owner_pairing_status(
        self,
        *,
        tenant_id: UUID,
        owner_user_id: UUID,
        pairing_session_id: UUID,
    ) -> PairingSnapshot:
        with self._session_factory.begin() as session:
            return self._repository_factory(session).get_owner_pairing_status(
                tenant_id=tenant_id,
                owner_user_id=owner_user_id,
                pairing_session_id=pairing_session_id,
            )

    def create_device_challenge(
        self,
        command: CreateDeviceChallengeCommand,
        *,
        mutation: PairingMutation,
    ) -> DeviceAuthenticationSnapshot:
        return self._run_idempotent_mutation(
            lambda repository: repository.create_device_challenge(
                command,
                mutation=mutation,
            )
        )

    def consume_device_challenge(
        self,
        command: ConsumeDeviceChallengeCommand,
        *,
        mutation: PairingMutation,
    ) -> DeviceAuthenticationSnapshot:
        return self._run_idempotent_mutation(
            lambda repository: repository.consume_device_challenge(
                command,
                mutation=mutation,
            )
        )

    def active_device_binding(
        self,
        *,
        tenant_id: UUID | None,
        device_id: UUID,
        credential_id: UUID,
        now: datetime,
    ) -> DeviceAuthenticationSnapshot:
        with self._session_factory.begin() as session:
            return self._repository_factory(session).active_device_binding(
                tenant_id=tenant_id,
                device_id=device_id,
                credential_id=credential_id,
                now=now,
            )

    def active_legacy_device_binding(
        self,
        *,
        tenant_id: UUID,
        device_id: UUID,
        now: datetime,
    ) -> DeviceAuthenticationSnapshot:
        with self._session_factory.begin() as session:
            return self._repository_factory(session).active_legacy_device_binding(
                tenant_id=tenant_id,
                device_id=device_id,
                now=now,
            )

    def get_device_challenge(
        self,
        *,
        device_id: UUID,
        credential_id: UUID,
        challenge_id: UUID,
        now: datetime,
    ) -> DeviceAuthenticationSnapshot:
        with self._session_factory.begin() as session:
            return self._repository_factory(session).get_device_challenge(
                device_id=device_id,
                credential_id=credential_id,
                challenge_id=challenge_id,
                now=now,
            )


def _is_idempotency_race(
    error: IntegrityError | OperationalError | PairingStateConflict,
) -> bool:
    if isinstance(error, PairingStateConflict):
        return "CAS failed" in str(error)
    if isinstance(error, IntegrityError):
        return True
    message = str(error.orig).casefold()
    return "locked" in message or "busy" in message


class OperationScopedIdentityRepository:
    """Create one transaction and Session for every identity operation."""

    def __init__(
        self,
        session_factory: SessionFactory,
        repository_factory: IdentityRepositoryFactory,
    ) -> None:
        self._session_factory = session_factory
        self._repository_factory = repository_factory

    def _run(
        self,
        operation: Callable[[SqlAlchemyIdentityRepositoryBase], ResultT],
    ) -> ResultT:
        try:
            with self._session_factory.begin() as session:
                return operation(self._repository_factory(session))
        except SQLAlchemyError:
            raise IdentityRepositoryFailure from None

    def store_password_credential(
        self,
        credential: PasswordCredential,
    ) -> PasswordCredential:
        return self._run(
            lambda repository: repository.store_password_credential(credential)
        )

    def credential_by_subject(
        self,
        *,
        tenant_id: UUID,
        subject: str,
    ) -> PasswordCredential | None:
        return self._run(
            lambda repository: repository.credential_by_subject(
                tenant_id=tenant_id,
                subject=subject,
            )
        )

    def create_refresh_session(
        self,
        refresh_session: RefreshSession,
    ) -> RefreshSession:
        return self._run(
            lambda repository: repository.create_refresh_session(refresh_session)
        )

    def refresh_session_by_id(
        self,
        *,
        tenant_id: UUID,
        refresh_session_id: UUID,
    ) -> RefreshSession | None:
        return self._run(
            lambda repository: repository.refresh_session_by_id(
                tenant_id=tenant_id,
                refresh_session_id=refresh_session_id,
            )
        )

    def rotate_refresh_session(
        self,
        *,
        tenant_id: UUID,
        refresh_session_id: UUID,
        expected_digest: str,
        replacement_digest: str,
        now: datetime,
    ) -> RefreshSession:
        return self._run(
            lambda repository: repository.rotate_refresh_session(
                tenant_id=tenant_id,
                refresh_session_id=refresh_session_id,
                expected_digest=expected_digest,
                replacement_digest=replacement_digest,
                now=now,
            )
        )

    def revoke_refresh_session(
        self,
        *,
        tenant_id: UUID,
        refresh_session_id: UUID,
        now: datetime,
    ) -> RefreshSession:
        return self._run(
            lambda repository: repository.revoke_refresh_session(
                tenant_id=tenant_id,
                refresh_session_id=refresh_session_id,
                now=now,
            )
        )

    def issue_websocket_ticket(
        self,
        ticket: WebSocketTicket,
    ) -> WebSocketTicket:
        return self._run(lambda repository: repository.issue_websocket_ticket(ticket))

    def consume_websocket_ticket(
        self,
        claim: WebSocketTicketClaim,
        *,
        now: datetime,
    ) -> WebSocketTicket:
        return self._run(
            lambda repository: repository.consume_websocket_ticket(
                claim,
                now=now,
            )
        )


class OperationScopedSessionProjectionRepository:
    """Create one transaction and Session for every projection operation."""

    def __init__(
        self,
        session_factory: SessionFactory,
        repository_factory: ProjectionRepositoryFactory,
    ) -> None:
        self._session_factory = session_factory
        self._repository_factory = repository_factory

    def _run(
        self,
        operation: Callable[
            [SqlAlchemySessionProjectionRepositoryBase],
            ResultT,
        ],
    ) -> ResultT:
        with self._session_factory.begin() as session:
            return operation(self._repository_factory(session))

    def upsert_session(
        self,
        projection: SessionProjection,
    ) -> ProjectionWriteResult:
        return self._run(lambda repository: repository.upsert_session(projection))

    def upsert_message(
        self,
        projection: SessionMessageProjection,
    ) -> ProjectionWriteResult:
        return self._run(lambda repository: repository.upsert_message(projection))

    def upsert_event(
        self,
        projection: SessionEventProjection,
    ) -> ProjectionWriteResult:
        return self._run(lambda repository: repository.upsert_event(projection))

    def list_agents(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        workspace_id: UUID | None = None,
    ) -> tuple[AgentProjection, ...]:
        return self._run(
            lambda repository: repository.list_agents(
                tenant_id=tenant_id,
                user_id=user_id,
                workspace_id=workspace_id,
            )
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
        return self._run(
            lambda repository: repository.list_sessions(
                tenant_id=tenant_id,
                user_id=user_id,
                limit=limit,
                offset=offset,
                min_messages=min_messages,
                agent_id=agent_id,
                profile=profile,
            )
        )

    def session_detail(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        session_key: str,
        agent_id: UUID | None = None,
        profile: str | None = None,
    ) -> SessionProjection | None:
        return self._run(
            lambda repository: repository.session_detail(
                tenant_id=tenant_id,
                user_id=user_id,
                session_key=session_key,
                agent_id=agent_id,
                profile=profile,
            )
        )

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
        return self._run(
            lambda repository: repository.session_messages(
                tenant_id=tenant_id,
                user_id=user_id,
                session_key=session_key,
                after_sequence=after_sequence,
                limit=limit,
                offset=offset,
                agent_id=agent_id,
                profile=profile,
            )
        )

    def session_event_head(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        session_key: str,
        agent_id: UUID | None = None,
        profile: str | None = None,
    ) -> int:
        return self._run(
            lambda repository: repository.session_event_head(
                tenant_id=tenant_id,
                user_id=user_id,
                session_key=session_key,
                agent_id=agent_id,
                profile=profile,
            )
        )

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
        return self._run(
            lambda repository: repository.session_transcript(
                tenant_id=tenant_id,
                user_id=user_id,
                session_key=session_key,
                after_sequence=after_sequence,
                limit=limit,
                offset=offset,
                agent_id=agent_id,
                profile=profile,
            )
        )


class SqlAlchemyLoginTenantResolver:
    """Resolve a subject only when one active tenant owns it."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    def tenant_for_subject(self, subject: str) -> UUID | None:
        if not isinstance(subject, str) or not subject:
            return None
        statement = (
            select(PasswordCredentialModel.tenant_id)
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
                PasswordCredentialModel.subject == subject,
                PasswordCredentialModel.status == "active",
                UserModel.status == "active",
                TenantModel.status == "active",
            )
            .distinct()
            .limit(2)
        )
        with self._session_factory.begin() as session:
            tenant_ids = session.scalars(statement).all()
        return tenant_ids[0] if len(tenant_ids) == 1 else None


class SqlAlchemyDatabaseProbe:
    """Run a bounded ORM query using an operation-scoped Session."""

    critical = True
    deadline_seconds = 3.0

    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        name: str,
    ) -> None:
        self._session_factory = session_factory
        self.name = name

    async def check(self) -> None:
        await asyncio.to_thread(self._check_sync)

    def _check_sync(self) -> None:
        statement = select(TenantModel.tenant_id).limit(1)
        with self._session_factory.begin() as session:
            session.execute(statement).first()
