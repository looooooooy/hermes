from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from importlib.util import find_spec
from pathlib import Path
from threading import Barrier, Event, Lock
from uuid import UUID, uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import (
    DDL,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    PrimaryKeyConstraint,
    String,
    Table,
    Text,
    UniqueConstraint,
    create_engine,
    event,
    inspect,
    select,
)
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from hermes_cloud.domain.connector_gateway import ConnectorIdentity
from hermes_cloud.modules.identity.domain import PasswordCredential
from hermes_cloud.platform.postgres.models import TenantModel, UserModel
from hermes_cloud.platform.sqlalchemy.observer_projection_models import (
    ObserverInboxModel,
)
from hermes_cloud.platform.sqlite import migrations as sqlite_migrations
from hermes_cloud.platform.sqlite.engine import build_sqlite_engine
from hermes_cloud.platform.sqlite.migrations import (
    CURRENT_SQLITE_SCHEMA_VERSION,
    PUBLISHED_SQLITE_MIGRATIONS,
    SQLITE_MIGRATION_TABLE,
    SQLiteSchemaMigration,
    upgrade_sqlite_schema,
)
from hermes_cloud.platform.sqlite.repositories.identity import (
    SQLiteIdentityRepository,
)
from hermes_cloud.platform.sqlite.schema import build_sqlite_metadata

LEGACY_SCHEMA_FIXTURE = (
    Path(__file__).parents[2] / "fixtures" / "sqlite" / "20260731T084500Z-schema.json"
)

# Read-only schema evidence from deployed release 20260801T131728Z. The release
# has the same Inspector shape as the deterministic rev10 catalog, but its rev1
# baseline retained this exact SQLAlchemy constraint order in sqlite_master.
DEPLOYED_20260801T131728Z_V10_CONSTRAINT_ORDER = {
    "access_grants": (0, 3, 5, 1, 2, 4),
    "agents": (0, 3, 1, 2),
    "attempts": (0, 3, 6, 5, 1, 4, 2),
    "audit_events": (0, 1, 3, 2),
    "commands": (0, 4, 3, 2, 6, 1, 5),
    "control_commands": (0, 7, 3, 1, 2, 4, 6, 5),
    "device_authentication_challenges": (0, 1, 3, 8, 4, 7, 9, 5, 6, 2),
    "device_credential_public_keys": (0, 1, 4, 2, 3),
    "device_credentials": (0, 1, 4, 3, 5, 6, 7, 2),
    "device_lifecycles": (0, 2, 5, 4, 6, 1, 3),
    "devices": (0, 2, 1, 4, 3),
    "hermes_cloud_migrations": (0, 2, 1),
    "inbox_messages": (0, 2, 1),
    "memberships": (0, 2, 1, 4, 3),
    "outbox_events": (0, 1, 3, 4, 2),
    "pairing_claim_limits": (0, 5, 4, 3, 1, 2),
    "pairing_enrollment_proofs": (0, 4, 10, 7, 6, 3, 2, 8, 9, 11, 1, 5),
    "pairing_idempotency_records": (0, 4, 9, 10, 5, 2, 8, 7, 3, 1, 6),
    "pairing_offers": (0, 2, 5, 6, 8, 4, 7, 3, 1),
    "pairing_sessions": (0, 8, 7, 2, 3, 1, 9, 5, 4, 6, 10),
    "password_credentials": (0, 2, 3, 6, 5, 4, 1),
    "policies": (0, 2, 4, 1, 3),
    "refresh_sessions": (0, 7, 5, 3, 8, 1, 4, 6, 2),
    "roles": (0, 1, 3, 5, 2, 4),
    "session_cursors": (0, 2, 5, 3, 4, 1),
    "session_events": (0, 2, 5, 3, 4, 1),
    "session_messages": (0, 1, 3, 2, 6, 4, 5),
    "sessions": (0, 5, 2, 6, 1, 7, 3, 8, 4),
    "tenants": (0, 2, 1),
    "transitions": (0, 4, 1, 5, 2, 3),
    "users": (0, 4, 3, 2, 1),
    "websocket_tickets": (0, 2, 4, 1, 6, 5, 8, 7, 3),
    "workspace_memberships": (0, 4, 3, 6, 5, 7, 1, 2),
    "workspaces": (0, 4, 1, 2, 3),
}
DEPLOYED_20260801T131728Z_V10_SIGNATURE = (
    "f43658517c47ec0336e7e061ec4ee04aa976f3ee9d91b659a8c35720bb3944be",
    "df2b0f97389e0844c7e0f665b2d4a3caf52b460d4b94551a74bd34ccebd54820",
)


def _database_url(path: Path) -> str:
    return f"sqlite+pysqlite:///{path}"


def test_revision_13_is_the_catalog_recovery_boundary() -> None:
    assert CURRENT_SQLITE_SCHEMA_VERSION == 13
    assert PUBLISHED_SQLITE_MIGRATIONS[-2].name == (
        "0012_session_catalog_v1"
    )
    assert PUBLISHED_SQLITE_MIGRATIONS[-1].name == (
        "0013_session_catalog_recovery"
    )


def test_published_versioned_database_signatures_are_frozen() -> None:
    assert sqlite_migrations.PUBLISHED_SQLITE_VERSIONED_DATABASE_SIGNATURES == (
        (
            1,
            "1a1bae52abe0ed37df54a8228a07f675335cc7d56933a8271377149bb66f5b08",
            "d2c3acac84c8a878a6151dea6faf34cae896fa551293503a5a1fb9a2fab114dc",
        ),
        (
            2,
            "412a25939ea1ad7f73feb4cc104b828d2cfa3e16ea370de03898fde194beb5ab",
            "8c32fcc93cc4b0da10b61fe73db250e6a0aa79f1f44d5c131c0e5dabb80dc569",
        ),
        (
            3,
            "837fd45bd6122cdf9944f834af6c17e106f23ee8ae7721080bf9deb28c02b9d5",
            "d253d39f51000e8640cda71cca3a8a9843ba12f26b00595412cbe958e0aa1f7f",
        ),
        (
            4,
            "785a3219ac5e28055afca4fb8a2ecb6dfcb8cb5822ef01331e9c50b8d2f79321",
            "51701b0680344c0903cbb4e13a02f825ebcbf101ce4455cb3f91b6fb4c2a3f90",
        ),
        (
            5,
            "3d362bd44c52de259786c5735f6cdf51b440d93beb5ad950cc6d6ee2ea82879d",
            "811b80b8e14f1b73957886ae3f1d5b40fe1a64c173745c0ac983c9cc975c2910",
        ),
        (
            6,
            "a9d8bc32cdfadda815603937376e5459dfd6d37073b8097ac1c96f3447ef8ea7",
            "5a3f51ebd8ca250ea20e17fa33a64a25c0a7ce62f70b5bb4672923cdce029795",
        ),
        (
            7,
            "fb84e2cb6a89005f1391823780b10797e3ab173370694a5368f8401462cc3728",
            "ec4281633af120e35cf9e520323a90e5764a4c4a53093141b74e6458ef4a2640",
        ),
        (
            8,
            "b8fb53ec2ea6512bb323c8a239c04b4661dadede58dd3e0763e244280ae54cd8",
            "674a6b69ed1de03bfe0e6e37335b32efcb19122a0e31d652308085741431500f",
        ),
        (
            9,
            "cd6d69a79500454e738f430530aee85eed53dc1a273e4826753eecf614e1501b",
            "d018da7ce3e2e9a92d90e6babc07374603c87a4d8fa45e86a3b00dc4e5e9cefd",
        ),
        (
            10,
            "cd6d69a79500454e738f430530aee85eed53dc1a273e4826753eecf614e1501b",
            "d018da7ce3e2e9a92d90e6babc07374603c87a4d8fa45e86a3b00dc4e5e9cefd",
        ),
        (
            11,
            "477f375ded4d2b94af99937bda0471e7bdca541ad0c2ff8e9b0742cc1ac5aa17",
            "9cd9cae934ac0f18998fa88f28ff2030a2ca4088b2a7b3282b5ca0ba75cd890b",
        ),
        (
            12,
            "e97b56be169f52db2e48d9e86b04d24f2c858a8638b2d64b2d1ba25400c13f6d",
            "952081ea3adb794c378af59e7977fbe966b744a766a7ef1eebf6799cf415d682",
        ),
        (
            13,
            "ac1e2e27a6d8341929fa89cd854b81831c6c577ee5ef7e0d87d8a243e46166fa",
            "3071a7209223b19650eff422614775582329f42e5007ab28936fda339e86ffe4",
        ),
    )


def test_published_current_component_signatures_are_frozen() -> None:
    assert sqlite_migrations.PUBLISHED_SQLITE_CURRENT_SESSION_IDENTITY_SIGNATURE == (
        "363985e16c9dba16d0cd328039a3034968d281762bef5f2066fe256c6fbf7146",
        "29220c8b67daa35a12d0c4f16299f12688d6e7c748b6796cfea6a8bdbca53018",
    )
    assert sqlite_migrations.PUBLISHED_SQLITE_CURRENT_OBSERVER_SIGNATURE == (
        "71f149ed1dfe729ad77f0e4eacd55a69120ea7ae385ec39d37bed64dffaa9b09",
        "d2f3683da3d5a00d1f04aa1df495024e9c8502e6939c150781f0e1978f82808f",
    )
    assert sqlite_migrations.PUBLISHED_SQLITE_CONNECTOR_TRANSPORT_SIGNATURE == (
        "f5b029f741077ccaff7a7c5a2c040b18537b623de5f6294bf8f1b07f938bbda1",
        "4aeeafd605c7023a55d7df8e1971dc491642907724eb50360782835c7d66fe77",
    )
    assert sqlite_migrations.PUBLISHED_SQLITE_CONNECTOR_HANDSHAKE_SIGNATURE == (
        "bb55ec1899fcb8d7d93c8f1a486e115603ad4c4a34c35da259b6da904a6575d3",
        "6899eb241728655d8a48fe2d1948fdda312fbb7fb3eab8f2b873408701dc90f3",
    )
    assert sqlite_migrations.PUBLISHED_SQLITE_SESSION_CATALOG_SIGNATURE == (
        "b4e13440f9c0cba3ed999e588c7f065f8667d22346f5190087659b9cbd363184",
        "4233877360d79737f7d70ce44ec0a32924ede3de2a4358066d03207687bdf499",
    )


def test_deployed_rev10_compatibility_catalog_freezes_only_exact_schema_evidence(
) -> None:
    assert (
        sqlite_migrations.PUBLISHED_SQLITE_VERSIONED_DATABASE_COMPATIBILITY_SOURCES
        == (
            sqlite_migrations.PublishedSQLiteVersionedDatabaseCompatibilitySource(
                release="20260801T131728Z",
                version=10,
                source="versioned-10",
                schema_fingerprint=DEPLOYED_20260801T131728Z_V10_SIGNATURE[0],
                raw_manifest_checksum=DEPLOYED_20260801T131728Z_V10_SIGNATURE[1],
            ),
        )
    )


def test_schema_catalog_orm_uses_the_sqlite_324_compatible_name() -> None:
    assert sqlite_migrations.SQLiteSchemaObject.__table__.name == "sqlite_master"


def _replay_published_legacy_fixture(
    engine: Engine,
    *,
    mutate_raw_ddl: bool = False,
) -> None:
    manifest = json.loads(LEGACY_SCHEMA_FIXTURE.read_text(encoding="utf-8"))
    object_order = {"table": 0, "index": 1, "view": 2, "trigger": 3}
    ordered_manifest = sorted(
        manifest,
        key=lambda row: (object_order[row["type"]], row["name"]),
    )
    with engine.begin() as connection:
        for row in ordered_manifest:
            definition = row["definition"]
            if mutate_raw_ddl and row["type"] == "table" and row["name"] == "tenants":
                definition = definition.replace(
                    "slug TEXT NOT NULL",
                    "slug /* unrecognized release drift */ TEXT NOT NULL",
                )
            connection.execute(DDL(definition.replace("%", "%%")))


def _add_typed_orm_ledger(
    engine: Engine,
    *,
    through_version: int = 1,
    mutate_raw_ddl: bool = False,
) -> None:
    with engine.begin() as connection:
        if mutate_raw_ddl:
            connection.execute(
                DDL(
                    """CREATE TABLE hermes_sqlite_schema_migrations (
                    version /* unrecognized ledger drift */ INTEGER NOT NULL,
                    name VARCHAR(120) NOT NULL,
                    checksum VARCHAR(64) NOT NULL,
                    applied_at DATETIME NOT NULL,
                    PRIMARY KEY (version),
                    UNIQUE (name)
                    )"""
                )
            )
        else:
            SQLiteSchemaMigration.__table__.create(connection)
    with Session(engine) as session, session.begin():
        for migration in PUBLISHED_SQLITE_MIGRATIONS[:through_version]:
            session.add(
                SQLiteSchemaMigration(
                    version=migration.version,
                    name=migration.name,
                    checksum=migration.checksum,
                    applied_at=applied_at(),
                )
            )


def _apply_published_migrations_through(engine: Engine, version: int) -> None:
    """Build an immutable historical source using its typed operations."""

    with engine.begin() as connection:
        operations = Operations(MigrationContext.configure(connection))
        SQLiteSchemaMigration.__table__.create(connection)
        for migration in PUBLISHED_SQLITE_MIGRATIONS[:version]:
            migration.upgrade(operations)
    with Session(engine) as session, session.begin():
        for migration in PUBLISHED_SQLITE_MIGRATIONS[:version]:
            session.add(
                SQLiteSchemaMigration(
                    version=migration.version,
                    name=migration.name,
                    checksum=migration.checksum,
                    applied_at=applied_at(),
                )
            )


def _apply_deployed_20260801t131728z_rev10_fixture(engine: Engine) -> None:
    reference = create_engine("sqlite+pysqlite:///:memory:")
    try:
        with reference.begin() as connection:
            sqlite_migrations._create_v1_schema(
                Operations(MigrationContext.configure(connection))
            )


        manifest = sqlite_migrations._raw_schema_manifest(reference)
    finally:
        reference.dispose()

    object_order = {"table": 0, "index": 1, "view": 2, "trigger": 3}
    with engine.begin() as connection:
        operations = Operations(MigrationContext.configure(connection))
        SQLiteSchemaMigration.__table__.create(connection)
        for row in sorted(
            manifest,
            key=lambda item: (object_order[item["type"]], item["name"]),
        ):
            definition = row["definition"]
            assert definition is not None
            order = DEPLOYED_20260801T131728Z_V10_CONSTRAINT_ORDER.get(row["name"])
            if row["type"] == "table" and order is not None:
                opening, closing = sqlite_migrations._table_body_bounds(definition)
                segments = sqlite_migrations._split_table_body(
                    definition[opening + 1 : closing]
                )
                columns = [
                    segment.strip()
                    for segment in segments
                    if not sqlite_migrations._is_table_constraint(segment)
                ]
                constraints = [
                    segment.strip()
                    for segment in segments
                    if sqlite_migrations._is_table_constraint(segment)
                ]
                definition = (
                    f"{definition[: opening + 1]}\n\t"
                    + ", \n\t".join(
                        (*columns, *(constraints[index] for index in order))
                    )
                    + f"\n{definition[closing:]}"
                )
            connection.execute(DDL(definition.replace("%", "%%")))
        for migration in PUBLISHED_SQLITE_MIGRATIONS[1:10]:
            migration.upgrade(operations)
    with Session(engine) as session, session.begin():
        for migration in PUBLISHED_SQLITE_MIGRATIONS[:10]:
            session.add(
                SQLiteSchemaMigration(
                    version=migration.version,
                    name=migration.name,
                    checksum=migration.checksum,
                    applied_at=applied_at(),
                )
            )


def _mutate_deployed_rev10_raw_table_ddl(engine: Engine) -> None:
    definition = next(
        row["definition"]
        for row in sqlite_migrations._raw_schema_manifest(engine)
        if row["type"] == "table" and row["name"] == "tenants"
    )
    assert definition is not None
    mutated = definition.replace(
        "slug TEXT NOT NULL",
        "slug /* unpublished raw-only drift */ TEXT NOT NULL",
    )
    assert mutated != definition
    with engine.begin() as connection:
        connection.execute(DDL("DROP TABLE tenants"))
        connection.execute(DDL(mutated.replace("%", "%%")))


def test_exact_deployed_20260801t131728z_rev10_upgrades_to_current(
    tmp_path: Path,
) -> None:
    database = tmp_path / "deployed-20260801t131728z-rev10.sqlite3"
    engine = build_sqlite_engine(_database_url(database), allow_missing=True)
    try:
        _apply_deployed_20260801t131728z_rev10_fixture(engine)

        assert sqlite_migrations._schema_signature(engine) == (
            DEPLOYED_20260801T131728Z_V10_SIGNATURE
        )
        plan = sqlite_migrations.plan_sqlite_schema(engine)
        assert plan.schema_version == 10
        assert plan.source == "versioned-10"

        result = upgrade_sqlite_schema(engine)
        assert result.source == "versioned-10"
        assert sqlite_migrations.plan_sqlite_schema(engine).source == "current"
        with Session(engine) as session:
            assert (
                session.query(SQLiteSchemaMigration).count()
                == CURRENT_SQLITE_SCHEMA_VERSION
            )
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "mutation",
    (
        "single_field",
        "raw_only",
        "ledger_checksum",
        "missing_v10",
        "extra_v11",
        "future_row",
    ),
)
def test_deployed_20260801t131728z_rev10_compatibility_fails_closed_on_drift(
    tmp_path: Path,
    mutation: str,
) -> None:
    database = tmp_path / f"deployed-rev10-{mutation}.sqlite3"
    engine = build_sqlite_engine(_database_url(database), allow_missing=True)
    try:
        _apply_deployed_20260801t131728z_rev10_fixture(engine)
        if mutation == "single_field":
            with engine.begin() as connection:
                Operations(MigrationContext.configure(connection)).add_column(
                    "tenants",
                    Column("unpublished_compatibility_field", String(1)),
                )
        elif mutation == "raw_only":
            _mutate_deployed_rev10_raw_table_ddl(engine)
            canonical, raw = sqlite_migrations._schema_signature(engine)
            assert canonical == DEPLOYED_20260801T131728Z_V10_SIGNATURE[0]
            assert raw != DEPLOYED_20260801T131728Z_V10_SIGNATURE[1]
        elif mutation == "ledger_checksum":
            with Session(engine) as session, session.begin():
                applied = session.get(SQLiteSchemaMigration, 10)
                assert applied is not None
                applied.checksum = "0" * 64
        elif mutation == "missing_v10":
            with Session(engine) as session, session.begin():
                applied = session.get(SQLiteSchemaMigration, 10)
                assert applied is not None
                session.delete(applied)
        elif mutation == "extra_v11":
            migration = PUBLISHED_SQLITE_MIGRATIONS[10]
            with Session(engine) as session, session.begin():
                session.add(
                    SQLiteSchemaMigration(
                        version=migration.version,
                        name=migration.name,
                        checksum=migration.checksum,
                        applied_at=applied_at(),
                    )
                )
        else:
            with Session(engine) as session, session.begin():
                session.add(
                    SQLiteSchemaMigration(
                        version=12,
                        name="0012_unpublished_future",
                        checksum="1" * 64,
                        applied_at=applied_at(),
                    )
                )
        schema_before = sqlite_migrations.sqlite_raw_manifest_checksum(engine)

        with pytest.raises(
            sqlite_migrations.SQLiteMigrationHistoryConflict,
            match="SQLite migration history conflicts",
        ):
            sqlite_migrations.plan_sqlite_schema(engine)
        with pytest.raises(
            sqlite_migrations.SQLiteMigrationHistoryConflict,
            match="SQLite migration history conflicts",
        ):
            upgrade_sqlite_schema(engine)

        assert sqlite_migrations.sqlite_raw_manifest_checksum(engine) == schema_before
        with Session(engine) as session:
            if mutation == "extra_v11":
                assert session.get(SQLiteSchemaMigration, 11) is not None
            else:
                assert session.get(SQLiteSchemaMigration, 11) is None
            if mutation == "ledger_checksum":
                applied = session.get(SQLiteSchemaMigration, 10)
                assert applied is not None
                assert applied.checksum == "0" * 64
            elif mutation == "missing_v10":
                assert session.get(SQLiteSchemaMigration, 10) is None
            elif mutation == "future_row":
                assert session.get(SQLiteSchemaMigration, 12) is not None
    finally:
        engine.dispose()


@pytest.mark.parametrize("version", (1, 5, 9, 10))
def test_deployed_rev10_compatibility_preserves_published_versioned_sources(
    tmp_path: Path,
    version: int,
) -> None:
    database = tmp_path / f"published-versioned-{version}.sqlite3"
    engine = build_sqlite_engine(_database_url(database), allow_missing=True)
    try:
        _apply_published_migrations_through(engine, version)

        assert sqlite_migrations.plan_sqlite_schema(engine).source == (
            f"versioned-{version}"
        )
        assert upgrade_sqlite_schema(engine).source == f"versioned-{version}"
        assert sqlite_migrations.plan_sqlite_schema(engine).source == "current"
    finally:
        engine.dispose()


def _apply_legacy_v1_overlays_through(engine: Engine, version: int) -> None:
    """Replay published overlay migrations after an adopted legacy v1 base."""

    for migration in PUBLISHED_SQLITE_MIGRATIONS[1:version]:
        with engine.begin() as connection:
            migration.upgrade(Operations(MigrationContext.configure(connection)))
        with Session(engine) as session, session.begin():
            session.add(
                SQLiteSchemaMigration(
                    version=migration.version,
                    name=migration.name,
                    checksum=migration.checksum,
                    applied_at=applied_at(),
                )
            )


def _apply_published_legacy_fixture_through_current(engine: Engine) -> None:
    _replay_published_legacy_fixture(engine)
    _add_typed_orm_ledger(engine)
    _apply_legacy_v1_overlays_through(engine, CURRENT_SQLITE_SCHEMA_VERSION)


def _legacy_current_component_signature(
    engine: Engine,
    component: str,
) -> tuple[str, str]:
    table_names = frozenset(inspect(engine).get_table_names())
    if component == "session_identity":
        signature = sqlite_migrations._session_identity_schema_signature(engine)
        assert signature is not None
        return signature
    if component == "observer":
        component_tables = frozenset(
            table.name
            for table in (
                *sqlite_migrations.ObserverProjectionBase.metadata.tables.values(),
                *sqlite_migrations.ObserverSubscriptionBase.metadata.tables.values(),
            )
        )
    elif component == "transport":
        component_tables = frozenset(
            {sqlite_migrations.ConnectorTransportCursorModel.__table__.name}
        )
    elif component == "handshake":
        component_tables = frozenset(
            {
                sqlite_migrations.ConnectorTransportHandshakeOwnershipModel.__table__.name,
                sqlite_migrations.ConnectorObserverReceiptModel.__table__.name,
            }
        )
    else:
        raise AssertionError(f"unknown component: {component}")
    return sqlite_migrations._schema_signature(
        engine,
        excluded_table_names=table_names - component_tables,
    )


def _assert_identity_repository_read_write(engine: Engine, *, slug: str) -> None:
    tenant_id = uuid4()
    user_id = uuid4()
    now = applied_at()
    with Session(engine) as session, session.begin():
        session.add(
            TenantModel(
                tenant_id=tenant_id,
                slug=slug,
                display_name="Migration Gate",
                status="active",
                created_at=now,
            )
        )
        session.add(
            UserModel(
                tenant_id=tenant_id,
                user_id=user_id,
                subject=f"{slug}-user",
                display_name="Migration Gate User",
                email=None,
                status="active",
                created_at=now,
            )
        )
        session.flush()
        repository = SQLiteIdentityRepository(session)
        credential = PasswordCredential(
            tenant_id=tenant_id,
            credential_id=uuid4(),
            user_id=user_id,
            subject=f"{slug}-user",
            password_hash="$argon2id$synthetic-migration-gate",
            status="active",
            created_at=now,
            updated_at=now,
        )
        repository.store_password_credential(credential)
        assert (
            repository.credential_by_subject(
                tenant_id=tenant_id,
                subject=credential.subject,
            )
            == credential
        )


def _shape_metadata(mutation: str | None = None) -> MetaData:
    metadata = MetaData()
    parent_reference = (
        () if mutation == "foreign_key" else (ForeignKey("parents.parent_id"),)
    )
    parent_id = Column(
        "parent_id",
        Integer,
        primary_key=mutation != "primary_key",
        nullable=False,
    )
    Table("parents", metadata, parent_id)
    child_arguments: list[object] = [
        Column("child_id", Integer, primary_key=True),
        Column(
            "parent_id",
            Integer,
            *parent_reference,
            nullable=False,
        ),
        Column(
            "label",
            String(21 if mutation == "column_type" else 20),
            nullable=mutation == "nullable",
        ),
    ]
    if mutation != "unique":
        child_arguments.append(UniqueConstraint("label", name="uq_children_label"))
    if mutation != "check":
        child_arguments.append(
            CheckConstraint("length(label) > 0", name="ck_children_label")
        )
    children = Table("children", metadata, *child_arguments)
    if mutation != "index":
        Index("ix_children_parent_id", children.c.parent_id)
    if mutation == "table":
        Table("unexpected", metadata, Column("id", Integer, primary_key=True))
    return metadata


def _shape_fingerprint(tmp_path: Path, mutation: str | None = None) -> str:
    database = tmp_path / f"shape-{mutation or 'baseline'}.sqlite3"
    engine = build_sqlite_engine(_database_url(database), allow_missing=True)
    try:
        _shape_metadata(mutation).create_all(engine)
        return sqlite_migrations.sqlite_schema_fingerprint(engine)
    finally:
        engine.dispose()


def _feature_fingerprint(tmp_path: Path, mutation: str | None = None) -> str:
    database = tmp_path / f"feature-{mutation or 'baseline'}.sqlite3"
    engine = build_sqlite_engine(_database_url(database), allow_missing=True)
    try:
        if mutation == "virtual_table":
            with engine.begin() as connection:
                connection.execute(
                    DDL("CREATE VIRTUAL TABLE feature_table USING fts5(content)")
                )
        else:
            metadata = MetaData()
            column_type = Text(collation="NOCASE") if mutation == "collate" else Text()
            table_arguments: list[object] = [
                Column("id", Integer, nullable=False),
                Column("content", column_type, nullable=False),
                PrimaryKeyConstraint(
                    "id",
                    sqlite_on_conflict="FAIL" if mutation == "on_conflict" else None,
                ),
            ]
            table_options = {
                "sqlite_autoincrement": mutation == "autoincrement",
                "sqlite_strict": mutation == "strict",
                "sqlite_with_rowid": mutation != "without_rowid",
            }
            Table(
                "feature_table",
                metadata,
                *table_arguments,
                **table_options,
            )
            metadata.create_all(engine)
        return sqlite_migrations.sqlite_schema_fingerprint(engine)
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "mutation",
    (
        "table",
        "column_type",
        "nullable",
        "primary_key",
        "foreign_key",
        "unique",
        "index",
        "check",
    ),
)
def test_schema_fingerprint_covers_every_legacy_adoption_dimension(
    tmp_path: Path,
    mutation: str,
) -> None:
    assert _shape_fingerprint(tmp_path, mutation) != _shape_fingerprint(tmp_path)


@pytest.mark.parametrize(
    "mutation",
    (
        "autoincrement",
        "collate",
        "on_conflict",
        "strict",
        "virtual_table",
        "without_rowid",
    ),
)
def test_schema_fingerprint_covers_sqlite_table_ddl_features(
    tmp_path: Path,
    mutation: str,
) -> None:
    assert _feature_fingerprint(tmp_path, mutation) != _feature_fingerprint(tmp_path)


@pytest.mark.parametrize(
    ("definition", "expected"),
    (
        (
            "CREATE TABLE feature_table (id INTEGER) WITHOUT /* block gap */ ROWID",
            (("without_rowid", 1),),
        ),
        (
            "CREATE TABLE feature_table (id INTEGER) WITHOUT -- line gap\n ROWID",
            (("without_rowid", 1),),
        ),
        (
            (
                "CREATE TABLE feature_table ("
                "id INTEGER PRIMARY KEY ON /* block gap */ CONFLICT FAIL)"
            ),
            (("on_conflict", 1),),
        ),
        (
            "CREATE /* block gap */ VIRTUAL TABLE feature_table USING fts5(content)",
            (("virtual_table", 1),),
        ),
    ),
)
def test_table_ddl_feature_lexer_treats_sql_comments_as_whitespace(
    definition: str,
    expected: tuple[tuple[str, int], ...],
) -> None:
    assert sqlite_migrations._table_ddl_features(definition) == expected


def test_table_ddl_feature_lexer_ignores_comments_and_quoted_content() -> None:
    definition = """
        CREATE TABLE feature_table (
            "WITHOUT /* quoted */ ROWID" TEXT,
            [ON -- quoted
             CONFLICT] TEXT,
            note TEXT DEFAULT 'CREATE /* quoted */ VIRTUAL TABLE'
        )
        /* WITHOUT ROWID ON CONFLICT CREATE VIRTUAL TABLE */
        -- STRICT AUTOINCREMENT COLLATE
    """

    assert sqlite_migrations._table_ddl_features(definition) == ()


def test_table_ddl_feature_lexer_rejects_an_unterminated_block_comment() -> None:
    with pytest.raises(
        sqlite_migrations.SQLiteMigrationHistoryConflict,
        match="unterminated SQLite block comment",
    ):
        sqlite_migrations._table_ddl_features(
            "CREATE TABLE feature_table (id INTEGER) /* unterminated"
        )


def test_table_ddl_canonicalizer_uses_only_lf_to_end_line_comments() -> None:
    typed = sqlite_migrations._canonical_table_ddl("CREATE TABLE t(a b)")
    untyped = sqlite_migrations._canonical_table_ddl("CREATE TABLE t(a)")

    assert (
        sqlite_migrations._canonical_table_ddl("CREATE TABLE t(a -- comment\n b)")
        == typed
    )
    assert (
        sqlite_migrations._canonical_table_ddl("CREATE TABLE t(a -- comment\r\n b)")
        == typed
    )
    lone_carriage_return = sqlite_migrations._canonical_table_ddl(
        "CREATE TABLE t(a -- comment\rb\n)"
    )
    assert lone_carriage_return == untyped
    assert lone_carriage_return != typed


def test_table_ddl_canonicalizer_preserves_nbsp_as_identifier_content() -> None:
    assert sqlite_migrations._canonical_table_ddl(
        "CREATE TABLE t(a\u00a0b)"
    ) != sqlite_migrations._canonical_table_ddl("CREATE TABLE t(a b)")


def test_table_ddl_canonicalizer_rejects_vertical_tab() -> None:
    with pytest.raises(sqlite_migrations.SQLiteMigrationHistoryConflict):
        sqlite_migrations._canonical_table_ddl("CREATE TABLE t(a\vINTEGER)")


def test_table_ddl_canonicalizer_handles_quoted_structure_and_token_boundaries() -> (
    None
):
    canonical = json.loads(
        sqlite_migrations._canonical_table_ddl(
            """
            CREATE TABLE "quoted,table" (
                "a,b" TEXT DEFAULT 'it''s,/* literal */',
                [c(d)] BLOB DEFAULT X'AB',
                `e``f` NUMERIC DEFAULT 1.02
            )
            """
        )
    )

    assert canonical["columns"] == [
        "\"a,b\" TEXT DEFAULT 'it''s,/* literal */'",
        "[c(d)] BLOB DEFAULT X'AB'",
        "`e``f` NUMERIC DEFAULT 1.02",
    ]
    assert sqlite_migrations._canonical_table_ddl(
        "CREATE TABLE t(a BLOB DEFAULT X'AB')"
    ) != sqlite_migrations._canonical_table_ddl("CREATE TABLE t(a BLOB DEFAULT X 'AB')")
    assert sqlite_migrations._canonical_table_ddl(
        "CREATE TABLE t(a NUMERIC DEFAULT 1 2)"
    ) != sqlite_migrations._canonical_table_ddl("CREATE TABLE t(a NUMERIC DEFAULT 12)")


@pytest.mark.parametrize(
    "unterminated",
    (
        "CREATE TABLE t('a TEXT)",
        'CREATE TABLE t("a TEXT)',
        "CREATE TABLE t(`a TEXT)",
        "CREATE TABLE t([a TEXT)",
    ),
)
def test_table_ddl_canonicalizer_rejects_unterminated_quotes(
    unterminated: str,
) -> None:
    with pytest.raises(sqlite_migrations.SQLiteMigrationHistoryConflict):
        sqlite_migrations._canonical_table_ddl(unterminated)


def test_table_ddl_canonicalizer_preserves_table_constraint_order() -> None:
    first = """
        CREATE TABLE example (
            id INTEGER,
            label TEXT DEFAULT 'literal,/*not a comment*/',
            payload TEXT CHECK (json_valid(payload, 1)),
            CONSTRAINT "uq,label" UNIQUE (label COLLATE NOCASE ASC),
            FOREIGN /* gap */ KEY (id)
                REFERENCES parent(id) MATCH FULL DEFERRABLE INITIALLY DEFERRED,
            CHECK (instr(label, ',') > 0),
            PRIMARY KEY (id)
        ) WITHOUT ROWID
    """
    reordered = """
        CREATE TABLE example (
            id INTEGER,
            label TEXT DEFAULT 'literal,/*not a comment*/',
            payload TEXT CHECK (json_valid(payload, 1)),
            PRIMARY KEY (id),
            CHECK (instr(label, ',') > 0),
            CONSTRAINT "uq,label" UNIQUE (label COLLATE NOCASE ASC),
            FOREIGN KEY (id)
                REFERENCES parent(id) MATCH FULL DEFERRABLE INITIALLY DEFERRED
        ) WITHOUT /* gap */ ROWID
    """

    assert sqlite_migrations._canonical_table_ddl(
        first
    ) != sqlite_migrations._canonical_table_ddl(reordered)


def test_constraint_order_fingerprint_tracks_conflicting_unique_behavior(
    tmp_path: Path,
) -> None:
    class ConflictBase(DeclarativeBase):
        pass

    class ConflictRow(ConflictBase):
        __tablename__ = "conflict_rows"

        id: Mapped[int] = mapped_column(primary_key=True)
        first_value: Mapped[int]
        second_value: Mapped[int]

    definitions = (
        """
            CREATE TABLE conflict_rows (
                id INTEGER PRIMARY KEY,
                first_value INTEGER NOT NULL,
                second_value INTEGER NOT NULL,
                UNIQUE (first_value) ON CONFLICT IGNORE,
                UNIQUE (second_value) ON CONFLICT FAIL
            )
        """,
        """
            CREATE TABLE conflict_rows (
                id INTEGER PRIMARY KEY,
                first_value INTEGER NOT NULL,
                second_value INTEGER NOT NULL,
                UNIQUE (second_value) ON CONFLICT FAIL,
                UNIQUE (first_value) ON CONFLICT IGNORE
            )
        """,
    )
    observed: list[tuple[bool, int, str]] = []
    for position, definition in enumerate(definitions):
        database = tmp_path / f"constraint-order-{position}.sqlite3"
        engine = build_sqlite_engine(_database_url(database), allow_missing=True)
        try:
            with engine.begin() as connection:
                connection.execute(DDL(definition))
            with Session(engine) as session, session.begin():
                session.add(ConflictRow(id=1, first_value=1, second_value=1))
            rejected = False
            try:
                with Session(engine) as session, session.begin():
                    session.add(ConflictRow(id=2, first_value=1, second_value=1))
            except IntegrityError:
                rejected = True
            with Session(engine) as session:
                row_count = session.query(ConflictRow).count()
            observed.append(
                (
                    rejected,
                    row_count,
                    sqlite_migrations.sqlite_schema_fingerprint(engine),
                )
            )
        finally:
            engine.dispose()

    assert {
        (rejected, row_count) for rejected, row_count, _fingerprint in observed
    } == {
        (False, 1),
        (True, 1),
    }
    assert observed[0][2] != observed[1][2]


def test_table_ddl_canonicalizer_classifies_only_leading_table_constraints() -> None:
    canonical = json.loads(
        sqlite_migrations._canonical_table_ddl(
            """
            CREATE TABLE child (
                "PRIMARY" INTEGER CONSTRAINT inline_pk PRIMARY KEY,
                payload TEXT,
                CONSTRAINT named_pk PRIMARY KEY ("PRIMARY"),
                CONSTRAINT named_unique UNIQUE (payload),
                CONSTRAINT named_check CHECK (length(payload) > 0),
                CONSTRAINT named_fk FOREIGN KEY ("PRIMARY")
                    REFERENCES parent(id) MATCH FULL
            )
            """
        )
    )

    assert len(canonical["columns"]) == 2
    assert len(canonical["constraints"]) == 4
    with pytest.raises(sqlite_migrations.SQLiteMigrationHistoryConflict):
        sqlite_migrations._canonical_table_ddl(
            "CREATE TABLE invalid (id INTEGER, PRIMARY KEY (id), late TEXT)"
        )


@pytest.mark.parametrize(
    "invalid",
    (
        "CREATE TABLE copy AS SELECT printf('%s', value) FROM source",
        "CREATE TABLE t(a INTEGER); CREATE TABLE u(b INTEGER)",
        "CREATE TABLE t(a INTEGER);",
        "CREATE TABLE t(a INTEGER) (STRICT)",
    ),
)
def test_table_ddl_canonicalizer_rejects_noncanonical_table_statements(
    invalid: str,
) -> None:
    with pytest.raises(sqlite_migrations.SQLiteMigrationHistoryConflict):
        sqlite_migrations._canonical_table_ddl(invalid)


@pytest.mark.parametrize(
    "invalid_suffix",
    (
        "UNKNOWN",
        "STRICT STRICT",
        "STRICT, STRICT",
        "WITHOUT ROWID WITHOUT ROWID",
        "WITHOUT ROWID, WITHOUT ROWID",
        "STRICT WITHOUT ROWID",
        "STRICT,",
    ),
)
def test_table_ddl_canonicalizer_rejects_ambiguous_table_options(
    invalid_suffix: str,
) -> None:
    with pytest.raises(sqlite_migrations.SQLiteMigrationHistoryConflict):
        sqlite_migrations._canonical_table_ddl(
            f"CREATE TABLE t(id INTEGER) {invalid_suffix}"
        )


@pytest.mark.parametrize(
    "suffix",
    (
        "STRICT",
        "WITHOUT ROWID",
        "STRICT, WITHOUT ROWID",
        "WITHOUT ROWID, STRICT",
    ),
)
def test_table_ddl_canonicalizer_accepts_complete_table_options(
    suffix: str,
) -> None:
    canonical = json.loads(
        sqlite_migrations._canonical_table_ddl(
            f"CREATE TABLE t(id INTEGER PRIMARY KEY) {suffix}"
        )
    )

    assert canonical["suffix"] == suffix


def test_virtual_table_canonicalizer_retains_the_complete_definition() -> None:
    baseline = sqlite_migrations._canonical_table_ddl(
        "CREATE VIRTUAL TABLE docs USING fts5(title, body, tokenize='porter')"
    )
    mutation = sqlite_migrations._canonical_table_ddl(
        "CREATE VIRTUAL TABLE docs USING fts5(title, body, tokenize='unicode61')"
    )

    assert baseline != mutation
    assert "tokenize='porter'" in baseline


@pytest.mark.parametrize(
    "mutation",
    (
        "CREATE TABLE example (label TEXT, id INTEGER, PRIMARY KEY (id))",
        "CREATE TABLE example (id INTEGER, label TEXT, PRIMARY KEY (id DESC))",
        "CREATE TABLE example (id INTEGER, label TEXT, UNIQUE (label DESC))",
        (
            "CREATE TABLE example (id INTEGER, label TEXT, "
            "FOREIGN KEY (id) REFERENCES parent(id) MATCH FULL)"
        ),
        ("CREATE TABLE example (id INTEGER, label TEXT, CHECK (length(label) > 1))"),
        (
            "CREATE TABLE example (id INTEGER, "
            "label TEXT GENERATED ALWAYS AS (id || ',x') STORED)"
        ),
        "CREATE TABLE example (id INTEGER, label TEXT) STRICT",
    ),
)
def test_table_ddl_canonicalizer_preserves_every_non_order_semantic(
    mutation: str,
) -> None:
    baseline = "CREATE TABLE example (id INTEGER, label TEXT, PRIMARY KEY (id))"

    assert sqlite_migrations._canonical_table_ddl(
        mutation
    ) != sqlite_migrations._canonical_table_ddl(baseline)


@pytest.mark.parametrize(
    "foreign_key_option",
    (
        "ON DELETE CASCADE",
        "ON UPDATE SET NULL",
        "DEFERRABLE",
        "DEFERRABLE INITIALLY DEFERRED",
        "MATCH FULL",
    ),
)
def test_table_ddl_canonicalizer_preserves_inline_foreign_key_options(
    foreign_key_option: str,
) -> None:
    baseline = (
        "CREATE TABLE child (id INTEGER, parent_id INTEGER REFERENCES parent(id))"
    )
    mutation = (
        "CREATE TABLE child (id INTEGER, parent_id INTEGER "
        f"REFERENCES parent(id) {foreign_key_option})"
    )

    assert sqlite_migrations._canonical_table_ddl(
        mutation
    ) != sqlite_migrations._canonical_table_ddl(baseline)


@pytest.mark.parametrize(
    ("baseline", "mutation"),
    (
        (
            "CREATE TABLE t(id INTEGER, PRIMARY KEY (id ASC))",
            "CREATE TABLE t(id INTEGER, PRIMARY KEY (id DESC))",
        ),
        (
            "CREATE TABLE t(value TEXT COLLATE NOCASE)",
            "CREATE TABLE t(value TEXT COLLATE RTRIM)",
        ),
        (
            "CREATE TABLE t(id INTEGER, UNIQUE(id) ON CONFLICT IGNORE)",
            "CREATE TABLE t(id INTEGER, UNIQUE(id) ON CONFLICT FAIL)",
        ),
        (
            "CREATE TABLE t(id INTEGER REFERENCES p(id) MATCH SIMPLE)",
            "CREATE TABLE t(id INTEGER REFERENCES p(id) MATCH FULL)",
        ),
        (
            "CREATE TABLE t(id INTEGER REFERENCES p(id) ON DELETE CASCADE)",
            "CREATE TABLE t(id INTEGER REFERENCES p(id) ON DELETE RESTRICT)",
        ),
        (
            "CREATE TABLE t(id INTEGER REFERENCES p(id) ON UPDATE CASCADE)",
            "CREATE TABLE t(id INTEGER REFERENCES p(id) ON UPDATE SET NULL)",
        ),
        (
            "CREATE TABLE t(id INTEGER REFERENCES p(id) NOT DEFERRABLE)",
            (
                "CREATE TABLE t(id INTEGER REFERENCES p(id) "
                "DEFERRABLE INITIALLY DEFERRED)"
            ),
        ),
        (
            "CREATE TABLE t(id INTEGER REFERENCES p(id))",
            "CREATE TABLE t(id INTEGER, FOREIGN KEY(id) REFERENCES p(id))",
        ),
        (
            "CREATE TABLE t(a INTEGER, b INTEGER GENERATED ALWAYS AS (a) VIRTUAL)",
            "CREATE TABLE t(a INTEGER, b INTEGER GENERATED ALWAYS AS (a) STORED)",
        ),
    ),
)
def test_table_ddl_canonicalizer_freezes_pairwise_sqlite_semantics(
    baseline: str,
    mutation: str,
) -> None:
    assert sqlite_migrations._canonical_table_ddl(
        baseline
    ) != sqlite_migrations._canonical_table_ddl(mutation)


@pytest.mark.parametrize(
    "invalid",
    (
        "CREATE TABLE example (id INTEGER",
        "CREATE TABLE example (id INTEGER, CONSTRAINT named UNKNOWN (id))",
        "CREATE TABLE example (id INTEGER) /* unterminated",
    ),
)
def test_table_ddl_canonicalizer_fails_closed_on_ambiguous_structure(
    invalid: str,
) -> None:
    with pytest.raises(sqlite_migrations.SQLiteMigrationHistoryConflict):
        sqlite_migrations._canonical_table_ddl(invalid)


def test_unpublished_unledgered_current_schema_fails_closed_without_side_effects(
    tmp_path: Path,
) -> None:
    database = tmp_path / "unpublished-unledgered-current.sqlite3"
    engine = build_sqlite_engine(_database_url(database), allow_missing=True)
    try:
        build_sqlite_metadata().create_all(engine)
        session_identity_signature = (
            sqlite_migrations._session_identity_schema_signature(engine)
        )

        with pytest.raises(
            RuntimeError,
            match="SQLite migration history conflicts",
        ):
            upgrade_sqlite_schema(engine)

        assert (
            sqlite_migrations._session_identity_schema_signature(engine)
            == session_identity_signature
        )
        assert SQLITE_MIGRATION_TABLE not in inspect(engine).get_table_names()
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("mutation", "needle", "replacement"),
    (
        ("primary-key-asc", "PRIMARY KEY (version)", "PRIMARY KEY (version ASC)"),
        ("primary-key-desc", "PRIMARY KEY (version)", "PRIMARY KEY (version DESC)"),
        (
            "primary-key-comment-desc",
            "PRIMARY KEY (version)",
            "PRIMARY KEY (version /* gap */ DESC)",
        ),
        ("unique-asc", "UNIQUE (name)", "UNIQUE (name ASC)"),
        ("unique-desc", "UNIQUE (name)", "UNIQUE (name DESC)"),
    ),
)
def test_legacy_adoption_rejects_indexed_column_sort_direction(
    tmp_path: Path,
    mutation: str,
    needle: str,
    replacement: str,
) -> None:
    database = tmp_path / f"legacy-{mutation}.sqlite3"
    engine = build_sqlite_engine(_database_url(database), allow_missing=True)

    def inject_indexed_column_direction(
        _connection: object,
        _cursor: object,
        statement: str,
        parameters: object,
        _context: object,
        _executemany: bool,
    ) -> tuple[str, object]:
        if (
            statement.lstrip()
            .upper()
            .startswith("CREATE TABLE HERMES_CLOUD_MIGRATIONS")
        ):
            assert needle in statement
            return statement.replace(needle, replacement), parameters
        return statement, parameters

    event.listen(
        engine,
        "before_cursor_execute",
        inject_indexed_column_direction,
        retval=True,
    )
    try:
        build_sqlite_metadata().create_all(engine)
    finally:
        event.remove(
            engine,
            "before_cursor_execute",
            inject_indexed_column_direction,
        )

    try:
        with pytest.raises(RuntimeError, match="SQLite migration history conflicts"):
            upgrade_sqlite_schema(engine)

        assert SQLITE_MIGRATION_TABLE not in inspect(engine).get_table_names()
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("mutation", "inline_reference"),
    (
        (
            "inline-foreign-key-cascade",
            "REFERENCES tenants (tenant_id) ON DELETE CASCADE",
        ),
        (
            "inline-foreign-key-deferred",
            ("REFERENCES tenants (tenant_id) DEFERRABLE INITIALLY DEFERRED"),
        ),
    ),
)
def test_legacy_adoption_rejects_inline_foreign_key_semantic_drift(
    tmp_path: Path,
    mutation: str,
    inline_reference: str,
) -> None:
    database = tmp_path / f"legacy-{mutation}.sqlite3"
    engine = build_sqlite_engine(_database_url(database), allow_missing=True)

    def move_foreign_key_to_column(
        _connection: object,
        _cursor: object,
        statement: str,
        parameters: object,
        _context: object,
        _executemany: bool,
    ) -> tuple[str, object]:
        if statement.lstrip().upper().startswith("CREATE TABLE AUDIT_EVENTS"):
            column = "tenant_id CHAR(32) NOT NULL"
            constraint = "FOREIGN KEY(tenant_id) REFERENCES tenants (tenant_id)"
            assert column in statement
            if f"\t{constraint}, \n" in statement:
                statement = statement.replace(f"\t{constraint}, \n", "")
            else:
                assert f", \n\t{constraint}\n" in statement
                statement = statement.replace(f", \n\t{constraint}\n", "\n")
            statement = statement.replace(
                column,
                f"{column} {inline_reference}",
            )
        return statement, parameters

    event.listen(
        engine,
        "before_cursor_execute",
        move_foreign_key_to_column,
        retval=True,
    )
    try:
        build_sqlite_metadata().create_all(engine)
    finally:
        event.remove(engine, "before_cursor_execute", move_foreign_key_to_column)

    try:
        with pytest.raises(RuntimeError, match="SQLite migration history conflicts"):
            upgrade_sqlite_schema(engine)

        assert SQLITE_MIGRATION_TABLE not in inspect(engine).get_table_names()
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("mutation", "needle", "replacement"),
    (
        ("without-rowid-block-comment", "WITHOUT ROWID", "WITHOUT /* gap */ ROWID"),
        (
            "without-rowid-line-comment",
            "WITHOUT ROWID",
            "WITHOUT -- gap\n ROWID",
        ),
        ("on-conflict-block-comment", "ON CONFLICT", "ON /* gap */ CONFLICT"),
    ),
)
def test_legacy_adoption_rejects_comment_separated_table_features(
    tmp_path: Path,
    mutation: str,
    needle: str,
    replacement: str,
) -> None:
    database = tmp_path / f"legacy-{mutation}.sqlite3"
    engine = build_sqlite_engine(_database_url(database), allow_missing=True)
    metadata = build_sqlite_metadata()
    if mutation.startswith("without-rowid"):
        metadata.tables["tenants"].dialect_options["sqlite"]["with_rowid"] = False
    else:
        metadata.tables["tenants"].primary_key.dialect_options["sqlite"][
            "on_conflict"
        ] = "FAIL"

    def inject_comment_separator(
        _connection: object,
        _cursor: object,
        statement: str,
        parameters: object,
        _context: object,
        _executemany: bool,
    ) -> tuple[str, object]:
        if statement.lstrip().upper().startswith("CREATE TABLE TENANTS"):
            assert needle in statement
            return statement.replace(needle, replacement), parameters
        return statement, parameters

    event.listen(
        engine,
        "before_cursor_execute",
        inject_comment_separator,
        retval=True,
    )
    try:
        metadata.create_all(engine)
    finally:
        event.remove(engine, "before_cursor_execute", inject_comment_separator)

    try:
        with pytest.raises(RuntimeError, match="SQLite migration history conflicts"):
            upgrade_sqlite_schema(engine)

        assert SQLITE_MIGRATION_TABLE not in inspect(engine).get_table_names()
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "mutation",
    ("blocking_trigger", "view", "collation", "on_conflict", "without_rowid"),
)
def test_legacy_adoption_rejects_noncanonical_sqlite_ddl(
    tmp_path: Path,
    mutation: str,
) -> None:
    database = tmp_path / f"legacy-{mutation}.sqlite3"
    engine = build_sqlite_engine(_database_url(database), allow_missing=True)
    try:
        metadata = build_sqlite_metadata()
        if mutation == "collation":
            slug = metadata.tables["tenants"].c.slug
            slug.type = Text(collation="NOCASE")
        elif mutation == "without_rowid":
            metadata.tables["tenants"].dialect_options["sqlite"]["with_rowid"] = False
        elif mutation == "on_conflict":
            metadata.tables["tenants"].primary_key.dialect_options["sqlite"][
                "on_conflict"
            ] = "FAIL"
        metadata.create_all(engine)
        if mutation == "blocking_trigger":
            with engine.begin() as connection:
                connection.execute(
                    DDL(
                        "CREATE TRIGGER block_tenant_insert "
                        "BEFORE INSERT ON tenants "
                        "BEGIN SELECT RAISE(ABORT, 'blocked'); END"
                    )
                )
        elif mutation == "view":
            with engine.begin() as connection:
                connection.execute(
                    DDL("CREATE VIEW tenant_slugs AS SELECT slug FROM tenants")
                )

        with pytest.raises(RuntimeError, match="SQLite migration history conflicts"):
            upgrade_sqlite_schema(engine)

        assert SQLITE_MIGRATION_TABLE not in inspect(engine).get_table_names()
    finally:
        engine.dispose()


def test_view_only_catalog_is_not_planned_or_applied_as_an_empty_database(
    tmp_path: Path,
) -> None:
    database = tmp_path / "view-only.sqlite3"
    engine = build_sqlite_engine(_database_url(database), allow_missing=True)
    try:
        with engine.begin() as connection:
            connection.execute(DDL("CREATE VIEW unexpected_view AS SELECT 1 AS value"))

        assert inspect(engine).get_table_names() == []
        assert inspect(engine).get_view_names() == ["unexpected_view"]
        with pytest.raises(RuntimeError, match="SQLite migration history conflicts"):
            sqlite_migrations.plan_sqlite_schema(engine)
        with pytest.raises(RuntimeError, match="SQLite migration history conflicts"):
            upgrade_sqlite_schema(engine)

        assert SQLITE_MIGRATION_TABLE not in inspect(engine).get_table_names()
        assert inspect(engine).get_view_names() == ["unexpected_view"]
    finally:
        engine.dispose()


def test_published_legacy_source_catalog_freezes_the_real_release_signatures() -> None:
    assert sqlite_migrations.PUBLISHED_SQLITE_LEGACY_SOURCES == (
        sqlite_migrations.PublishedSQLiteLegacySource(
            release="20260731T084500Z",
            schema_fingerprint=(
                "43616337e19b7a4bbb70f2d2887d68dd8222d329f891f02553c40d84523ca89e"
            ),
            raw_manifest_checksum=(
                "cda09c49697e5bc711cbb536ea5505c61f822bd9f7e24ab0cda4685a5ab5f656"
            ),
            session_identity_remainder_schema_fingerprint=(
                "3ab91ba3647c09e099ef57dc5078c5dd4449a031ecfdfa520f42cf8dc6adc7c6"
            ),
            session_identity_remainder_raw_manifest_checksum=(
                "b5e00db8a3f8783f995782743e20a4e71d9664736a914a24a12e686227d2f594"
            ),
        ),
    )


def test_versioned_compatibility_catalog_freezes_only_deployed_legacy_v1_v5() -> None:
    assert sqlite_migrations.PUBLISHED_SQLITE_VERSIONED_COMPATIBILITY_SOURCES == (
        sqlite_migrations.PublishedSQLiteVersionedCompatibilitySource(
            release="20260731T084500Z-legacy-v1-to-v5",
            version=5,
            source="versioned-5-compatible",
            schema_fingerprint=(
                "eeb0f49b2b217c73916c1b0567daa1de8ef7fec37d1646fcdb605ad1d9d9bdec"
            ),
            raw_manifest_checksum=(
                "635bec16a52694aab9a822c145960bb20be85d1b50f6079b1d8bfaaeda7a676a"
            ),
            legacy_base_schema_fingerprint=(
                "43616337e19b7a4bbb70f2d2887d68dd8222d329f891f02553c40d84523ca89e"
            ),
            legacy_base_raw_manifest_checksum=(
                "cda09c49697e5bc711cbb536ea5505c61f822bd9f7e24ab0cda4685a5ab5f656"
            ),
            ledger_schema_fingerprint=(
                "ead3fa7217c8da81251761ff11a28d7faac48924581a965c49ef3adc57c92491"
            ),
            ledger_raw_manifest_checksum=(
                "4429c9c258d7dda582c3f2ccf2a5c27cc109ec614df31e2b0da9ebb5c8264501"
            ),
            observer_schema_fingerprint=(
                "da7f04a3c979353044cf2f4088e7772d95b6ce9f9c2ad551b06672c36eeefbd7"
            ),
            observer_raw_manifest_checksum=(
                "a2a3cbdfaf35db1847f697aa283ddd4f954c6aa321154b1b656bb55aa18171f8"
            ),
            transport_schema_fingerprint=(
                "f5b029f741077ccaff7a7c5a2c040b18537b623de5f6294bf8f1b07f938bbda1"
            ),
            transport_raw_manifest_checksum=(
                "4aeeafd605c7023a55d7df8e1971dc491642907724eb50360782835c7d66fe77"
            ),
            handshake_schema_fingerprint=(
                "bb55ec1899fcb8d7d93c8f1a486e115603ad4c4a34c35da259b6da904a6575d3"
            ),
            handshake_raw_manifest_checksum=(
                "6899eb241728655d8a48fe2d1948fdda312fbb7fb3eab8f2b873408701dc90f3"
            ),
        ),
    )


def test_real_published_legacy_fixture_upgrades_without_mutating_the_fixture(
    tmp_path: Path,
) -> None:
    source = sqlite_migrations.PUBLISHED_SQLITE_LEGACY_SOURCES[0]
    fixture_before = LEGACY_SCHEMA_FIXTURE.read_bytes()
    assert hashlib.sha256(fixture_before).hexdigest() == source.raw_manifest_checksum
    assert len(json.loads(fixture_before)) == 77

    database = tmp_path / "real-published-legacy.sqlite3"
    engine = build_sqlite_engine(_database_url(database), allow_missing=True)
    try:
        _replay_published_legacy_fixture(engine)
        assert (
            sqlite_migrations.sqlite_schema_fingerprint(engine)
            == source.schema_fingerprint
        )
        assert (
            sqlite_migrations.sqlite_raw_manifest_checksum(engine)
            == source.raw_manifest_checksum
        )
        assert sqlite_migrations.plan_sqlite_schema(engine).source == "legacy-current"

        assert upgrade_sqlite_schema(engine).source == "legacy-current"

        assert sqlite_migrations.plan_sqlite_schema(engine).source == "current"
        _assert_identity_repository_read_write(engine, slug="real-legacy-fixture")
    finally:
        engine.dispose()

    assert LEGACY_SCHEMA_FIXTURE.read_bytes() == fixture_before


def test_real_legacy_business_schema_with_exact_v1_ledger_upgrades_to_current(
    tmp_path: Path,
) -> None:
    source = sqlite_migrations.PUBLISHED_SQLITE_LEGACY_SOURCES[0]
    fixture_before = LEGACY_SCHEMA_FIXTURE.read_bytes()
    preserved_tenant_id = uuid4()
    database = tmp_path / "real-published-legacy-versioned-v1.sqlite3"
    engine = build_sqlite_engine(_database_url(database), allow_missing=True)
    try:
        _replay_published_legacy_fixture(engine)
        _add_typed_orm_ledger(engine)
        with Session(engine) as session, session.begin():
            session.add(
                TenantModel(
                    tenant_id=preserved_tenant_id,
                    slug="remote-v1-preserved",
                    display_name="Remote v1 preserved",
                    status="active",
                    created_at=applied_at(),
                )
            )

        excluded_ledger = frozenset({SQLITE_MIGRATION_TABLE})
        assert (
            sqlite_migrations.sqlite_schema_fingerprint(
                engine,
                excluded_table_names=excluded_ledger,
            )
            == source.schema_fingerprint
        )
        assert (
            sqlite_migrations.sqlite_raw_manifest_checksum(
                engine,
                excluded_table_names=excluded_ledger,
            )
            == source.raw_manifest_checksum
        )

        assert sqlite_migrations.plan_sqlite_schema(engine).source == "versioned-1"
        assert upgrade_sqlite_schema(engine).source == "versioned-1"
        assert sqlite_migrations.plan_sqlite_schema(engine).source == "current"

        with Session(engine) as session:
            preserved = session.get(TenantModel, preserved_tenant_id)
            assert preserved is not None
            assert preserved.slug == "remote-v1-preserved"
            assert session.query(SQLiteSchemaMigration).count() == len(
                PUBLISHED_SQLITE_MIGRATIONS
            )
        _assert_identity_repository_read_write(
            engine,
            slug="remote-v1-post-upgrade",
        )
    finally:
        engine.dispose()

    assert LEGACY_SCHEMA_FIXTURE.read_bytes() == fixture_before


def test_exact_deployed_legacy_v1_to_v5_source_upgrades_with_explicit_identity(
    tmp_path: Path,
) -> None:
    fixture_before = LEGACY_SCHEMA_FIXTURE.read_bytes()
    database = tmp_path / "deployed-legacy-v1-to-v5.sqlite3"
    engine = build_sqlite_engine(_database_url(database), allow_missing=True)
    try:
        _replay_published_legacy_fixture(engine)
        _add_typed_orm_ledger(engine)
        _apply_legacy_v1_overlays_through(engine, 5)

        assert sqlite_migrations.sqlite_schema_fingerprint(engine) == (
            "eeb0f49b2b217c73916c1b0567daa1de8ef7fec37d1646fcdb605ad1d9d9bdec"
        )
        assert sqlite_migrations._expected_versioned_database_fingerprint(5) == (
            "3d362bd44c52de259786c5735f6cdf51b440d93beb5ad950cc6d6ee2ea82879d"
        )
        assert sqlite_migrations.plan_sqlite_schema(engine).source == (
            "versioned-5-compatible"
        )

        assert upgrade_sqlite_schema(engine).source == "versioned-5-compatible"

        assert sqlite_migrations.plan_sqlite_schema(engine).source == "current"
        with Session(engine) as session:
            assert session.query(SQLiteSchemaMigration).count() == len(
                PUBLISHED_SQLITE_MIGRATIONS
            )
        _assert_identity_repository_read_write(
            engine,
            slug="legacy-v1-v5-post-upgrade",
        )
    finally:
        engine.dispose()

    assert LEGACY_SCHEMA_FIXTURE.read_bytes() == fixture_before


@pytest.mark.parametrize(
    "mutation",
    (
        "legacy_base_raw",
        "ledger_raw",
        "ledger_checksum",
        "extra_object",
        "observer_overlay",
        "transport_overlay",
        "handshake_overlay",
        "other_version_4",
        "other_version_6",
    ),
)
def test_deployed_legacy_v1_to_v5_compatibility_is_exact_and_fail_closed(
    tmp_path: Path,
    mutation: str,
) -> None:
    database = tmp_path / f"deployed-legacy-v1-v5-{mutation}.sqlite3"
    engine = build_sqlite_engine(_database_url(database), allow_missing=True)
    source_version = (
        int(mutation.rsplit("_", 1)[-1]) if mutation.startswith("other_version_") else 5
    )
    try:
        _replay_published_legacy_fixture(
            engine,
            mutate_raw_ddl=mutation == "legacy_base_raw",
        )
        _add_typed_orm_ledger(
            engine,
            mutate_raw_ddl=mutation == "ledger_raw",
        )
        _apply_legacy_v1_overlays_through(engine, source_version)

        if mutation == "ledger_checksum":
            with Session(engine) as session, session.begin():
                applied = session.get(SQLiteSchemaMigration, 3)
                assert applied is not None
                applied.checksum = "0" * 64
        elif mutation == "extra_object":
            with engine.begin() as connection:
                connection.execute(
                    DDL("CREATE VIEW unexpected_v5_view AS SELECT 1 AS value")
                )
        elif mutation.endswith("_overlay"):
            table_names = {
                "observer_overlay": "observer_sessions",
                "transport_overlay": "connector_transport_cursors",
                "handshake_overlay": "connector_transport_handshake_ownership",
            }
            table = Table(
                table_names[mutation],
                MetaData(),
                autoload_with=engine,
            )
            Index(f"unexpected_{mutation}_idx", table.c.tenant_id).create(engine)

        schema_before = sqlite_migrations.sqlite_raw_manifest_checksum(engine)

        with pytest.raises(RuntimeError, match="SQLite migration history conflicts"):
            sqlite_migrations.plan_sqlite_schema(engine)
        with pytest.raises(RuntimeError, match="SQLite migration history conflicts"):
            upgrade_sqlite_schema(engine)

        assert sqlite_migrations.sqlite_raw_manifest_checksum(engine) == schema_before
        with Session(engine) as session:
            assert session.query(SQLiteSchemaMigration).count() == source_version
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "mutation",
    (
        "raw_ddl",
        "extra_object",
        "ledger_checksum",
        "ledger_raw_ddl",
        "ledger_structure",
        "unknown_legacy_pair",
        "versioned_2",
    ),
)
def test_versioned_legacy_v1_compatibility_remains_exact_and_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    source = sqlite_migrations.PUBLISHED_SQLITE_LEGACY_SOURCES[0]
    database = tmp_path / f"legacy-versioned-v1-{mutation}.sqlite3"
    engine = build_sqlite_engine(_database_url(database), allow_missing=True)
    try:
        _replay_published_legacy_fixture(
            engine,
            mutate_raw_ddl=mutation == "raw_ddl",
        )
        if mutation == "ledger_structure":
            drifted_ledger = Table(
                SQLITE_MIGRATION_TABLE,
                MetaData(),
                Column("version", String(10), primary_key=True),
                Column("name", String(255), nullable=False),
                Column("checksum", String(64), nullable=False),
                Column("applied_at", DateTime(timezone=True), nullable=False),
            )
            drifted_ledger.create(engine)
            with Session(engine) as session, session.begin():
                baseline = PUBLISHED_SQLITE_MIGRATIONS[0]
                session.add(
                    SQLiteSchemaMigration(
                        version=baseline.version,
                        name=baseline.name,
                        checksum=baseline.checksum,
                        applied_at=applied_at(),
                    )
                )
        else:
            _add_typed_orm_ledger(
                engine,
                through_version=2 if mutation == "versioned_2" else 1,
                mutate_raw_ddl=mutation == "ledger_raw_ddl",
            )

        if mutation == "extra_object":
            with engine.begin() as connection:
                connection.execute(
                    DDL("CREATE VIEW unexpected_v1_view AS SELECT 1 AS value")
                )
        elif mutation == "ledger_checksum":
            with Session(engine) as session, session.begin():
                applied = session.get(SQLiteSchemaMigration, 1)
                assert applied is not None
                applied.checksum = "0" * 64
        elif mutation == "unknown_legacy_pair":
            monkeypatch.setattr(
                sqlite_migrations,
                "PUBLISHED_SQLITE_LEGACY_SOURCES",
                (
                    sqlite_migrations.PublishedSQLiteLegacySource(
                        release="unknown-legacy-release",
                        schema_fingerprint="0" * 64,
                        raw_manifest_checksum="1" * 64,
                        session_identity_remainder_schema_fingerprint="2" * 64,
                        session_identity_remainder_raw_manifest_checksum="3" * 64,
                    ),
                ),
            )

        excluded_ledger = frozenset({SQLITE_MIGRATION_TABLE})
        if mutation == "raw_ddl":
            assert (
                sqlite_migrations.sqlite_schema_fingerprint(
                    engine,
                    excluded_table_names=excluded_ledger,
                )
                == source.schema_fingerprint
            )
            assert (
                sqlite_migrations.sqlite_raw_manifest_checksum(
                    engine,
                    excluded_table_names=excluded_ledger,
                )
                != source.raw_manifest_checksum
            )
        elif mutation == "ledger_raw_ddl":
            non_ledger_objects = frozenset(
                str(schema_object["name"])
                for schema_object in sqlite_migrations._raw_schema_manifest(engine)
                if schema_object["name"] != SQLITE_MIGRATION_TABLE
            )
            assert (
                sqlite_migrations.sqlite_schema_fingerprint(
                    engine,
                    excluded_table_names=non_ledger_objects,
                )
                == sqlite_migrations.expected_sqlite_ledger_fingerprint()
            )
            assert (
                sqlite_migrations.sqlite_raw_manifest_checksum(
                    engine,
                    excluded_table_names=non_ledger_objects,
                )
                != sqlite_migrations.PUBLISHED_SQLITE_V1_LEDGER_SIGNATURE[1]
            )
        schema_before = sqlite_migrations.sqlite_raw_manifest_checksum(engine)

        with pytest.raises(RuntimeError, match="SQLite migration history conflicts"):
            sqlite_migrations.plan_sqlite_schema(engine)
        with pytest.raises(RuntimeError, match="SQLite migration history conflicts"):
            upgrade_sqlite_schema(engine)

        assert sqlite_migrations.sqlite_raw_manifest_checksum(engine) == schema_before
        with Session(engine) as session:
            applied = session.query(SQLiteSchemaMigration).all()
        assert len(applied) == (2 if mutation == "versioned_2" else 1)
    finally:
        engine.dispose()


def test_real_published_legacy_fixture_drift_fails_without_a_ledger(
    tmp_path: Path,
) -> None:
    fixture_before = LEGACY_SCHEMA_FIXTURE.read_bytes()
    database = tmp_path / "drifted-real-published-legacy.sqlite3"
    engine = build_sqlite_engine(_database_url(database), allow_missing=True)
    try:
        _replay_published_legacy_fixture(engine)
        Table(
            "unexpected_business_table",
            MetaData(),
            Column("id", Integer, primary_key=True),
        ).create(engine)

        with pytest.raises(RuntimeError, match="SQLite migration history conflicts"):
            upgrade_sqlite_schema(engine)

        assert SQLITE_MIGRATION_TABLE not in inspect(engine).get_table_names()
    finally:
        engine.dispose()

    assert LEGACY_SCHEMA_FIXTURE.read_bytes() == fixture_before


@pytest.mark.parametrize(
    "source",
    ("empty", "legacy-current", "versioned-5-compatible"),
)
def test_upgrade_revalidates_the_planned_source_after_a_competing_schema_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: str,
) -> None:
    database = tmp_path / f"planned-source-race-{source}.sqlite3"
    database_url = _database_url(database)
    engine = build_sqlite_engine(database_url, allow_missing=True)
    competitor = build_sqlite_engine(database_url, allow_missing=True)
    original_plan = sqlite_migrations.plan_sqlite_schema
    drift_injected = False
    try:
        if source == "legacy-current":
            _replay_published_legacy_fixture(engine)
        elif source == "versioned-5-compatible":
            _replay_published_legacy_fixture(engine)
            _add_typed_orm_ledger(engine)
            _apply_legacy_v1_overlays_through(engine, 5)

        def plan_then_drift(candidate: Engine) -> object:
            nonlocal drift_injected
            result = original_plan(candidate)
            if not drift_injected and result.source == source:
                drift_injected = True
                Table(
                    "unexpected_competing_table",
                    MetaData(),
                    Column("id", Integer, primary_key=True),
                ).create(competitor)
            return result

        monkeypatch.setattr(
            sqlite_migrations,
            "plan_sqlite_schema",
            plan_then_drift,
        )

        with pytest.raises(RuntimeError, match="SQLite migration history conflicts"):
            upgrade_sqlite_schema(engine)

        table_names = set(inspect(engine).get_table_names())
        assert "unexpected_competing_table" in table_names
        assert (SQLITE_MIGRATION_TABLE in table_names) is (
            source == "versioned-5-compatible"
        )
        if source == "versioned-5-compatible":
            with Session(engine) as session:
                assert session.query(SQLiteSchemaMigration).count() == 5

        with pytest.raises(RuntimeError, match="SQLite migration history conflicts"):
            original_plan(engine)
        assert (SQLITE_MIGRATION_TABLE in inspect(engine).get_table_names()) is (
            source == "versioned-5-compatible"
        )
    finally:
        competitor.dispose()
        engine.dispose()


@pytest.mark.parametrize("drift", ("raw_schema", "ledger"))
def test_deployed_rev10_upgrade_revalidates_full_evidence_after_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    drift: str,
) -> None:
    database = tmp_path / f"deployed-rev10-plan-guard-{drift}.sqlite3"
    database_url = _database_url(database)
    engine = build_sqlite_engine(database_url, allow_missing=True)
    competitor = build_sqlite_engine(database_url, allow_missing=True)
    original_plan = sqlite_migrations.plan_sqlite_schema
    drift_injected = False
    try:
        _apply_deployed_20260801t131728z_rev10_fixture(engine)

        def plan_then_drift(candidate: Engine) -> object:
            nonlocal drift_injected
            result = original_plan(candidate)
            if not drift_injected and result.source == "versioned-10":
                drift_injected = True
                if drift == "raw_schema":
                    _mutate_deployed_rev10_raw_table_ddl(competitor)
                else:
                    with Session(competitor) as session, session.begin():
                        applied = session.get(SQLiteSchemaMigration, 10)
                        assert applied is not None
                        applied.checksum = "0" * 64
            return result

        monkeypatch.setattr(
            sqlite_migrations,
            "plan_sqlite_schema",
            plan_then_drift,
        )

        with pytest.raises(
            sqlite_migrations.SQLiteMigrationHistoryConflict,
            match="SQLite migration history conflicts",
        ):
            upgrade_sqlite_schema(engine)

        assert drift_injected is True
        table_names = set(inspect(engine).get_table_names())
        assert "sessions_v10" not in table_names
        assert "profile" not in {
            column["name"] for column in inspect(engine).get_columns("sessions")
        }
        with Session(engine) as session:
            assert session.get(SQLiteSchemaMigration, 11) is None
            assert session.query(SQLiteSchemaMigration).count() == 10
            applied = session.get(SQLiteSchemaMigration, 10)
            assert applied is not None
            assert applied.checksum == (
                "0" * 64
                if drift == "ledger"
                else PUBLISHED_SQLITE_MIGRATIONS[9].checksum
            )
        if drift == "raw_schema":
            canonical, raw = sqlite_migrations._schema_signature(engine)
            assert canonical == DEPLOYED_20260801T131728Z_V10_SIGNATURE[0]
            assert raw != DEPLOYED_20260801T131728Z_V10_SIGNATURE[1]
        with pytest.raises(
            sqlite_migrations.SQLiteMigrationHistoryConflict,
            match="SQLite migration history conflicts",
        ):
            original_plan(engine)
    finally:
        competitor.dispose()
        engine.dispose()


def test_legacy_adoption_has_no_dynamic_metadata_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "metadata-current.sqlite3"
    engine = build_sqlite_engine(_database_url(database), allow_missing=True)
    try:
        build_sqlite_metadata().create_all(engine)
        monkeypatch.setattr(
            sqlite_migrations,
            "PUBLISHED_SQLITE_LEGACY_SOURCES",
            (),
        )

        with pytest.raises(RuntimeError, match="SQLite migration history conflicts"):
            sqlite_migrations.plan_sqlite_schema(engine)
    finally:
        engine.dispose()


def test_legacy_raw_manifest_digest_is_independent_of_canonical_sql(
    tmp_path: Path,
) -> None:
    engines = [
        build_sqlite_engine(
            _database_url(tmp_path / f"raw-manifest-{position}.sqlite3"),
            allow_missing=True,
        )
        for position in range(2)
    ]
    try:
        definitions = (
            "CREATE TABLE source_table (value INTEGER)",
            "CREATE TABLE source_table (value /* release comment */ INTEGER)",
        )
        for engine, definition in zip(engines, definitions, strict=True):
            with engine.begin() as connection:
                connection.execute(DDL(definition))

        assert sqlite_migrations.sqlite_schema_fingerprint(
            engines[0]
        ) == sqlite_migrations.sqlite_schema_fingerprint(engines[1])
        assert sqlite_migrations.sqlite_raw_manifest_checksum(
            engines[0]
        ) != sqlite_migrations.sqlite_raw_manifest_checksum(engines[1])
    finally:
        for engine in engines:
            engine.dispose()


def test_drifted_legacy_schema_fails_closed_without_creating_a_ledger(
    tmp_path: Path,
) -> None:
    database = tmp_path / "drifted-legacy.sqlite3"
    engine = build_sqlite_engine(_database_url(database), allow_missing=True)
    try:
        build_sqlite_metadata().create_all(engine)
        unexpected = Table(
            "unexpected_business_table",
            MetaData(),
            Column("id", Integer, primary_key=True),
        )
        unexpected.create(engine)

        with pytest.raises(RuntimeError, match="SQLite migration history conflicts"):
            upgrade_sqlite_schema(engine)

        assert SQLITE_MIGRATION_TABLE not in inspect(engine).get_table_names()
    finally:
        engine.dispose()


def test_current_schema_rejects_inline_desc_primary_key_and_preserves_history(
    tmp_path: Path,
) -> None:
    database = tmp_path / "current-inline-primary-key-desc.sqlite3"
    engine = build_sqlite_engine(_database_url(database), allow_missing=True)
    try:
        upgrade_sqlite_schema(engine)
        with Session(engine) as session:
            original = session.get(
                SQLiteSchemaMigration,
                CURRENT_SQLITE_SCHEMA_VERSION,
            )
            assert original is not None
            original_values = (
                original.version,
                original.name,
                original.checksum,
                original.applied_at,
            )

        SQLiteSchemaMigration.__table__.drop(engine)
        replacement_table = SQLiteSchemaMigration.__table__.to_metadata(MetaData())

        def inline_desc_primary_key(
            _connection: object,
            _cursor: object,
            statement: str,
            parameters: object,
            _context: object,
            _executemany: bool,
        ) -> tuple[str, object]:
            if (
                statement.lstrip()
                .upper()
                .startswith("CREATE TABLE HERMES_SQLITE_SCHEMA_MIGRATIONS")
            ):
                assert "version INTEGER NOT NULL," in statement
                assert "\tPRIMARY KEY (version), \n" in statement
                statement = statement.replace(
                    "version INTEGER NOT NULL,",
                    "version INTEGER NOT NULL PRIMARY KEY DESC,",
                ).replace("\tPRIMARY KEY (version), \n", "")
            return statement, parameters

        event.listen(
            engine,
            "before_cursor_execute",
            inline_desc_primary_key,
            retval=True,
        )
        try:
            replacement_table.create(engine)
        finally:
            event.remove(engine, "before_cursor_execute", inline_desc_primary_key)

        with Session(engine) as session, session.begin():
            session.add(
                SQLiteSchemaMigration(
                    version=original_values[0],
                    name=original_values[1],
                    checksum=original_values[2],
                    applied_at=original_values[3],
                )
            )

        with pytest.raises(RuntimeError, match="SQLite migration history conflicts"):
            sqlite_migrations.plan_sqlite_schema(engine)

        with Session(engine) as session:
            preserved = session.get(
                SQLiteSchemaMigration,
                CURRENT_SQLITE_SCHEMA_VERSION,
            )
            assert preserved is not None
            assert (
                preserved.version,
                preserved.name,
                preserved.checksum,
                preserved.applied_at,
            ) == original_values
    finally:
        engine.dispose()


def test_current_schema_is_idempotent_and_validates_the_orm_ledger(
    tmp_path: Path,
) -> None:
    database = tmp_path / "current.sqlite3"
    engine = build_sqlite_engine(_database_url(database), allow_missing=True)
    try:
        upgrade_sqlite_schema(engine)
        upgrade_sqlite_schema(engine)

        with Session(engine) as session:
            applied = session.query(SQLiteSchemaMigration).all()
        assert [(row.version, row.name, row.checksum) for row in applied] == [
            (migration.version, migration.name, migration.checksum)
            for migration in PUBLISHED_SQLITE_MIGRATIONS
        ]
    finally:
        engine.dispose()


def test_current_schema_rejects_mutated_or_future_orm_history(
    tmp_path: Path,
) -> None:
    for mutation in ("checksum", "future"):
        database = tmp_path / f"{mutation}.sqlite3"
        engine = build_sqlite_engine(_database_url(database), allow_missing=True)
        try:
            upgrade_sqlite_schema(engine)
            with Session(engine) as session, session.begin():
                if mutation == "checksum":
                    applied = session.get(SQLiteSchemaMigration, 1)
                    assert applied is not None
                    applied.checksum = "0" * 64
                else:
                    session.add(
                        SQLiteSchemaMigration(
                            version=CURRENT_SQLITE_SCHEMA_VERSION + 1,
                            name="unknown_future",
                            checksum="f" * 64,
                            applied_at=applied_at(),
                        )
                    )

            with pytest.raises(
                RuntimeError,
                match="SQLite migration history conflicts",
            ):
                upgrade_sqlite_schema(engine)
        finally:
            engine.dispose()


def test_concurrent_collision_revalidates_until_current_condition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    collision = OperationalError(
        "CREATE TABLE hermes_sqlite_schema_migrations",
        {},
        sqlite3.OperationalError("database is locked"),
    )
    attempts = 0

    def collide(*_args: object, **_kwargs: object) -> None:
        raise collision

    def observe_current(_engine: Engine) -> object:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise OperationalError(
                "SELECT sqlite_master",
                {},
                sqlite3.OperationalError("database is locked"),
            )
        return sqlite_migrations.SQLiteUpgradeResult(
            schema_version=CURRENT_SQLITE_SCHEMA_VERSION,
            source="current",
        )

    monkeypatch.setattr(sqlite_migrations, "_apply_migration_transaction", collide)
    monkeypatch.setattr(sqlite_migrations, "plan_sqlite_schema", observe_current)
    monkeypatch.setattr(sqlite_migrations, "sleep", lambda _seconds: None)

    result = sqlite_migrations._apply_or_observe_concurrent_current(
        object(),
        PUBLISHED_SQLITE_MIGRATIONS[0],
        create_business_schema=True,
        planned_source="legacy-current",
        observer_cipher=None,
    )

    assert result is not None
    assert result.source == "current"
    assert attempts == 3


def test_current_schema_rejects_a_structurally_drifted_ledger(
    tmp_path: Path,
) -> None:
    database = tmp_path / "ledger-drift.sqlite3"
    engine = build_sqlite_engine(_database_url(database), allow_missing=True)
    try:
        upgrade_sqlite_schema(engine)
        SQLiteSchemaMigration.__table__.drop(engine)
        Table(
            SQLITE_MIGRATION_TABLE,
            MetaData(),
            Column("version", String(10), primary_key=True),
        ).create(engine)

        with pytest.raises(RuntimeError, match="SQLite migration history conflicts"):
            upgrade_sqlite_schema(engine)
    finally:
        engine.dispose()


def test_versioned_history_rejects_mutated_old_revision_when_replay_matches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "mutated-published-v4.sqlite3"
    engine = build_sqlite_engine(_database_url(database), allow_missing=True)
    original = PUBLISHED_SQLITE_MIGRATIONS[3]

    def mutated_upgrade(operations: Operations) -> None:
        original.upgrade(operations)
        operations.create_index(
            "agents_unpublished_status_idx",
            "agents",
            ["status"],
        )

    mutated_catalog = (
        *PUBLISHED_SQLITE_MIGRATIONS[:3],
        sqlite_migrations.PublishedSQLiteMigration(
            version=original.version,
            name=original.name,
            checksum=original.checksum,
            upgrade=mutated_upgrade,
        ),
        *PUBLISHED_SQLITE_MIGRATIONS[4:],
    )
    monkeypatch.setattr(
        sqlite_migrations,
        "PUBLISHED_SQLITE_MIGRATIONS",
        mutated_catalog,
    )
    sqlite_migrations._expected_versioned_database_fingerprint.cache_clear()
    try:
        with engine.begin() as connection:
            operations = Operations(MigrationContext.configure(connection))
            SQLiteSchemaMigration.__table__.create(connection)
            for migration in mutated_catalog[:4]:
                migration.upgrade(operations)
        with Session(engine) as session, session.begin():
            for migration in mutated_catalog[:4]:
                session.add(
                    SQLiteSchemaMigration(
                        version=migration.version,
                        name=migration.name,
                        checksum=migration.checksum,
                        applied_at=applied_at(),
                    )
                )

        assert sqlite_migrations.sqlite_schema_fingerprint(engine) == (
            sqlite_migrations._expected_versioned_database_fingerprint(4)
        )
        with pytest.raises(
            sqlite_migrations.SQLiteMigrationHistoryConflict,
            match="SQLite migration history conflicts",
        ):
            sqlite_migrations._validate_versioned_history(engine)
    finally:
        sqlite_migrations._expected_versioned_database_fingerprint.cache_clear()
        engine.dispose()


def test_legacy_based_current_fallback_rejects_raw_ledger_ddl_drift(
    tmp_path: Path,
) -> None:
    database = tmp_path / "legacy-based-current-ledger-raw-drift.sqlite3"
    engine = build_sqlite_engine(_database_url(database), allow_missing=True)
    try:
        _replay_published_legacy_fixture(engine)
        _add_typed_orm_ledger(engine)
        assert upgrade_sqlite_schema(engine).source == "versioned-1"
        with Session(engine) as session:
            applied_rows = [
                (row.version, row.name, row.checksum, row.applied_at)
                for row in session.query(SQLiteSchemaMigration)
                .order_by(SQLiteSchemaMigration.version)
                .all()
            ]

        SQLiteSchemaMigration.__table__.drop(engine)
        _add_typed_orm_ledger(
            engine,
            through_version=0,
            mutate_raw_ddl=True,
        )
        with Session(engine) as session, session.begin():
            for version, name, checksum, migration_applied_at in applied_rows:
                session.add(
                    SQLiteSchemaMigration(
                        version=version,
                        name=name,
                        checksum=checksum,
                        applied_at=migration_applied_at,
                    )
                )

        assert (
            sqlite_migrations._ledger_signature(engine)[0]
            == sqlite_migrations.PUBLISHED_SQLITE_V1_LEDGER_SIGNATURE[0]
        )
        assert (
            sqlite_migrations._ledger_signature(engine)[1]
            != sqlite_migrations.PUBLISHED_SQLITE_V1_LEDGER_SIGNATURE[1]
        )
        with pytest.raises(RuntimeError, match="SQLite migration history conflicts"):
            sqlite_migrations.plan_sqlite_schema(engine)
    finally:
        engine.dispose()


def test_legacy_current_validation_never_calls_dynamic_component_helpers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "legacy-current-fixed-components.sqlite3"
    engine = build_sqlite_engine(_database_url(database), allow_missing=True)

    def dynamic_replay_is_forbidden() -> tuple[str, str]:
        raise AssertionError("production validation called dynamic replay")

    try:
        _apply_published_legacy_fixture_through_current(engine)
        for helper_name in (
            "_expected_current_session_identity_signature",
            "_expected_observer_projection_signature",
            "_expected_connector_transport_signature",
            "_expected_connector_handshake_signature",
        ):
            monkeypatch.setattr(
                sqlite_migrations,
                helper_name,
                dynamic_replay_is_forbidden,
            )

        assert sqlite_migrations.plan_sqlite_schema(engine).source == "current"
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    (
        "component",
        "dynamic_helper_name",
        "table_name",
        "index_name",
        "indexed_columns",
    ),
    (
        (
            "session_identity",
            "_expected_current_session_identity_signature",
            "sessions",
            "session_projection_acl_idx",
            ("tenant_id", "workspace_id", "updated_at"),
        ),
        (
            "observer",
            "_expected_observer_projection_signature",
            "observer_sessions",
            "observer_sessions_lookup_idx",
            ("tenant_id", "session_key", "profile"),
        ),
        (
            "transport",
            "_expected_connector_transport_signature",
            "connector_transport_cursors",
            "connector_transport_cursors_active_idx",
            ("tenant_id", "state", "updated_at"),
        ),
        (
            "handshake",
            "_expected_connector_handshake_signature",
            "connector_transport_handshake_ownership",
            "connector_transport_handshake_lease_idx",
            ("tenant_id", "state", "lease_expires_at"),
        ),
    ),
)
def test_legacy_current_component_raw_drift_ignores_dynamic_expected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    component: str,
    dynamic_helper_name: str,
    table_name: str,
    index_name: str,
    indexed_columns: tuple[str, ...],
) -> None:
    database = tmp_path / f"legacy-current-{component}-raw-drift.sqlite3"
    engine = build_sqlite_engine(_database_url(database), allow_missing=True)
    try:
        _apply_published_legacy_fixture_through_current(engine)
        published_signature = getattr(sqlite_migrations, dynamic_helper_name)()
        columns = ", ".join(
            (
                f"{column} /* unpublished raw drift */"
                if position == 0
                else column
            )
            for position, column in enumerate(indexed_columns)
        )
        with engine.begin() as connection:
            connection.execute(DDL(f"DROP INDEX {index_name}"))
            connection.execute(
                DDL(f"CREATE INDEX {index_name} ON {table_name} ({columns})")
            )

        actual_signature = _legacy_current_component_signature(engine, component)
        assert actual_signature != published_signature
        assert actual_signature[1] != published_signature[1]
        monkeypatch.setattr(
            sqlite_migrations,
            dynamic_helper_name,
            lambda: actual_signature,
        )

        with pytest.raises(
            sqlite_migrations.SQLiteMigrationHistoryConflict,
            match="SQLite migration history conflicts",
        ):
            sqlite_migrations.plan_sqlite_schema(engine)
    finally:
        engine.dispose()


def applied_at() -> datetime:
    return datetime(2026, 7, 31, tzinfo=UTC)


def test_upgrade_coverage_refuses_to_claim_two_unpublished_histories() -> None:
    coverage = sqlite_migrations.sqlite_upgrade_coverage()

    assert coverage.published_versions == tuple(range(1, 14))
    assert coverage.recent_historical_versions == (11, 12)
    assert coverage.recent_two_covered is True


def test_catalog_length_alone_cannot_claim_two_verified_upgrade_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    baseline = PUBLISHED_SQLITE_MIGRATIONS[0]
    monkeypatch.setattr(
        sqlite_migrations,
        "PUBLISHED_SQLITE_MIGRATIONS",
        (
            *PUBLISHED_SQLITE_MIGRATIONS,
            sqlite_migrations.PublishedSQLiteMigration(
                version=14,
                name="0014_synthetic_unverified",
                checksum="2" * 64,
                upgrade=baseline.upgrade,
            ),
            sqlite_migrations.PublishedSQLiteMigration(
                version=15,
                name="0015_synthetic_unverified",
                checksum="3" * 64,
                upgrade=baseline.upgrade,
            ),
        ),
    )

    coverage = sqlite_migrations.sqlite_upgrade_coverage()

    assert coverage.recent_historical_versions == (11, 12)
    assert coverage.recent_two_covered is False


def test_latest_published_checksum_freezes_the_complete_current_shape() -> None:
    assert (
        PUBLISHED_SQLITE_MIGRATIONS[-1].checksum
        == sqlite_migrations.expected_sqlite_schema_fingerprint()
    )


def test_v5_adds_connector_handshake_ownership_without_mutating_v4(
    tmp_path: Path,
) -> None:
    database = tmp_path / "handshake-v5.sqlite3"
    engine = build_sqlite_engine(_database_url(database), allow_missing=True)
    try:
        upgrade_sqlite_schema(engine)
        inspection = inspect(engine)

        assert CURRENT_SQLITE_SCHEMA_VERSION == 13
        assert PUBLISHED_SQLITE_MIGRATIONS[3].name == (
            "0004_connector_transport_cursor"
        )
        assert PUBLISHED_SQLITE_MIGRATIONS[3].checksum == (
            "78613cdbf54520c55c30ab4f0d7e1c09722b14bbcf4577358ce738905c29d02d"
        )
        assert "connector_transport_handshake_ownership" in (
            inspection.get_table_names()
        )
    finally:
        engine.dispose()


def test_v6_adds_observer_v2_state_without_mutating_v5(tmp_path: Path) -> None:
    database = tmp_path / "observer-v2-state-v6.sqlite3"
    engine = build_sqlite_engine(_database_url(database), allow_missing=True)
    try:
        upgrade_sqlite_schema(engine)
        inspection = inspect(engine)

        assert CURRENT_SQLITE_SCHEMA_VERSION == 13
        assert PUBLISHED_SQLITE_MIGRATIONS[4].name == (
            "0005_connector_handshake_ownership"
        )
        assert PUBLISHED_SQLITE_MIGRATIONS[4].checksum == (
            "4568039d452469ed3b60c7dd525366770af22cbde8a607ab4c909c42605d22ad"
        )
        assert "observer_v2_states" in inspection.get_table_names()
        assert [
            column["name"] for column in inspection.get_columns("observer_v2_states")
        ] == [
            "tenant_id",
            "session_id",
            "observer_contract",
            "lifecycle_projection",
        ]
    finally:
        engine.dispose()


def test_v7_scopes_observer_inbox_sequence_to_runtime_generation_and_preserves_v6(
    tmp_path: Path,
) -> None:
    database = tmp_path / "observer-inbox-runtime-generation-v7.sqlite3"
    engine = build_sqlite_engine(_database_url(database), allow_missing=True)
    tenant_id = uuid4()
    message_id = uuid4()
    try:
        _apply_published_migrations_through(engine, 6)
        assert (
            sqlite_migrations.sqlite_schema_fingerprint(
                engine,
                excluded_table_names=frozenset({SQLITE_MIGRATION_TABLE}),
            )
            == PUBLISHED_SQLITE_MIGRATIONS[5].checksum
        )
        with Session(engine) as session, session.begin():
            session.add(
                sqlite_migrations.ObserverInboxV6Model(
                    tenant_id=tenant_id,
                    message_id=message_id,
                    workspace_id=uuid4(),
                    agent_id=uuid4(),
                    device_id=uuid4(),
                    connector_instance_id="11111111-1111-4111-8111-111111111111",
                    connector_sequence=1,
                    message_type="session.snapshot.v2",
                    payload_digest="a" * 64,
                    binding_digest="b" * 64,
                    received_at=applied_at(),
                )
            )

        result = upgrade_sqlite_schema(engine)

        assert result.source == "versioned-6"
        inspection = inspect(engine)
        assert [
            column["name"]
            for column in inspection.get_columns("observer_inbox_messages")
        ] == [
            "tenant_id",
            "message_id",
            "workspace_id",
            "agent_id",
            "device_id",
            "connector_instance_id",
            "runtime_generation",
            "connector_sequence",
            "message_type",
            "payload_digest",
            "binding_digest",
            "received_at",
            "retention_until",
        ]
        assert {
            tuple(constraint["column_names"])
            for constraint in inspection.get_unique_constraints(
                "observer_inbox_messages"
            )
        } == {
            (
                "tenant_id",
                "device_id",
                "connector_instance_id",
                "runtime_generation",
                "connector_sequence",
            )
        }
        with Session(engine) as session:
            migrated = session.get(ObserverInboxModel, (tenant_id, message_id))
            assert migrated is not None
            assert migrated.runtime_generation == (
                sqlite_migrations.OBSERVER_INBOX_V6_RUNTIME_GENERATION
            )
            assert migrated.retention_until.replace(tzinfo=UTC) == (
                applied_at() + timedelta(days=30)
            )
    finally:
        engine.dispose()


def test_v7_failure_rolls_back_table_rebuild_and_is_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "observer-inbox-runtime-generation-v7-rollback.sqlite3"
    engine = build_sqlite_engine(_database_url(database), allow_missing=True)
    try:
        _apply_published_migrations_through(engine, 6)
        with Session(engine) as session, session.begin():
            session.add(
                sqlite_migrations.ObserverInboxV6Model(
                    tenant_id=uuid4(),
                    message_id=uuid4(),
                    workspace_id=uuid4(),
                    agent_id=uuid4(),
                    device_id=uuid4(),
                    connector_instance_id="22222222-2222-4222-8222-222222222222",
                    connector_sequence=1,
                    message_type="session.snapshot.v2",
                    payload_digest="c" * 64,
                    binding_digest="d" * 64,
                    received_at=applied_at(),
                )
            )

        original_bulk_insert = Operations.bulk_insert

        def fail_v7_copy(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("synthetic v7 copy failure")

        monkeypatch.setattr(Operations, "bulk_insert", fail_v7_copy)
        with pytest.raises(RuntimeError, match="synthetic v7 copy failure"):
            upgrade_sqlite_schema(engine)
        monkeypatch.setattr(Operations, "bulk_insert", original_bulk_insert)

        assert "runtime_generation" not in {
            column["name"]
            for column in inspect(engine).get_columns("observer_inbox_messages")
        }
        with Session(engine) as session:
            assert session.get(SQLiteSchemaMigration, 7) is None

        assert upgrade_sqlite_schema(engine).source == "versioned-6"
        assert "runtime_generation" in {
            column["name"]
            for column in inspect(engine).get_columns("observer_inbox_messages")
        }
    finally:
        engine.dispose()


def test_v8_keeps_never_dispatched_v7_intent_unbound_for_first_reservation(
    tmp_path: Path,
) -> None:
    assert (
        find_spec(
            "hermes_cloud.platform.sqlalchemy.observer_subscription_migration_models"
        )
        is not None
    )
    from hermes_cloud.platform.sqlalchemy.observer_subscription import (
        SqlAlchemyObserverSubscriptionRouter,
    )
    from hermes_cloud.platform.sqlalchemy.observer_subscription_migration_models import (
        ObserverSubscriptionIntentV7Model,
        ObserverSubscriptionTargetV7Model,
    )
    from hermes_cloud.platform.sqlalchemy.observer_subscription_models import (
        ObserverSubscriptionIntentModel,
    )

    database = tmp_path / "observer-subscription-wire-contract-v8.sqlite3"
    engine = build_sqlite_engine(_database_url(database), allow_missing=True)
    tenant_id = uuid4()
    target_id = uuid4()
    request_id = uuid4()
    payload = {
        "request_id": str(request_id),
        "subscription_id": str(target_id),
        "profile": "default",
        "session_key": "legacy-session",
        "target_source": "cloud_authorized_binding",
        "requested_at": "2026-08-01T00:00:00Z",
    }
    try:
        _apply_published_migrations_through(engine, 7)
        assert (
            sqlite_migrations.sqlite_schema_fingerprint(
                engine,
                excluded_table_names=frozenset({SQLITE_MIGRATION_TABLE}),
            )
            == PUBLISHED_SQLITE_MIGRATIONS[6].checksum
        )
        with Session(engine) as session, session.begin():
            session.add(
                ObserverSubscriptionTargetV7Model(
                    tenant_id=tenant_id,
                    target_subscription_id=target_id,
                    workspace_id=uuid4(),
                    agent_id=uuid4(),
                    device_id=uuid4(),
                    profile="default",
                    session_key="legacy-session",
                    state="active",
                    active_ref_count=1,
                    next_intent_sequence=1,
                    revision=1,
                    created_at=applied_at(),
                    updated_at=applied_at(),
                )
            )
            session.flush()
            session.add(
                ObserverSubscriptionIntentV7Model(
                    tenant_id=tenant_id,
                    request_id=request_id,
                    supersedes_request_id=None,
                    target_subscription_id=target_id,
                    intent_sequence=0,
                    workspace_id=uuid4(),
                    agent_id=uuid4(),
                    device_id=uuid4(),
                    message_type="session.observe.open",
                    payload=payload,
                    state="pending",
                    dispatch_connection_id=None,
                    dispatch_sequence=None,
                    dispatch_attempts=0,
                    dispatched_at=None,
                    settled_at=None,
                    created_at=applied_at(),
                    updated_at=applied_at(),
                )
            )

        result = upgrade_sqlite_schema(engine)

        assert result.source == "versioned-7"
        assert CURRENT_SQLITE_SCHEMA_VERSION == 13
        assert PUBLISHED_SQLITE_MIGRATIONS[6].name == (
            "0007_observer_inbox_runtime_epoch"
        )
        assert PUBLISHED_SQLITE_MIGRATIONS[6].checksum == (
            "82087977133534a69e563dc4430cb4788bf6cd8136d379842ec6067a2b86e764"
        )
        assert [
            column["name"]
            for column in inspect(engine).get_columns("observer_subscription_intents")
        ][-3:] == [
            "observer_contract",
            "wire_message_type",
            "wire_payload_digest",
        ]
        with Session(engine) as session:
            migrated = session.get(
                ObserverSubscriptionIntentModel,
                (tenant_id, request_id),
            )
            assert migrated is not None
            assert migrated.observer_contract is None
            assert migrated.wire_message_type is None
            assert migrated.wire_payload_digest is None
            wire_type, wire_digest = SqlAlchemyObserverSubscriptionRouter._wire_binding(
                migrated,
                observer_contract=2,
            )
            SqlAlchemyObserverSubscriptionRouter._require_or_freeze_wire_binding(
                migrated,
                observer_contract=2,
                wire_message_type=wire_type,
                wire_payload_digest=wire_digest,
            )
            session.commit()
            assert migrated.observer_contract == 2
            assert migrated.wire_message_type == "session.observe.open.v2"
    finally:
        engine.dispose()


@pytest.mark.parametrize("possible_wire_contract", (1, 2))
@pytest.mark.parametrize(
    ("message_type", "state", "target_state", "active_ref_count", "replacement"),
    (
        ("session.observe.open", "dispatching", "active", 1, True),
        ("session.observe.open", "settled", "active", 1, True),
        ("session.observe.close", "dispatching", "closing", 0, True),
        ("session.observe.close", "settled", "closed", 0, False),
    ),
)
def test_v8_never_guesses_unknown_dispatched_v7_wire_contract(
    tmp_path: Path,
    possible_wire_contract: int,
    message_type: str,
    state: str,
    target_state: str,
    active_ref_count: int,
    replacement: bool,
) -> None:
    from hermes_cloud.platform.postgres.models import OutboxEventModel
    from hermes_cloud.platform.sqlalchemy.observer_subscription import (
        SqlAlchemyObserverSubscriptionRouter,
    )
    from hermes_cloud.platform.sqlalchemy.observer_subscription_migration_models import (
        ObserverConnectorRouteV7Model,
        ObserverSubscriptionIntentV7Model,
        ObserverSubscriptionTargetV7Model,
    )
    from hermes_cloud.platform.sqlalchemy.observer_subscription_models import (
        ObserverConnectorRouteModel,
        ObserverSubscriptionIntentModel,
        ObserverSubscriptionTargetModel,
    )

    database = tmp_path / (
        f"observer-subscription-v8-unknown-{message_type.rsplit('.', 1)[-1]}-"
        f"{state}-possible-v{possible_wire_contract}.sqlite3"
    )
    engine = build_sqlite_engine(_database_url(database), allow_missing=True)
    tenant_id = uuid4()
    target_id = uuid4()
    request_id = uuid4()
    workspace_id = uuid4()
    agent_id = uuid4()
    device_id = uuid4()
    timestamp_key = (
        "requested_at" if message_type == "session.observe.open" else "closed_at"
    )
    payload: dict[str, object] = {
        "request_id": str(request_id),
        "subscription_id": str(target_id),
        "profile": "default",
        "session_key": "legacy-session",
        "target_source": "cloud_authorized_binding",
        timestamp_key: "2026-08-01T00:00:00Z",
    }
    if message_type == "session.observe.close":
        payload["reason"] = "client_unsubscribe"
    try:
        _apply_published_migrations_through(engine, 7)
        with Session(engine) as session, session.begin():
            session.add(
                TenantModel(
                    tenant_id=tenant_id,
                    slug=f"wire-unknown-{request_id.hex}",
                    display_name="Unknown legacy wire",
                    status="active",
                    created_at=applied_at(),
                )
            )
            session.flush()
            session.add(
                ObserverSubscriptionTargetV7Model(
                    tenant_id=tenant_id,
                    target_subscription_id=target_id,
                    workspace_id=workspace_id,
                    agent_id=agent_id,
                    device_id=device_id,
                    profile="default",
                    session_key="legacy-session",
                    state=target_state,
                    active_ref_count=active_ref_count,
                    next_intent_sequence=1,
                    revision=1,
                    created_at=applied_at(),
                    updated_at=applied_at(),
                )
            )
            session.flush()
            session.add_all(
                [
                    ObserverConnectorRouteV7Model(
                        tenant_id=tenant_id,
                        device_id=device_id,
                        agent_id=agent_id,
                        connector_instance_id=("92000000-0000-4000-8000-000000000001"),
                        connection_id="91000000-0000-4000-8000-000000000001",
                        runtime_generation="legacy-runtime-generation",
                        state="active",
                        next_connector_sequence=7,
                        next_cloud_sequence=5,
                        revision=1,
                        connected_at=applied_at(),
                        updated_at=applied_at(),
                    ),
                    ObserverSubscriptionIntentV7Model(
                        tenant_id=tenant_id,
                        request_id=request_id,
                        supersedes_request_id=None,
                        target_subscription_id=target_id,
                        intent_sequence=0,
                        workspace_id=workspace_id,
                        agent_id=agent_id,
                        device_id=device_id,
                        message_type=message_type,
                        payload=payload,
                        state=state,
                        dispatch_connection_id=("91000000-0000-4000-8000-000000000001"),
                        dispatch_sequence=4,
                        dispatch_attempts=1,
                        dispatched_at=applied_at(),
                        settled_at=(applied_at() if state == "settled" else None),
                        created_at=applied_at(),
                        updated_at=applied_at(),
                    ),
                    OutboxEventModel(
                        tenant_id=tenant_id,
                        event_id=request_id,
                        workspace_id=workspace_id,
                        aggregate_type="observer_subscription",
                        aggregate_id=target_id,
                        event_type=message_type,
                        payload={"request_id": str(request_id)},
                        state=("published" if state == "settled" else "publishing"),
                        publish_attempts=1,
                        available_at=applied_at(),
                        published_at=(applied_at() if state == "settled" else None),
                        created_at=applied_at(),
                    ),
                ]
            )

        assert upgrade_sqlite_schema(engine).source == "versioned-7"

        with Session(engine) as session:
            migrated = session.get(
                ObserverSubscriptionIntentModel,
                (tenant_id, request_id),
            )
            assert migrated is not None
            assert migrated.state == "cancelled"
            assert migrated.observer_contract is None
            assert migrated.wire_message_type is None
            assert migrated.wire_payload_digest is None
            old_outbox = session.get(OutboxEventModel, (tenant_id, request_id))
            assert old_outbox is not None
            assert old_outbox.state == "dead"

            intents = session.scalars(
                select(ObserverSubscriptionIntentModel).where(
                    ObserverSubscriptionIntentModel.tenant_id == tenant_id,
                    ObserverSubscriptionIntentModel.request_id != request_id,
                )
            ).all()
            assert len(intents) == int(replacement)
            target = session.get(
                ObserverSubscriptionTargetModel,
                (tenant_id, target_id),
            )
            assert target is not None
            assert target.active_ref_count == active_ref_count
            assert target.state == target_state
            assert target.next_intent_sequence == 1 + int(replacement)
            route = session.get(
                ObserverConnectorRouteModel,
                (tenant_id, device_id),
            )
            assert route is not None
            assert route.next_connector_sequence == 7
            assert route.next_cloud_sequence == 5
            replacement_request_id = None
            replacement_wire_type = None
            replacement_wire_digest = None
            if replacement:
                retried = intents[0]
                replacement_request_id = str(retried.request_id)
                assert retried.request_id != request_id
                assert retried.supersedes_request_id == request_id
                assert retried.message_type == message_type
                assert retried.state == "pending"
                assert retried.dispatch_sequence is None
                assert retried.observer_contract is None
                assert retried.payload["request_id"] == str(retried.request_id)
                new_outbox = session.get(
                    OutboxEventModel,
                    (tenant_id, retried.request_id),
                )
                assert new_outbox is not None
                assert new_outbox.state == "pending"
                replacement_wire_type, replacement_wire_digest = (
                    SqlAlchemyObserverSubscriptionRouter._wire_binding(
                        retried,
                        observer_contract=possible_wire_contract,
                    )
                )

        factory = sessionmaker(bind=engine, expire_on_commit=False)
        router = SqlAlchemyObserverSubscriptionRouter(
            factory,
            now=applied_at,
            poll_interval_seconds=0.001,
        )
        identity = ConnectorIdentity(
            tenant_id=str(tenant_id),
            device_id=str(device_id),
            agent_id=str(agent_id),
            scopes=("session.observe",),
            legacy_seed=False,
        )

        async def reserve_after_upgrade() -> None:
            with pytest.raises(RuntimeError, match="target changed"):
                await router.reserve_subscription_intent(
                    identity=identity,
                    connection_id="91000000-0000-4000-8000-000000000001",
                    connector_instance_id="92000000-0000-4000-8000-000000000001",
                    request_id=str(request_id),
                    message_id=str(request_id),
                    sequence=4,
                    observer_contract=possible_wire_contract,
                    wire_message_type=(
                        f"{message_type}.v2"
                        if possible_wire_contract == 2
                        else message_type
                    ),
                    wire_payload_digest="0" * 64,
                )
            if replacement_request_id is not None:
                assert replacement_wire_type is not None
                assert replacement_wire_digest is not None
                reserved = await router.reserve_subscription_intent(
                    identity=identity,
                    connection_id="91000000-0000-4000-8000-000000000001",
                    connector_instance_id="92000000-0000-4000-8000-000000000001",
                    request_id=replacement_request_id,
                    message_id=replacement_request_id,
                    sequence=5,
                    observer_contract=possible_wire_contract,
                    wire_message_type=replacement_wire_type,
                    wire_payload_digest=replacement_wire_digest,
                )
                assert reserved.request_id == replacement_request_id

        asyncio.run(reserve_after_upgrade())

        with Session(engine) as session:
            old = session.get(
                ObserverSubscriptionIntentModel,
                (tenant_id, request_id),
            )
            assert old is not None
            assert old.state == "cancelled"
            assert old.observer_contract is None
            if replacement_request_id is not None:
                retried = session.get(
                    ObserverSubscriptionIntentModel,
                    (tenant_id, UUID(replacement_request_id)),
                )
                assert retried is not None
                assert retried.observer_contract == possible_wire_contract
                assert retried.dispatch_sequence == 5
            route = session.get(
                ObserverConnectorRouteModel,
                (tenant_id, device_id),
            )
            assert route is not None
            assert route.next_connector_sequence == 7
            assert route.next_cloud_sequence == 5

        assert upgrade_sqlite_schema(engine).source == "current"

        with Session(engine) as session:
            all_intents = session.scalars(
                select(ObserverSubscriptionIntentModel).where(
                    ObserverSubscriptionIntentModel.tenant_id == tenant_id
                )
            ).all()
            assert len(all_intents) == 1 + int(replacement)
            target = session.get(
                ObserverSubscriptionTargetModel,
                (tenant_id, target_id),
            )
            assert target is not None
            assert target.active_ref_count == active_ref_count
            assert target.state == target_state
            assert target.next_intent_sequence == 1 + int(replacement)
            route = session.get(
                ObserverConnectorRouteModel,
                (tenant_id, device_id),
            )
            assert route is not None
            assert route.next_connector_sequence == 7
            assert route.next_cloud_sequence == 5
    finally:
        engine.dispose()


def test_v9_backfills_explicit_inbox_retention_without_mutating_v8(
    tmp_path: Path,
) -> None:
    database = tmp_path / "observer-inbox-retention-v9.sqlite3"
    engine = build_sqlite_engine(_database_url(database), allow_missing=True)
    tenant_id = uuid4()
    message_id = uuid4()
    received_at = applied_at()
    try:
        _apply_published_migrations_through(engine, 8)
        assert (
            sqlite_migrations.sqlite_schema_fingerprint(
                engine,
                excluded_table_names=frozenset({SQLITE_MIGRATION_TABLE}),
            )
            == PUBLISHED_SQLITE_MIGRATIONS[7].checksum
        )
        with Session(engine) as session, session.begin():
            session.add(
                sqlite_migrations.ObserverInboxV7Model(
                    tenant_id=tenant_id,
                    message_id=message_id,
                    workspace_id=uuid4(),
                    agent_id=uuid4(),
                    device_id=uuid4(),
                    connector_instance_id="33333333-3333-4333-8333-333333333333",
                    runtime_generation="legacy-v8-epoch",
                    connector_sequence=1,
                    message_type="session.snapshot.v2",
                    payload_digest="e" * 64,
                    binding_digest="f" * 64,
                    received_at=received_at,
                )
            )

        result = upgrade_sqlite_schema(engine)

        assert result.source == "versioned-8"
        assert CURRENT_SQLITE_SCHEMA_VERSION == 13
        assert PUBLISHED_SQLITE_MIGRATIONS[7].name == (
            "0008_observer_subscription_wire_contract"
        )
        assert PUBLISHED_SQLITE_MIGRATIONS[7].checksum == (
            "294e5303bc88fded09ed5f5a0f510de7fdd1d187c4f8e2d6085dee4344fd458d"
        )
        inspection = inspect(engine)
        assert [
            column["name"]
            for column in inspection.get_columns("observer_inbox_messages")
        ][-2:] == ["received_at", "retention_until"]
        assert "observer_inbox_retention_idx" in {
            index["name"] for index in inspection.get_indexes("observer_inbox_messages")
        }
        with Session(engine) as session:
            migrated = session.get(ObserverInboxModel, (tenant_id, message_id))
            assert migrated is not None
            assert migrated.retention_until.replace(tzinfo=UTC) == (
                received_at + timedelta(days=30)
            )
    finally:
        engine.dispose()


def test_v9_backfill_failure_rolls_back_and_is_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "observer-inbox-retention-v9-rollback.sqlite3"
    engine = build_sqlite_engine(_database_url(database), allow_missing=True)
    tenant_id = uuid4()
    message_id = uuid4()
    try:
        _apply_published_migrations_through(engine, 8)
        with Session(engine) as session, session.begin():
            session.add(
                sqlite_migrations.ObserverInboxV7Model(
                    tenant_id=tenant_id,
                    message_id=message_id,
                    workspace_id=uuid4(),
                    agent_id=uuid4(),
                    device_id=uuid4(),
                    connector_instance_id="44444444-4444-4444-8444-444444444444",
                    runtime_generation="legacy-v8-epoch",
                    connector_sequence=1,
                    message_type="session.snapshot",
                    payload_digest="1" * 64,
                    binding_digest="2" * 64,
                    received_at=applied_at(),
                )
            )

        original_bulk_insert = Operations.bulk_insert

        def fail_v9_copy(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("synthetic v9 copy failure")

        monkeypatch.setattr(Operations, "bulk_insert", fail_v9_copy)
        with pytest.raises(RuntimeError, match="synthetic v9 copy failure"):
            upgrade_sqlite_schema(engine)
        monkeypatch.setattr(Operations, "bulk_insert", original_bulk_insert)

        assert "retention_until" not in {
            column["name"]
            for column in inspect(engine).get_columns("observer_inbox_messages")
        }
        with Session(engine) as session:
            assert session.get(SQLiteSchemaMigration, 9) is None
            assert (
                session.get(
                    sqlite_migrations.ObserverInboxV7Model,
                    (tenant_id, message_id),
                )
                is not None
            )

        assert upgrade_sqlite_schema(engine).source == "versioned-8"
        with Session(engine) as session:
            migrated = session.get(ObserverInboxModel, (tenant_id, message_id))
            assert migrated is not None
            assert migrated.retention_until.replace(tzinfo=UTC) == (
                applied_at() + timedelta(days=30)
            )
    finally:
        engine.dispose()


def test_target_fingerprints_are_stable_in_fresh_processes() -> None:
    script = (
        "from hermes_cloud.platform.sqlite.migrations import "
        "expected_current_database_fingerprint, "
        "expected_sqlite_schema_fingerprint; "
        "print(expected_sqlite_schema_fingerprint()); "
        "print(expected_current_database_fingerprint())"
    )
    environment = dict(os.environ)
    environment.pop("PYTHONHASHSEED", None)
    observed = []
    for _attempt in range(3):
        result = subprocess.run(
            [sys.executable, "-c", script],
            check=True,
            capture_output=True,
            encoding="utf-8",
            env=environment,
            timeout=10,
        )
        observed.append(tuple(result.stdout.splitlines()))

    assert len(set(observed)) == 1
    assert observed[0] == (
        sqlite_migrations.expected_sqlite_schema_fingerprint(),
        sqlite_migrations.expected_current_database_fingerprint(),
    )


def test_interrupted_empty_upgrade_never_records_false_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "interrupted.sqlite3"
    engine = build_sqlite_engine(_database_url(database), allow_missing=True)
    original_invoke = Operations.invoke
    calls = 0

    def fail_during_typed_ddl(
        operations: Operations,
        operation: object,
    ) -> object:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("synthetic typed DDL failure")
        return original_invoke(operations, operation)

    monkeypatch.setattr(Operations, "invoke", fail_during_typed_ddl)
    try:
        with pytest.raises(RuntimeError, match="synthetic typed DDL failure"):
            upgrade_sqlite_schema(engine)
        tables_after_failure = set(inspect(engine).get_table_names())
        assert SQLITE_MIGRATION_TABLE not in tables_after_failure
    finally:
        engine.dispose()

    monkeypatch.undo()
    database.chmod(0o660)
    retry_engine = build_sqlite_engine(_database_url(database))
    try:
        if tables_after_failure:
            with pytest.raises(
                RuntimeError,
                match="SQLite migration history conflicts",
            ):
                upgrade_sqlite_schema(retry_engine)
        else:
            assert upgrade_sqlite_schema(retry_engine).source == "empty"
    finally:
        retry_engine.dispose()


@pytest.mark.parametrize("source", ("empty", "legacy-current"))
@pytest.mark.parametrize("failure_point", ("flush", "commit"))
def test_ledger_failure_rolls_back_all_ddl_and_is_immediately_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: str,
    failure_point: str,
) -> None:
    database = tmp_path / f"{source}-{failure_point}.sqlite3"
    engine = build_sqlite_engine(_database_url(database), allow_missing=True)
    if source == "legacy-current":
        _replay_published_legacy_fixture(engine)
        baseline_fingerprint = sqlite_migrations.sqlite_schema_fingerprint(engine)
    else:
        baseline_fingerprint = None

    original = getattr(Session, failure_point)

    def fail_after_ledger_operation(
        session: Session,
        *args: object,
        **kwargs: object,
    ) -> object:
        original(session, *args, **kwargs)
        raise RuntimeError(f"synthetic ledger {failure_point} failure")

    monkeypatch.setattr(Session, failure_point, fail_after_ledger_operation)
    try:
        with pytest.raises(
            RuntimeError,
            match=f"synthetic ledger {failure_point} failure",
        ):
            upgrade_sqlite_schema(engine)
    finally:
        monkeypatch.undo()

    try:
        table_names = {
            name
            for name in inspect(engine).get_table_names()
            if not name.startswith("sqlite_")
        }
        assert SQLITE_MIGRATION_TABLE not in table_names
        if source == "empty":
            assert table_names == set()
        else:
            assert (
                sqlite_migrations.sqlite_schema_fingerprint(engine)
                == baseline_fingerprint
            )

        assert upgrade_sqlite_schema(engine).source == source
        assert sqlite_migrations.plan_sqlite_schema(engine).source == "current"
        with Session(engine) as session:
            assert session.query(SQLiteSchemaMigration).count() == len(
                PUBLISHED_SQLITE_MIGRATIONS
            )
    finally:
        engine.dispose()


@pytest.mark.parametrize("source", ("empty", "legacy-current", "versioned-1"))
def test_concurrent_apply_is_bounded_and_converges_to_one_current_ledger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: str,
) -> None:
    database = tmp_path / f"concurrent-{source}.sqlite3"
    database_url = _database_url(database)
    if source == "legacy-current":
        seed_engine = build_sqlite_engine(database_url, allow_missing=True)
        try:
            _replay_published_legacy_fixture(seed_engine)
        finally:
            seed_engine.dispose()
        database.chmod(0o660)
    elif source == "versioned-1":
        seed_engine = build_sqlite_engine(database_url, allow_missing=True)
        try:
            with seed_engine.begin() as connection:
                operations = Operations(MigrationContext.configure(connection))
                baseline = PUBLISHED_SQLITE_MIGRATIONS[0]
                baseline.upgrade(operations)
                SQLiteSchemaMigration.__table__.create(connection)
                with Session(
                    bind=connection,
                    join_transaction_mode="create_savepoint",
                ) as session:
                    session.add(
                        SQLiteSchemaMigration(
                            version=baseline.version,
                            name=baseline.name,
                            checksum=baseline.checksum,
                            applied_at=applied_at(),
                        )
                    )
                    session.commit()
        finally:
            seed_engine.dispose()
        database.chmod(0o660)

    engines = [
        build_sqlite_engine(database_url, allow_missing=True),
        build_sqlite_engine(database_url, allow_missing=True),
    ]
    original_plan = sqlite_migrations.plan_sqlite_schema
    barrier = Barrier(2)
    counter_lock = Lock()
    synchronized_calls = 0

    def synchronized_initial_plan(engine: Engine) -> object:
        nonlocal synchronized_calls
        result = original_plan(engine)
        should_wait = False
        with counter_lock:
            if synchronized_calls < 2 and result.source == source:
                synchronized_calls += 1
                should_wait = True
        if should_wait:
            barrier.wait(timeout=10)
        return result

    monkeypatch.setattr(
        sqlite_migrations,
        "plan_sqlite_schema",
        synchronized_initial_plan,
    )
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(upgrade_sqlite_schema, engine) for engine in engines
            ]
            results = [future.result(timeout=15) for future in futures]
    finally:
        for engine in engines:
            engine.dispose()

    assert sorted(result.source for result in results) == sorted((source, "current"))
    database.chmod(0o660)
    validation_engine = build_sqlite_engine(database_url)
    try:
        assert original_plan(validation_engine).source == "current"
        with Session(validation_engine) as session:
            assert session.query(SQLiteSchemaMigration).count() == len(
                PUBLISHED_SQLITE_MIGRATIONS
            )
    finally:
        validation_engine.dispose()


@pytest.mark.parametrize("source", ("empty", "legacy-current"))
def test_guard_write_lock_serializes_later_schema_drift_and_keeps_it_detectable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: str,
) -> None:
    database = tmp_path / f"guard-lock-{source}.sqlite3"
    database_url = _database_url(database)
    upgrade_engine = build_sqlite_engine(database_url, allow_missing=True)
    competitor = build_sqlite_engine(database_url, allow_missing=True)
    original_revalidate = sqlite_migrations._revalidate_planned_source_after_guard
    original_plan = sqlite_migrations.plan_sqlite_schema
    guard_ready = Event()
    competitor_attempted = Event()
    competitor_finished = Event()
    if source == "legacy-current":
        _replay_published_legacy_fixture(upgrade_engine)

    def pause_with_guard_held(
        connection: Connection,
        planned_source: str,
    ) -> None:
        original_revalidate(connection, planned_source)
        guard_ready.set()
        assert competitor_attempted.wait(timeout=10)
        assert not competitor_finished.wait(timeout=0.2)

    def mark_competing_ddl_attempt(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        if "unexpected_after_guard" in statement:
            competitor_attempted.set()

    def create_competing_table() -> None:
        assert guard_ready.wait(timeout=10)
        try:
            Table(
                "unexpected_after_guard",
                MetaData(),
                Column("id", Integer, primary_key=True),
            ).create(competitor)
        finally:
            competitor_finished.set()

    monkeypatch.setattr(
        sqlite_migrations,
        "_revalidate_planned_source_after_guard",
        pause_with_guard_held,
    )
    event.listen(
        competitor,
        "before_cursor_execute",
        mark_competing_ddl_attempt,
    )
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            upgrade_future = executor.submit(upgrade_sqlite_schema, upgrade_engine)
            competitor_future = executor.submit(create_competing_table)
            assert upgrade_future.result(timeout=15).source == source
            competitor_future.result(timeout=15)

        assert competitor_finished.is_set()
        table_names = set(inspect(upgrade_engine).get_table_names())
        assert SQLITE_MIGRATION_TABLE in table_names
        assert "unexpected_after_guard" in table_names
        with pytest.raises(RuntimeError, match="SQLite migration history conflicts"):
            original_plan(upgrade_engine)
    finally:
        event.remove(
            competitor,
            "before_cursor_execute",
            mark_competing_ddl_attempt,
        )
        competitor.dispose()
        upgrade_engine.dispose()


def test_collision_revalidation_propagates_original_when_database_is_not_current(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "collision-without-current.sqlite3"
    engine = build_sqlite_engine(_database_url(database), allow_missing=True)
    collision = OperationalError(
        "typed migration DDL",
        {},
        sqlite3.OperationalError(
            "table hermes_sqlite_schema_migrations already exists"
        ),
    )
    original_plan = sqlite_migrations.plan_sqlite_schema
    plan_calls = 0

    def counted_plan(candidate: Engine) -> object:
        nonlocal plan_calls
        plan_calls += 1
        return original_plan(candidate)

    def fail_without_changing_database(*_args: object, **_kwargs: object) -> None:
        raise collision

    monkeypatch.setattr(sqlite_migrations, "plan_sqlite_schema", counted_plan)
    monkeypatch.setattr(
        sqlite_migrations,
        "_apply_migration_transaction",
        fail_without_changing_database,
    )
    monkeypatch.setattr(
        sqlite_migrations,
        "_CONCURRENT_CONVERGENCE_TIMEOUT_SECONDS",
        0.0,
    )
    try:
        with pytest.raises(OperationalError) as captured:
            upgrade_sqlite_schema(engine)
        assert captured.value is collision
        assert plan_calls == 2
        assert original_plan(engine).source == "empty"
    finally:
        engine.dispose()
