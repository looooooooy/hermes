"""Recovery semantics for re-pairing a previously unfinished device identity."""

from __future__ import annotations

from hermes_cloud.domain.persistence import PairingSessionState
from hermes_cloud.modules.device.domain import DeviceLifecycleState
from hermes_cloud.modules.device.ports import (
    ClaimPairingCommand,
    PairingMutation,
    PairingStateConflict,
)
from hermes_cloud.platform.postgres.models import DeviceModel, PairingSessionModel
from hermes_cloud.platform.sqlalchemy.repositories.device import (
    PairingOutcome,
    SqlAlchemyPairingRepositoryBase,
)


class RecoverablePairingRepositoryBase(SqlAlchemyPairingRepositoryBase):
    """Allow one device identity to safely recover from incomplete enrollment.

    ``devices.device_key`` is a live uniqueness key derived from the Connector
    instance identity. Historical device rows are intentionally retained for
    audit, so a retry must release that live key before inserting the replacement
    row. Two recovery cases are allowed:

    * revoked/retired historical bindings; and
    * an abandoned, never-activated pending binding for the exact same owner,
      workspace, Agent and Ed25519 device identity.

    Active/suspended devices, bindings with any issued credential, scope changes,
    owner changes, or key/fingerprint changes remain hard conflicts.
    """

    def claim_offer(
        self,
        command: ClaimPairingCommand,
        *,
        mutation: PairingMutation,
    ) -> PairingOutcome:
        offer = self._offer_model_by_code_digest(command.pairing_code_digest)
        if (
            offer is not None
            and offer.expires_at > command.now
            and offer.state == PairingSessionState.PENDING.value
            and offer.revision == command.expected_revision
            and self._owner_scope_is_active(
                tenant_id=command.tenant_id,
                owner_user_id=command.owner_user_id,
                workspace_id=command.workspace_id,
                agent_id=command.agent_id,
            )
        ):
            self._release_recoverable_device_key(
                command=command,
                device_key=offer.device_key,
                credential_fingerprint=offer.credential_fingerprint,
                public_key=offer.public_key,
            )
        return super().claim_offer(command, mutation=mutation)

    def _release_recoverable_device_key(
        self,
        *,
        command: ClaimPairingCommand,
        device_key: str,
        credential_fingerprint: str,
        public_key: bytes,
    ) -> None:
        rows = (
            self._session.query(DeviceModel)
            .filter(
                DeviceModel.tenant_id == command.tenant_id,
                DeviceModel.device_key == device_key,
            )
            .limit(2)
            .all()
        )
        if not rows:
            return
        if len(rows) != 1:
            raise PairingStateConflict("device identity is ambiguous")
        existing = rows[0]

        lifecycle = self._lifecycle_model(command.tenant_id, existing.device_id)
        if lifecycle is None or existing.status != "disabled":
            raise PairingStateConflict("device identity is already bound")

        if lifecycle.state in {
            DeviceLifecycleState.REVOKED.value,
            DeviceLifecycleState.RETIRED.value,
        }:
            self._tombstone_device_key(existing)
            return

        if lifecycle.state != DeviceLifecycleState.PENDING.value:
            raise PairingStateConflict("device identity is already bound")

        proof = self._proof_for_device(command.tenant_id, existing.device_id)
        if proof is None:
            raise PairingStateConflict("pending device pairing is unavailable")
        snapshot = self._snapshot(proof.pairing_offer_id)
        if (
            snapshot.session is None
            or snapshot.material is None
            or snapshot.lifecycle is None
            or snapshot.credential is not None
            or snapshot.lifecycle.device_id != existing.device_id
            or snapshot.lifecycle.workspace_id != command.workspace_id
            or snapshot.lifecycle.agent_id != command.agent_id
            or snapshot.session.workspace_id != command.workspace_id
            or snapshot.session.agent_id != command.agent_id
            or snapshot.material.claimed_by_user_id != command.owner_user_id
            or snapshot.session.state
            not in {
                PairingSessionState.CLAIMED,
                PairingSessionState.CONFIRMED,
            }
            or snapshot.offer.device_key != device_key
            or snapshot.offer.credential_fingerprint != credential_fingerprint
            or snapshot.offer.public_key != public_key
        ):
            raise PairingStateConflict("device identity is already bound")

        # The previous attempt never activated or issued a device credential. Mark
        # that enrollment as abandoned inside the new claim transaction, then
        # release only its live uniqueness key. Historical rows remain queryable.
        session_model = self._session.get(
            PairingSessionModel,
            (command.tenant_id, snapshot.session.pairing_session_id),
            populate_existing=True,
        )
        if session_model is None:
            raise PairingStateConflict("pending pairing session is unavailable")
        session_model.state = PairingSessionState.CANCELLED.value
        proof.revision = int(proof.revision) + 1
        proof.updated_at = command.now
        self._revoke_pending_lifecycle(
            command.tenant_id,
            existing.device_id,
            command.now,
        )
        self._tombstone_device_key(existing)
        self._session.flush()

    def _tombstone_device_key(self, existing: DeviceModel) -> None:
        existing.device_key = f"retired:{existing.device_id}"
        self._session.flush()


__all__ = ("RecoverablePairingRepositoryBase",)
