"""Dialect-neutral SQLAlchemy ORM persistence for device pairing."""

from __future__ import annotations

import hmac
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from math import ceil
from uuid import UUID

from sqlalchemy import and_, select, update

from hermes_cloud.domain.persistence import PairingSessionState
from hermes_cloud.modules.device.domain import (
    DeviceAuthenticationBinding,
    DeviceAuthenticationChallenge,
    DeviceAuthenticationSnapshot,
    DeviceCredential,
    DeviceCredentialStatus,
    DeviceLifecycle,
    DeviceLifecycleState,
    PairingKeyMaterial,
    PairingOffer,
    PairingSession,
    PairingSnapshot,
)
from hermes_cloud.modules.device.ports import (
    ActivatePairingCommand,
    CancelPairingCommand,
    ClaimPairingCommand,
    ConfirmPairingCommand,
    ConsumeDeviceChallengeCommand,
    CreateDeviceChallengeCommand,
    DeviceAuthenticationUnavailable,
    DeviceAuthorizationRevoked,
    DeviceAuthorizationSuspended,
    PairingChallengeExpired,
    PairingChallengeReplayed,
    PairingClaimRateLimited,
    PairingClaimUnavailable,
    PairingExpired,
    PairingIdempotencyConflict,
    PairingMutation,
    PairingNotFound,
    PairingOfferAuthenticationFailed,
    PairingScopeUnavailable,
    PairingStateConflict,
    RevokeDeviceCommand,
)
from hermes_cloud.platform.postgres.models import (
    AgentModel,
    DeviceAuthenticationChallengeModel,
    DeviceCredentialModel,
    DeviceCredentialPublicKeyModel,
    DeviceLifecycleModel,
    DeviceModel,
    PairingClaimLimitModel,
    PairingEnrollmentProofModel,
    PairingIdempotencyModel,
    PairingOfferModel,
    PairingSessionModel,
    TenantModel,
    UserModel,
    WorkspaceMembershipModel,
    WorkspaceModel,
)
from hermes_cloud.platform.sqlalchemy.repositories.base import (
    SqlAlchemySessionRepositoryBase,
)


@dataclass(frozen=True, slots=True)
class PairingFailure:
    error: RuntimeError


PairingOutcome = PairingSnapshot | PairingFailure


class SqlAlchemyPairingRepositoryBase(SqlAlchemySessionRepositoryBase):
    """Use mapped entities and one caller-owned SQLAlchemy Session."""

    def create_offer(
        self,
        offer: PairingOffer,
        *,
        mutation: PairingMutation,
    ) -> PairingOffer:
        if mutation.operation != "create" or mutation.expected_revision != 0:
            raise ValueError("offer creation requires revision zero")
        replay = self._mutation_record(mutation)
        if replay is not None:
            return self._replay_offer(offer, mutation, replay)

        model = self._offer_model(offer.pairing_offer_id)
        if model is None:
            model = PairingOfferModel(
                pairing_offer_id=offer.pairing_offer_id,
                pairing_code_digest=offer.pairing_code_digest,
                bootstrap_secret_digest=offer.bootstrap_secret_digest,
                public_key_algorithm=offer.algorithm,
                public_key=offer.public_key,
                credential_fingerprint=offer.credential_fingerprint,
                key_id=offer.key_id,
                device_key=offer.device_key,
                device_name=offer.device_name,
                platform=offer.platform,
                connector_version=offer.connector_version,
                state=offer.state.value,
                revision=offer.revision,
                expires_at=offer.expires_at,
                claimed_at=offer.claimed_at,
                created_at=offer.created_at,
            )
            self._session.add(model)
            self._session.flush()
        elif pairing_offer(model) != offer:
            raise PairingStateConflict("pairing offer identity contains other content")

        self._session.add(
            PairingIdempotencyModel(
                pairing_mutation_id=mutation.pairing_mutation_id,
                pairing_offer_id=offer.pairing_offer_id,
                operation=mutation.operation,
                idempotency_key_digest=mutation.idempotency_key_digest,
                principal_digest=mutation.principal_digest,
                request_digest=mutation.request_digest,
                expected_revision=mutation.expected_revision,
                result_revision=offer.revision,
                result_state=offer.state.value,
                result_code="OK",
                created_at=mutation.created_at,
                expires_at=mutation.expires_at,
            )
        )
        self._session.flush()
        return pairing_offer(model)

    def claim_offer(
        self,
        command: ClaimPairingCommand,
        *,
        mutation: PairingMutation,
    ) -> PairingOutcome:
        self._require_mutation(mutation, "claim", command.expected_revision)
        replay = self._mutation_record(mutation)
        if replay is not None:
            return self._replay_outcome(mutation, replay)
        claim_limit = self._locked_claim_limit(
            tenant_id=command.tenant_id,
            owner_user_id=command.owner_user_id,
        )
        if (
            claim_limit is not None
            and claim_limit.window_expires_at > command.now
            and claim_limit.failed_attempts >= 5
        ):
            retry_after_seconds = _retry_after_seconds(
                claim_limit.window_expires_at,
                command.now,
            )
            return self._claim_failure_without_offer(
                mutation,
                "PAIRING_CLAIM_RATE_LIMITED",
                PairingClaimRateLimited(retry_after_seconds),
                retry_after_seconds=retry_after_seconds,
            )
        if claim_limit is not None and claim_limit.window_expires_at <= command.now:
            self._session.delete(claim_limit)
            self._session.flush()
            claim_limit = None
        offer_model = self._offer_model_by_code_digest(
            command.pairing_code_digest,
        )
        if offer_model is None:
            return self._record_failed_code_lookup(
                command,
                mutation,
                claim_limit,
            )
        if offer_model.expires_at <= command.now:
            return self._record_failed_code_lookup(
                command,
                mutation,
                claim_limit,
            )
        if not self._owner_scope_is_active(
            tenant_id=command.tenant_id,
            owner_user_id=command.owner_user_id,
            workspace_id=command.workspace_id,
            agent_id=command.agent_id,
        ):
            return self._failure(
                mutation,
                offer_model,
                "FORBIDDEN",
                PairingScopeUnavailable("owner scope is unavailable"),
            )
        if (
            offer_model.state != PairingSessionState.PENDING.value
            or offer_model.revision != command.expected_revision
        ):
            return self._record_failed_code_lookup(
                command,
                mutation,
                claim_limit,
            )
        pairing_offer_id: UUID = offer_model.pairing_offer_id
        claim_statement = (
            update(PairingOfferModel)
            .where(
                PairingOfferModel.pairing_offer_id == pairing_offer_id,
                PairingOfferModel.state == "pending",
                PairingOfferModel.revision == command.expected_revision,
                PairingOfferModel.pairing_code_digest == command.pairing_code_digest,
                PairingOfferModel.expires_at > command.now,
            )
            .values(
                state="claimed",
                revision=1,
                claimed_at=command.now,
            )
            .execution_options(synchronize_session=False)
        )
        claim_result = self._session.execute(claim_statement)
        if getattr(claim_result, "rowcount", None) != 1:
            return self._record_failed_code_lookup(
                command,
                mutation,
                claim_limit,
            )
        offer_model = self._required_offer_model(pairing_offer_id)
        if claim_limit is not None:
            self._session.delete(claim_limit)
        self._session.add(
            DeviceModel(
                tenant_id=command.tenant_id,
                device_id=command.device_id,
                agent_id=command.agent_id,
                workspace_id=command.workspace_id,
                device_key=offer_model.device_key,
                status="disabled",
                created_at=command.now,
            )
        )
        self._session.add(
            PairingSessionModel(
                tenant_id=command.tenant_id,
                pairing_session_id=command.pairing_session_id,
                workspace_id=command.workspace_id,
                agent_id=command.agent_id,
                device_id=command.device_id,
                pairing_code_digest=offer_model.pairing_code_digest,
                state=PairingSessionState.CLAIMED.value,
                failed_attempts=0,
                expires_at=offer_model.expires_at,
                claimed_at=command.now,
                confirmed_at=None,
                created_at=offer_model.created_at,
            )
        )
        self._session.flush()
        self._session.add(
            DeviceLifecycleModel(
                tenant_id=command.tenant_id,
                device_id=command.device_id,
                workspace_id=command.workspace_id,
                agent_id=command.agent_id,
                state=DeviceLifecycleState.PENDING.value,
                revision=0,
                updated_at=command.now,
            )
        )
        self._session.add(
            PairingEnrollmentProofModel(
                tenant_id=command.tenant_id,
                pairing_session_id=command.pairing_session_id,
                pairing_offer_id=pairing_offer_id,
                owner_user_id=command.owner_user_id,
                device_display_name=command.device_display_name,
                claim_id=mutation.pairing_mutation_id,
                scopes=list(command.scopes),
                challenge_id=None,
                challenge_digest=None,
                challenge_expires_at=None,
                owner_confirmed_at=None,
                confirmation_digest=None,
                revision=1,
                created_at=command.now,
                updated_at=command.now,
            )
        )
        self._session.flush()
        self._record_result(
            mutation,
            offer_model,
            result_state=PairingSessionState.CLAIMED.value,
            result_revision=1,
        )
        self._session.flush()
        return self._snapshot(pairing_offer_id)

    def confirm_owner(
        self,
        command: ConfirmPairingCommand,
        *,
        mutation: PairingMutation,
    ) -> PairingOutcome:
        self._require_mutation(mutation, "confirm", command.expected_revision)
        replay = self._mutation_record(mutation)
        if replay is not None:
            return self._replay_outcome(mutation, replay)
        snapshot = self._snapshot_by_session(
            command.tenant_id,
            command.pairing_session_id,
        )
        if (
            snapshot.session is None
            or snapshot.material is None
            or not self._owner_scope_is_active(
                tenant_id=command.tenant_id,
                owner_user_id=command.owner_user_id,
                workspace_id=snapshot.session.workspace_id,
                agent_id=snapshot.session.agent_id,
            )
        ):
            raise PairingScopeUnavailable("owner scope is unavailable")
        offer_model = self._required_offer_model(snapshot.offer.pairing_offer_id)
        if snapshot.session.expires_at <= command.now:
            return self._failure(
                mutation,
                offer_model,
                "PAIRING_EXPIRED",
                PairingExpired("pairing session is expired"),
            )
        if (
            snapshot.session.state is not PairingSessionState.CLAIMED
            or snapshot.material.revision != command.expected_revision
            or snapshot.offer.credential_fingerprint != command.credential_fingerprint
        ):
            return self._failure(
                mutation,
                offer_model,
                "PAIRING_STATE_CONFLICT",
                PairingStateConflict("owner confirmation conflicts"),
            )
        if command.challenge_expires_at > snapshot.session.expires_at:
            raise ValueError("challenge expiry exceeds pairing expiry")
        session_statement = (
            update(PairingSessionModel)
            .where(
                PairingSessionModel.tenant_id == command.tenant_id,
                PairingSessionModel.pairing_session_id == command.pairing_session_id,
                PairingSessionModel.state == "claimed",
            )
            .values(
                state="confirmed",
                confirmed_at=command.now,
            )
            .execution_options(synchronize_session=False)
        )
        proof_statement = (
            update(PairingEnrollmentProofModel)
            .where(
                PairingEnrollmentProofModel.tenant_id == command.tenant_id,
                PairingEnrollmentProofModel.pairing_session_id
                == command.pairing_session_id,
                PairingEnrollmentProofModel.owner_user_id == command.owner_user_id,
                PairingEnrollmentProofModel.revision == command.expected_revision,
                PairingEnrollmentProofModel.challenge_digest.is_(None),
            )
            .values(
                challenge_id=command.challenge_id,
                challenge_digest=command.challenge_digest,
                challenge_expires_at=command.challenge_expires_at,
                owner_confirmed_at=command.now,
                revision=2,
                updated_at=command.now,
            )
            .execution_options(synchronize_session=False)
        )
        self._require_one_row(
            self._session.execute(session_statement),
            "owner confirmation session",
        )
        self._require_one_row(
            self._session.execute(proof_statement),
            "owner confirmation proof",
        )
        self._record_result(
            mutation,
            offer_model,
            result_state=PairingSessionState.CONFIRMED.value,
            result_revision=2,
        )
        self._session.flush()
        return self._snapshot(snapshot.offer.pairing_offer_id)

    def activate_verified_credential(
        self,
        command: ActivatePairingCommand,
        *,
        mutation: PairingMutation,
    ) -> PairingOutcome:
        self._require_mutation(mutation, "proof", command.expected_revision)
        replay = self._mutation_record(mutation)
        if replay is not None:
            return self._replay_outcome(mutation, replay)
        snapshot = self._snapshot(command.pairing_offer_id)
        offer_model = self._required_offer_model(command.pairing_offer_id)
        if offer_model.bootstrap_secret_digest != command.bootstrap_secret_digest:
            return self._failure(
                mutation,
                offer_model,
                "UNAUTHORIZED",
                PairingStateConflict("pairing offer authentication failed"),
            )
        if (
            snapshot.session is None
            or snapshot.material is None
            or snapshot.lifecycle is None
            or snapshot.session.pairing_session_id != command.pairing_session_id
        ):
            raise PairingStateConflict("pairing binding is unavailable")
        if snapshot.material.revision >= 3:
            return self._failure(
                mutation,
                offer_model,
                "CHALLENGE_REPLAYED",
                PairingChallengeReplayed("device challenge was already consumed"),
            )
        if (
            snapshot.session.state is not PairingSessionState.CONFIRMED
            or snapshot.material.revision != command.expected_revision
            or snapshot.material.challenge_id != command.challenge_id
            or snapshot.material.challenge_digest != command.challenge_digest
            or snapshot.material.challenge_expires_at is None
            or snapshot.material.challenge_expires_at <= command.now
            or command.credential.device_id != snapshot.lifecycle.device_id
            or command.credential.credential_fingerprint
            != snapshot.offer.credential_fingerprint
            or command.credential.public_key != snapshot.offer.public_key
        ):
            return self._failure(
                mutation,
                offer_model,
                "CHALLENGE_INVALID",
                PairingStateConflict("verified device proof conflicts"),
            )
        proof_statement = (
            update(PairingEnrollmentProofModel)
            .where(
                PairingEnrollmentProofModel.tenant_id == command.tenant_id,
                PairingEnrollmentProofModel.pairing_session_id
                == command.pairing_session_id,
                PairingEnrollmentProofModel.revision == command.expected_revision,
                PairingEnrollmentProofModel.challenge_id == command.challenge_id,
                PairingEnrollmentProofModel.challenge_digest
                == command.challenge_digest,
                PairingEnrollmentProofModel.challenge_expires_at > command.now,
                PairingEnrollmentProofModel.confirmation_digest.is_(None),
            )
            .values(
                confirmation_digest=command.confirmation_digest,
                revision=3,
                updated_at=command.now,
            )
            .execution_options(synchronize_session=False)
        )
        lifecycle_statement = (
            update(DeviceLifecycleModel)
            .where(
                DeviceLifecycleModel.tenant_id == command.tenant_id,
                DeviceLifecycleModel.device_id == command.credential.device_id,
                DeviceLifecycleModel.state == "pending",
                DeviceLifecycleModel.revision == 0,
            )
            .values(
                state="active",
                revision=1,
                updated_at=command.now,
            )
            .execution_options(synchronize_session=False)
        )
        device_statement = (
            update(DeviceModel)
            .where(
                DeviceModel.tenant_id == command.tenant_id,
                DeviceModel.device_id == command.credential.device_id,
                DeviceModel.status == "disabled",
            )
            .values(status="active")
            .execution_options(synchronize_session=False)
        )
        self._require_one_row(self._session.execute(proof_statement), "proof")
        self._require_one_row(
            self._session.execute(lifecycle_statement),
            "device activation",
        )
        self._require_one_row(
            self._session.execute(device_statement),
            "device availability activation",
        )
        credential = command.credential
        self._session.add(
            DeviceCredentialModel(
                tenant_id=credential.tenant_id,
                credential_id=credential.credential_id,
                device_id=credential.device_id,
                credential_type="public_key",
                key_id=credential.key_id,
                credential_fingerprint=credential.credential_fingerprint,
                status=credential.status.value,
                issued_at=credential.issued_at,
                expires_at=credential.expires_at,
                revoked_at=credential.revoked_at,
            )
        )
        self._session.add(
            DeviceCredentialPublicKeyModel(
                tenant_id=credential.tenant_id,
                credential_id=credential.credential_id,
                algorithm=credential.algorithm,
                public_key=credential.public_key,
                credential_fingerprint=credential.credential_fingerprint,
                created_at=credential.issued_at,
            )
        )
        self._record_result(
            mutation,
            offer_model,
            result_state=DeviceLifecycleState.ACTIVE.value,
            result_revision=3,
        )
        self._session.flush()
        return self._snapshot(command.pairing_offer_id)

    def cancel_pairing(
        self,
        command: CancelPairingCommand,
        *,
        mutation: PairingMutation,
    ) -> PairingOutcome:
        self._require_mutation(mutation, "cancel", command.expected_revision)
        replay = self._mutation_record(mutation)
        if replay is not None:
            return self._replay_outcome(mutation, replay)
        snapshot = self._snapshot_by_session(
            command.tenant_id,
            command.pairing_session_id,
        )
        if (
            snapshot.session is None
            or snapshot.material is None
            or snapshot.lifecycle is None
            or snapshot.material.claimed_by_user_id != command.owner_user_id
        ):
            raise PairingScopeUnavailable("owner pairing scope is unavailable")
        offer_model = self._required_offer_model(snapshot.offer.pairing_offer_id)
        if (
            snapshot.material.revision != command.expected_revision
            or snapshot.session.state
            not in {
                PairingSessionState.CLAIMED,
                PairingSessionState.CONFIRMED,
            }
        ):
            return self._failure(
                mutation,
                offer_model,
                "PAIRING_STATE_CONFLICT",
                PairingStateConflict("pairing cancellation conflicts"),
            )
        session_statement = (
            update(PairingSessionModel)
            .where(
                PairingSessionModel.tenant_id == command.tenant_id,
                PairingSessionModel.pairing_session_id == command.pairing_session_id,
                PairingSessionModel.state == snapshot.session.state.value,
            )
            .values(state="cancelled")
            .execution_options(synchronize_session=False)
        )
        proof_statement = (
            update(PairingEnrollmentProofModel)
            .where(
                PairingEnrollmentProofModel.tenant_id == command.tenant_id,
                PairingEnrollmentProofModel.pairing_session_id
                == command.pairing_session_id,
                PairingEnrollmentProofModel.revision == command.expected_revision,
            )
            .values(
                revision=command.expected_revision + 1,
                updated_at=command.now,
            )
            .execution_options(synchronize_session=False)
        )
        self._require_one_row(
            self._session.execute(session_statement),
            "pairing cancellation",
        )
        self._require_one_row(
            self._session.execute(proof_statement),
            "pairing cancellation revision",
        )
        self._revoke_pending_lifecycle(
            command.tenant_id,
            snapshot.lifecycle.device_id,
            command.now,
        )
        self._record_result(
            mutation,
            offer_model,
            result_state=PairingSessionState.CANCELLED.value,
            result_revision=command.expected_revision + 1,
        )
        self._session.flush()
        return self._snapshot(snapshot.offer.pairing_offer_id)

    def expire_offer(
        self,
        pairing_offer_id: UUID,
        *,
        now: datetime,
    ) -> PairingOffer:
        if now.utcoffset() is None:
            raise ValueError("expiry time must include a timezone")
        model = self._required_offer_model(pairing_offer_id)
        if model.state == PairingSessionState.EXPIRED.value:
            return pairing_offer(model)
        if model.state != PairingSessionState.PENDING.value:
            raise PairingStateConflict("only pending offers can expire")
        if now < model.expires_at:
            raise PairingStateConflict("pairing offer is not due to expire")
        statement = (
            update(PairingOfferModel)
            .where(
                PairingOfferModel.pairing_offer_id == pairing_offer_id,
                PairingOfferModel.state == "pending",
                PairingOfferModel.revision == 0,
                PairingOfferModel.expires_at <= now,
            )
            .values(
                state="expired",
                revision=1,
            )
            .execution_options(synchronize_session=False)
        )
        self._require_one_row(self._session.execute(statement), "offer expiry")
        return pairing_offer(self._required_offer_model(pairing_offer_id))

    def revoke_device(
        self,
        command: RevokeDeviceCommand,
        *,
        mutation: PairingMutation,
    ) -> PairingOutcome:
        self._require_mutation(mutation, "revoke", mutation.expected_revision)
        replay = self._mutation_record(mutation)
        if replay is not None:
            return self._replay_outcome(mutation, replay)
        proof = self._proof_for_device(command.tenant_id, command.device_id)
        lifecycle_model = self._lifecycle_model(
            command.tenant_id,
            command.device_id,
        )
        if proof is None or lifecycle_model is None:
            raise PairingStateConflict("device pairing binding is unavailable")
        if proof.owner_user_id != command.owner_user_id:
            raise PairingScopeUnavailable("device owner scope is unavailable")
        pairing_offer_id: UUID = proof.pairing_offer_id
        offer_model = self._required_offer_model(pairing_offer_id)
        if lifecycle_model.revision != mutation.expected_revision:
            return self._failure(
                mutation,
                offer_model,
                "PAIRING_STATE_CONFLICT",
                PairingStateConflict("device revision conflicts"),
            )
        if lifecycle_model.state != DeviceLifecycleState.REVOKED.value:
            lifecycle_revision: int = lifecycle_model.revision
            lifecycle_statement = (
                update(DeviceLifecycleModel)
                .where(
                    DeviceLifecycleModel.tenant_id == command.tenant_id,
                    DeviceLifecycleModel.device_id == command.device_id,
                    DeviceLifecycleModel.state.in_(
                        (
                            "pending",
                            "active",
                            "suspended",
                        )
                    ),
                    DeviceLifecycleModel.revision == lifecycle_revision,
                )
                .values(
                    state="revoked",
                    revision=lifecycle_revision + 1,
                    updated_at=command.now,
                )
                .execution_options(synchronize_session=False)
            )
            self._require_one_row(
                self._session.execute(lifecycle_statement),
                "device revocation",
            )
            self._session.execute(
                update(DeviceModel)
                .where(
                    DeviceModel.tenant_id == command.tenant_id,
                    DeviceModel.device_id == command.device_id,
                )
                .values(status="disabled")
                .execution_options(synchronize_session=False)
            )
            self._session.execute(
                update(DeviceCredentialModel)
                .where(
                    DeviceCredentialModel.tenant_id == command.tenant_id,
                    DeviceCredentialModel.device_id == command.device_id,
                    DeviceCredentialModel.status == "active",
                )
                .values(
                    status="revoked",
                    revoked_at=command.now,
                )
                .execution_options(synchronize_session=False)
            )
        lifecycle_model = self._lifecycle_model(
            command.tenant_id,
            command.device_id,
        )
        assert lifecycle_model is not None
        self._record_result(
            mutation,
            offer_model,
            result_state=DeviceLifecycleState.REVOKED.value,
            result_revision=lifecycle_model.revision,
        )
        self._session.flush()
        return self._snapshot(pairing_offer_id)

    def get_offer(
        self,
        pairing_offer_id: UUID,
        *,
        bootstrap_secret_digest: str,
        now: datetime,
    ) -> PairingSnapshot:
        snapshot = self._authenticated_offer(
            pairing_offer_id,
            bootstrap_secret_digest=bootstrap_secret_digest,
        )
        if snapshot.offer.expires_at <= now:
            raise PairingExpired("pairing offer is expired")
        return snapshot

    def _authenticated_offer(
        self,
        pairing_offer_id: UUID,
        *,
        bootstrap_secret_digest: str,
    ) -> PairingSnapshot:
        offer = self._offer_model(pairing_offer_id)
        if offer is None:
            raise PairingNotFound("pairing offer is unavailable")
        if not hmac.compare_digest(
            offer.bootstrap_secret_digest,
            bootstrap_secret_digest,
        ):
            raise PairingOfferAuthenticationFailed(
                "pairing offer authentication failed"
            )
        return self._snapshot(pairing_offer_id)

    def replay_pairing_mutation(
        self,
        mutation: PairingMutation,
    ) -> PairingSnapshot | None:
        record = self._mutation_record(mutation)
        if record is None:
            return None
        outcome = self._replay_outcome(mutation, record)
        if isinstance(outcome, PairingFailure):
            raise outcome.error
        return outcome

    def get_pairing_for_proof(
        self,
        pairing_session_id: UUID,
        *,
        bootstrap_secret_digest: str,
        now: datetime,
    ) -> PairingSnapshot:
        snapshot = self.get_pairing_for_proof_history(
            pairing_session_id,
            bootstrap_secret_digest=bootstrap_secret_digest,
        )
        if snapshot.offer.expires_at <= now:
            raise PairingExpired("pairing offer is expired")
        return snapshot

    def get_pairing_for_proof_history(
        self,
        pairing_session_id: UUID,
        *,
        bootstrap_secret_digest: str,
    ) -> PairingSnapshot:
        proof = self._session.execute(
            select(PairingEnrollmentProofModel)
            .where(
                PairingEnrollmentProofModel.pairing_session_id == pairing_session_id,
            )
            .limit(1)
        ).scalar_one_or_none()
        if proof is None:
            raise PairingNotFound("pairing proof is unavailable")
        snapshot = self._authenticated_offer(
            proof.pairing_offer_id,
            bootstrap_secret_digest=bootstrap_secret_digest,
        )
        if (
            snapshot.session is None
            or snapshot.session.pairing_session_id != pairing_session_id
        ):
            raise PairingStateConflict("pairing proof binding is unavailable")
        return snapshot

    def get_owner_pairing(
        self,
        *,
        tenant_id: UUID,
        owner_user_id: UUID,
        pairing_session_id: UUID,
        now: datetime,
    ) -> PairingSnapshot:
        snapshot = self._snapshot_by_session(tenant_id, pairing_session_id)
        if (
            snapshot.material is None
            or snapshot.material.claimed_by_user_id != owner_user_id
            or snapshot.session is None
        ):
            raise PairingScopeUnavailable("owner pairing scope is unavailable")
        if snapshot.session.expires_at <= now:
            raise PairingExpired("pairing session is expired")
        return snapshot

    def get_owner_pairing_status(
        self,
        *,
        tenant_id: UUID,
        owner_user_id: UUID,
        pairing_session_id: UUID,
    ) -> PairingSnapshot:
        proof = self._session.execute(
            select(PairingEnrollmentProofModel)
            .where(
                PairingEnrollmentProofModel.tenant_id == tenant_id,
                PairingEnrollmentProofModel.pairing_session_id == pairing_session_id,
            )
            .limit(1)
        ).scalar_one_or_none()
        if proof is None:
            raise PairingNotFound("pairing session is unavailable")
        snapshot = self._snapshot(proof.pairing_offer_id)
        if (
            snapshot.material is None
            or snapshot.material.claimed_by_user_id != owner_user_id
            or snapshot.session is None
        ):
            raise PairingScopeUnavailable("owner pairing scope is unavailable")
        return snapshot

    def create_device_challenge(
        self,
        command: CreateDeviceChallengeCommand,
        *,
        mutation: PairingMutation,
    ) -> DeviceAuthenticationSnapshot:
        self._require_mutation(mutation, "device_challenge", 0)
        challenge = command.challenge
        binding = self.active_device_binding(
            tenant_id=challenge.tenant_id,
            device_id=challenge.device_id,
            credential_id=challenge.credential_id,
            now=command.now,
        ).binding
        replay = self._mutation_record(mutation)
        if replay is not None:
            if replay.request_digest != mutation.request_digest:
                raise PairingIdempotencyConflict(
                    "idempotency key is bound to another request"
                )
            model = self._session.get(
                DeviceAuthenticationChallengeModel,
                (binding.tenant_id, challenge.challenge_id),
                populate_existing=True,
            )
            if model is None or model.pairing_mutation_id != replay.pairing_mutation_id:
                raise PairingStateConflict("idempotent device challenge is unavailable")
            return DeviceAuthenticationSnapshot(
                binding=binding,
                challenge=device_authentication_challenge(model),
            )
        proof = self._proof_for_device(binding.tenant_id, binding.device_id)
        if proof is None:
            raise DeviceAuthenticationUnavailable(
                "device authentication binding is unavailable"
            )
        offer = self._required_offer_model(proof.pairing_offer_id)
        self._record_result(
            mutation,
            offer,
            result_state=DeviceLifecycleState.ACTIVE.value,
            result_revision=binding.lifecycle_revision,
        )
        self._session.flush()
        self._session.add(
            DeviceAuthenticationChallengeModel(
                tenant_id=challenge.tenant_id,
                challenge_id=challenge.challenge_id,
                device_id=challenge.device_id,
                credential_id=challenge.credential_id,
                pairing_mutation_id=mutation.pairing_mutation_id,
                challenge_digest=challenge.challenge_digest,
                proof_digest=None,
                issued_at=challenge.issued_at,
                expires_at=challenge.expires_at,
                consumed_at=None,
                revision=0,
            )
        )
        self._session.flush()
        return DeviceAuthenticationSnapshot(
            binding=binding,
            challenge=challenge,
        )

    def consume_device_challenge(
        self,
        command: ConsumeDeviceChallengeCommand,
        *,
        mutation: PairingMutation,
    ) -> DeviceAuthenticationSnapshot:
        self._require_mutation(mutation, "device_token", 0)
        active = self.active_device_binding(
            tenant_id=None,
            device_id=command.device_id,
            credential_id=command.credential_id,
            now=command.now,
        )
        binding = active.binding
        challenge_model = self._session.get(
            DeviceAuthenticationChallengeModel,
            (binding.tenant_id, command.challenge_id),
            populate_existing=True,
        )
        replay = self._mutation_record(mutation)
        if replay is not None:
            if replay.request_digest != mutation.request_digest:
                raise PairingIdempotencyConflict(
                    "idempotency key is bound to another request"
                )
            if challenge_model is None or challenge_model.consumed_at is None:
                raise PairingStateConflict("idempotent device proof is unavailable")
            return DeviceAuthenticationSnapshot(
                binding=binding,
                challenge=device_authentication_challenge(challenge_model),
            )
        if (
            challenge_model is None
            or challenge_model.device_id != command.device_id
            or challenge_model.credential_id != command.credential_id
            or not hmac.compare_digest(
                challenge_model.challenge_digest,
                command.challenge_digest,
            )
        ):
            raise DeviceAuthenticationUnavailable(
                "device authentication challenge is unavailable"
            )
        if challenge_model.consumed_at is not None or challenge_model.revision != 0:
            raise PairingChallengeReplayed("device authentication challenge replayed")
        if challenge_model.expires_at <= command.now:
            raise PairingChallengeExpired("device authentication challenge expired")
        statement = (
            update(DeviceAuthenticationChallengeModel)
            .where(
                DeviceAuthenticationChallengeModel.tenant_id == binding.tenant_id,
                DeviceAuthenticationChallengeModel.challenge_id == command.challenge_id,
                DeviceAuthenticationChallengeModel.device_id == command.device_id,
                DeviceAuthenticationChallengeModel.credential_id
                == command.credential_id,
                DeviceAuthenticationChallengeModel.challenge_digest
                == command.challenge_digest,
                DeviceAuthenticationChallengeModel.expires_at > command.now,
                DeviceAuthenticationChallengeModel.consumed_at.is_(None),
                DeviceAuthenticationChallengeModel.revision == 0,
            )
            .values(
                proof_digest=command.proof_digest,
                consumed_at=command.now,
                revision=1,
            )
            .execution_options(synchronize_session=False)
        )
        self._require_one_row(
            self._session.execute(statement),
            "device challenge consumption",
        )
        proof = self._proof_for_device(binding.tenant_id, binding.device_id)
        if proof is None:
            raise DeviceAuthenticationUnavailable(
                "device authentication binding is unavailable"
            )
        offer = self._required_offer_model(proof.pairing_offer_id)
        self._record_result(
            mutation,
            offer,
            result_state=DeviceLifecycleState.ACTIVE.value,
            result_revision=binding.lifecycle_revision,
        )
        self._session.flush()
        consumed = self._session.get(
            DeviceAuthenticationChallengeModel,
            (binding.tenant_id, command.challenge_id),
            populate_existing=True,
        )
        assert consumed is not None
        return DeviceAuthenticationSnapshot(
            binding=binding,
            challenge=device_authentication_challenge(consumed),
        )

    def active_device_binding(
        self,
        *,
        tenant_id: UUID | None,
        device_id: UUID,
        credential_id: UUID,
        now: datetime,
    ) -> DeviceAuthenticationSnapshot:
        query = self._session.query(
            DeviceCredentialModel,
            DeviceCredentialPublicKeyModel,
            DeviceLifecycleModel,
            PairingEnrollmentProofModel,
        )
        query = query.join(
            DeviceCredentialPublicKeyModel,
            and_(
                DeviceCredentialPublicKeyModel.tenant_id
                == DeviceCredentialModel.tenant_id,
                DeviceCredentialPublicKeyModel.credential_id
                == DeviceCredentialModel.credential_id,
            ),
        )
        query = query.join(
            DeviceLifecycleModel,
            and_(
                DeviceLifecycleModel.tenant_id == DeviceCredentialModel.tenant_id,
                DeviceLifecycleModel.device_id == DeviceCredentialModel.device_id,
            ),
        )
        query = query.join(
            TenantModel,
            TenantModel.tenant_id == DeviceLifecycleModel.tenant_id,
        )
        query = query.join(
            WorkspaceModel,
            and_(
                WorkspaceModel.tenant_id == DeviceLifecycleModel.tenant_id,
                WorkspaceModel.workspace_id == DeviceLifecycleModel.workspace_id,
            ),
        )
        query = query.join(
            AgentModel,
            and_(
                AgentModel.tenant_id == DeviceLifecycleModel.tenant_id,
                AgentModel.agent_id == DeviceLifecycleModel.agent_id,
                AgentModel.workspace_id == DeviceLifecycleModel.workspace_id,
            ),
        )
        query = query.join(
            PairingSessionModel,
            and_(
                PairingSessionModel.tenant_id == DeviceCredentialModel.tenant_id,
                PairingSessionModel.device_id == DeviceCredentialModel.device_id,
            ),
        )
        query = query.join(
            PairingEnrollmentProofModel,
            and_(
                PairingEnrollmentProofModel.tenant_id == PairingSessionModel.tenant_id,
                PairingEnrollmentProofModel.pairing_session_id
                == PairingSessionModel.pairing_session_id,
            ),
        )
        query = query.join(
            PairingOfferModel,
            PairingOfferModel.pairing_offer_id
            == PairingEnrollmentProofModel.pairing_offer_id,
        )
        query = query.filter(
            DeviceCredentialModel.device_id == device_id,
            DeviceCredentialModel.credential_id == credential_id,
            TenantModel.status == "active",
            WorkspaceModel.status == "active",
            AgentModel.status == "active",
            PairingSessionModel.workspace_id == DeviceLifecycleModel.workspace_id,
            PairingSessionModel.agent_id == DeviceLifecycleModel.agent_id,
            PairingSessionModel.state == PairingSessionState.CONFIRMED.value,
            PairingEnrollmentProofModel.confirmation_digest.is_not(None),
            PairingOfferModel.key_id == DeviceCredentialModel.key_id,
            PairingOfferModel.credential_fingerprint
            == DeviceCredentialModel.credential_fingerprint,
        )
        rows = query.all()
        authorized_rows = tuple(
            row for row in rows if tenant_id is None or row[0].tenant_id == tenant_id
        )
        if len(authorized_rows) != 1:
            raise DeviceAuthenticationUnavailable(
                "device authentication binding is unavailable"
            )
        row = authorized_rows[0]
        credential, public_key, lifecycle, proof = row
        if lifecycle.state == DeviceLifecycleState.SUSPENDED.value:
            raise DeviceAuthorizationSuspended("device authorization suspended")
        if lifecycle.state in {
            DeviceLifecycleState.REVOKED.value,
            DeviceLifecycleState.RETIRED.value,
        }:
            raise DeviceAuthorizationRevoked("device authorization revoked")
        if (
            lifecycle.state != DeviceLifecycleState.ACTIVE.value
            or credential.status != DeviceCredentialStatus.ACTIVE.value
            or (credential.expires_at is not None and credential.expires_at <= now)
        ):
            raise DeviceAuthenticationUnavailable(
                "device authentication binding is unavailable"
            )
        return DeviceAuthenticationSnapshot(
            binding=DeviceAuthenticationBinding(
                tenant_id=credential.tenant_id,
                device_id=credential.device_id,
                credential_id=credential.credential_id,
                workspace_id=lifecycle.workspace_id,
                agent_id=lifecycle.agent_id,
                scopes=tuple(proof.scopes),
                public_key=public_key.public_key,
                lifecycle_state=DeviceLifecycleState(lifecycle.state),
                lifecycle_revision=lifecycle.revision,
                credential_status=DeviceCredentialStatus(credential.status),
                credential_expires_at=credential.expires_at,
            )
        )

    def active_legacy_device_binding(
        self,
        *,
        tenant_id: UUID,
        device_id: UUID,
        now: datetime,
    ) -> DeviceAuthenticationSnapshot:
        credentials = self._session.query(DeviceCredentialModel).filter(
            DeviceCredentialModel.tenant_id == tenant_id,
            DeviceCredentialModel.device_id == device_id,
            DeviceCredentialModel.status == DeviceCredentialStatus.ACTIVE.value,
        )
        credentials = credentials.all()
        if len(credentials) != 1:
            raise DeviceAuthenticationUnavailable(
                "legacy device authentication binding is unavailable"
            )
        return self.active_device_binding(
            tenant_id=tenant_id,
            device_id=device_id,
            credential_id=credentials[0].credential_id,
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
        active = self.active_device_binding(
            tenant_id=None,
            device_id=device_id,
            credential_id=credential_id,
            now=now,
        )
        model = self._session.get(
            DeviceAuthenticationChallengeModel,
            (active.binding.tenant_id, challenge_id),
            populate_existing=True,
        )
        if (
            model is None
            or model.device_id != device_id
            or model.credential_id != credential_id
        ):
            raise DeviceAuthenticationUnavailable(
                "device authentication challenge is unavailable"
            )
        if model.consumed_at is None and model.expires_at <= now:
            raise PairingChallengeExpired("device authentication challenge expired")
        return DeviceAuthenticationSnapshot(
            binding=active.binding,
            challenge=device_authentication_challenge(model),
        )

    def _mutation_record(
        self,
        mutation: PairingMutation,
    ) -> PairingIdempotencyModel | None:
        statement = (
            select(PairingIdempotencyModel)
            .where(
                PairingIdempotencyModel.operation == mutation.operation,
                PairingIdempotencyModel.idempotency_key_digest
                == mutation.idempotency_key_digest,
                PairingIdempotencyModel.principal_digest == mutation.principal_digest,
            )
            .limit(1)
        )
        return self._session.execute(statement).scalar_one_or_none()

    def _offer_model(self, pairing_offer_id: UUID) -> PairingOfferModel | None:
        return self._session.get(
            PairingOfferModel,
            pairing_offer_id,
            populate_existing=True,
        )

    def _offer_model_by_code_digest(
        self,
        pairing_code_digest: str,
    ) -> PairingOfferModel | None:
        statement = (
            select(PairingOfferModel)
            .where(
                PairingOfferModel.pairing_code_digest == pairing_code_digest,
            )
            .limit(1)
        )
        return self._session.execute(statement).scalar_one_or_none()

    def _required_offer_model(self, pairing_offer_id: UUID) -> PairingOfferModel:
        model = self._offer_model(pairing_offer_id)
        if model is None:
            raise PairingStateConflict("pairing offer is unavailable")
        self._session.refresh(model)
        return model

    def _owner_scope_is_active(
        self,
        *,
        tenant_id: UUID,
        owner_user_id: UUID,
        workspace_id: UUID,
        agent_id: UUID,
    ) -> bool:
        statement = (
            select(UserModel.user_id)
            .join(
                TenantModel,
                TenantModel.tenant_id == UserModel.tenant_id,
            )
            .join(
                WorkspaceMembershipModel,
                and_(
                    WorkspaceMembershipModel.tenant_id == UserModel.tenant_id,
                    WorkspaceMembershipModel.user_id == UserModel.user_id,
                ),
            )
            .join(
                WorkspaceModel,
                and_(
                    WorkspaceModel.tenant_id == WorkspaceMembershipModel.tenant_id,
                    WorkspaceModel.workspace_id
                    == WorkspaceMembershipModel.workspace_id,
                ),
            )
            .join(
                AgentModel,
                and_(
                    AgentModel.tenant_id == WorkspaceModel.tenant_id,
                    AgentModel.workspace_id == WorkspaceModel.workspace_id,
                ),
            )
            .where(
                UserModel.tenant_id == tenant_id,
                UserModel.user_id == owner_user_id,
                UserModel.status == "active",
                TenantModel.status == "active",
                WorkspaceMembershipModel.workspace_id == workspace_id,
                WorkspaceMembershipModel.status == "active",
                WorkspaceModel.workspace_id == workspace_id,
                WorkspaceModel.status == "active",
                AgentModel.agent_id == agent_id,
                AgentModel.status == "active",
            )
            .limit(1)
        )
        return self._session.execute(statement).scalar_one_or_none() is not None

    def _locked_claim_limit(
        self,
        *,
        tenant_id: UUID,
        owner_user_id: UUID,
    ) -> PairingClaimLimitModel | None:
        owner = self._session.execute(
            select(UserModel.user_id)
            .where(
                UserModel.tenant_id == tenant_id,
                UserModel.user_id == owner_user_id,
                UserModel.status == "active",
            )
            .with_for_update()
            .limit(1)
        ).scalar_one_or_none()
        if owner is None:
            raise PairingScopeUnavailable("owner principal is unavailable")
        return self._session.get(
            PairingClaimLimitModel,
            (tenant_id, owner_user_id),
            populate_existing=True,
        )

    def _record_failed_code_lookup(
        self,
        command: ClaimPairingCommand,
        mutation: PairingMutation,
        claim_limit: PairingClaimLimitModel | None,
    ) -> PairingFailure:
        if claim_limit is None:
            failed_attempts = 1
            self._session.add(
                PairingClaimLimitModel(
                    tenant_id=command.tenant_id,
                    owner_user_id=command.owner_user_id,
                    failed_attempts=failed_attempts,
                    revision=0,
                    window_started_at=command.now,
                    window_expires_at=command.now + timedelta(minutes=5),
                    updated_at=command.now,
                )
            )
        else:
            current_attempts: int = claim_limit.failed_attempts
            current_revision: int = claim_limit.revision
            window_expires_at: datetime = claim_limit.window_expires_at
            failed_attempts = current_attempts + 1
            statement = (
                update(PairingClaimLimitModel)
                .where(
                    PairingClaimLimitModel.tenant_id == command.tenant_id,
                    PairingClaimLimitModel.owner_user_id == command.owner_user_id,
                    PairingClaimLimitModel.revision == current_revision,
                    PairingClaimLimitModel.failed_attempts == current_attempts,
                    PairingClaimLimitModel.window_expires_at == window_expires_at,
                    PairingClaimLimitModel.window_expires_at > command.now,
                )
                .values(
                    failed_attempts=failed_attempts,
                    revision=current_revision + 1,
                    updated_at=command.now,
                )
                .execution_options(synchronize_session=False)
            )
            self._require_one_row(
                self._session.execute(statement),
                "claim failure limit",
            )
        if failed_attempts >= 5:
            window_expires_at = (
                command.now + timedelta(minutes=5)
                if claim_limit is None
                else claim_limit.window_expires_at
            )
            retry_after_seconds = _retry_after_seconds(
                window_expires_at,
                command.now,
            )
            return self._claim_failure_without_offer(
                mutation,
                "PAIRING_CLAIM_RATE_LIMITED",
                PairingClaimRateLimited(retry_after_seconds),
                retry_after_seconds=retry_after_seconds,
            )
        return self._claim_failure_without_offer(
            mutation,
            "PAIRING_CLAIM_UNAVAILABLE",
            PairingClaimUnavailable("pairing claim unavailable"),
        )

    def _claim_failure_without_offer(
        self,
        mutation: PairingMutation,
        result_code: str,
        error: RuntimeError,
        *,
        retry_after_seconds: int | None = None,
    ) -> PairingFailure:
        self._record_result(
            mutation,
            None,
            result_state=PairingSessionState.PENDING.value,
            result_revision=0,
            result_code=result_code,
            retry_after_seconds=retry_after_seconds,
        )
        self._session.flush()
        return PairingFailure(error)

    def _snapshot_by_session(
        self,
        tenant_id: UUID,
        pairing_session_id: UUID,
    ) -> PairingSnapshot:
        proof = self._session.execute(
            select(PairingEnrollmentProofModel)
            .where(
                PairingEnrollmentProofModel.tenant_id == tenant_id,
                PairingEnrollmentProofModel.pairing_session_id == pairing_session_id,
            )
            .limit(1)
        ).scalar_one_or_none()
        if proof is None:
            raise PairingStateConflict("pairing session binding is unavailable")
        pairing_offer_id: UUID = proof.pairing_offer_id
        return self._snapshot(pairing_offer_id)

    def _snapshot(self, pairing_offer_id: UUID) -> PairingSnapshot:
        offer_model = self._required_offer_model(pairing_offer_id)
        self._session.refresh(
            offer_model,
            attribute_names=("enrollment_proof",),
        )
        proof = offer_model.enrollment_proof
        if proof is None:
            return PairingSnapshot(offer=pairing_offer(offer_model))
        tenant_id: UUID = proof.tenant_id
        pairing_session_id: UUID = proof.pairing_session_id
        session_model = self._session.get(
            PairingSessionModel,
            (tenant_id, pairing_session_id),
            populate_existing=True,
        )
        if session_model is None or session_model.device_id is None:
            raise PairingStateConflict("pairing session row is unavailable")
        device_id: UUID = session_model.device_id
        device_model = self._session.get(
            DeviceModel,
            (tenant_id, device_id),
            populate_existing=True,
        )
        lifecycle_model = self._lifecycle_model(
            tenant_id,
            device_id,
        )
        if device_model is None or lifecycle_model is None:
            raise PairingStateConflict("pairing device rows are unavailable")
        credential_fingerprint: str = offer_model.credential_fingerprint
        credential_model = self._session.execute(
            select(DeviceCredentialModel)
            .where(
                DeviceCredentialModel.tenant_id == tenant_id,
                DeviceCredentialModel.device_id == device_id,
                DeviceCredentialModel.credential_fingerprint == credential_fingerprint,
            )
            .limit(1)
            .execution_options(populate_existing=True)
        ).scalar_one_or_none()
        credential = (
            None
            if credential_model is None
            else self._device_credential(credential_model)
        )
        return PairingSnapshot(
            offer=pairing_offer(offer_model),
            session=pairing_session(session_model),
            material=pairing_key_material(offer_model, proof, device_model),
            lifecycle=device_lifecycle(lifecycle_model),
            credential=credential,
        )

    def _device_credential(
        self,
        model: DeviceCredentialModel,
    ) -> DeviceCredential:
        public_key = self._session.get(
            DeviceCredentialPublicKeyModel,
            (model.tenant_id, model.credential_id),
            populate_existing=True,
        )
        if public_key is None:
            raise PairingStateConflict("device credential public key is unavailable")
        return DeviceCredential(
            tenant_id=model.tenant_id,
            credential_id=model.credential_id,
            device_id=model.device_id,
            algorithm=public_key.algorithm,
            key_id=model.key_id,
            public_key=public_key.public_key,
            credential_fingerprint=model.credential_fingerprint,
            status=DeviceCredentialStatus(model.status),
            issued_at=model.issued_at,
            expires_at=model.expires_at,
            revoked_at=model.revoked_at,
        )

    def _proof_for_device(
        self,
        tenant_id: UUID,
        device_id: UUID,
    ) -> PairingEnrollmentProofModel | None:
        statement = (
            select(PairingEnrollmentProofModel)
            .join(
                PairingSessionModel,
                and_(
                    PairingSessionModel.tenant_id
                    == PairingEnrollmentProofModel.tenant_id,
                    PairingSessionModel.pairing_session_id
                    == PairingEnrollmentProofModel.pairing_session_id,
                ),
            )
            .where(
                PairingEnrollmentProofModel.tenant_id == tenant_id,
                PairingSessionModel.device_id == device_id,
            )
            .limit(1)
        )
        return self._session.execute(statement).scalar_one_or_none()

    def _lifecycle_model(
        self,
        tenant_id: UUID,
        device_id: UUID,
    ) -> DeviceLifecycleModel | None:
        return self._session.get(
            DeviceLifecycleModel,
            (tenant_id, device_id),
            populate_existing=True,
        )

    def _revoke_pending_lifecycle(
        self,
        tenant_id: UUID,
        device_id: UUID,
        now: datetime,
    ) -> None:
        lifecycle = self._lifecycle_model(tenant_id, device_id)
        if lifecycle is None:
            raise PairingStateConflict("device lifecycle is unavailable")
        if lifecycle.state == DeviceLifecycleState.REVOKED.value:
            return
        lifecycle_revision: int = lifecycle.revision
        statement = (
            update(DeviceLifecycleModel)
            .where(
                DeviceLifecycleModel.tenant_id == tenant_id,
                DeviceLifecycleModel.device_id == device_id,
                DeviceLifecycleModel.state == "pending",
                DeviceLifecycleModel.revision == lifecycle_revision,
            )
            .values(
                state="revoked",
                revision=lifecycle_revision + 1,
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        self._require_one_row(self._session.execute(statement), "lifecycle revoke")
        self._session.execute(
            update(DeviceModel)
            .where(
                DeviceModel.tenant_id == tenant_id,
                DeviceModel.device_id == device_id,
            )
            .values(status="disabled")
            .execution_options(synchronize_session=False)
        )

    def _require_mutation(
        self,
        mutation: PairingMutation,
        operation: str,
        expected_revision: int,
    ) -> None:
        if (
            mutation.operation != operation
            or mutation.expected_revision != expected_revision
        ):
            raise ValueError("pairing mutation does not match the operation")

    def _record_result(
        self,
        mutation: PairingMutation,
        offer: PairingOfferModel | None,
        *,
        result_state: str,
        result_revision: int,
        result_code: str = "OK",
        retry_after_seconds: int | None = None,
    ) -> None:
        self._session.add(
            PairingIdempotencyModel(
                pairing_mutation_id=mutation.pairing_mutation_id,
                pairing_offer_id=(None if offer is None else offer.pairing_offer_id),
                operation=mutation.operation,
                idempotency_key_digest=mutation.idempotency_key_digest,
                principal_digest=mutation.principal_digest,
                request_digest=mutation.request_digest,
                expected_revision=mutation.expected_revision,
                result_revision=result_revision,
                result_state=result_state,
                result_code=result_code,
                retry_after_seconds=retry_after_seconds,
                created_at=mutation.created_at,
                expires_at=mutation.expires_at,
            )
        )

    def _failure(
        self,
        mutation: PairingMutation,
        offer: PairingOfferModel,
        result_code: str,
        error: RuntimeError,
    ) -> PairingFailure:
        self._record_result(
            mutation,
            offer,
            result_state=offer.state,
            result_revision=offer.revision,
            result_code=result_code,
        )
        self._session.flush()
        return PairingFailure(error)

    def _replay_outcome(
        self,
        mutation: PairingMutation,
        record: PairingIdempotencyModel,
    ) -> PairingOutcome:
        if record.request_digest != mutation.request_digest:
            raise PairingIdempotencyConflict(
                "idempotency key is bound to another request"
            )
        if record.result_code != "OK":
            return PairingFailure(
                _replayed_error(
                    record.result_code,
                    retry_after_seconds=record.retry_after_seconds,
                )
            )
        if record.pairing_offer_id is None:
            raise PairingStateConflict("idempotent pairing result is unavailable")
        pairing_offer_id: UUID = record.pairing_offer_id
        return self._historical_snapshot(
            self._snapshot(pairing_offer_id),
            record,
        )

    @staticmethod
    def _historical_snapshot(
        current: PairingSnapshot,
        record: PairingIdempotencyModel,
    ) -> PairingSnapshot:
        if record.operation in {"cancel", "revoke"}:
            return current
        if (
            current.session is None
            or current.material is None
            or current.lifecycle is None
            or current.session.claimed_at is None
        ):
            raise PairingStateConflict("idempotent pairing result is unavailable")

        offer = replace(
            current.offer,
            state=PairingSessionState.CLAIMED,
            revision=1,
        )
        claimed_at = current.session.claimed_at
        pending_lifecycle = replace(
            current.lifecycle,
            state=DeviceLifecycleState.PENDING,
            revision=0,
            updated_at=claimed_at,
        )
        if record.operation == "claim":
            if (
                record.result_state != PairingSessionState.CLAIMED.value
                or record.result_revision != 1
            ):
                raise PairingStateConflict("idempotent pairing result conflicts")
            return PairingSnapshot(
                offer=offer,
                session=replace(
                    current.session,
                    state=PairingSessionState.CLAIMED,
                    confirmed_at=None,
                ),
                material=replace(
                    current.material,
                    challenge_id=None,
                    challenge_digest=None,
                    challenge_expires_at=None,
                    owner_confirmed_at=None,
                    confirmation_digest=None,
                    revision=1,
                    updated_at=claimed_at,
                ),
                lifecycle=pending_lifecycle,
                credential=None,
            )
        if record.operation == "confirm":
            owner_confirmed_at = current.material.owner_confirmed_at
            if (
                record.result_state != PairingSessionState.CONFIRMED.value
                or record.result_revision != 2
                or owner_confirmed_at is None
            ):
                raise PairingStateConflict("idempotent pairing result conflicts")
            return PairingSnapshot(
                offer=offer,
                session=replace(
                    current.session,
                    state=PairingSessionState.CONFIRMED,
                ),
                material=replace(
                    current.material,
                    confirmation_digest=None,
                    revision=2,
                    updated_at=owner_confirmed_at,
                ),
                lifecycle=pending_lifecycle,
                credential=None,
            )
        if record.operation == "proof":
            credential = current.credential
            if (
                record.result_state != DeviceLifecycleState.ACTIVE.value
                or record.result_revision != 3
                or credential is None
            ):
                raise PairingStateConflict("idempotent pairing result conflicts")
            return PairingSnapshot(
                offer=offer,
                session=replace(
                    current.session,
                    state=PairingSessionState.CONFIRMED,
                ),
                material=replace(
                    current.material,
                    revision=3,
                    updated_at=credential.issued_at,
                ),
                lifecycle=replace(
                    current.lifecycle,
                    state=DeviceLifecycleState.ACTIVE,
                    revision=1,
                    updated_at=credential.issued_at,
                ),
                credential=replace(
                    credential,
                    status=DeviceCredentialStatus.ACTIVE,
                    revoked_at=None,
                ),
            )
        raise PairingStateConflict("idempotent pairing result is unavailable")

    @staticmethod
    def _require_one_row(result: object, operation: str) -> None:
        if getattr(result, "rowcount", None) != 1:
            raise PairingStateConflict(f"pairing {operation} CAS failed")

    def _replay_offer(
        self,
        expected: PairingOffer,
        mutation: PairingMutation,
        record: PairingIdempotencyModel,
    ) -> PairingOffer:
        if record.request_digest != mutation.request_digest:
            raise PairingIdempotencyConflict(
                "idempotency key is bound to another request"
            )
        if record.pairing_offer_id is None:
            raise PairingStateConflict("idempotent pairing result is unavailable")
        pairing_offer_id: UUID = record.pairing_offer_id
        model = self._offer_model(pairing_offer_id)
        if model is None:
            raise PairingStateConflict("idempotent pairing result is unavailable")
        offer = pairing_offer(model)
        if (
            record.result_code != "OK"
            or offer.pairing_offer_id != expected.pairing_offer_id
            or offer.pairing_code_digest != expected.pairing_code_digest
            or offer.bootstrap_secret_digest != expected.bootstrap_secret_digest
            or offer.algorithm != expected.algorithm
            or offer.public_key != expected.public_key
            or offer.credential_fingerprint != expected.credential_fingerprint
            or offer.key_id != expected.key_id
            or offer.device_key != expected.device_key
            or offer.device_name != expected.device_name
            or offer.platform != expected.platform
            or offer.connector_version != expected.connector_version
        ):
            raise PairingStateConflict("idempotent pairing result conflicts")
        return replace(
            offer,
            state=PairingSessionState.PENDING,
            revision=0,
            claimed_at=None,
        )


def pairing_offer(model: PairingOfferModel) -> PairingOffer:
    return PairingOffer(
        pairing_offer_id=model.pairing_offer_id,
        pairing_code_digest=model.pairing_code_digest,
        bootstrap_secret_digest=model.bootstrap_secret_digest,
        algorithm=model.public_key_algorithm,
        public_key=model.public_key,
        credential_fingerprint=model.credential_fingerprint,
        key_id=model.key_id,
        device_key=model.device_key,
        device_name=model.device_name,
        platform=model.platform,
        connector_version=model.connector_version,
        state=PairingSessionState(model.state),
        revision=model.revision,
        expires_at=model.expires_at,
        claimed_at=model.claimed_at,
        created_at=model.created_at,
    )


def pairing_session(model: PairingSessionModel) -> PairingSession:
    return PairingSession(
        tenant_id=model.tenant_id,
        pairing_session_id=model.pairing_session_id,
        workspace_id=model.workspace_id,
        agent_id=model.agent_id,
        device_id=model.device_id,
        pairing_code_digest=model.pairing_code_digest,
        state=PairingSessionState(model.state),
        failed_attempts=model.failed_attempts,
        expires_at=model.expires_at,
        claimed_at=model.claimed_at,
        confirmed_at=model.confirmed_at,
        created_at=model.created_at,
    )


def pairing_key_material(
    offer: PairingOfferModel,
    proof: PairingEnrollmentProofModel,
    device: DeviceModel,
) -> PairingKeyMaterial:
    return PairingKeyMaterial(
        tenant_id=proof.tenant_id,
        pairing_session_id=proof.pairing_session_id,
        algorithm=offer.public_key_algorithm,
        public_key=offer.public_key,
        credential_fingerprint=offer.credential_fingerprint,
        key_id=offer.key_id,
        device_key=device.device_key,
        device_name=proof.device_display_name,
        platform=offer.platform,
        scopes=tuple(proof.scopes),
        claim_id=proof.claim_id,
        claimed_by_user_id=proof.owner_user_id,
        challenge_id=proof.challenge_id,
        challenge_digest=proof.challenge_digest,
        challenge_expires_at=proof.challenge_expires_at,
        owner_confirmed_at=proof.owner_confirmed_at,
        confirmation_digest=proof.confirmation_digest,
        revision=proof.revision,
        created_at=proof.created_at,
        updated_at=proof.updated_at,
    )


def device_lifecycle(model: DeviceLifecycleModel) -> DeviceLifecycle:
    return DeviceLifecycle(
        tenant_id=model.tenant_id,
        device_id=model.device_id,
        workspace_id=model.workspace_id,
        agent_id=model.agent_id,
        state=DeviceLifecycleState(model.state),
        revision=model.revision,
        updated_at=model.updated_at,
    )


def device_authentication_challenge(
    model: DeviceAuthenticationChallengeModel,
) -> DeviceAuthenticationChallenge:
    return DeviceAuthenticationChallenge(
        tenant_id=model.tenant_id,
        challenge_id=model.challenge_id,
        device_id=model.device_id,
        credential_id=model.credential_id,
        challenge_digest=model.challenge_digest,
        issued_at=model.issued_at,
        expires_at=model.expires_at,
        consumed_at=model.consumed_at,
    )


def _retry_after_seconds(expires_at: datetime, now: datetime) -> int:
    remaining = ceil((expires_at - now).total_seconds())
    return max(1, min(300, remaining))


def _replayed_error(
    result_code: str,
    *,
    retry_after_seconds: int | None,
) -> RuntimeError:
    if result_code == "PAIRING_CLAIM_UNAVAILABLE":
        return PairingClaimUnavailable("pairing claim unavailable")
    if result_code == "PAIRING_CLAIM_RATE_LIMITED":
        if retry_after_seconds is None:
            return PairingStateConflict("claim retry-after result is unavailable")
        return PairingClaimRateLimited(retry_after_seconds)
    if result_code == "FORBIDDEN":
        return PairingScopeUnavailable("owner scope is unavailable")
    if result_code == "PAIRING_EXPIRED":
        return PairingExpired("pairing is expired")
    if result_code == "CHALLENGE_REPLAYED":
        return PairingChallengeReplayed("device challenge was already consumed")
    return PairingStateConflict("pairing mutation previously failed")
