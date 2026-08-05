from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.ext.automap import automap_base
from sqlalchemy.orm import Session

from hermes_connector.adapters.sqlite_migrations import (
    MIGRATION_V1,
    MIGRATION_V2,
    MIGRATION_V3,
    MIGRATION_V4,
    MIGRATION_V5,
    MIGRATION_V6,
    MIGRATION_V7,
    MIGRATION_V8,
    MIGRATION_V9,
    Migration,
    MigrationChecksumMismatch,
    UnsupportedSchemaVersion,
    apply_migrations,
)
from hermes_connector.adapters.sqlite_models import SchemaMigration
from hermes_connector.adapters.sqlite_storage import SQLiteStorageComponent
from hermes_connector.bootstrap.config import ConnectorConfig


class SQLiteMigrationTest(unittest.TestCase):
    def test_v1_schema_pragmas_constraints_indexes_and_checksum(self) -> None:
        async def scenario(path: Path) -> None:
            storage = SQLiteStorageComponent(path, ConnectorConfig())
            await storage.start()
            diagnostics = await storage.diagnostics()

            self.assertEqual(diagnostics.journal_mode, "wal")
            self.assertTrue(diagnostics.foreign_keys)
            self.assertEqual(diagnostics.synchronous, 2)
            self.assertEqual(diagnostics.busy_timeout_ms, 5_000)
            await storage.stop()

            reopened = SQLiteStorageComponent(path, ConnectorConfig())
            await reopened.start()
            await reopened.stop()

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "connector.sqlite3"
            asyncio.run(scenario(path))

            engine = create_engine(f"sqlite+pysqlite:///{path}")
            try:
                inspector = inspect(engine)
                tables = set(inspector.get_table_names())
                self.assertTrue(
                    {
                        "schema_migrations",
                        "inbox_messages",
                        "outbox_messages",
                        "stream_cursors",
                        "cloud_session_checkpoint",
                        "control_commands",
                        "observer_outbox",
                        "transport_frame_journal",
                        "owner_control_results",
                        "session_catalog_outbox",
                        "session_catalog_ack_receipts",
                    }.issubset(tables)
                )
                indexes = {
                    index["name"]
                    for table_name in ("inbox_messages", "outbox_messages")
                    for index in inspector.get_indexes(table_name)
                }
                self.assertIn("idx_inbox_state_received", indexes)
                self.assertIn("idx_outbox_pending_order", indexes)
                with Session(engine) as session:
                    records = session.scalars(select(SchemaMigration)).all()
                self.assertEqual(
                    [(record.version, record.checksum) for record in records],
                    [
                        (1, MIGRATION_V1.checksum),
                        (2, MIGRATION_V2.checksum),
                        (3, MIGRATION_V3.checksum),
                        (4, MIGRATION_V4.checksum),
                        (5, MIGRATION_V5.checksum),
                        (6, MIGRATION_V6.checksum),
                        (7, MIGRATION_V7.checksum),
                        (8, MIGRATION_V8.checksum),
                        (9, MIGRATION_V9.checksum),
                    ],
                )
                self.assertEqual(
                    MIGRATION_V1.checksum,
                    "f3d03d3cae1efdb05081372f1e31ea283e12027815d39529e12b87bac56fd8fc",
                )
                self.assertEqual(len(MIGRATION_V1.audit_digest), 64)
                self.assertEqual(len(MIGRATION_V2.audit_digest), 64)
                self.assertEqual(len(MIGRATION_V3.audit_digest), 64)
                self.assertEqual(len(MIGRATION_V4.audit_digest), 64)
                self.assertEqual(len(MIGRATION_V5.audit_digest), 64)
                self.assertEqual(len(MIGRATION_V6.audit_digest), 64)
                self.assertEqual(MIGRATION_V6.checksum, MIGRATION_V6.audit_digest)
                self.assertEqual(len(MIGRATION_V7.audit_digest), 64)
                self.assertEqual(MIGRATION_V7.checksum, MIGRATION_V7.audit_digest)
                self.assertEqual(MIGRATION_V8.checksum, MIGRATION_V8.audit_digest)
                self.assertEqual(MIGRATION_V9.checksum, MIGRATION_V9.audit_digest)
                receipt_columns = {
                    column["name"]
                    for column in inspector.get_columns(
                        "session_catalog_ack_receipts"
                    )
                }
                self.assertNotIn("session_id", receipt_columns)
                self.assertEqual(
                    inspector.get_pk_constraint(
                        "session_catalog_ack_receipts"
                    )["constrained_columns"],
                    ["profile", "runtime_generation"],
                )
                catalog_indexes = {
                    index["name"]
                    for index in inspector.get_indexes("session_catalog_outbox")
                }
                self.assertIn(
                    "idx_session_catalog_outbox_fact_attempt",
                    catalog_indexes,
                )
                self.assertIn(
                    "idx_session_catalog_outbox_pending_sequence",
                    catalog_indexes,
                )
                self.assertEqual(
                    MIGRATION_V6.checksum,
                    "b469fffd1bf069cadd8b2fb686d21f923454e9f3b4639eb3af44769e3796ecd3",
                )
                journal_uniques = {
                    constraint["name"]
                    for constraint in inspector.get_unique_constraints(
                        "transport_frame_journal"
                    )
                }
                self.assertIn("uq_transport_journal_epoch_sequence", journal_uniques)
                self.assertIn("uq_transport_journal_business_attempt", journal_uniques)
                checkpoint_columns = {
                    column["name"]
                    for column in inspector.get_columns("cloud_session_checkpoint")
                }
                self.assertTrue(
                    {
                        "transport_epoch_id",
                        "runtime_generation",
                        "fresh_epoch_required",
                        "transport_recovery_floor",
                    }.issubset(checkpoint_columns)
                )
                observer_indexes = {
                    index["name"] for index in inspector.get_indexes("observer_outbox")
                }
                observer_uniques = {
                    constraint["name"]
                    for constraint in inspector.get_unique_constraints(
                        "observer_outbox"
                    )
                }
                self.assertIn(
                    "idx_observer_outbox_fact_attempt",
                    observer_indexes,
                )
                self.assertNotIn(
                    "uq_observer_outbox_fact_identity",
                    observer_uniques,
                )
                self.assertTrue(inspector.get_pk_constraint("inbox_messages"))
                self.assertTrue(inspector.get_check_constraints("inbox_messages"))
                self.assertTrue(inspector.get_unique_constraints("outbox_messages"))
                self.assertTrue(inspector.get_check_constraints("outbox_messages"))
            finally:
                engine.dispose()

    def test_unknown_higher_schema_version_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "connector.sqlite3"
            engine = create_engine(f"sqlite+pysqlite:///{path}")
            SchemaMigration.__table__.create(engine)
            with Session(engine) as session, session.begin():
                session.add(
                    SchemaMigration(
                        version=10,
                        checksum="future",
                        applied_at="now",
                    )
                )
            engine.dispose()

            storage = SQLiteStorageComponent(path, ConnectorConfig())
            with self.assertRaises(UnsupportedSchemaVersion):
                asyncio.run(storage.start())

    def test_v5_legacy_unknown_epoch_data_is_retired_and_forces_fresh(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "connector.sqlite3"
            engine = create_engine(f"sqlite+pysqlite:///{path}")
            apply_migrations(
                engine,
                (MIGRATION_V1, MIGRATION_V2, MIGRATION_V3, MIGRATION_V4, MIGRATION_V5),
            )
            legacy = automap_base()
            legacy.prepare(autoload_with=engine)
            outbox = legacy.classes.outbox_messages
            observer = legacy.classes.observer_outbox
            checkpoint = legacy.classes.cloud_session_checkpoint
            with Session(engine) as session, session.begin():
                session.add(
                    outbox(
                        message_id="legacy-message",
                        stream="cloud",
                        sequence=9,
                        state="pending",
                        payload=b"legacy",
                        created_at="2026-07-31T00:00:00Z",
                        acked_at=None,
                    )
                )
                session.add(
                    observer(
                        message_id="83000000-0000-4000-8000-000000000099",
                        payload_digest="a" * 64,
                        connector_sequence=9,
                        message_type="session.event",
                        profile="default",
                        session_key="session-1",
                        runtime_generation="legacy-runtime",
                        runtime_session_id="legacy-session",
                        event_sequence=1,
                        payload=b"{}",
                        frame=b"{}",
                        state="pending",
                        created_at="2026-07-31T00:00:00Z",
                        settled_at=None,
                    )
                )
                session.add(
                    checkpoint(
                        id=1,
                        previous_connection_id=("87000000-0000-4000-8000-000000000099"),
                        next_outbound_sequence=10,
                        next_inbound_sequence=12,
                        reconciliation_required=False,
                        updated_at="2026-07-31T00:00:00Z",
                    )
                )

            apply_migrations(engine)

            from hermes_connector.adapters.persistence.sqlite.models.cloud_session import (
                CloudSessionCheckpointRow,
            )
            from hermes_connector.adapters.persistence.sqlite.models.observer_outbox import (
                ObserverOutboxRow,
            )
            from hermes_connector.adapters.sqlite_models import OutboxMessage

            with Session(engine) as session:
                migrated_outbox = session.get(OutboxMessage, 1)
                migrated_observer = session.get(
                    ObserverOutboxRow,
                    "83000000-0000-4000-8000-000000000099",
                )
                migrated_checkpoint = session.get(CloudSessionCheckpointRow, 1)
                self.assertIsNotNone(migrated_outbox)
                self.assertEqual(migrated_outbox.state, "retired")
                self.assertIsNotNone(migrated_observer)
                self.assertEqual(migrated_observer.state, "retired")
                self.assertIsNone(migrated_observer.transport_epoch_id)
                self.assertIsNotNone(migrated_checkpoint)
                self.assertTrue(migrated_checkpoint.fresh_epoch_required)
                self.assertIsNone(migrated_checkpoint.transport_epoch_id)
            observer_uniques = {
                tuple(constraint["column_names"])
                for constraint in inspect(engine).get_unique_constraints(
                    "observer_outbox"
                )
            }
            self.assertNotIn(("connector_sequence",), observer_uniques)
            self.assertIn(
                ("transport_epoch_id", "connector_sequence"),
                observer_uniques,
            )
            engine.dispose()

    def test_known_version_checksum_mismatch_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "connector.sqlite3"
            engine = create_engine(f"sqlite+pysqlite:///{path}")
            SchemaMigration.__table__.create(engine)
            with Session(engine) as session, session.begin():
                session.add(
                    SchemaMigration(
                        version=1,
                        checksum="wrong",
                        applied_at="now",
                    )
                )
            engine.dispose()

            storage = SQLiteStorageComponent(path, ConnectorConfig())
            with self.assertRaises(MigrationChecksumMismatch):
                asyncio.run(storage.start())

    def test_failed_migration_rolls_back_every_statement(self) -> None:
        engine = create_engine("sqlite+pysqlite:///:memory:")

        def fail_after_table(operations: object) -> None:
            operations.create_table(  # type: ignore[attr-defined]
                "must_roll_back",
                sa.Column("id", sa.Integer(), primary_key=True),
            )
            raise RuntimeError("migration failed")

        bad_migration = Migration(
            version=1,
            checksum="bad-test",
            audit_signature=("test:rollback",),
            upgrade=fail_after_table,
        )

        with self.assertRaises(RuntimeError):
            apply_migrations(engine, (bad_migration,))

        objects = set(inspect(engine).get_table_names())
        self.assertNotIn("must_roll_back", objects)
        self.assertIn("schema_migrations", objects)
        with Session(engine) as session:
            self.assertEqual(session.scalars(select(SchemaMigration)).all(), [])
        engine.dispose()


if __name__ == "__main__":
    unittest.main()
