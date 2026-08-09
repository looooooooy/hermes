"""Recovery semantics for re-pairing a previously revoked device identity."""

from __future__ import annotations

from sqlalchemy import select, update

from hermes_cloud.domain.persistence import PairingSessionState
from hermes_cloud.modules.device.domain import DeviceLifecycleState
from hermes_cloud.modules.device.ports import (
    ClaimPairingCommand,
    PairingMutation,
    PairingStateConflict,
)
from hermes_cloud.platform.postgres.models import DeviceModel
from hermes_cloud.platform.sqlalchemy.repositories.device import (
    PairingOutcome,
    SqlAlchemyPairingRepositoryBase,
)


class RecoverablePairingRepositoryBase(SqlAlchemyPairingRepositoryBase):
    """Allow a revoked local device identity to be paired again safely.

    A cancelled/failed pairing intentionally keeps its historical device row for
    audit.  ``devices.device_key`` is nevertheless a live uniqueness key.  If a
    later pairing offer from the same Connector instance tries to claim again,
    leaving the revoked row on the live key would turn the retry into a database
    ``IntegrityError`` and an HTTP 500.  Release only revoked/retired rows after
    the new claim has already passed code, owner-scope, expiry and revision
    checks; the original Connector identity remains preserved on PairingOffer.
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
            self._release_revoked_device_key(
                tenant_id=command.tenant_id,
                device_key=offer.device_key,
            )
        return super().claim_offer(command, mutation=mutation)

    def _release_revoked_device_key(
        self,
        *,
        tenant_id,
        device_key: str,
    ) -> None:
        existing = self._session.execute(
            select(DeviceModel)
            .where(
                DeviceModel.tenant_id == tenant_id,
                DeviceModel.device_key == device_key,
            )
            .limit(1)
        ).scalar_one_or_none()
        if existing is None:
            return

        lifecycle = self._lifecycle_model(tenant_id, existing.device_id)
        if (
            lifecycle is None
            or lifecycle.state
            not in {
                DeviceLifecycleState.REVOKED.value,
                DeviceLifecycleState.RETIRED.value,
            }
            or existing.status != "disabled"
        ):
            raise PairingStateConflict("device identity is already bound")

        tombstone = f"retired:{existing.device_id}"
        result = self._session.execute(
            update(DeviceModel)
            .where(
                DeviceModel.tenant_id == tenant_id,
                DeviceModel.device_id == existing.device_id,
                DeviceModel.device_key == device_key,
                DeviceModel.status == "disabled",
            )
            .values(device_key=tombstone)
            .execution_options(synchronize_session=False)
        )
        if getattr(result, "rowcount", None) != 1:
            raise PairingStateConflict("revoked device identity could not be released")
        self._session.flush()


__all__ = ("RecoverablePairingRepositoryBase",)
