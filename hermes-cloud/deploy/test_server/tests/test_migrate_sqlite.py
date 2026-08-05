from __future__ import annotations

import base64
import contextlib
import importlib.util
import io
import json
import stat
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Column, Integer, MetaData, Table, inspect, select
from sqlalchemy.orm import Session

ROOT = Path(__file__).parents[1]
CLOUD_ROOT = ROOT.parents[1]
RUNNER = ROOT / "scripts" / "migrate_sqlite.py"
sys.path.insert(0, str(CLOUD_ROOT / "src"))

spec = importlib.util.spec_from_file_location("hermes_cloud_sqlite_migration", RUNNER)
if spec is None or spec.loader is None:
    raise RuntimeError("SQLite migration runner cannot be loaded")
sqlite_migration = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = sqlite_migration
spec.loader.exec_module(sqlite_migration)

from hermes_cloud.modules.identity.domain import PasswordCredential
from hermes_cloud.platform.postgres.models import TenantModel, UserModel
from hermes_cloud.platform.sqlalchemy.observer_projection_models import (
    ObserverEventModel,
    ObserverProjectionBase,
    ObserverSessionModel,
)
from hermes_cloud.platform.sqlalchemy.observer_subscription_models import (
    ObserverSubscriptionBase,
)
from hermes_cloud.platform.sqlite import migrations as sqlite_migrations
from hermes_cloud.platform.sqlite.engine import build_sqlite_engine
from hermes_cloud.platform.sqlite.migrations import (
    CURRENT_SQLITE_SCHEMA_VERSION,
    PUBLISHED_SQLITE_MIGRATIONS,
    SQLITE_MIGRATION_TABLE,
    SQLiteSchemaMigration,
    sqlite_upgrade_coverage,
)
from hermes_cloud.platform.sqlite.repositories.identity import (
    SQLiteIdentityRepository,
)
from hermes_cloud.platform.sqlite.schema import build_sqlite_metadata

TENANT_ID = UUID("10000000-0000-4000-8000-000000000001")
WORKSPACE_ID = UUID("30000000-0000-4000-8000-000000000001")
AGENT_ID = UUID("50000000-0000-4000-8000-000000000001")
DEVICE_ID = UUID("60000000-0000-4000-8000-000000000001")
NOW = datetime(2026, 7, 31, 9, 0, tzinfo=UTC)


def _private_file(path: Path, value: str) -> str:
    path.write_text(value, encoding="utf-8")
    path.chmod(0o600)
    return str(path)


def _database_url(path: Path) -> str:
    return f"sqlite+pysqlite:///{path}"


def _observer_keyring(path: Path) -> str:
    return _private_file(
        path,
        json.dumps(
            {
                "version": 1,
                "tenants": {
                    "10000000-0000-4000-8000-000000000001": {
                        "current": "test-v1",
                        "keys": {
                            "test-v1": base64.b64encode(b"k" * 32).decode("ascii")
                        },
                    }
                },
            }
        ),
    )


def _seed_versioned_v2_plaintext_observer(engine: object) -> None:
    session_id = UUID("75000000-0000-4000-8000-000000000001")
    with engine.begin() as connection:  # type: ignore[union-attr]
        operations = Operations(MigrationContext.configure(connection))
        for migration in PUBLISHED_SQLITE_MIGRATIONS[:2]:
            migration.upgrade(operations)
        SQLiteSchemaMigration.__table__.create(connection)
        with Session(
            bind=connection,
            join_transaction_mode="create_savepoint",
        ) as session:
            session.add_all(
                [
                    SQLiteSchemaMigration(
                        version=migration.version,
                        name=migration.name,
                        checksum=migration.checksum,
                        applied_at=NOW,
                    )
                    for migration in PUBLISHED_SQLITE_MIGRATIONS[:2]
                ]
            )
            session.add(
                ObserverSessionModel(
                    tenant_id=TENANT_ID,
                    session_id=session_id,
                    workspace_id=WORKSPACE_ID,
                    agent_id=AGENT_ID,
                    device_id=DEVICE_ID,
                    profile="default",
                    session_key="session-root-1",
                    runtime_session_id="runtime-session-1",
                    runtime_generation="runtime-20260731-01",
                    connector_instance_id="74000000-0000-4000-8000-000000000001",
                    connection_id="73000000-0000-4000-8000-000000000001",
                    running=True,
                    status="running",
                    event_sequence=1,
                    snapshot_event_sequence=0,
                    snapshot_head_sequence=0,
                    messages=[{"role": "assistant", "content": "v2 plaintext"}],
                    inflight={
                        "user": None,
                        "assistant": None,
                        "streaming": False,
                        "error": None,
                    },
                    replay_events=[],
                    payload_digest="d" * 64,
                    updated_at=NOW,
                    retention_until=NOW + timedelta(days=30),
                )
            )
            session.add(
                ObserverEventModel(
                    tenant_id=TENANT_ID,
                    session_id=session_id,
                    event_sequence=1,
                    event_sequence_start=1,
                    session_key="session-root-1",
                    runtime_session_id="runtime-session-1",
                    event_type="message.delta",
                    payload={"text": "v2 event plaintext"},
                    payload_digest="e" * 64,
                    occurred_at=NOW,
                    retention_until=NOW + timedelta(days=30),
                )
            )
            session.commit()


def _publish_legacy_source(
    monkeypatch: pytest.MonkeyPatch,
    engine: object,
) -> None:
    excluded_identity = frozenset(sqlite_migrations._SESSION_PROJECTION_V10_TABLES)
    monkeypatch.setattr(
        sqlite_migrations,
        "PUBLISHED_SQLITE_LEGACY_SOURCES",
        (
            sqlite_migrations.PublishedSQLiteLegacySource(
                release="test-published-legacy-source",
                schema_fingerprint=sqlite_migrations.sqlite_schema_fingerprint(engine),
                raw_manifest_checksum=(
                    sqlite_migrations.sqlite_raw_manifest_checksum(engine)
                ),
                session_identity_remainder_schema_fingerprint=(
                    sqlite_migrations.sqlite_schema_fingerprint(
                        engine,
                        excluded_table_names=excluded_identity,
                    )
                ),
                session_identity_remainder_raw_manifest_checksum=(
                    sqlite_migrations.sqlite_raw_manifest_checksum(
                        engine,
                        excluded_table_names=excluded_identity,
                    )
                ),
            ),
        ),
    )


def test_sqlite_migration_is_dry_run_first_and_apply_is_idempotent(
    tmp_path: Path,
) -> None:
    database = tmp_path / "hermes-cloud.sqlite3"
    environment = {
        "HERMES_MIGRATION_DSN_FILE": _private_file(
            tmp_path / "migration-dsn",
            _database_url(database),
        ),
        "HERMES_OBSERVER_KEYRING_FILE": _observer_keyring(
            tmp_path / "observer-keyring.json"
        ),
    }
    stdout = io.StringIO()
    expected_table_count = len(build_sqlite_metadata().tables)
    coverage = sqlite_upgrade_coverage()

    with contextlib.redirect_stdout(stdout):
        sqlite_migration.main([], environment=environment)

    assert stdout.getvalue() == (
        "sqlite_migration_mode=plan "
        f"table_count={expected_table_count} "
        f"schema_version={CURRENT_SQLITE_SCHEMA_VERSION} "
        "historical_source_count="
        f"{len(sqlite_migrations.VERIFIED_SQLITE_UPGRADE_SOURCE_VERSIONS)} "
        "source=empty "
        f"recent_two_covered={str(coverage.recent_two_covered).lower()}\n"
    )
    assert not database.exists()

    for expected_existing, expected_source in (
        (False, "empty"),
        (True, "current"),
    ):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            sqlite_migration.main(["--apply"], environment=environment)
        assert stdout.getvalue() == (
            "sqlite_migration_mode=apply "
            f"table_count={expected_table_count} "
            f"database_existing={str(expected_existing).lower()} "
            f"schema_version={CURRENT_SQLITE_SCHEMA_VERSION} "
            f"source={expected_source} "
            f"recent_two_covered={str(coverage.recent_two_covered).lower()}\n"
        )
        assert database.is_file()
        assert stat.S_IMODE(database.stat().st_mode) == 0o660

    engine = build_sqlite_engine(_database_url(database))
    try:
        assert set(inspect(engine).get_table_names()) == {
            table.name for table in build_sqlite_metadata().tables.values()
        } | {
            table.name for table in ObserverProjectionBase.metadata.tables.values()
        } | {
            table.name for table in ObserverSubscriptionBase.metadata.tables.values()
        } | {"hermes_sqlite_schema_migrations"}

        tenant_id = uuid4()
        user_id = uuid4()
        tenant = TenantModel(
            tenant_id=tenant_id,
            slug="migration-gate",
            display_name="Migration Gate",
            status="active",
        )
        with Session(engine) as session, session.begin():
            session.add(tenant)
            session.add(
                UserModel(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    subject="migration-gate-user",
                    display_name="Migration Gate User",
                    email=None,
                    status="active",
                )
            )
            session.flush()
            repository = SQLiteIdentityRepository(session)
            credential = PasswordCredential(
                tenant_id=tenant_id,
                credential_id=uuid4(),
                user_id=user_id,
                subject="migration-gate-user",
                password_hash="$argon2id$synthetic-migration-gate",
                status="active",
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
            assert repository.store_password_credential(credential) == credential
            assert (
                repository.credential_by_subject(
                    tenant_id=tenant_id,
                    subject=credential.subject,
                )
                == credential
            )
    finally:
        engine.dispose()


def test_sqlite_migration_redacts_configuration_and_arguments(
    tmp_path: Path,
) -> None:
    sentinel = "synthetic-secret-must-not-leak"
    dsn_file = _private_file(tmp_path / "migration-dsn", sentinel)

    with pytest.raises(SystemExit) as invalid_config:
        sqlite_migration.main(
            ["--apply"],
            environment={
                "HERMES_MIGRATION_DSN_FILE": dsn_file,
                "HERMES_OBSERVER_KEYRING_FILE": _observer_keyring(
                    tmp_path / "observer-keyring.json"
                ),
            },
        )
    assert str(invalid_config.value) == "SQLite migration failed"
    assert sentinel not in str(invalid_config.value)

    stderr = io.StringIO()
    with (
        contextlib.redirect_stderr(stderr),
        pytest.raises(SystemExit) as invalid_arguments,
    ):
        sqlite_migration._arguments(["--password", sentinel])
    assert invalid_arguments.value.code == 2
    assert sentinel not in stderr.getvalue()
    assert stderr.getvalue().endswith("error: invalid arguments\n")


def test_sqlite_plan_validates_legacy_shape_without_mutating_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "legacy-current.sqlite3"
    database_url = _database_url(database)
    engine = build_sqlite_engine(database_url, allow_missing=True)
    try:
        build_sqlite_metadata().create_all(engine)
        _publish_legacy_source(monkeypatch, engine)
    finally:
        engine.dispose()
    database.chmod(0o660)
    environment = {
        "HERMES_MIGRATION_DSN_FILE": _private_file(
            tmp_path / "migration-dsn",
            database_url,
        ),
        "HERMES_OBSERVER_KEYRING_FILE": _observer_keyring(
            tmp_path / "observer-keyring.json"
        ),
    }
    stdout = io.StringIO()

    with contextlib.redirect_stdout(stdout):
        sqlite_migration.main([], environment=environment)

    assert "source=legacy-current" in stdout.getvalue()
    validation_engine = build_sqlite_engine(database_url)
    try:
        assert (
            SQLITE_MIGRATION_TABLE not in inspect(validation_engine).get_table_names()
        )
    finally:
        validation_engine.dispose()


def test_sqlite_plan_fails_closed_on_unknown_shape_without_success_output(
    tmp_path: Path,
) -> None:
    database = tmp_path / "drifted.sqlite3"
    database_url = _database_url(database)
    engine = build_sqlite_engine(database_url, allow_missing=True)
    try:
        build_sqlite_metadata().create_all(engine)
        Table(
            "unexpected_business_table",
            MetaData(),
            Column("id", Integer, primary_key=True),
        ).create(engine)
    finally:
        engine.dispose()
    database.chmod(0o660)
    environment = {
        "HERMES_MIGRATION_DSN_FILE": _private_file(
            tmp_path / "migration-dsn",
            database_url,
        ),
        "HERMES_OBSERVER_KEYRING_FILE": _observer_keyring(
            tmp_path / "observer-keyring.json"
        ),
    }
    stdout = io.StringIO()

    with (
        contextlib.redirect_stdout(stdout),
        pytest.raises(SystemExit, match="SQLite migration failed"),
    ):
        sqlite_migration.main([], environment=environment)

    assert stdout.getvalue() == ""
    validation_engine = build_sqlite_engine(database_url)
    try:
        assert (
            SQLITE_MIGRATION_TABLE not in inspect(validation_engine).get_table_names()
        )
    finally:
        validation_engine.dispose()


def test_sqlite_plan_for_missing_database_rejects_catalog_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "missing.sqlite3"
    published = sqlite_migrations.PUBLISHED_SQLITE_MIGRATIONS[0]
    monkeypatch.setattr(
        sqlite_migrations,
        "PUBLISHED_SQLITE_MIGRATIONS",
        (
            sqlite_migrations.PublishedSQLiteMigration(
                version=published.version,
                name=published.name,
                checksum="0" * 64,
                upgrade=published.upgrade,
            ),
        ),
    )
    environment = {
        "HERMES_MIGRATION_DSN_FILE": _private_file(
            tmp_path / "migration-dsn",
            _database_url(database),
        ),
        "HERMES_OBSERVER_KEYRING_FILE": _observer_keyring(
            tmp_path / "observer-keyring.json"
        ),
    }
    stdout = io.StringIO()

    with (
        contextlib.redirect_stdout(stdout),
        pytest.raises(SystemExit, match="SQLite migration failed"),
    ):
        sqlite_migration.main([], environment=environment)

    assert stdout.getvalue() == ""
    assert not database.exists()


def test_secure_database_file_does_not_rechmod_an_existing_secure_shared_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "shared-hermes-cloud.sqlite3"
    database.write_bytes(b"existing")
    database.chmod(0o660)

    def reject_redundant_chmod(_descriptor: int, _mode: int) -> None:
        raise PermissionError("non-owner cannot redundantly chmod shared database")

    monkeypatch.setattr(sqlite_migration.os, "fchmod", reject_redundant_chmod)

    sqlite_migration._secure_database_file(database)


def test_sqlite_migration_runner_passes_keyring_cipher_to_upgrade(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "cipher-wired.sqlite3"
    original_upgrade = sqlite_migration.upgrade_sqlite_schema
    observed = []

    def require_cipher(engine, *, observer_cipher):
        observed.append(observer_cipher)
        return original_upgrade(engine, observer_cipher=observer_cipher)

    monkeypatch.setattr(sqlite_migration, "upgrade_sqlite_schema", require_cipher)
    sqlite_migration.main(
        ["--apply"],
        environment={
            "HERMES_MIGRATION_DSN_FILE": _private_file(
                tmp_path / "migration-dsn",
                _database_url(database),
            ),
            "HERMES_OBSERVER_KEYRING_FILE": _observer_keyring(
                tmp_path / "observer-keyring.json"
            ),
        },
    )

    from hermes_cloud.platform.sqlalchemy.observer_encryption import (
        AesGcmTenantEnvelopeCipher,
    )

    assert len(observed) == 1
    assert isinstance(observed[0], AesGcmTenantEnvelopeCipher)


def test_sqlite_migration_runner_atomically_encrypts_real_v2_plaintext(
    tmp_path: Path,
) -> None:
    database = tmp_path / "v2-plaintext.sqlite3"
    database_url = _database_url(database)
    engine = build_sqlite_engine(database_url, allow_missing=True)
    try:
        _seed_versioned_v2_plaintext_observer(engine)
    finally:
        engine.dispose()
    database.chmod(0o660)

    sqlite_migration.main(
        ["--apply"],
        environment={
            "HERMES_MIGRATION_DSN_FILE": _private_file(
                tmp_path / "migration-dsn",
                database_url,
            ),
            "HERMES_OBSERVER_KEYRING_FILE": _observer_keyring(
                tmp_path / "observer-keyring.json"
            ),
        },
    )

    validation_engine = build_sqlite_engine(database_url)
    try:
        with Session(validation_engine) as session:
            stored = session.scalar(select(ObserverSessionModel))
            event = session.scalar(select(ObserverEventModel))
            assert stored is not None
            assert event is not None
            assert stored.messages["algorithm"] == "A256GCM"
            assert event.payload["algorithm"] == "A256GCM"
            assert "v2 plaintext" not in json.dumps(stored.messages)
            assert "v2 event plaintext" not in json.dumps(event.payload)
            assert session.get(SQLiteSchemaMigration, 3) is not None
    finally:
        validation_engine.dispose()


@pytest.mark.parametrize("keyring", (None, '{"version":1,"tenants":{}}'))
def test_sqlite_migration_runner_bad_keyring_preserves_real_v2_database(
    tmp_path: Path,
    keyring: str | None,
) -> None:
    database = tmp_path / "v2-keyring-failure.sqlite3"
    database_url = _database_url(database)
    engine = build_sqlite_engine(database_url, allow_missing=True)
    try:
        _seed_versioned_v2_plaintext_observer(engine)
    finally:
        engine.dispose()
    database.chmod(0o660)
    before = database.read_bytes()
    environment = {
        "HERMES_MIGRATION_DSN_FILE": _private_file(
            tmp_path / "migration-dsn",
            database_url,
        )
    }
    if keyring is not None:
        environment["HERMES_OBSERVER_KEYRING_FILE"] = _private_file(
            tmp_path / "bad-observer-keyring.json",
            keyring,
        )

    with pytest.raises(SystemExit, match="SQLite migration failed"):
        sqlite_migration.main(["--apply"], environment=environment)

    assert database.read_bytes() == before
    validation_engine = build_sqlite_engine(database_url)
    try:
        assert (
            "observer_inbox_messages"
            not in inspect(validation_engine).get_table_names()
        )
        with Session(validation_engine) as session:
            stored = session.scalar(select(ObserverSessionModel))
            assert stored is not None
            assert isinstance(stored.messages, list)
            assert session.get(SQLiteSchemaMigration, 3) is None
    finally:
        validation_engine.dispose()


@pytest.mark.parametrize("keyring", (None, '{"version":1,"tenants":{}}'))
def test_sqlite_migration_runner_fails_closed_before_creating_database_for_keyring(
    tmp_path: Path,
    keyring: str | None,
) -> None:
    database = tmp_path / "keyring-failure.sqlite3"
    environment = {
        "HERMES_MIGRATION_DSN_FILE": _private_file(
            tmp_path / "migration-dsn",
            _database_url(database),
        )
    }
    if keyring is not None:
        environment["HERMES_OBSERVER_KEYRING_FILE"] = _private_file(
            tmp_path / "bad-observer-keyring.json",
            keyring,
        )

    with pytest.raises(SystemExit, match="SQLite migration failed"):
        sqlite_migration.main(["--apply"], environment=environment)

    assert not database.exists()
