from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier, Lock
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from hermes_cloud.domain.persistence import PairingSessionState
from hermes_cloud.modules.device.domain import (
    PAIRING_SESSION_TTL,
    DeviceAuthenticationChallenge,
    DeviceCredential,
    DeviceCredentialStatus,
    DeviceLifecycleState,
    PairingOffer,
)
from hermes_cloud.modules.device.ports import (
    ActivatePairingCommand,
    CancelPairingCommand,
    ClaimPairingCommand,
    ConfirmPairingCommand,
    ConsumeDeviceChallengeCommand,
    CreateDeviceChallengeCommand,
    PairingClaimRateLimited,
    PairingClaimUnavailable,
    PairingIdempotencyConflict,
    PairingMutation,
    PairingNotFound,
    PairingScopeUnavailable,
    RevokeDeviceCommand,
)
from hermes_cloud.platform.postgres.models import (
    AgentModel,
    DeviceAuthenticationChallengeModel,
    DeviceCredentialModel,
    DeviceLifecycleModel,
    DeviceModel,
    PairingClaimLimitModel,
    PairingIdempotencyModel,
    PairingOfferModel,
    PairingSessionModel,
    RoleModel,
    TenantModel,
    UserModel,
    WorkspaceMembershipModel,
    WorkspaceModel,
)
from hermes_cloud.platform.sqlalchemy.runtime import OperationScopedPairingRepository
from hermes_cloud.platform.sqlite.engine import build_sqlite_engine
from hermes_cloud.platform.sqlite.repositories.device import (
    SQLitePairingRepository,
)
from hermes_cloud.platform.sqlite.runtime import (
    SQLiteOperationScopedPairingRepository,
)
from hermes_cloud.platform.sqlite.schema import build_sqlite_metadata

NOW = datetime(2026, 7, 31, 13, 0, tzinfo=UTC)
OFFER_ID = UUID("11111111-1111-4111-8111-111111111111")
MUTATION_ID = UUID("22222222-2222-4222-8222-222222222222")
TENANT_ID = UUID("33333333-3333-4333-8333-333333333333")
USER_ID = UUID("44444444-4444-4444-8444-444444444444")
WORKSPACE_ID = UUID("55555555-5555-4555-8555-555555555555")
AGENT_ID = UUID("66666666-6666-4666-8666-666666666666")
PAIRING_SESSION_ID = UUID("77777777-7777-4777-8777-777777777777")
DEVICE_ID = UUID("88888888-8888-4888-8888-888888888888")
CREDENTIAL_ID = UUID("99999999-9999-4999-8999-999999999999")
CHALLENGE_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
PUBLIC_KEY = bytes(range(32))
FINGERPRINT = "630dcd2966c4336691125448bbb25b4ff412a49c732db2c8abc1b8581bd710dd"


def _factory(tmp_path: Path) -> tuple[object, sessionmaker[Session]]:
    database = tmp_path / "pairing.sqlite3"
    engine = build_sqlite_engine(
        f"sqlite+pysqlite:///{database}",
        allow_missing=True,
    )
    build_sqlite_metadata().create_all(engine)
    database.chmod(0o660)
    return engine, sessionmaker(bind=engine, expire_on_commit=False)


def _offer() -> PairingOffer:
    return PairingOffer(
        pairing_offer_id=OFFER_ID,
        pairing_code_digest="a" * 64,
        bootstrap_secret_digest="b" * 64,
        algorithm="ed25519",
        public_key=PUBLIC_KEY,
        credential_fingerprint=FINGERPRINT,
        key_id=FINGERPRINT,
        device_key="connector-instance-01",
        device_name="Office Mac",
        platform="macos",
        connector_version="1.0.0",
        state=PairingSessionState.PENDING,
        revision=0,
        expires_at=NOW + PAIRING_SESSION_TTL,
        claimed_at=None,
        created_at=NOW,
    )


def _mutation(*, request_digest: str = "c" * 64) -> PairingMutation:
    return PairingMutation(
        pairing_mutation_id=MUTATION_ID,
        operation="create",
        idempotency_key_digest="d" * 64,
        principal_digest="e" * 64,
        request_digest=request_digest,
        expected_revision=0,
        created_at=NOW,
        expires_at=NOW + timedelta(days=1),
    )


def _operation_mutation(
    operation: str,
    *,
    key_digit: str,
    request_digit: str,
    expected_revision: int,
) -> PairingMutation:
    return PairingMutation(
        pairing_mutation_id=UUID(
            f"{key_digit * 8}-{key_digit * 4}-4{key_digit * 3}-8{key_digit * 3}-{key_digit * 12}"
        ),
        operation=operation,
        idempotency_key_digest=key_digit * 64,
        principal_digest="e" * 64,
        request_digest=request_digit * 64,
        expected_revision=expected_revision,
        created_at=NOW,
        expires_at=NOW + timedelta(days=1),
    )


def _seed_owner_scope(factory: sessionmaker[Session]) -> None:
    role_id = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
    with factory.begin() as session:
        session.add(
            TenantModel(
                tenant_id=TENANT_ID,
                slug="pairing",
                display_name="Pairing",
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
                role_id=role_id,
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
                workspace_key="pairing",
                display_name="Pairing",
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
                role_id=role_id,
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


def _claim_command(
    *,
    code_digest: str = "a" * 64,
    now: datetime = NOW + timedelta(seconds=10),
) -> ClaimPairingCommand:
    return ClaimPairingCommand(
        pairing_session_id=PAIRING_SESSION_ID,
        tenant_id=TENANT_ID,
        owner_user_id=USER_ID,
        workspace_id=WORKSPACE_ID,
        agent_id=AGENT_ID,
        device_id=DEVICE_ID,
        device_display_name="Owner Confirmed Mac",
        scopes=("session.observe", "session.control.request"),
        pairing_code_digest=code_digest,
        expected_revision=0,
        now=now,
    )


def _confirm_command() -> ConfirmPairingCommand:
    return ConfirmPairingCommand(
        tenant_id=TENANT_ID,
        owner_user_id=USER_ID,
        pairing_session_id=PAIRING_SESSION_ID,
        credential_fingerprint=FINGERPRINT,
        expected_revision=1,
        challenge_id=CHALLENGE_ID,
        challenge_digest="1" * 64,
        challenge_expires_at=NOW + timedelta(seconds=50),
        now=NOW + timedelta(seconds=20),
    )


def _credential() -> DeviceCredential:
    return DeviceCredential(
        tenant_id=TENANT_ID,
        credential_id=CREDENTIAL_ID,
        device_id=DEVICE_ID,
        algorithm="ed25519",
        key_id=FINGERPRINT,
        public_key=PUBLIC_KEY,
        credential_fingerprint=FINGERPRINT,
        status=DeviceCredentialStatus.ACTIVE,
        issued_at=NOW + timedelta(seconds=30),
        expires_at=NOW + timedelta(days=30),
        revoked_at=None,
    )


def _proof_command() -> ActivatePairingCommand:
    return ActivatePairingCommand(
        tenant_id=TENANT_ID,
        pairing_offer_id=OFFER_ID,
        pairing_session_id=PAIRING_SESSION_ID,
        bootstrap_secret_digest="b" * 64,
        challenge_id=CHALLENGE_ID,
        challenge_digest="1" * 64,
        confirmation_digest="2" * 64,
        credential=_credential(),
        expected_revision=2,
        now=NOW + timedelta(seconds=30),
    )


def _prepare_confirmed_pairing(repository: OperationScopedPairingRepository) -> None:
    repository.create_offer(_offer(), mutation=_mutation())
    repository.claim_offer(
        _claim_command(),
        mutation=_operation_mutation(
            "claim",
            key_digit="3",
            request_digit="4",
            expected_revision=0,
        ),
    )
    repository.confirm_owner(
        _confirm_command(),
        mutation=_operation_mutation(
            "confirm",
            key_digit="5",
            request_digit="6",
            expected_revision=1,
        ),
    )


def _prepare_active_pairing(repository: OperationScopedPairingRepository) -> None:
    _prepare_confirmed_pairing(repository)
    repository.activate_verified_credential(
        _proof_command(),
        mutation=_operation_mutation(
            "proof",
            key_digit="7",
            request_digit="8",
            expected_revision=2,
        ),
    )


def test_owner_status_reads_are_concurrent_immutable_and_non_enumerable(
    tmp_path: Path,
) -> None:
    engine, factory = _factory(tmp_path)
    _seed_owner_scope(factory)
    repository = SQLiteOperationScopedPairingRepository(factory)
    try:
        repository.create_offer(_offer(), mutation=_mutation())
        repository.claim_offer(
            _claim_command(),
            mutation=_operation_mutation(
                "claim",
                key_digit="3",
                request_digit="4",
                expected_revision=0,
            ),
        )
        with factory.begin() as session:
            mutation_count = len(session.scalars(select(PairingIdempotencyModel)).all())

        def read_status(_index: int) -> tuple[PairingSessionState, int]:
            snapshot = repository.get_owner_pairing_status(
                tenant_id=TENANT_ID,
                owner_user_id=USER_ID,
                pairing_session_id=PAIRING_SESSION_ID,
            )
            assert snapshot.session is not None
            assert snapshot.material is not None
            return snapshot.session.state, snapshot.material.revision

        with ThreadPoolExecutor(max_workers=4) as executor:
            results = tuple(executor.map(read_status, range(8)))

        assert results == ((PairingSessionState.CLAIMED, 1),) * 8
        with pytest.raises(PairingScopeUnavailable):
            repository.get_owner_pairing_status(
                tenant_id=TENANT_ID,
                owner_user_id=UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd"),
                pairing_session_id=PAIRING_SESSION_ID,
            )
        with pytest.raises(PairingNotFound):
            repository.get_owner_pairing_status(
                tenant_id=TENANT_ID,
                owner_user_id=USER_ID,
                pairing_session_id=UUID("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"),
            )
        with factory.begin() as session:
            persisted = session.get(
                PairingSessionModel,
                (TENANT_ID, PAIRING_SESSION_ID),
            )
            assert persisted is not None
            assert persisted.state == "claimed"
            assert (
                len(session.scalars(select(PairingIdempotencyModel)).all())
                == mutation_count
            )
    finally:
        engine.dispose()


def _barrier_repository(
    operation: str,
) -> type[SQLitePairingRepository]:
    selection_barrier = Barrier(2)
    selection_lock = Lock()
    selections = 0

    class BarrierPairingRepository(SQLitePairingRepository):
        def _mutation_record(
            self,
            mutation: PairingMutation,
        ) -> PairingIdempotencyModel | None:
            nonlocal selections
            record = super()._mutation_record(mutation)
            with selection_lock:
                should_wait = (
                    mutation.operation == operation
                    and record is None
                    and selections < 2
                )
                if should_wait:
                    selections += 1
            if should_wait:
                selection_barrier.wait(timeout=2)
            return record

    return BarrierPairingRepository


def test_create_offer_is_digest_only_and_exactly_idempotent(
    tmp_path: Path,
) -> None:
    engine, factory = _factory(tmp_path)
    repository = SQLiteOperationScopedPairingRepository(factory)
    try:
        created = repository.create_offer(_offer(), mutation=_mutation())
        replayed = repository.create_offer(_offer(), mutation=_mutation())

        assert created == _offer()
        assert replayed == created
        with factory.begin() as session:
            stored_offer = session.scalar(
                select(PairingOfferModel).where(
                    PairingOfferModel.pairing_offer_id == OFFER_ID
                )
            )
            records = session.scalars(select(PairingIdempotencyModel)).all()
        assert stored_offer is not None
        assert stored_offer.pairing_code_digest == "a" * 64
        assert stored_offer.bootstrap_secret_digest == "b" * 64
        assert len(records) == 1
        assert records[0].request_digest == "c" * 64
        assert {
            "pairing_code",
            "bootstrap_secret",
            "private_key",
        }.isdisjoint(PairingOfferModel.__table__.columns.keys())
    finally:
        engine.dispose()


def test_create_offer_same_key_with_different_digest_is_conflict(
    tmp_path: Path,
) -> None:
    engine, factory = _factory(tmp_path)
    repository = SQLiteOperationScopedPairingRepository(factory)
    try:
        repository.create_offer(_offer(), mutation=_mutation())

        with pytest.raises(PairingIdempotencyConflict):
            repository.create_offer(
                _offer(),
                mutation=_mutation(request_digest="f" * 64),
            )

        with factory.begin() as session:
            assert len(session.scalars(select(PairingOfferModel)).all()) == 1
            assert len(session.scalars(select(PairingIdempotencyModel)).all()) == 1
    finally:
        engine.dispose()


def test_concurrent_create_same_idempotency_key_replays_winner(
    tmp_path: Path,
) -> None:
    engine, factory = _factory(tmp_path)
    repository = OperationScopedPairingRepository(
        factory,
        _barrier_repository("create"),
    )
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            created = tuple(
                executor.map(
                    lambda _: repository.create_offer(
                        _offer(),
                        mutation=_mutation(),
                    ),
                    range(2),
                )
            )

        assert created == (_offer(), _offer())
        with factory.begin() as session:
            assert len(session.scalars(select(PairingOfferModel)).all()) == 1
            assert len(session.scalars(select(PairingIdempotencyModel)).all()) == 1
    finally:
        engine.dispose()


def test_concurrent_claim_same_idempotency_key_replays_winner(
    tmp_path: Path,
) -> None:
    engine, factory = _factory(tmp_path)
    _seed_owner_scope(factory)
    repository = OperationScopedPairingRepository(
        factory,
        _barrier_repository("claim"),
    )
    mutation = _operation_mutation(
        "claim",
        key_digit="3",
        request_digit="4",
        expected_revision=0,
    )
    try:
        repository.create_offer(_offer(), mutation=_mutation())
        with ThreadPoolExecutor(max_workers=2) as executor:
            claimed = tuple(
                executor.map(
                    lambda _: repository.claim_offer(
                        _claim_command(),
                        mutation=mutation,
                    ),
                    range(2),
                )
            )

        assert claimed[0] == claimed[1]
        assert claimed[0].session is not None
        assert claimed[0].session.state is PairingSessionState.CLAIMED
        with factory.begin() as session:
            records = session.scalars(
                select(PairingIdempotencyModel).where(
                    PairingIdempotencyModel.operation == "claim"
                )
            ).all()
        assert len(records) == 1
    finally:
        engine.dispose()


def test_concurrent_proof_same_idempotency_key_replays_winner(
    tmp_path: Path,
) -> None:
    engine, factory = _factory(tmp_path)
    _seed_owner_scope(factory)
    repository = OperationScopedPairingRepository(
        factory,
        _barrier_repository("proof"),
    )
    mutation = _operation_mutation(
        "proof",
        key_digit="7",
        request_digit="8",
        expected_revision=2,
    )
    try:
        _prepare_confirmed_pairing(repository)
        with ThreadPoolExecutor(max_workers=2) as executor:
            activated = tuple(
                executor.map(
                    lambda _: repository.activate_verified_credential(
                        _proof_command(),
                        mutation=mutation,
                    ),
                    range(2),
                )
            )

        assert activated[0] == activated[1]
        assert activated[0].lifecycle is not None
        assert activated[0].lifecycle.state is DeviceLifecycleState.ACTIVE
        with factory.begin() as session:
            assert len(session.scalars(select(DeviceCredentialModel)).all()) == 1
    finally:
        engine.dispose()


def test_concurrent_device_challenge_same_idempotency_key_replays_winner(
    tmp_path: Path,
) -> None:
    engine, factory = _factory(tmp_path)
    _seed_owner_scope(factory)
    repository = OperationScopedPairingRepository(
        factory,
        _barrier_repository("device_challenge"),
    )
    challenge = DeviceAuthenticationChallenge(
        tenant_id=TENANT_ID,
        challenge_id=CHALLENGE_ID,
        device_id=DEVICE_ID,
        credential_id=CREDENTIAL_ID,
        challenge_digest="9" * 64,
        issued_at=NOW + timedelta(seconds=40),
        expires_at=NOW + timedelta(seconds=90),
        consumed_at=None,
    )
    command = CreateDeviceChallengeCommand(
        challenge=challenge,
        now=challenge.issued_at,
    )
    mutation = _operation_mutation(
        "device_challenge",
        key_digit="b",
        request_digit="c",
        expected_revision=0,
    )
    try:
        _prepare_active_pairing(repository)
        with ThreadPoolExecutor(max_workers=2) as executor:
            issued = tuple(
                executor.map(
                    lambda _: repository.create_device_challenge(
                        command,
                        mutation=mutation,
                    ),
                    range(2),
                )
            )

        assert issued[0] == issued[1]
        assert issued[0].challenge == challenge
        with factory.begin() as session:
            assert (
                len(session.scalars(select(DeviceAuthenticationChallengeModel)).all())
                == 1
            )
    finally:
        engine.dispose()


def test_concurrent_device_token_same_idempotency_key_replays_winner(
    tmp_path: Path,
) -> None:
    engine, factory = _factory(tmp_path)
    _seed_owner_scope(factory)
    repository = OperationScopedPairingRepository(
        factory,
        _barrier_repository("device_token"),
    )
    challenge = DeviceAuthenticationChallenge(
        tenant_id=TENANT_ID,
        challenge_id=CHALLENGE_ID,
        device_id=DEVICE_ID,
        credential_id=CREDENTIAL_ID,
        challenge_digest="9" * 64,
        issued_at=NOW + timedelta(seconds=40),
        expires_at=NOW + timedelta(seconds=90),
        consumed_at=None,
    )
    challenge_command = CreateDeviceChallengeCommand(
        challenge=challenge,
        now=challenge.issued_at,
    )
    challenge_mutation = _operation_mutation(
        "device_challenge",
        key_digit="b",
        request_digit="c",
        expected_revision=0,
    )
    token_command = ConsumeDeviceChallengeCommand(
        device_id=DEVICE_ID,
        credential_id=CREDENTIAL_ID,
        challenge_id=CHALLENGE_ID,
        challenge_digest=challenge.challenge_digest,
        proof_digest="d" * 64,
        now=NOW + timedelta(seconds=45),
    )
    token_mutation = _operation_mutation(
        "device_token",
        key_digit="d",
        request_digit="e",
        expected_revision=0,
    )
    try:
        _prepare_active_pairing(repository)
        repository.create_device_challenge(
            challenge_command,
            mutation=challenge_mutation,
        )
        with ThreadPoolExecutor(max_workers=2) as executor:
            consumed = tuple(
                executor.map(
                    lambda _: repository.consume_device_challenge(
                        token_command,
                        mutation=token_mutation,
                    ),
                    range(2),
                )
            )

        assert consumed[0] == consumed[1]
        assert consumed[0].challenge is not None
        assert consumed[0].challenge.consumed_at == token_command.now
        with factory.begin() as session:
            records = session.scalars(
                select(PairingIdempotencyModel).where(
                    PairingIdempotencyModel.operation == "device_token"
                )
            ).all()
        assert len(records) == 1
    finally:
        engine.dispose()


def test_claim_confirm_and_verified_proof_activate_one_credential(
    tmp_path: Path,
) -> None:
    engine, factory = _factory(tmp_path)
    _seed_owner_scope(factory)
    repository = SQLiteOperationScopedPairingRepository(factory)
    try:
        repository.create_offer(_offer(), mutation=_mutation())
        claim_mutation = _operation_mutation(
            "claim",
            key_digit="3",
            request_digit="4",
            expected_revision=0,
        )
        claimed = repository.claim_offer(
            _claim_command(),
            mutation=claim_mutation,
        )
        replayed_claim = repository.claim_offer(
            _claim_command(),
            mutation=claim_mutation,
        )

        assert replayed_claim == claimed
        assert claimed.session is not None
        assert claimed.session.state is PairingSessionState.CLAIMED
        assert claimed.material is not None
        assert claimed.material.scopes == (
            "session.observe",
            "session.control.request",
        )
        assert claimed.lifecycle is not None
        assert claimed.lifecycle.state is DeviceLifecycleState.PENDING
        assert claimed.credential is None

        confirmed = repository.confirm_owner(
            _confirm_command(),
            mutation=_operation_mutation(
                "confirm",
                key_digit="5",
                request_digit="6",
                expected_revision=1,
            ),
        )
        assert confirmed.session is not None
        assert confirmed.session.state is PairingSessionState.CONFIRMED
        assert confirmed.material is not None
        assert confirmed.material.revision == 2
        assert confirmed.credential is None

        active = repository.activate_verified_credential(
            ActivatePairingCommand(
                tenant_id=TENANT_ID,
                pairing_offer_id=OFFER_ID,
                pairing_session_id=PAIRING_SESSION_ID,
                bootstrap_secret_digest="b" * 64,
                challenge_id=CHALLENGE_ID,
                challenge_digest="1" * 64,
                confirmation_digest="2" * 64,
                credential=_credential(),
                expected_revision=2,
                now=NOW + timedelta(seconds=30),
            ),
            mutation=_operation_mutation(
                "proof",
                key_digit="7",
                request_digit="8",
                expected_revision=2,
            ),
        )
        assert active.lifecycle is not None
        assert active.lifecycle.state is DeviceLifecycleState.ACTIVE
        assert active.credential == _credential()

        with factory.begin() as session:
            credentials = session.scalars(select(DeviceCredentialModel)).all()
            device = session.scalar(
                select(DeviceModel).where(DeviceModel.device_id == DEVICE_ID)
            )
        assert len(credentials) == 1
        assert device is not None
        assert device.status == "active"
    finally:
        engine.dispose()


def test_replays_original_snapshot_after_later_pairing_transitions(
    tmp_path: Path,
) -> None:
    engine, factory = _factory(tmp_path)
    _seed_owner_scope(factory)
    repository = SQLiteOperationScopedPairingRepository(factory)
    claim_mutation = _operation_mutation(
        "claim",
        key_digit="3",
        request_digit="4",
        expected_revision=0,
    )
    confirm_mutation = _operation_mutation(
        "confirm",
        key_digit="5",
        request_digit="6",
        expected_revision=1,
    )
    proof_mutation = _operation_mutation(
        "proof",
        key_digit="7",
        request_digit="8",
        expected_revision=2,
    )
    proof_command = ActivatePairingCommand(
        tenant_id=TENANT_ID,
        pairing_offer_id=OFFER_ID,
        pairing_session_id=PAIRING_SESSION_ID,
        bootstrap_secret_digest="b" * 64,
        challenge_id=CHALLENGE_ID,
        challenge_digest="1" * 64,
        confirmation_digest="2" * 64,
        credential=_credential(),
        expected_revision=2,
        now=NOW + timedelta(seconds=30),
    )
    try:
        created = repository.create_offer(_offer(), mutation=_mutation())
        claimed = repository.claim_offer(
            _claim_command(),
            mutation=claim_mutation,
        )
        confirmed = repository.confirm_owner(
            _confirm_command(),
            mutation=confirm_mutation,
        )
        activated = repository.activate_verified_credential(
            proof_command,
            mutation=proof_mutation,
        )
        repository.revoke_device(
            RevokeDeviceCommand(
                tenant_id=TENANT_ID,
                owner_user_id=USER_ID,
                device_id=DEVICE_ID,
                now=NOW + timedelta(minutes=1),
            ),
            mutation=_operation_mutation(
                "revoke",
                key_digit="9",
                request_digit="a",
                expected_revision=1,
            ),
        )

        assert repository.create_offer(_offer(), mutation=_mutation()) == created
        assert (
            repository.claim_offer(
                _claim_command(),
                mutation=claim_mutation,
            )
            == claimed
        )
        assert (
            repository.confirm_owner(
                _confirm_command(),
                mutation=confirm_mutation,
            )
            == confirmed
        )
        assert (
            repository.activate_verified_credential(
                proof_command,
                mutation=proof_mutation,
            )
            == activated
        )
    finally:
        engine.dispose()


def test_failed_claim_is_idempotent_and_fifth_attempt_blocks_only_principal(
    tmp_path: Path,
) -> None:
    engine, factory = _factory(tmp_path)
    _seed_owner_scope(factory)
    repository = SQLiteOperationScopedPairingRepository(factory)
    try:
        repository.create_offer(_offer(), mutation=_mutation())
        first = _operation_mutation(
            "claim",
            key_digit="3",
            request_digit="4",
            expected_revision=0,
        )
        with pytest.raises(PairingClaimUnavailable):
            repository.claim_offer(
                _claim_command(code_digest="f" * 64),
                mutation=first,
            )
        with pytest.raises(PairingClaimUnavailable):
            repository.claim_offer(
                _claim_command(code_digest="f" * 64),
                mutation=first,
            )

        for attempt in range(2, 6):
            mutation = _operation_mutation(
                "claim",
                key_digit=str(attempt + 2),
                request_digit=str(attempt + 3),
                expected_revision=0,
            )
            error = PairingClaimRateLimited if attempt == 5 else PairingClaimUnavailable
            with pytest.raises(error):
                repository.claim_offer(
                    _claim_command(code_digest="f" * 64),
                    mutation=mutation,
                )

        with factory.begin() as session:
            offer = session.get(PairingOfferModel, OFFER_ID)
            claim_limit = session.get(
                PairingClaimLimitModel,
                (TENANT_ID, USER_ID),
            )
            assert offer is not None
            assert offer.state == PairingSessionState.PENDING.value
            assert offer.revision == 0
            assert claim_limit is not None
            assert claim_limit.failed_attempts == 5

        with pytest.raises(PairingClaimRateLimited) as blocked:
            repository.claim_offer(
                _claim_command(),
                mutation=_operation_mutation(
                    "claim",
                    key_digit="9",
                    request_digit="a",
                    expected_revision=0,
                ),
            )
        assert 1 <= blocked.value.retry_after_seconds <= 300

        later_offer = replace(
            _offer(),
            pairing_offer_id=UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd"),
            pairing_code_digest="2" * 64,
            bootstrap_secret_digest="3" * 64,
            created_at=NOW + timedelta(minutes=5, seconds=1),
            expires_at=NOW + timedelta(minutes=10, seconds=1),
        )
        repository.create_offer(
            later_offer,
            mutation=_operation_mutation(
                "create",
                key_digit="e",
                request_digit="f",
                expected_revision=0,
            ),
        )
        claimed = repository.claim_offer(
            _claim_command(
                code_digest="2" * 64,
                now=NOW + timedelta(minutes=6),
            ),
            mutation=_operation_mutation(
                "claim",
                key_digit="b",
                request_digit="c",
                expected_revision=0,
            ),
        )
        assert claimed.offer.state is PairingSessionState.CLAIMED
    finally:
        engine.dispose()


def test_claim_scope_is_derived_from_active_owner_membership(
    tmp_path: Path,
) -> None:
    engine, factory = _factory(tmp_path)
    _seed_owner_scope(factory)
    repository = SQLiteOperationScopedPairingRepository(factory)
    try:
        repository.create_offer(_offer(), mutation=_mutation())
        command = replace(
            _claim_command(),
            workspace_id=UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd"),
        )
        with pytest.raises(PairingScopeUnavailable):
            repository.claim_offer(
                command,
                mutation=_operation_mutation(
                    "claim",
                    key_digit="3",
                    request_digit="4",
                    expected_revision=0,
                ),
            )

        with factory.begin() as session:
            offer = session.get(PairingOfferModel, OFFER_ID)
            assert offer is not None
            assert offer.state == PairingSessionState.PENDING.value
            assert session.scalars(select(DeviceModel)).all() == []
    finally:
        engine.dispose()


def test_unavailable_claim_states_are_uniform_and_do_not_mutate_offers(
    tmp_path: Path,
) -> None:
    engine, factory = _factory(tmp_path)
    _seed_owner_scope(factory)
    repository = SQLiteOperationScopedPairingRepository(factory)
    try:
        repository.create_offer(_offer(), mutation=_mutation())
        repository.claim_offer(
            _claim_command(),
            mutation=_operation_mutation(
                "claim",
                key_digit="3",
                request_digit="4",
                expected_revision=0,
            ),
        )
        with pytest.raises(PairingClaimUnavailable, match="claim unavailable"):
            repository.claim_offer(
                replace(
                    _claim_command(),
                    pairing_session_id=UUID("12121212-1212-4212-8212-121212121212"),
                    device_id=UUID("13131313-1313-4313-8313-131313131313"),
                ),
                mutation=_operation_mutation(
                    "claim",
                    key_digit="5",
                    request_digit="6",
                    expected_revision=0,
                ),
            )

        expired_offer = replace(
            _offer(),
            pairing_offer_id=UUID("14141414-1414-4414-8414-141414141414"),
            pairing_code_digest="2" * 64,
            bootstrap_secret_digest="3" * 64,
        )
        repository.create_offer(
            expired_offer,
            mutation=_operation_mutation(
                "create",
                key_digit="7",
                request_digit="8",
                expected_revision=0,
            ),
        )
        with pytest.raises(PairingClaimUnavailable, match="claim unavailable"):
            repository.claim_offer(
                replace(
                    _claim_command(),
                    pairing_session_id=UUID("15151515-1515-4515-8515-151515151515"),
                    device_id=UUID("16161616-1616-4616-8616-161616161616"),
                    pairing_code_digest="2" * 64,
                    now=expired_offer.expires_at,
                ),
                mutation=_operation_mutation(
                    "claim",
                    key_digit="9",
                    request_digit="a",
                    expected_revision=0,
                ),
            )

        with factory.begin() as session:
            claimed_model = session.get(PairingOfferModel, OFFER_ID)
            expired_model = session.get(
                PairingOfferModel,
                expired_offer.pairing_offer_id,
            )
        assert claimed_model is not None
        assert claimed_model.state == "claimed"
        assert claimed_model.revision == 1
        assert expired_model is not None
        assert expired_model.state == "pending"
        assert expired_model.revision == 0
    finally:
        engine.dispose()


def test_concurrent_claim_compare_and_swap_has_exactly_one_winner(
    tmp_path: Path,
) -> None:
    engine, factory = _factory(tmp_path)
    _seed_owner_scope(factory)
    repository = SQLiteOperationScopedPairingRepository(factory)
    try:
        repository.create_offer(_offer(), mutation=_mutation())
        commands = (
            _claim_command(),
            replace(
                _claim_command(),
                pairing_session_id=UUID("12121212-1212-4212-8212-121212121212"),
                device_id=UUID("13131313-1313-4313-8313-131313131313"),
            ),
        )
        mutations = (
            _operation_mutation(
                "claim",
                key_digit="3",
                request_digit="4",
                expected_revision=0,
            ),
            _operation_mutation(
                "claim",
                key_digit="5",
                request_digit="6",
                expected_revision=0,
            ),
        )

        def claim(index: int) -> str:
            try:
                repository.claim_offer(
                    commands[index],
                    mutation=mutations[index],
                )
            except PairingClaimUnavailable:
                return "unavailable"
            return "claimed"

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = tuple(executor.map(claim, range(2)))

        assert sorted(outcomes) == ["claimed", "unavailable"]
        with factory.begin() as session:
            assert len(session.scalars(select(DeviceModel)).all()) == 1
    finally:
        engine.dispose()


def test_concurrent_unknown_codes_update_one_principal_limit_without_db_error(
    tmp_path: Path,
) -> None:
    engine, factory = _factory(tmp_path)
    _seed_owner_scope(factory)
    repository = SQLiteOperationScopedPairingRepository(factory)
    try:
        repository.create_offer(_offer(), mutation=_mutation())

        def claim(index: int) -> str:
            digit = ("2", "3")[index]
            try:
                repository.claim_offer(
                    _claim_command(code_digest=digit * 64),
                    mutation=_operation_mutation(
                        "claim",
                        key_digit=("4", "5")[index],
                        request_digit=("6", "7")[index],
                        expected_revision=0,
                    ),
                )
            except PairingClaimUnavailable:
                return "unavailable"
            return "claimed"

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = tuple(executor.map(claim, range(2)))

        assert outcomes == ("unavailable", "unavailable")
        with factory.begin() as session:
            claim_limit = session.get(
                PairingClaimLimitModel,
                (TENANT_ID, USER_ID),
            )
            offer = session.get(PairingOfferModel, OFFER_ID)
        assert claim_limit is not None
        assert claim_limit.failed_attempts == 2
        assert offer is not None
        assert offer.state == "pending"
        assert offer.revision == 0
    finally:
        engine.dispose()


def test_cancel_expire_and_revoke_are_monotonic_and_idempotent(
    tmp_path: Path,
) -> None:
    engine, factory = _factory(tmp_path)
    _seed_owner_scope(factory)
    repository = SQLiteOperationScopedPairingRepository(factory)
    try:
        repository.create_offer(_offer(), mutation=_mutation())
        repository.claim_offer(
            _claim_command(),
            mutation=_operation_mutation(
                "claim",
                key_digit="3",
                request_digit="4",
                expected_revision=0,
            ),
        )
        cancelled = repository.cancel_pairing(
            CancelPairingCommand(
                tenant_id=TENANT_ID,
                owner_user_id=USER_ID,
                pairing_session_id=PAIRING_SESSION_ID,
                expected_revision=1,
                now=NOW + timedelta(seconds=20),
            ),
            mutation=_operation_mutation(
                "cancel",
                key_digit="5",
                request_digit="6",
                expected_revision=1,
            ),
        )
        assert cancelled.session is not None
        assert cancelled.session.state is PairingSessionState.CANCELLED
        assert cancelled.lifecycle is not None
        assert cancelled.lifecycle.state is DeviceLifecycleState.REVOKED

        other_offer = replace(
            _offer(),
            pairing_offer_id=UUID("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"),
            pairing_code_digest="3" * 64,
            bootstrap_secret_digest="4" * 64,
        )
        repository.create_offer(
            other_offer,
            mutation=_operation_mutation(
                "create",
                key_digit="7",
                request_digit="8",
                expected_revision=0,
            ),
        )
        expired = repository.expire_offer(
            other_offer.pairing_offer_id,
            now=other_offer.expires_at,
        )
        assert expired.state is PairingSessionState.EXPIRED
        assert (
            repository.expire_offer(
                other_offer.pairing_offer_id,
                now=other_offer.expires_at,
            )
            == expired
        )
    finally:
        engine.dispose()


def test_revoke_active_device_revokes_credential_and_lifecycle(
    tmp_path: Path,
) -> None:
    engine, factory = _factory(tmp_path)
    _seed_owner_scope(factory)
    repository = SQLiteOperationScopedPairingRepository(factory)
    try:
        repository.create_offer(_offer(), mutation=_mutation())
        repository.claim_offer(
            _claim_command(),
            mutation=_operation_mutation(
                "claim",
                key_digit="3",
                request_digit="4",
                expected_revision=0,
            ),
        )
        repository.confirm_owner(
            _confirm_command(),
            mutation=_operation_mutation(
                "confirm",
                key_digit="5",
                request_digit="6",
                expected_revision=1,
            ),
        )
        repository.activate_verified_credential(
            ActivatePairingCommand(
                tenant_id=TENANT_ID,
                pairing_offer_id=OFFER_ID,
                pairing_session_id=PAIRING_SESSION_ID,
                bootstrap_secret_digest="b" * 64,
                challenge_id=CHALLENGE_ID,
                challenge_digest="1" * 64,
                confirmation_digest="2" * 64,
                credential=_credential(),
                expected_revision=2,
                now=NOW + timedelta(seconds=30),
            ),
            mutation=_operation_mutation(
                "proof",
                key_digit="7",
                request_digit="8",
                expected_revision=2,
            ),
        )
        revoke_mutation = _operation_mutation(
            "revoke",
            key_digit="9",
            request_digit="a",
            expected_revision=1,
        )
        revoked = repository.revoke_device(
            RevokeDeviceCommand(
                tenant_id=TENANT_ID,
                owner_user_id=USER_ID,
                device_id=DEVICE_ID,
                now=NOW + timedelta(minutes=1),
            ),
            mutation=revoke_mutation,
        )
        replayed = repository.revoke_device(
            RevokeDeviceCommand(
                tenant_id=TENANT_ID,
                owner_user_id=USER_ID,
                device_id=DEVICE_ID,
                now=NOW + timedelta(minutes=1),
            ),
            mutation=revoke_mutation,
        )
        assert replayed == revoked
        assert revoked.lifecycle is not None
        assert revoked.lifecycle.state is DeviceLifecycleState.REVOKED
        assert revoked.credential is not None
        assert revoked.credential.status is DeviceCredentialStatus.REVOKED

        with factory.begin() as session:
            lifecycle = session.get(
                DeviceLifecycleModel,
                (TENANT_ID, DEVICE_ID),
            )
            credential = session.get(
                DeviceCredentialModel,
                (TENANT_ID, CREDENTIAL_ID),
            )
        assert lifecycle is not None
        assert lifecycle.state == "revoked"
        assert credential is not None
        assert credential.status == "revoked"
    finally:
        engine.dispose()
