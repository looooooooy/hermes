from __future__ import annotations

import importlib.util
import io
import sys
import warnings
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier, Event, Lock
from types import ModuleType
from uuid import UUID, uuid4, uuid5

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import event as sqlalchemy_event
from sqlalchemy import select
from sqlalchemy.exc import OperationalError, SAWarning
from sqlalchemy.orm import Session, sessionmaker

ROOT = Path(__file__).parents[1]
CLOUD_ROOT = ROOT.parents[1]
RUNNER = ROOT / "scripts" / "cleanup_test_seed_session.py"
sys.path.insert(0, str(CLOUD_ROOT / "src"))

from hermes_cloud.platform.postgres.models import (
    AgentModel,
    PasswordCredentialModel,
    RefreshSessionModel,
    RoleModel,
    TenantModel,
    UserModel,
    WorkspaceMembershipModel,
    WorkspaceModel,
)
from hermes_cloud.platform.sqlalchemy.observer_projection_models import (
    ObserverDeletionLedgerModel,
    ObserverEventModel,
    ObserverSessionModel,
)
from hermes_cloud.platform.sqlalchemy.observer_subscription_models import (
    ObserverSubscriptionTargetModel,
)
from hermes_cloud.platform.sqlalchemy.session_projection_migration_models import (
    SessionEventProjectionV10Model,
    SessionMessageProjectionV10Model,
    SessionProjectionCursorV10Model,
    SessionProjectionV10Model,
    WebSocketTicketV10Model,
)
from hermes_cloud.platform.sqlite.engine import build_sqlite_engine
from hermes_cloud.platform.sqlite.migrations import (
    PUBLISHED_SQLITE_MIGRATIONS,
    SQLiteSchemaMigration,
    upgrade_sqlite_schema,
)

NOW = datetime(2026, 7, 31, tzinfo=UTC)
RETENTION = NOW + timedelta(days=365)
NAMESPACE = UUID("ba84c827-b174-47f8-bbbd-52cbaf7232b9")


def _stable_id(kind: str, *parts: str) -> UUID:
    return uuid5(NAMESPACE, "\x1f".join((kind, *parts)))


def _load_runner() -> ModuleType:
    assert RUNNER.is_file(), "cleanup_test_seed_session.py is not implemented"
    spec = importlib.util.spec_from_file_location("hermes_cloud_seed_cleanup", RUNNER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _environment() -> dict[str, str]:
    return {
        "HERMES_SEED_TENANT_SLUG": "android-test",
        "HERMES_SEED_TENANT_DISPLAY_NAME": "Android Test",
        "HERMES_SEED_USERNAME": "android-user",
        "HERMES_SEED_USER_DISPLAY_NAME": "Android User",
        "HERMES_SEED_WORKSPACE_KEY": "android",
        "HERMES_SEED_WORKSPACE_DISPLAY_NAME": "Android",
        "HERMES_SEED_AGENT_KEY": "android-agent",
    }


def _apply_through_v10(engine: object) -> None:
    with engine.begin() as connection:  # type: ignore[union-attr]
        operations = Operations(MigrationContext.configure(connection))
        SQLiteSchemaMigration.__table__.create(connection)
        for migration in PUBLISHED_SQLITE_MIGRATIONS[:10]:
            migration.upgrade(operations)
    with Session(engine) as session, session.begin():
        session.add_all(
            SQLiteSchemaMigration(
                version=migration.version,
                name=migration.name,
                checksum=migration.checksum,
                applied_at=NOW,
            )
            for migration in PUBLISHED_SQLITE_MIGRATIONS[:10]
        )


def _seed_v10_database(engine: object) -> dict[str, UUID]:
    tenant_id = _stable_id("tenant", "android-test")
    user_id = _stable_id("user", "android-test", "android-user")
    role_id = _stable_id("role", "android-test", "android", "test-user")
    workspace_id = _stable_id("workspace", "android-test", "android")
    membership_id = _stable_id(
        "workspace-membership",
        str(tenant_id),
        str(workspace_id),
        str(user_id),
        str(role_id),
    )
    credential_id = _stable_id("password-credential", "android-test", "android-user")
    agent_id = _stable_id("agent", "android-test", "android", "android-agent")
    seed_session_id = _stable_id(
        "session", "android-test", "android", "android-bootstrap"
    )
    seed_message_id = _stable_id("session-message", str(seed_session_id), "1")
    other_session_id = uuid4()
    other_message_id = uuid4()
    refresh_session_id = uuid4()
    with Session(engine) as session, session.begin():
        session.add(
            TenantModel(
                tenant_id=tenant_id,
                slug="android-test",
                display_name="Android Test",
                status="active",
                created_at=NOW,
            )
        )
        session.flush()
        session.add(
            UserModel(
                tenant_id=tenant_id,
                user_id=user_id,
                subject="android-user",
                display_name="Android User",
                email=None,
                status="active",
                created_at=NOW,
            )
        )
        session.add(
            RoleModel(
                tenant_id=tenant_id,
                role_id=role_id,
                role_key="test-user",
                display_name="Test user",
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
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                workspace_key="android",
                display_name="Android",
                status="active",
                created_by=user_id,
                created_at=NOW,
            )
        )
        session.flush()
        session.add_all(
            (
                WorkspaceMembershipModel(
                    tenant_id=tenant_id,
                    workspace_membership_id=membership_id,
                    workspace_id=workspace_id,
                    user_id=user_id,
                    role_id=role_id,
                    status="active",
                    joined_at=NOW,
                    revoked_at=None,
                ),
                AgentModel(
                    tenant_id=tenant_id,
                    agent_id=agent_id,
                    workspace_id=workspace_id,
                    agent_key="android-agent",
                    status="active",
                    last_seen_at=None,
                    created_at=NOW,
                ),
                PasswordCredentialModel(
                    tenant_id=tenant_id,
                    credential_id=credential_id,
                    user_id=user_id,
                    subject="android-user",
                    password_hash="$argon2id$explicit-test-seed",
                    status="active",
                    created_at=NOW,
                    updated_at=NOW,
                ),
                RefreshSessionModel(
                    tenant_id=tenant_id,
                    refresh_session_id=refresh_session_id,
                    user_id=user_id,
                    token_digest="a" * 64,
                    rotation=0,
                    created_at=NOW,
                    rotated_at=None,
                    revoked_at=None,
                    expires_at=NOW + timedelta(hours=1),
                    retention_until=NOW + timedelta(days=1),
                ),
            )
        )
        session.flush()
        session.add_all(
            (
                SessionProjectionV10Model(
                    tenant_id=tenant_id,
                    session_id=seed_session_id,
                    session_key="android-bootstrap",
                    workspace_id=workspace_id,
                    agent_id=agent_id,
                    title="Hermes Cloud test session",
                    state="active",
                    revision=1,
                    lineage_tip_message_id=seed_message_id,
                    lineage_tip_sequence=1,
                    started_at=NOW,
                    updated_at=NOW,
                    closed_at=None,
                    retention_until=RETENTION,
                ),
                SessionProjectionV10Model(
                    tenant_id=tenant_id,
                    session_id=other_session_id,
                    session_key="preserve-me",
                    workspace_id=workspace_id,
                    agent_id=None,
                    title="Unrelated session",
                    state="active",
                    revision=1,
                    lineage_tip_message_id=other_message_id,
                    lineage_tip_sequence=1,
                    started_at=NOW,
                    updated_at=NOW,
                    closed_at=None,
                    retention_until=RETENTION,
                ),
            )
        )
        session.flush()
        session.add_all(
            (
                SessionMessageProjectionV10Model(
                    tenant_id=tenant_id,
                    session_id=seed_session_id,
                    message_id=seed_message_id,
                    sequence=1,
                    role="assistant",
                    content={"text": "Hermes Cloud is connected."},
                    parent_message_id=None,
                    created_at=NOW,
                    retention_until=RETENTION,
                ),
                SessionMessageProjectionV10Model(
                    tenant_id=tenant_id,
                    session_id=seed_session_id,
                    message_id=uuid4(),
                    sequence=2,
                    role="user",
                    content={"text": "old test interaction"},
                    parent_message_id=seed_message_id,
                    created_at=NOW + timedelta(seconds=1),
                    retention_until=RETENTION,
                ),
                SessionEventProjectionV10Model(
                    tenant_id=tenant_id,
                    session_id=seed_session_id,
                    event_id=uuid4(),
                    sequence=1,
                    event_type="message.complete",
                    payload={"test": True},
                    occurred_at=NOW,
                    retention_until=RETENTION,
                ),
                SessionProjectionCursorV10Model(
                    tenant_id=tenant_id,
                    session_id=seed_session_id,
                    stream="events",
                    last_sequence=1,
                    updated_at=NOW,
                ),
                SessionMessageProjectionV10Model(
                    tenant_id=tenant_id,
                    session_id=other_session_id,
                    message_id=other_message_id,
                    sequence=1,
                    role="assistant",
                    content={"text": "preserve"},
                    parent_message_id=None,
                    created_at=NOW,
                    retention_until=RETENTION,
                ),
                WebSocketTicketV10Model(
                    tenant_id=tenant_id,
                    ticket_id=uuid4(),
                    ticket_digest="b" * 64,
                    principal_type="user",
                    principal_id=user_id,
                    refresh_session_id=refresh_session_id,
                    session_key="android-bootstrap",
                    observer_scope=["session.control"],
                    issued_at=NOW,
                    expires_at=NOW + timedelta(minutes=1),
                    consumed_at=None,
                    retention_until=NOW + timedelta(days=1),
                ),
                WebSocketTicketV10Model(
                    tenant_id=tenant_id,
                    ticket_id=uuid4(),
                    ticket_digest="c" * 64,
                    principal_type="user",
                    principal_id=user_id,
                    refresh_session_id=refresh_session_id,
                    session_key=None,
                    observer_scope=["session.observe"],
                    issued_at=NOW,
                    expires_at=NOW + timedelta(minutes=1),
                    consumed_at=None,
                    retention_until=NOW + timedelta(days=1),
                ),
            )
        )
    return {
        "tenant_id": tenant_id,
        "seed_session_id": seed_session_id,
        "seed_message_id": seed_message_id,
        "other_session_id": other_session_id,
        "other_message_id": other_message_id,
        "agent_id": agent_id,
        "workspace_id": workspace_id,
    }


def _new_database(tmp_path: Path):
    engine = build_sqlite_engine(
        f"sqlite+pysqlite:///{tmp_path / 'cleanup.sqlite3'}",
        allow_missing=True,
    )
    _apply_through_v10(engine)
    identity = _seed_v10_database(engine)
    return engine, identity


def test_dry_run_and_apply_remove_only_the_exact_seed_session_graph(
    tmp_path: Path,
) -> None:
    runner = _load_runner()
    engine, identity = _new_database(tmp_path)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    config = runner.CleanupConfig.from_environment(_environment())

    planned = runner.cleanup_test_seed_session(
        session_factory=factory,
        config=config,
        apply=False,
    )

    assert planned.mode == "plan"
    assert planned.status == "ready"
    assert planned.session_id == identity["seed_session_id"]
    assert planned.counts == runner.CleanupCounts(
        sessions=1,
        messages=2,
        events=1,
        cursors=1,
        tickets=1,
    )
    with Session(engine) as session:
        assert (
            session.get(
                SessionProjectionV10Model,
                (identity["tenant_id"], identity["seed_session_id"]),
            )
            is not None
        )

    applied = runner.cleanup_test_seed_session(
        session_factory=factory,
        config=config,
        apply=True,
    )

    assert applied.mode == "apply"
    assert applied.status == "removed"
    assert applied.counts == planned.counts
    with Session(engine) as session:
        assert (
            session.get(
                SessionProjectionV10Model,
                (identity["tenant_id"], identity["seed_session_id"]),
            )
            is None
        )
        assert (
            session.get(
                SessionProjectionV10Model,
                (identity["tenant_id"], identity["other_session_id"]),
            )
            is not None
        )
        assert (
            session.get(
                SessionMessageProjectionV10Model,
                (
                    identity["tenant_id"],
                    identity["other_session_id"],
                    identity["other_message_id"],
                ),
            )
            is not None
        )
        tickets = tuple(session.scalars(select(WebSocketTicketV10Model)).all())
        assert len(tickets) == 1
        assert tickets[0].session_key is None
        assert session.get(TenantModel, identity["tenant_id"]) is not None
        assert (
            session.get(
                AgentModel,
                (identity["tenant_id"], identity["agent_id"]),
            )
            is not None
        )
    engine.dispose()


@pytest.mark.parametrize(
    "mutation",
    ("session_title", "seed_message", "missing_seed_message"),
)
def test_conflicting_or_partial_seed_graph_fails_closed_without_writes(
    tmp_path: Path,
    mutation: str,
) -> None:
    runner = _load_runner()
    engine, identity = _new_database(tmp_path)
    with Session(engine) as session, session.begin():
        projection = session.get(
            SessionProjectionV10Model,
            (identity["tenant_id"], identity["seed_session_id"]),
        )
        seed_message = session.get(
            SessionMessageProjectionV10Model,
            (
                identity["tenant_id"],
                identity["seed_session_id"],
                identity["seed_message_id"],
            ),
        )
        assert projection is not None and seed_message is not None
        if mutation == "session_title":
            projection.title = "Same key, not the explicit seed"
        elif mutation == "seed_message":
            seed_message.content = {"text": "changed"}
        else:
            session.delete(seed_message)

    with pytest.raises(runner.CleanupConflict):
        runner.cleanup_test_seed_session(
            session_factory=sessionmaker(bind=engine, expire_on_commit=False),
            config=runner.CleanupConfig.from_environment(_environment()),
            apply=True,
        )

    with Session(engine) as session:
        assert (
            session.get(
                SessionProjectionV10Model,
                (identity["tenant_id"], identity["seed_session_id"]),
            )
            is not None
        )
    engine.dispose()


def test_authoritative_observer_evidence_refuses_seed_cleanup(tmp_path: Path) -> None:
    runner = _load_runner()
    engine, identity = _new_database(tmp_path)
    with Session(engine) as session, session.begin():
        session.add(
            ObserverSessionModel(
                tenant_id=identity["tenant_id"],
                session_id=uuid4(),
                workspace_id=identity["workspace_id"],
                agent_id=identity["agent_id"],
                device_id=uuid4(),
                profile="default",
                session_key="android-bootstrap",
                runtime_session_id="real-runtime-session",
                runtime_generation="generation-1",
                connector_instance_id=str(uuid4()),
                connection_id=str(uuid4()),
                running=True,
                status="running",
                event_sequence=0,
                snapshot_event_sequence=0,
                snapshot_head_sequence=0,
                messages={},
                inflight={},
                replay_events={},
                payload_digest="d" * 64,
                updated_at=NOW,
                retention_until=RETENTION,
            )
        )

    with pytest.raises(runner.CleanupConflict, match="Observer"):
        runner.cleanup_test_seed_session(
            session_factory=sessionmaker(bind=engine, expire_on_commit=False),
            config=runner.CleanupConfig.from_environment(_environment()),
            apply=True,
        )
    engine.dispose()


def _add_subscription_target(
    engine: object,
    identity: dict[str, UUID],
    *,
    session_key: str,
) -> UUID:
    target_subscription_id = uuid4()
    with Session(engine) as session, session.begin():
        session.add(
            ObserverSubscriptionTargetModel(
                tenant_id=identity["tenant_id"],
                target_subscription_id=target_subscription_id,
                workspace_id=identity["workspace_id"],
                agent_id=identity["agent_id"],
                device_id=uuid4(),
                profile="default",
                session_key=session_key,
                state="active",
                active_ref_count=1,
                next_intent_sequence=1,
                revision=1,
                created_at=NOW,
                updated_at=NOW,
            )
        )
    return target_subscription_id


def test_authoritative_subscription_target_independently_refuses_cleanup(
    tmp_path: Path,
) -> None:
    runner = _load_runner()
    engine, identity = _new_database(tmp_path)
    _add_subscription_target(engine, identity, session_key="android-bootstrap")

    with pytest.raises(runner.CleanupConflict, match="Observer"):
        runner.cleanup_test_seed_session(
            session_factory=sessionmaker(bind=engine, expire_on_commit=False),
            config=runner.CleanupConfig.from_environment(_environment()),
            apply=True,
        )

    with Session(engine) as session:
        assert (
            session.get(
                SessionProjectionV10Model,
                (identity["tenant_id"], identity["seed_session_id"]),
            )
            is not None
        )
    engine.dispose()


def test_subscription_target_guard_is_session_key_mutation_sensitive(
    tmp_path: Path,
) -> None:
    runner = _load_runner()
    engine, identity = _new_database(tmp_path)
    target_id = _add_subscription_target(engine, identity, session_key="preserve-me")
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    config = runner.CleanupConfig.from_environment(_environment())

    assert (
        runner.cleanup_test_seed_session(
            session_factory=factory,
            config=config,
            apply=False,
        ).status
        == "ready"
    )

    with Session(engine) as session, session.begin():
        target = session.get(
            ObserverSubscriptionTargetModel,
            (identity["tenant_id"], target_id),
        )
        assert target is not None
        target.session_key = "android-bootstrap"

    with pytest.raises(runner.CleanupConflict, match="Observer"):
        runner.cleanup_test_seed_session(
            session_factory=factory,
            config=config,
            apply=False,
        )
    engine.dispose()


@pytest.mark.parametrize(
    "evidence",
    (("ledger",), ("event",), ("ledger", "event")),
    ids=("orphan-ledger", "orphan-event", "orphan-ledger-and-event"),
)
def test_orphan_observer_evidence_refuses_seed_cleanup(
    tmp_path: Path,
    evidence: tuple[str, ...],
) -> None:
    runner = _load_runner()
    engine, identity = _new_database(tmp_path)
    envelope = {
        "version": 1,
        "algorithm": "A256GCM",
        "key_version": "test-v1",
        "kek_fingerprint": "a" * 64,
        "wrap_nonce": "nonce",
        "wrapped_dek": "wrapped",
        "wrap_tag": "tag",
        "payload_nonce": "payload-nonce",
        "ciphertext": "ciphertext",
        "payload_tag": "payload-tag",
    }
    with Session(engine) as session, session.begin():
        if "ledger" in evidence:
            session.add(
                ObserverDeletionLedgerModel(
                    tenant_id=identity["tenant_id"],
                    session_id=uuid4(),
                    workspace_id=identity["workspace_id"],
                    agent_id=identity["agent_id"],
                    profile="default",
                    session_key="android-bootstrap",
                    state="pending",
                    attempts=1,
                    available_at=NOW,
                    last_error_code=None,
                    created_at=NOW,
                    updated_at=NOW,
                    deleted_at=None,
                )
            )
        if "event" in evidence:
            session.add(
                ObserverEventModel(
                    tenant_id=identity["tenant_id"],
                    session_id=uuid4(),
                    event_sequence=1,
                    event_sequence_start=1,
                    session_key="android-bootstrap",
                    runtime_session_id="observed-runtime-session",
                    event_type="message.complete",
                    payload=envelope,
                    payload_digest="b" * 64,
                    occurred_at=NOW,
                    retention_until=RETENTION,
                )
            )

    with pytest.raises(runner.CleanupConflict, match="Observer"):
        runner.cleanup_test_seed_session(
            session_factory=sessionmaker(bind=engine, expire_on_commit=False),
            config=runner.CleanupConfig.from_environment(_environment()),
            apply=True,
        )
    engine.dispose()


def test_cleanup_is_idempotent_after_the_exact_graph_is_absent(tmp_path: Path) -> None:
    runner = _load_runner()
    engine, _identity = _new_database(tmp_path)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    config = runner.CleanupConfig.from_environment(_environment())
    runner.cleanup_test_seed_session(
        session_factory=factory,
        config=config,
        apply=True,
    )

    second = runner.cleanup_test_seed_session(
        session_factory=factory,
        config=config,
        apply=True,
    )

    assert second.mode == "apply"
    assert second.status == "absent"
    assert second.counts == runner.CleanupCounts()
    engine.dispose()


def test_apply_writer_ownership_closes_the_observer_guard_delete_window(
    tmp_path: Path,
) -> None:
    runner = _load_runner()
    engine, identity = _new_database(tmp_path)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    config = runner.CleanupConfig.from_environment(_environment())
    delete_reached = Event()
    allow_delete = Event()
    insert_started = Event()
    insert_finished = Event()

    def pause_before_delete(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        normalized = " ".join(statement.split()).upper()
        if normalized.startswith("DELETE") and "WEBSOCKET_TICKETS" in normalized:
            delete_reached.set()
            assert allow_delete.wait(timeout=3)

    def insert_observer() -> None:
        insert_started.set()
        try:
            with Session(engine) as session, session.begin():
                session.add(
                    ObserverSessionModel(
                        tenant_id=identity["tenant_id"],
                        session_id=uuid4(),
                        workspace_id=identity["workspace_id"],
                        agent_id=identity["agent_id"],
                        device_id=uuid4(),
                        profile="default",
                        session_key="android-bootstrap",
                        runtime_session_id="racing-runtime-session",
                        runtime_generation="racing-generation",
                        connector_instance_id=str(uuid4()),
                        connection_id=str(uuid4()),
                        running=True,
                        status="running",
                        event_sequence=0,
                        snapshot_event_sequence=0,
                        snapshot_head_sequence=0,
                        messages={},
                        inflight={},
                        replay_events={},
                        payload_digest="e" * 64,
                        updated_at=NOW,
                        retention_until=RETENTION,
                    )
                )
        finally:
            insert_finished.set()

    sqlalchemy_event.listen(engine, "before_cursor_execute", pause_before_delete)
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            cleanup_future = executor.submit(
                runner.cleanup_test_seed_session,
                session_factory=factory,
                config=config,
                apply=True,
            )
            assert delete_reached.wait(timeout=3)
            insert_future = executor.submit(insert_observer)
            assert insert_started.wait(timeout=3)
            inserted_inside_guard_delete_window = insert_finished.wait(timeout=0.25)
            allow_delete.set()
            cleanup = cleanup_future.result(timeout=5)
            insert_future.result(timeout=5)
    finally:
        allow_delete.set()
        sqlalchemy_event.remove(engine, "before_cursor_execute", pause_before_delete)

    assert not inserted_inside_guard_delete_window
    assert cleanup.status == "removed"
    with Session(engine) as session:
        assert (
            session.get(
                SessionProjectionV10Model,
                (identity["tenant_id"], identity["seed_session_id"]),
            )
            is None
        )
        assert (
            session.scalar(
                select(ObserverSessionModel.session_id).where(
                    ObserverSessionModel.tenant_id == identity["tenant_id"],
                    ObserverSessionModel.session_key == "android-bootstrap",
                )
            )
            is not None
        )
    engine.dispose()


def test_concurrent_apply_has_one_removed_one_absent_and_no_stale_delete_warning(
    tmp_path: Path,
) -> None:
    runner = _load_runner()
    engine, _identity = _new_database(tmp_path)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    config = runner.CleanupConfig.from_environment(_environment())
    writer_barrier = Barrier(2)
    writer_attempts = 0
    writer_lock = Lock()

    def synchronize_writer_attempts(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        nonlocal writer_attempts
        normalized = " ".join(statement.split()).upper()
        if normalized.startswith("UPDATE") and "SESSIONS SET" in normalized:
            with writer_lock:
                writer_attempts += 1
            writer_barrier.wait(timeout=3)

    sqlalchemy_event.listen(
        engine, "before_cursor_execute", synchronize_writer_attempts
    )
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", SAWarning)
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = tuple(
                    executor.submit(
                        runner.cleanup_test_seed_session,
                        session_factory=factory,
                        config=config,
                        apply=True,
                    )
                    for _ in range(2)
                )
                results = tuple(future.result(timeout=8) for future in futures)
    finally:
        sqlalchemy_event.remove(
            engine,
            "before_cursor_execute",
            synchronize_writer_attempts,
        )

    assert writer_attempts == 2
    assert sorted(result.status for result in results) == ["absent", "removed"]
    removed = next(result for result in results if result.status == "removed")
    absent = next(result for result in results if result.status == "absent")
    assert removed.counts == runner.CleanupCounts(
        sessions=1,
        messages=2,
        events=1,
        cursors=1,
        tickets=1,
    )
    assert absent.counts == runner.CleanupCounts()
    assert not any(issubclass(item.category, SAWarning) for item in caught)
    engine.dispose()


def test_dependency_scans_are_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = _load_runner()
    engine, _identity = _new_database(tmp_path)
    monkeypatch.setattr(runner, "_MAX_SEED_DEPENDENCY_ROWS", 1, raising=False)

    with pytest.raises(runner.CleanupConflict, match="bounded"):
        runner.cleanup_test_seed_session(
            session_factory=sessionmaker(bind=engine, expire_on_commit=False),
            config=runner.CleanupConfig.from_environment(_environment()),
            apply=False,
        )
    engine.dispose()


def test_cleanup_queries_only_bounded_primary_keys_except_seed_fingerprint(
    tmp_path: Path,
) -> None:
    runner = _load_runner()
    engine, _identity = _new_database(tmp_path)
    statements: list[str] = []

    def capture_statement(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(" ".join(statement.split()))

    sqlalchemy_event.listen(engine, "before_cursor_execute", capture_statement)
    try:
        result = runner.cleanup_test_seed_session(
            session_factory=sessionmaker(bind=engine, expire_on_commit=False),
            config=runner.CleanupConfig.from_environment(_environment()),
            apply=False,
        )
    finally:
        sqlalchemy_event.remove(engine, "before_cursor_execute", capture_statement)

    assert result.status == "ready"
    observer_queries = {
        table: next(query for query in statements if f"FROM {table}" in query)
        for table in (
            "observer_sessions",
            "observer_subscription_targets",
            "observer_deletion_ledger",
            "observer_events",
        )
    }
    assert all(" LIMIT " in query for query in observer_queries.values())
    assert "observer_sessions.messages" not in observer_queries["observer_sessions"]
    assert "observer_events.payload" not in observer_queries["observer_events"]

    message_key_query = next(
        query
        for query in statements
        if "session_messages" in query and "session_messages.content" not in query
    )
    event_key_query = next(query for query in statements if "session_events" in query)
    cursor_key_query = next(query for query in statements if "session_cursors" in query)
    ticket_key_query = next(
        query for query in statements if "websocket_tickets" in query
    )
    assert all(
        " LIMIT " in query
        for query in (
            message_key_query,
            event_key_query,
            cursor_key_query,
            ticket_key_query,
        )
    )
    assert "session_events.payload" not in event_key_query
    assert "websocket_tickets.ticket_digest" not in ticket_key_query
    assert "websocket_tickets.observer_scope" not in ticket_key_query
    engine.dispose()


def test_cleanup_unblocks_revision_11_without_relabeling_the_seed(
    tmp_path: Path,
) -> None:
    runner = _load_runner()
    engine, identity = _new_database(tmp_path)
    with Session(engine) as session, session.begin():
        other_message = session.get(
            SessionMessageProjectionV10Model,
            (
                identity["tenant_id"],
                identity["other_session_id"],
                identity["other_message_id"],
            ),
        )
        other_session = session.get(
            SessionProjectionV10Model,
            (identity["tenant_id"], identity["other_session_id"]),
        )
        assert other_message is not None and other_session is not None
        session.delete(other_message)
        session.flush()
        session.delete(other_session)

    runner.cleanup_test_seed_session(
        session_factory=sessionmaker(bind=engine, expire_on_commit=False),
        config=runner.CleanupConfig.from_environment(_environment()),
        apply=True,
    )

    assert upgrade_sqlite_schema(engine).schema_version == 11
    engine.dispose()


def test_cli_plan_is_read_only_and_never_prints_the_database_reference(
    tmp_path: Path,
) -> None:
    runner = _load_runner()
    engine, identity = _new_database(tmp_path)
    engine.dispose()
    database_path = tmp_path / "cleanup.sqlite3"
    database_path.chmod(0o660)
    database_url = f"sqlite+pysqlite:///{database_path}"
    reference = tmp_path / "bootstrap-dsn"
    reference.write_text(database_url)
    reference.chmod(0o600)
    output = io.StringIO()

    with redirect_stdout(output):
        runner.main(
            [],
            environment={
                **_environment(),
                "HERMES_BOOTSTRAP_DSN_FILE": str(reference),
            },
        )

    rendered = output.getvalue()
    assert "cleanup_mode=plan status=ready" in rendered
    assert str(database_path) not in rendered
    assert database_url not in rendered
    reopened = build_sqlite_engine(database_url)
    with Session(reopened) as session:
        assert (
            session.get(
                SessionProjectionV10Model,
                (identity["tenant_id"], identity["seed_session_id"]),
            )
            is not None
        )
    reopened.dispose()


class _CommitThenRaiseTransaction:
    def __init__(self, delegate: object) -> None:
        self._delegate = delegate

    def __enter__(self) -> Session:
        return self._delegate.__enter__()  # type: ignore[no-any-return]

    def __exit__(self, exc_type, exc, traceback) -> bool | None:
        result = self._delegate.__exit__(exc_type, exc, traceback)
        if exc_type is None:
            raise RuntimeError("commit acknowledgement was lost")
        return result


class _CommitThenRaiseFactory:
    def __init__(self, delegate: object) -> None:
        self._delegate = delegate

    def begin(self) -> _CommitThenRaiseTransaction:
        return _CommitThenRaiseTransaction(self._delegate.begin())


class _CommitOperationalErrorTransaction(_CommitThenRaiseTransaction):
    def __exit__(self, exc_type, exc, traceback) -> bool | None:
        result = self._delegate.__exit__(exc_type, exc, traceback)
        if exc_type is None:
            raise OperationalError(
                "COMMIT",
                {},
                RuntimeError("commit acknowledgement unavailable"),
            )
        return result


class _CommitOperationalNoSecondPlanFactory:
    def __init__(self, delegate: object) -> None:
        self._delegate = delegate
        self.begin_calls = 0

    def begin(self) -> _CommitOperationalErrorTransaction:
        self.begin_calls += 1
        if self.begin_calls != 1:
            raise AssertionError("commit-stage failure must not trigger a second plan")
        return _CommitOperationalErrorTransaction(self._delegate.begin())


def test_lost_commit_acknowledgement_is_explicit_and_rerun_is_determinate(
    tmp_path: Path,
) -> None:
    runner = _load_runner()
    engine, _identity = _new_database(tmp_path)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    config = runner.CleanupConfig.from_environment(_environment())

    with pytest.raises(runner.CleanupCommitOutcomeUnknown):
        runner.cleanup_test_seed_session(
            session_factory=_CommitThenRaiseFactory(factory),
            config=config,
            apply=True,
        )

    rerun = runner.cleanup_test_seed_session(
        session_factory=factory,
        config=config,
        apply=False,
    )
    assert rerun.status == "absent"
    assert rerun.counts == runner.CleanupCounts()
    engine.dispose()


def test_commit_operational_error_is_unknown_and_cli_never_claims_unchanged(
    tmp_path: Path,
) -> None:
    runner = _load_runner()
    engine, _identity = _new_database(tmp_path)
    engine.dispose()
    database_path = tmp_path / "cleanup.sqlite3"
    database_path.chmod(0o660)
    database_url = f"sqlite+pysqlite:///{database_path}"
    reference = tmp_path / "bootstrap-dsn"
    reference.write_text(database_url)
    reference.chmod(0o600)
    captured: dict[str, _CommitOperationalNoSecondPlanFactory] = {}

    def build_factory(**options: object) -> object:
        delegate = sessionmaker(**options)
        wrapped = _CommitOperationalNoSecondPlanFactory(delegate)
        captured["factory"] = wrapped
        return wrapped

    with pytest.raises(SystemExit) as raised:
        runner.main(
            ["--apply"],
            environment={
                **_environment(),
                "HERMES_BOOTSTRAP_DSN_FILE": str(reference),
            },
            session_factory_builder=build_factory,
        )

    assert str(raised.value) == "cleanup outcome unknown; rerun plan"
    assert captured["factory"].begin_calls == 1
