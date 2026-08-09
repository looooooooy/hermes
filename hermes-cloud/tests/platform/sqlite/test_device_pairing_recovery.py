from __future__ import annotations

from dataclasses import replace
from uuid import UUID

from sqlalchemy import select

from hermes_cloud.domain.persistence import PairingSessionState
from hermes_cloud.modules.device.domain import DeviceLifecycleState
from hermes_cloud.modules.device.ports import CancelPairingCommand
from hermes_cloud.platform.postgres.models import DeviceLifecycleModel, DeviceModel
from hermes_cloud.platform.sqlite.runtime import SQLiteOperationScopedPairingRepository
from tests.platform.sqlite.test_device_pairing_repository import (
    NOW,
    TENANT_ID,
    USER_ID,
    _claim_command,
    _factory,
    _mutation,
    _offer,
    _operation_mutation,
    _seed_owner_scope,
)

SECOND_OFFER_ID = UUID("12121212-1212-4212-8212-121212121212")
SECOND_SESSION_ID = UUID("13131313-1313-4313-8313-131313131313")
SECOND_DEVICE_ID = UUID("14141414-1414-4414-8414-141414141414")


def test_revoked_device_identity_can_be_claimed_again_without_integrity_error(
    tmp_path,
) -> None:
    engine, factory = _factory(tmp_path)
    _seed_owner_scope(factory)
    repository = SQLiteOperationScopedPairingRepository(factory)
    try:
        first = repository.create_offer(_offer(), mutation=_mutation())
        claimed = repository.claim_offer(
            _claim_command(),
            mutation=_operation_mutation(
                "claim",
                key_digit="3",
                request_digit="4",
                expected_revision=0,
            ),
        )
        assert first.device_key == "connector-instance-01"
        assert claimed.session is not None
        assert claimed.session.state is PairingSessionState.CLAIMED

        cancelled = repository.cancel_pairing(
            CancelPairingCommand(
                tenant_id=TENANT_ID,
                owner_user_id=USER_ID,
                pairing_session_id=claimed.session.pairing_session_id,
                expected_revision=1,
                now=NOW.replace(minute=1),
            ),
            mutation=_operation_mutation(
                "cancel",
                key_digit="5",
                request_digit="6",
                expected_revision=1,
            ),
        )
        assert cancelled.lifecycle is not None
        assert cancelled.lifecycle.state is DeviceLifecycleState.REVOKED

        second_offer = replace(
            _offer(),
            pairing_offer_id=SECOND_OFFER_ID,
            pairing_code_digest="7" * 64,
            bootstrap_secret_digest="8" * 64,
        )
        repository.create_offer(
            second_offer,
            mutation=_operation_mutation(
                "create",
                key_digit="9",
                request_digit="a",
                expected_revision=0,
            ),
        )
        second_claim = repository.claim_offer(
            replace(
                _claim_command(),
                pairing_session_id=SECOND_SESSION_ID,
                device_id=SECOND_DEVICE_ID,
                pairing_code_digest=second_offer.pairing_code_digest,
                now=NOW.replace(minute=2),
            ),
            mutation=_operation_mutation(
                "claim",
                key_digit="b",
                request_digit="c",
                expected_revision=0,
            ),
        )

        assert second_claim.session is not None
        assert second_claim.session.state is PairingSessionState.CLAIMED
        assert second_claim.lifecycle is not None
        assert second_claim.lifecycle.device_id == SECOND_DEVICE_ID
        assert second_claim.lifecycle.state is DeviceLifecycleState.PENDING

        with factory.begin() as session:
            devices = tuple(
                session.scalars(
                    select(DeviceModel)
                    .where(DeviceModel.tenant_id == TENANT_ID)
                    .order_by(DeviceModel.device_id)
                ).all()
            )
            old_lifecycle = session.get(
                DeviceLifecycleModel,
                (TENANT_ID, cancelled.lifecycle.device_id),
            )

        live_key_rows = [
            device for device in devices if device.device_key == "connector-instance-01"
        ]
        retired_rows = [
            device for device in devices if device.device_key.startswith("retired:")
        ]
        assert len(live_key_rows) == 1
        assert live_key_rows[0].device_id == SECOND_DEVICE_ID
        assert len(retired_rows) == 1
        assert old_lifecycle is not None
        assert old_lifecycle.state == DeviceLifecycleState.REVOKED.value
    finally:
        engine.dispose()
