from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from hermes_cloud.domain.persistence import PairingSessionState
from hermes_cloud.modules.device.domain import DeviceLifecycleState, PairingOffer
from hermes_cloud.modules.device.ports import (
    CancelPairingCommand,
    ClaimPairingCommand,
    PairingMutation,
)
from hermes_cloud.platform.postgres.models import (
    AgentModel,
    DeviceLifecycleModel,
    DeviceModel,
    RoleModel,
    TenantModel,
    UserModel,
    WorkspaceMembershipModel,
    WorkspaceModel,
)
from hermes_cloud.platform.sqlite.engine import build_sqlite_engine
from hermes_cloud.platform.sqlite.runtime import SQLiteOperationScopedPairingRepository
from hermes_cloud.platform.sqlite.schema import build_sqlite_metadata

NOW = datetime(2026, 8, 9, 16, 0, tzinfo=UTC)
TENANT_ID = UUID("33333333-3333-4333-8333-333333333333")
USER_ID = UUID("44444444-4444-4444-8444-444444444444")
WORKSPACE_ID = UUID("55555555-5555-4555-8555-555555555555")
AGENT_ID = UUID("66666666-6666-4666-8666-666666666666")
ROLE_ID = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
FIRST_OFFER_ID = UUID("11111111-1111-4111-8111-111111111111")
FIRST_SESSION_ID = UUID("77777777-7777-4777-8777-777777777777")
FIRST_DEVICE_ID = UUID("88888888-8888-4888-8888-888888888888")
SECOND_OFFER_ID = UUID("12121212-1212-4212-8212-121212121212")
SECOND_SESSION_ID = UUID("13131313-1313-4313-8313-131313131313")
SECOND_DEVICE_ID = UUID("14141414-1414-4414-8414-141414141414")
PUBLIC_KEY = bytes(range(32))
FINGERPRINT = "630dcd2966c4336691125448bbb25b4ff412a49c732db2c8abc1b8581bd710dd"
DEVICE_KEY = "connector-instance-01"


def _factory(tmp_path: Path) -> tuple[object, sessionmaker[Session]]:
    database = tmp_path / "pairing-recovery.sqlite3"
    engine = build_sqlite_engine(
        f"sqlite+pysqlite:///{database}",
        allow_missing=True,
    )
    build_sqlite_metadata().create_all(engine)
    database.chmod(0o660)
    return engine, sessionmaker(bind=engine, expire_on_commit=False)


def _seed_owner_scope(factory: sessionmaker[Session]) -> None:
    with factory.begin() as session:
        session.add(
            TenantModel(
                tenant_id=TENANT_ID,
                slug="pairing-recovery",
                display_name="Pairing Recovery",
                status="active",
                created_at=NOW,
            )
        )
        session.flush()
        session.add(
            UserModel(
                tenant_id=TENANT_ID,
                user_id=USER_ID,
                subject="pairing-owner",
                display_name="Pairing Owner",
                email=None,
                status="active",
                created_at=NOW,
            )
        )
        session.add(
            RoleModel(
                tenant_id=TENANT_ID,
                role_id=ROLE_ID,
                role_key="owner",
                display_name="Owner",
                scope_type="workspace",
                permissions=[],
                status="active",
                version=1,
                created_at=NOW,
            )
        )
        session.flush()
        session.add(
            WorkspaceModel(
                tenant_id=TENANT_ID,
                workspace_id=WORKSPACE_ID,
                workspace_key="pairing-recovery",
                display_name="Pairing Recovery",
                status="active",
                created_by=USER_ID,
                created_at=NOW,
            )
        )
        session.flush()
        session.add(
            WorkspaceMembershipModel(
                tenant_id=TENANT_ID,
                workspace_membership_id=UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc"),
                workspace_id=WORKSPACE_ID,
                user_id=USER_ID,
                role_id=ROLE_ID,
                status="active",
                joined_at=NOW,
                revoked_at=None,
            )
        )
        session.add(
            AgentModel(
                tenant_id=TENANT_ID,
                agent_id=AGENT_ID,
                workspace_id=WORKSPACE_ID,
                agent_key="agent-pairing",
                status="active",
                last_seen_at=NOW,
                created_at=NOW,
            )
        )


def _offer(
    *,
    offer_id: UUID = FIRST_OFFER_ID,
    code_digest: str = "a" * 64,
    secret_digest: str = "b" * 64,
) -> PairingOffer:
    return PairingOffer(
        pairing_offer_id=offer_id,
        pairing_code_digest=code_digest,
        bootstrap_secret_digest=secret_digest,
        algorithm="ed25519",
        public_key=PUBLIC_KEY,
        credential_fingerprint=FINGERPRINT,
        key_id=FINGERPRINT,
        device_key=DEVICE_KEY,
        device_name="Hermes workstation",
        platform="macos",
        connector_version="0.1.0",
        state=PairingSessionState.PENDING,
        revision=0,
        expires_at=NOW + timedelta(minutes=5),
        claimed_at=None,
        created_at=NOW,
    )


def _mutation(
    operation: str,
    *,
    mutation_id: UUID,
    key_digit: str,
    request_digit: str,
    expected_revision: int,
    created_at: datetime,
) -> PairingMutation:
    return PairingMutation(
        pairing_mutation_id=mutation_id,
        operation=operation,
        idempotency_key_digest=key_digit * 64,
        principal_digest="e" * 64,
        request_digest=request_digit * 64,
        expected_revision=expected_revision,
        created_at=created_at,
        expires_at=created_at + timedelta(days=1),
    )


def _claim(
    *,
    pairing_session_id: UUID,
    device_id: UUID,
    code_digest: str,
    now: datetime,
) -> ClaimPairingCommand:
    return ClaimPairingCommand(
        pairing_session_id=pairing_session_id,
        tenant_id=TENANT_ID,
        owner_user_id=USER_ID,
        workspace_id=WORKSPACE_ID,
        agent_id=AGENT_ID,
        device_id=device_id,
        device_display_name="Hermes workstation",
        scopes=("session.observe", "session.control.request"),
        pairing_code_digest=code_digest,
        expected_revision=0,
        now=now,
    )


def test_revoked_device_identity_can_be_claimed_again_without_integrity_error(
    tmp_path: Path,
) -> None:
    engine, factory = _factory(tmp_path)
    _seed_owner_scope(factory)
    repository = SQLiteOperationScopedPairingRepository(factory)
    first_claim_at = NOW + timedelta(seconds=10)
    cancel_at = NOW + timedelta(seconds=20)
    second_claim_at = NOW + timedelta(seconds=30)
    try:
        first_offer = _offer()
        repository.create_offer(
            first_offer,
            mutation=_mutation(
                "create",
                mutation_id=UUID("21212121-2121-4121-8121-212121212121"),
                key_digit="1",
                request_digit="2",
                expected_revision=0,
                created_at=NOW,
            ),
        )
        claimed = repository.claim_offer(
            _claim(
                pairing_session_id=FIRST_SESSION_ID,
                device_id=FIRST_DEVICE_ID,
                code_digest=first_offer.pairing_code_digest,
                now=first_claim_at,
            ),
            mutation=_mutation(
                "claim",
                mutation_id=UUID("23232323-2323-4323-8323-232323232323"),
                key_digit="3",
                request_digit="4",
                expected_revision=0,
                created_at=first_claim_at,
            ),
        )
        assert claimed.session is not None
        assert claimed.session.state is PairingSessionState.CLAIMED

        cancelled = repository.cancel_pairing(
            CancelPairingCommand(
                tenant_id=TENANT_ID,
                owner_user_id=USER_ID,
                pairing_session_id=FIRST_SESSION_ID,
                expected_revision=1,
                now=cancel_at,
            ),
            mutation=_mutation(
                "cancel",
                mutation_id=UUID("25252525-2525-4525-8525-252525252525"),
                key_digit="5",
                request_digit="6",
                expected_revision=1,
                created_at=cancel_at,
            ),
        )
        assert cancelled.lifecycle is not None
        assert cancelled.lifecycle.state is DeviceLifecycleState.REVOKED

        second_offer = _offer(
            offer_id=SECOND_OFFER_ID,
            code_digest="7" * 64,
            secret_digest="8" * 64,
        )
        repository.create_offer(
            second_offer,
            mutation=_mutation(
                "create",
                mutation_id=UUID("29292929-2929-4929-8929-292929292929"),
                key_digit="9",
                request_digit="a",
                expected_revision=0,
                created_at=NOW + timedelta(seconds=21),
            ),
        )
        second_claim = repository.claim_offer(
            _claim(
                pairing_session_id=SECOND_SESSION_ID,
                device_id=SECOND_DEVICE_ID,
                code_digest=second_offer.pairing_code_digest,
                now=second_claim_at,
            ),
            mutation=_mutation(
                "claim",
                mutation_id=UUID("2b2b2b2b-2b2b-4b2b-8b2b-2b2b2b2b2b2b"),
                key_digit="b",
                request_digit="c",
                expected_revision=0,
                created_at=second_claim_at,
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
                (TENANT_ID, FIRST_DEVICE_ID),
            )

        live_key_rows = [device for device in devices if device.device_key == DEVICE_KEY]
        retired_rows = [
            device for device in devices if device.device_key.startswith("retired:")
        ]
        assert len(live_key_rows) == 1
        assert live_key_rows[0].device_id == SECOND_DEVICE_ID
        assert len(retired_rows) == 1
        assert retired_rows[0].device_id == FIRST_DEVICE_ID
        assert old_lifecycle is not None
        assert old_lifecycle.state == DeviceLifecycleState.REVOKED.value
    finally:
        engine.dispose()
