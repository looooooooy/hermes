from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Engine, inspect, select
from sqlalchemy.orm import Session

from hermes_connector.adapters.persistence.sqlite.migrations.cloud_session_v2 import (
    CLOUD_SESSION_V2_AUDIT_SIGNATURE,
    CLOUD_SESSION_V2_CHECKSUM,
    upgrade_cloud_session_v2,
)
from hermes_connector.adapters.persistence.sqlite.migrations.control_command_v3 import (
    CONTROL_COMMAND_V3_AUDIT_SIGNATURE,
    CONTROL_COMMAND_V3_CHECKSUM,
    upgrade_control_command_v3,
)
from hermes_connector.adapters.persistence.sqlite.migrations.durable_transport_v6 import (
    DURABLE_TRANSPORT_V6_AUDIT_SIGNATURE,
    DURABLE_TRANSPORT_V6_CHECKSUM,
    upgrade_durable_transport_v6,
)
from hermes_connector.adapters.persistence.sqlite.migrations.observer_attempt_v5 import (
    OBSERVER_ATTEMPT_V5_AUDIT_SIGNATURE,
    OBSERVER_ATTEMPT_V5_CHECKSUM,
    upgrade_observer_attempt_v5,
)
from hermes_connector.adapters.persistence.sqlite.migrations.observer_outbox_v4 import (
    OBSERVER_OUTBOX_V4_AUDIT_SIGNATURE,
    OBSERVER_OUTBOX_V4_CHECKSUM,
    upgrade_observer_outbox_v4,
)
from hermes_connector.adapters.persistence.sqlite.migrations.observer_v2_v7 import (
    OBSERVER_V2_V7_AUDIT_SIGNATURE,
    OBSERVER_V2_V7_CHECKSUM,
    upgrade_observer_v2_v7,
)
from hermes_connector.adapters.persistence.sqlite.migrations.session_catalog_ack_receipt_v9 import (
    SESSION_CATALOG_ACK_RECEIPT_V9_AUDIT_SIGNATURE,
    SESSION_CATALOG_ACK_RECEIPT_V9_CHECKSUM,
    upgrade_session_catalog_ack_receipt_v9,
)
from hermes_connector.adapters.persistence.sqlite.migrations.session_catalog_v8 import (
    SESSION_CATALOG_V8_AUDIT_SIGNATURE,
    SESSION_CATALOG_V8_CHECKSUM,
    upgrade_session_catalog_v8,
)
from hermes_connector.adapters.sqlite_models import (
    MAX_DURABLE_PAYLOAD_BYTES,
    SchemaMigration,
)


class MigrationError(RuntimeError):
    pass


class UnsupportedSchemaVersion(MigrationError):
    def __init__(self, version: int) -> None:
        super().__init__(f"unsupported SQLite schema version: {version}")
        self.version = version


class MigrationChecksumMismatch(MigrationError):
    def __init__(self, version: int) -> None:
        super().__init__(f"SQLite migration checksum mismatch: version {version}")
        self.version = version


Upgrade = Callable[[Operations], None]


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    checksum: str
    audit_signature: tuple[str, ...]
    upgrade: Upgrade

    @property
    def audit_digest(self) -> str:
        return hashlib.sha256(self.audit_bytes()).hexdigest()

    def audit_bytes(self) -> bytes:
        return "\n".join(self.audit_signature).encode("utf-8")


def _operations(connection: sa.Connection) -> Operations:
    return Operations(MigrationContext.configure(connection))


def _create_schema_migrations(operations: Operations) -> None:
    operations.create_table(
        "schema_migrations",
        sa.Column("version", sa.Integer(), primary_key=True),
        sa.Column("checksum", sa.Text(), nullable=False),
        sa.Column("applied_at", sa.Text(), nullable=False),
    )


def _upgrade_v1(operations: Operations) -> None:
    inbox_state = sa.Column("state", sa.Text(), nullable=False)
    inbox_payload = sa.Column("payload", sa.LargeBinary(), nullable=False)
    operations.create_table(
        "inbox_messages",
        sa.Column("message_id", sa.Text(), primary_key=True),
        sa.Column("digest", sa.Text(), nullable=False),
        inbox_state,
        inbox_payload,
        sa.Column("received_at", sa.Text(), nullable=False),
        sa.CheckConstraint(
            sa.func.length(inbox_state).between(1, 64),
            name="ck_inbox_state_length",
        ),
        sa.CheckConstraint(
            sa.func.length(inbox_payload) <= MAX_DURABLE_PAYLOAD_BYTES,
            name="ck_inbox_payload_size",
        ),
    )
    operations.create_index(
        "idx_inbox_state_received",
        "inbox_messages",
        ("state", "received_at", "message_id"),
    )

    outbox_sequence = sa.Column("sequence", sa.Integer(), nullable=False)
    outbox_state = sa.Column("state", sa.Text(), nullable=False)
    outbox_payload = sa.Column("payload", sa.LargeBinary(), nullable=False)
    operations.create_table(
        "outbox_messages",
        sa.Column(
            "id",
            sa.Integer(),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column("message_id", sa.Text(), nullable=False, unique=True),
        sa.Column("stream", sa.Text(), nullable=False),
        outbox_sequence,
        outbox_state,
        outbox_payload,
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("acked_at", sa.Text(), nullable=True),
        sa.CheckConstraint(
            outbox_sequence >= 0,
            name="ck_outbox_sequence_nonnegative",
        ),
        sa.CheckConstraint(
            outbox_state.in_(("pending", "acked")),
            name="ck_outbox_state",
        ),
        sa.CheckConstraint(
            sa.func.length(outbox_payload) <= MAX_DURABLE_PAYLOAD_BYTES,
            name="ck_outbox_payload_size",
        ),
        sa.UniqueConstraint(
            "stream",
            "sequence",
            name="uq_outbox_stream_sequence",
        ),
        sqlite_autoincrement=True,
    )
    operations.create_index(
        "idx_outbox_pending_order",
        "outbox_messages",
        ("state", "sequence", "id"),
    )

    cursor_sequence = sa.Column("sequence", sa.Integer(), nullable=False)
    operations.create_table(
        "stream_cursors",
        sa.Column("stream", sa.Text(), primary_key=True),
        cursor_sequence,
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.CheckConstraint(
            cursor_sequence >= 0,
            name="ck_cursor_sequence_nonnegative",
        ),
    )


_V1_AUDIT_SIGNATURE = (
    "schema:v1",
    "table:inbox_messages",
    "index:idx_inbox_state_received",
    "table:outbox_messages",
    "index:idx_outbox_pending_order",
    "table:stream_cursors",
    "limit:payload:262144",
)

# This checksum is the already-persisted v1 migration identity. Refactoring the
# implementation from literal DDL to Alembic operations must not invalidate an
# existing Connector database. ``audit_digest`` independently tracks this
# implementation's reviewable SQLAlchemy schema signature.
MIGRATION_V1 = Migration(
    version=1,
    checksum="f3d03d3cae1efdb05081372f1e31ea283e12027815d39529e12b87bac56fd8fc",
    audit_signature=_V1_AUDIT_SIGNATURE,
    upgrade=_upgrade_v1,
)

MIGRATION_V2 = Migration(
    version=2,
    checksum=CLOUD_SESSION_V2_CHECKSUM,
    audit_signature=CLOUD_SESSION_V2_AUDIT_SIGNATURE,
    upgrade=upgrade_cloud_session_v2,
)

MIGRATION_V3 = Migration(
    version=3,
    checksum=CONTROL_COMMAND_V3_CHECKSUM,
    audit_signature=CONTROL_COMMAND_V3_AUDIT_SIGNATURE,
    upgrade=upgrade_control_command_v3,
)

MIGRATION_V4 = Migration(
    version=4,
    checksum=OBSERVER_OUTBOX_V4_CHECKSUM,
    audit_signature=OBSERVER_OUTBOX_V4_AUDIT_SIGNATURE,
    upgrade=upgrade_observer_outbox_v4,
)

MIGRATION_V5 = Migration(
    version=5,
    checksum=OBSERVER_ATTEMPT_V5_CHECKSUM,
    audit_signature=OBSERVER_ATTEMPT_V5_AUDIT_SIGNATURE,
    upgrade=upgrade_observer_attempt_v5,
)

MIGRATION_V6 = Migration(
    version=6,
    checksum=DURABLE_TRANSPORT_V6_CHECKSUM,
    audit_signature=DURABLE_TRANSPORT_V6_AUDIT_SIGNATURE,
    upgrade=upgrade_durable_transport_v6,
)

MIGRATION_V7 = Migration(
    version=7,
    checksum=OBSERVER_V2_V7_CHECKSUM,
    audit_signature=OBSERVER_V2_V7_AUDIT_SIGNATURE,
    upgrade=upgrade_observer_v2_v7,
)

MIGRATION_V8 = Migration(
    version=8,
    checksum=SESSION_CATALOG_V8_CHECKSUM,
    audit_signature=SESSION_CATALOG_V8_AUDIT_SIGNATURE,
    upgrade=upgrade_session_catalog_v8,
)

MIGRATION_V9 = Migration(
    version=9,
    checksum=SESSION_CATALOG_ACK_RECEIPT_V9_CHECKSUM,
    audit_signature=SESSION_CATALOG_ACK_RECEIPT_V9_AUDIT_SIGNATURE,
    upgrade=upgrade_session_catalog_ack_receipt_v9,
)


def apply_migrations(
    engine: Engine,
    migrations: tuple[Migration, ...] = (
        MIGRATION_V1,
        MIGRATION_V2,
        MIGRATION_V3,
        MIGRATION_V4,
        MIGRATION_V5,
        MIGRATION_V6,
        MIGRATION_V7,
        MIGRATION_V8,
        MIGRATION_V9,
    ),
) -> None:
    ordered = tuple(sorted(migrations, key=lambda migration: migration.version))
    known_versions = {migration.version for migration in ordered}
    latest_version = max(known_versions, default=0)

    with engine.begin() as connection:
        if not inspect(connection).has_table("schema_migrations"):
            _create_schema_migrations(_operations(connection))

    with Session(engine) as session:
        applied = {
            record.version: record.checksum
            for record in session.scalars(select(SchemaMigration))
        }

    for version in applied:
        if version > latest_version or version not in known_versions:
            raise UnsupportedSchemaVersion(version)

    for migration in ordered:
        existing_checksum = applied.get(migration.version)
        if existing_checksum is not None:
            if existing_checksum != migration.checksum:
                raise MigrationChecksumMismatch(migration.version)
            continue
        _apply_one(engine, migration)


def _apply_one(engine: Engine, migration: Migration) -> None:
    with engine.begin() as connection:
        session = Session(bind=connection)
        try:
            # Flushing the version record first explicitly starts SQLite's
            # transaction before Alembic emits DDL, preserving atomic rollback.
            session.add(
                SchemaMigration(
                    version=migration.version,
                    checksum=migration.checksum,
                    applied_at=_utc_now(),
                )
            )
            session.flush()
            migration.upgrade(_operations(connection))
        finally:
            session.close()


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
