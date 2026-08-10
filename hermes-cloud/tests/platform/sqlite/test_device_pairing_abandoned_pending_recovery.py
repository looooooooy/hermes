from __future__ import annotations

from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy.orm import Session, sessionmaker

from hermes_cloud.domain.persistence import PairingSessionState
from hermes_cloud.modules.device.domain import DeviceLifecycleState, PairingOffer
from hermes_cloud.modules.device.ports import (
    ClaimPairingCommand,
    ConfirmPairingCommand,
    PairingMutation,
    PairingStateConflict,
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
from hermes_cloud.platform.sqlalchemy.repositories.device import PairingOutcome
from hermes_cloud.platform.sqlite.engine import build_sqlite_engine
from hermes_cloud.platform.sqlite.runtime import SQLiteOperationScopedPairingRepository
from hermes_cloud.platform.sqlite.schema import build_sqlite_metadata

NOW = datetime(2026, 8, 10, 0, 0, tzinfo=UTC)
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
CHALLENGE_ID = UUID("15151515-1515-4515-8515-151515151515")
PUBLIC_KEY = bytes(range(32))
FINGERPRINT = sha256(PUBLIC_KEY).hexdigest()
ROTATED_PUBLIC_KEY = bytes(reversed(range(32)))
ROTATED_FINGERPRINT = sha256(ROTATED_PUBLIC_KEY).hexdigest()
DEVICE_KEY = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


def _factory(tmp_path: Path) -> tuple[object, sessionmaker[Session]]:
    database = tmp_path / "pairing-abandoned.sqlite3"
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
                slug="pairing-abandoned",
                display_name="Pairing Abandoned",
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
                workspace_key="pairing-abandoned",
                display_name="Pairing Abandoned",
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
    offer_id: UUID,
    code_digest: str,
    secret_digest: str,
    public_key: bytes = PUBLIC_KEY,
    fingerprint: str = FINGERPRINT,
) -> PairingOffer:
    return PairingOffer(
        pairing_offer_id=offer_id,
        pairing_code_digest=code_digest,
        bootstrap_secret_digest=secret_digest,
        algorithm="ed25519",
        public_key=public_key,
        credential_fingerprint=fingerprint,
        key_id=fingerprint,
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
    created_at: datetime,
    expected_revision: int = 0,
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


def _first_claim(repository: SQLiteOperationScopedPairingRepository) -> None:
    first = _offer(
        offer_id=FIRST_OFFER_ID,
        code_digest="a" * 64,
        secret_digest="b" * 64,
    )
    repository.create_offer(
        first,
        mutation=_mutation(
            "create",
            mutation_id=UUID("21212121-2121-4121-8121-212121212121"),
            key_digit="1",
            request_digit="2",
            created_at=NOW,
        ),
    )
    claimed = repository.claim_offer(
        _claim(
            pairing_session_id=FIRST_SESSION_ID,
            device_id=FIRST_DEVICE_ID,
            code_digest=first.pairing_code_digest,
            now=NOW + timedelta(seconds=5),
        ),
        mutation=_mutation(
            "claim",
            mutation_id=UUID("23232323-2323-4323-8323-232323232323"),
            key_digit="3",
            request_digit="4",
            created_at=NOW + timedelta(seconds=5),
        ),
    )
    assert claimed.lifecycle is not None
    assert claimed.lifecycle.state is DeviceLifecycleState.PENDING


def _confirm_first_claim(repository: SQLiteOperationScopedPairingRepository) -> None:
    confirmed = repository.confirm_owner(
        ConfirmPairingCommand(
            tenant_id=TENANT_ID,
            owner_user_id=USER_ID,
            pairing_session_id=FIRST_SESSION_ID,
            credential_fingerprint=FINGERPRINT,
            expected_revision=1,
            challenge_id=CHALLENGE_ID,
            challenge_digest="d" * 64,
            challenge_expires_at=NOW + timedelta(seconds=50),
            now=NOW + timedelta(seconds=10),
        ),
        mutation=_mutation(
            "confirm",
            mutation_id=UUID("24242424-2424-4424-8424-242424242424"),
            key_digit="5",
            request_digit="6",
            created_at=NOW + timedelta(seconds=10),
            expected_revision=1,
        ),
    )
    assert confirmed.session is not None
    assert confirmed.session.state is PairingSessionState.CONFIRMED
    assert confirmed.lifecycle is not None
    assert confirmed.lifecycle.state is DeviceLifecycleState.PENDING
    assert confirmed.credential is None


def _second_claim(
    repository: SQLiteOperationScopedPairingRepository,
    *,
    public_key: bytes = PUBLIC_KEY,
    fingerprint: str = FINGERPRINT,
) -> PairingOutcome:
    second = _offer(
        offer_id=SECOND_OFFER_ID,
        code_digest="7" * 64,
        secret_digest="8" * 64,
        public_key=public_key,
        fingerprint=fingerprint,
    )
    repository.create_offer(
        second,
        mutation=_mutation(
            "create",
            mutation_id=UUID("29292929-2929-4929-8929-292929292929"),
            key_digit="9",
            request_digit="a",
            created_at=NOW + timedelta(seconds=15),
        ),
    )
    return repository.claim_offer(
        _claim(
            pairing_session_id=SECOND_SESSION_ID,
            device_id=SECOND_DEVICE_ID,
            code_digest=second.pairing_code_digest,
            now=NOW + timedelta(seconds=20),
        ),
        mutation=_mutation(
            "claim",
            mutation_id=UUID("2b2b2b2b-2b2b-4b2b-8b2b-2b2b2b2b2b2b"),
            key_digit="b",
            request_digit="c",
            created_at=NOW + timedelta(seconds=20),
        ),
    )


def _assert_recovered(
    factory: sessionmaker[Session],
    recovered: PairingOutcome,
) -> None:
    assert recovered.session is not None
    assert recovered.session.state is PairingSessionState.CLAIMED
    assert recovered.lifecycle is not None
    assert recovered.lifecycle.device_id == SECOND_DEVICE_ID
    assert recovered.lifecycle.state is DeviceLifecycleState.PENDING
    with factory.begin() as session:
        old = session.get(DeviceModel, (TENANT_ID, FIRST_DEVICE_ID))
        old_lifecycle = session.get(
            DeviceLifecycleModel,
            (TENANT_ID, FIRST_DEVICE_ID),
        )
        current = session.get(DeviceModel, (TENANT_ID, SECOND_DEVICE_ID))
    assert old is not None
    assert old.device_key == f"retired:{FIRST_DEVICE_ID}"
    assert old_lifecycle is not None
    assert old_lifecycle.state == DeviceLifecycleState.REVOKED.value
    assert current is not None
    assert current.device_key == DEVICE_KEY


def test_same_device_can_replace_abandoned_never_activated_pending_claim(
    tmp_path: Path,
) -> None:
    engine, factory = _factory(tmp_path)
    _seed_owner_scope(factory)
    repository = SQLiteOperationScopedPairingRepository(factory)
    try:
        _first_claim(repository)
        _assert_recovered(factory, _second_claim(repository))
    finally:
        engine.dispose()


def test_same_device_can_replace_confirmed_pending_binding_after_helper_failure(
    tmp_path: Path,
) -> None:
    engine, factory = _factory(tmp_path)
    _seed_owner_scope(factory)
    repository = SQLiteOperationScopedPairingRepository(factory)
    try:
        _first_claim(repository)
        _confirm_first_claim(repository)
        _assert_recovered(factory, _second_claim(repository))
    finally:
        engine.dispose()


def test_pending_binding_can_recover_after_native_keychain_identity_rotates(
    tmp_path: Path,
) -> None:
    engine, factory = _factory(tmp_path)
    _seed_owner_scope(factory)
    repository = SQLiteOperationScopedPairingRepository(factory)
    try:
        _first_claim(repository)
        _confirm_first_claim(repository)
        recovered = _second_claim(
            repository,
            public_key=ROTATED_PUBLIC_KEY,
            fingerprint=ROTATED_FINGERPRINT,
        )
        _assert_recovered(factory, recovered)
        assert recovered.offer.credential_fingerprint == ROTATED_FINGERPRINT
        assert recovered.offer.public_key == ROTATED_PUBLIC_KEY
    finally:
        engine.dispose()


def test_active_device_identity_remains_a_hard_conflict(tmp_path: Path) -> None:
    engine, factory = _factory(tmp_path)
    _seed_owner_scope(factory)
    repository = SQLiteOperationScopedPairingRepository(factory)
    try:
        _first_claim(repository)
        with factory.begin() as session:
            lifecycle = session.get(DeviceLifecycleModel, (TENANT_ID, FIRST_DEVICE_ID))
            device = session.get(DeviceModel, (TENANT_ID, FIRST_DEVICE_ID))
            assert lifecycle is not None
            assert device is not None
            lifecycle.state = DeviceLifecycleState.ACTIVE.value
            lifecycle.revision = 1
            device.status = "active"

        second = _offer(
            offer_id=SECOND_OFFER_ID,
            code_digest="7" * 64,
            secret_digest="8" * 64,
            public_key=ROTATED_PUBLIC_KEY,
            fingerprint=ROTATED_FINGERPRINT,
        )
        repository.create_offer(
            second,
            mutation=_mutation(
                "create",
                mutation_id=UUID("29292929-2929-4929-8929-292929292929"),
                key_digit="9",
                request_digit="a",
                created_at=NOW + timedelta(seconds=15),
            ),
        )
        with pytest.raises(PairingStateConflict, match="already bound"):
            repository.claim_offer(
                _claim(
                    pairing_session_id=SECOND_SESSION_ID,
                    device_id=SECOND_DEVICE_ID,
                    code_digest=second.pairing_code_digest,
                    now=NOW + timedelta(seconds=20),
                ),
                mutation=_mutation(
                    "claim",
                    mutation_id=UUID("2b2b2b2b-2b2b-4b2b-8b2b-2b2b2b2b2b2b"),
                    key_digit="b",
                    request_digit="c",
                    created_at=NOW + timedelta(seconds=20),
                ),
            )
    finally:
        engine.dispose()
