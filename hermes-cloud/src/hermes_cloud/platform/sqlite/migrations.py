"""Versioned SQLite DDL and migration history for the test-server profile."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import cache, lru_cache
from time import monotonic, sleep
from typing import Final
from uuid import NAMESPACE_URL, UUID, uuid5

from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.operations.ops import CreateIndexOp, CreateTableOp
from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    Engine,
    ForeignKeyConstraint,
    Integer,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    inspect,
    select,
)
from sqlalchemy.engine import Connection
from sqlalchemy.engine.reflection import Inspector
from sqlalchemy.exc import CompileError, IntegrityError, OperationalError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column
from sqlalchemy.schema import Constraint, Table

from hermes_cloud.adapters.connector_contract_v1 import (
    CloudEnvelopeV1Adapter,
    ContractConformanceError,
)
from hermes_cloud.domain.canonical_json import canonical_payload_digest
from hermes_cloud.platform.postgres.models import (
    ConnectorObserverReceiptModel,
    ConnectorTransportCursorModel,
    ConnectorTransportHandshakeOwnershipModel,
    OutboxEventModel,
)
from hermes_cloud.platform.sqlalchemy.observer_encryption import (
    ObserverEncryptionContext,
    TenantObserverCipher,
)
from hermes_cloud.platform.sqlalchemy.observer_projection_migration_models import (
    ObserverInboxV6Model,
    ObserverInboxV6Rows,
    ObserverInboxV7Model,
    ObserverInboxV8Rows,
    ObserverInboxV9Model,
)
from hermes_cloud.platform.sqlalchemy.observer_projection_models import (
    ObserverDeletionLedgerModel,
    ObserverEventModel,
    ObserverProjectionBase,
    ObserverSessionModel,
    ObserverV2StateModel,
)
from hermes_cloud.platform.sqlalchemy.observer_subscription_migration_models import (
    ObserverSubscriptionV7Base,
)
from hermes_cloud.platform.sqlalchemy.observer_subscription_models import (
    ObserverSubscriptionBase,
    ObserverSubscriptionIntentModel,
    ObserverSubscriptionTargetModel,
)
from hermes_cloud.platform.sqlalchemy.session_catalog_migration_models import (
    SessionCatalogAuthorityV12Rows,
    SessionCatalogInboxV12Rows,
    SessionCatalogV12Base,
)
from hermes_cloud.platform.sqlalchemy.session_catalog_models import (
    SessionCatalogAuthorityModel,
    SessionCatalogBase,
    SessionCatalogInboxModel,
    SessionCatalogSnapshotPageModel,
)
from hermes_cloud.platform.sqlalchemy.session_projection_migration_models import (
    SessionEventProjectionV10Rows,
    SessionMessageProjectionV10Rows,
    SessionProjectionCursorV10Rows,
    SessionProjectionV10Rows,
    SessionProjectionV11Rows,
    WebSocketTicketV10Rows,
    WebSocketTicketV11Rows,
)
from hermes_cloud.platform.sqlite.schema import (
    build_sqlite_metadata,
    build_sqlite_v10_metadata,
    build_sqlite_v12_metadata,
)

SQLITE_MIGRATION_TABLE: Final = "hermes_sqlite_schema_migrations"
SQLITE_SQL_WHITESPACE: Final = frozenset({"\t", "\n", "\f", "\r", " "})
OBSERVER_INBOX_V6_RUNTIME_GENERATION: Final = "legacy-v6"
OBSERVER_INBOX_LEGACY_RETENTION: Final = timedelta(days=30)
SESSION_CATALOG_INBOX_RETENTION: Final = timedelta(days=7)
SESSION_CATALOG_STAGING_TTL: Final = timedelta(minutes=10)
_CONCURRENT_CONVERGENCE_TIMEOUT_SECONDS: Final = 5.0
_CONCURRENT_CONVERGENCE_POLL_SECONDS: Final = 0.05


class _SQLiteMigrationBase(DeclarativeBase):
    pass


class SQLiteSchemaMigration(_SQLiteMigrationBase):
    """ORM mapping for the SQLite-only immutable migration ledger."""

    __tablename__ = SQLITE_MIGRATION_TABLE

    version: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    applied_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )


class SQLiteSchemaObject(_SQLiteMigrationBase):
    """Read-only ORM projection of SQLite's authoritative schema catalog."""

    __tablename__ = "sqlite_master"

    object_type: Mapped[str] = mapped_column("type", String, primary_key=True)
    name: Mapped[str] = mapped_column(String, primary_key=True)
    table_name: Mapped[str] = mapped_column("tbl_name", String, nullable=False)
    definition: Mapped[str | None] = mapped_column("sql", Text, nullable=True)


@dataclass(frozen=True, slots=True)
class PublishedSQLiteMigration:
    version: int
    name: str
    checksum: str
    upgrade: Callable[[Operations], None]


@dataclass(frozen=True, slots=True)
class PublishedSQLiteLegacySource:
    release: str
    schema_fingerprint: str
    raw_manifest_checksum: str
    session_identity_remainder_schema_fingerprint: str
    session_identity_remainder_raw_manifest_checksum: str


@dataclass(frozen=True, slots=True)
class PublishedSQLiteVersionedCompatibilitySource:
    release: str
    version: int
    source: str
    schema_fingerprint: str
    raw_manifest_checksum: str
    legacy_base_schema_fingerprint: str
    legacy_base_raw_manifest_checksum: str
    ledger_schema_fingerprint: str
    ledger_raw_manifest_checksum: str
    observer_schema_fingerprint: str
    observer_raw_manifest_checksum: str
    transport_schema_fingerprint: str
    transport_raw_manifest_checksum: str
    handshake_schema_fingerprint: str
    handshake_raw_manifest_checksum: str


@dataclass(frozen=True, slots=True)
class PublishedSQLiteVersionedDatabaseCompatibilitySource:
    release: str
    version: int
    source: str
    schema_fingerprint: str
    raw_manifest_checksum: str


@dataclass(frozen=True, slots=True)
class SQLiteUpgradeCoverage:
    published_versions: tuple[int, ...]
    recent_historical_versions: tuple[int, ...]
    recent_two_covered: bool


@dataclass(frozen=True, slots=True)
class SQLiteUpgradeResult:
    schema_version: int
    source: str


def _deterministic_create_table_op(
    table: Table,
) -> CreateTableOp:
    operation = CreateTableOp.from_table(table)
    constraints = sorted(
        (item for item in operation.columns if isinstance(item, Constraint)),
        key=_constraint_sort_key,
    )
    operation.columns = [
        *(item for item in operation.columns if not isinstance(item, Constraint)),
        *constraints,
    ]
    return operation


def _constraint_sort_key(constraint: Constraint) -> str:
    if not isinstance(
        constraint,
        (
            CheckConstraint,
            ForeignKeyConstraint,
            PrimaryKeyConstraint,
            UniqueConstraint,
        ),
    ):
        raise SQLiteMigrationHistoryConflict(
            "SQLite typed migration contains an unsupported constraint"
        )
    record: dict[str, object] = {
        "type": type(constraint).__name__,
        "name": constraint.name,
        "columns": tuple(column.name for column in constraint.columns),
        "dialect_options": {
            dialect: dict(options)
            for dialect, options in constraint.dialect_options.items()
        },
    }
    if isinstance(constraint, CheckConstraint):
        record["expression"] = str(constraint.sqltext)
    elif isinstance(constraint, ForeignKeyConstraint):
        record.update(
            {
                "targets": tuple(
                    element.target_fullname for element in constraint.elements
                ),
                "ondelete": constraint.ondelete,
                "onupdate": constraint.onupdate,
                "deferrable": constraint.deferrable,
                "initially": constraint.initially,
                "match": constraint.match,
                "use_alter": constraint.use_alter,
            }
        )
    return json.dumps(
        record,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    )


def _create_v1_schema(operations: Operations) -> None:
    metadata = build_sqlite_v10_metadata()
    later_tables = {
        ConnectorTransportCursorModel.__table__.name,
        ConnectorTransportHandshakeOwnershipModel.__table__.name,
        ConnectorObserverReceiptModel.__table__.name,
        *(table.name for table in SessionCatalogBase.metadata.tables.values()),
    }
    for table in metadata.sorted_tables:
        if table.name in later_tables:
            continue
        operations.invoke(_deterministic_create_table_op(table))
        for index in sorted(table.indexes, key=lambda candidate: candidate.name or ""):
            operations.invoke(CreateIndexOp.from_index(index))


def _create_v2_observer_projection(operations: Operations) -> None:
    published_tables = {
        ObserverEventModel.__table__.name,
        ObserverSessionModel.__table__.name,
    }
    for table in ObserverProjectionBase.metadata.sorted_tables:
        if table.name not in published_tables:
            continue
        operations.invoke(_deterministic_create_table_op(table))
        for index in sorted(table.indexes, key=lambda candidate: candidate.name or ""):
            operations.invoke(CreateIndexOp.from_index(index))


def _create_v3_observer_authority(operations: Operations) -> None:
    frozen_inbox = ObserverInboxV6Model.__table__
    operations.invoke(_deterministic_create_table_op(frozen_inbox))
    for index in sorted(
        frozen_inbox.indexes,
        key=lambda candidate: candidate.name or "",
    ):
        operations.invoke(CreateIndexOp.from_index(index))

    projection_tables = {
        ObserverDeletionLedgerModel.__table__.name,
    }
    subscription_tables = frozenset(ObserverSubscriptionV7Base.metadata.tables)
    for metadata, published_tables in (
        (ObserverProjectionBase.metadata, projection_tables),
        (ObserverSubscriptionV7Base.metadata, subscription_tables),
    ):
        for table in metadata.sorted_tables:
            if table.name not in published_tables:
                continue
            operations.invoke(_deterministic_create_table_op(table))
            for index in sorted(
                table.indexes,
                key=lambda candidate: candidate.name or "",
            ):
                operations.invoke(CreateIndexOp.from_index(index))


def _create_v4_connector_transport_cursor(operations: Operations) -> None:
    table = build_sqlite_metadata().tables[ConnectorTransportCursorModel.__table__.name]
    operations.invoke(_deterministic_create_table_op(table))
    for index in sorted(table.indexes, key=lambda candidate: candidate.name or ""):
        operations.invoke(CreateIndexOp.from_index(index))


def _create_v5_connector_handshake_ownership(operations: Operations) -> None:
    metadata = build_sqlite_metadata()
    for model in (
        ConnectorTransportHandshakeOwnershipModel,
        ConnectorObserverReceiptModel,
    ):
        table = metadata.tables[model.__table__.name]
        operations.invoke(_deterministic_create_table_op(table))
        for index in sorted(
            table.indexes,
            key=lambda candidate: candidate.name or "",
        ):
            operations.invoke(CreateIndexOp.from_index(index))


def _create_v6_observer_output_parity(operations: Operations) -> None:
    table = ObserverV2StateModel.__table__
    operations.invoke(_deterministic_create_table_op(table))
    for index in sorted(table.indexes, key=lambda candidate: candidate.name or ""):
        operations.invoke(CreateIndexOp.from_index(index))


def _create_v7_observer_inbox_runtime_epoch(operations: Operations) -> None:
    """Scope connector sequence idempotency to one runtime generation."""

    operations.drop_index(
        "observer_inbox_received_idx",
        table_name=ObserverInboxV6Model.__table__.name,
    )
    operations.rename_table(
        ObserverInboxV6Model.__table__.name,
        ObserverInboxV6Rows.__table__.name,
    )

    with Session(bind=operations.get_bind()) as session:
        legacy_rows = tuple(session.scalars(select(ObserverInboxV6Rows)).all())

    table = ObserverInboxV7Model.__table__
    operations.invoke(_deterministic_create_table_op(table))
    for index in sorted(table.indexes, key=lambda candidate: candidate.name or ""):
        operations.invoke(CreateIndexOp.from_index(index))

    if legacy_rows:
        operations.bulk_insert(
            table,
            [
                {
                    "tenant_id": row.tenant_id,
                    "message_id": row.message_id,
                    "workspace_id": row.workspace_id,
                    "agent_id": row.agent_id,
                    "device_id": row.device_id,
                    "connector_instance_id": row.connector_instance_id,
                    "runtime_generation": OBSERVER_INBOX_V6_RUNTIME_GENERATION,
                    "connector_sequence": row.connector_sequence,
                    "message_type": row.message_type,
                    "payload_digest": row.payload_digest,
                    "binding_digest": row.binding_digest,
                    "received_at": row.received_at,
                }
                for row in legacy_rows
            ],
        )
    operations.drop_table(ObserverInboxV6Rows.__table__.name)


def _create_v8_observer_subscription_wire_contract(operations: Operations) -> None:
    """Bind every dispatched subscription identity to one exact wire contract."""

    table_name = ObserverSubscriptionIntentModel.__table__.name
    operations.add_column(
        table_name,
        Column("observer_contract", Integer(), nullable=True),
    )
    operations.add_column(
        table_name,
        Column("wire_message_type", String(35), nullable=True),
    )
    operations.add_column(
        table_name,
        Column("wire_payload_digest", String(64), nullable=True),
    )
    with Session(bind=operations.get_bind()) as session, session.begin():
        legacy_intents = tuple(
            session.scalars(select(ObserverSubscriptionIntentModel)).all()
        )
        unsafe_by_target: dict[
            tuple[UUID, UUID], list[ObserverSubscriptionIntentModel]
        ] = {}
        safe_pending: set[tuple[UUID, UUID, str]] = set()
        for intent in legacy_intents:
            target_key = (
                UUID(str(intent.tenant_id)),
                UUID(str(intent.target_subscription_id)),
            )
            if _legacy_intent_was_never_dispatched(intent):
                safe_pending.add((*target_key, str(intent.message_type)))
                continue
            if intent.state == "cancelled":
                continue
            unsafe_by_target.setdefault(target_key, []).append(intent)
            intent.state = "cancelled"
            old_outbox = session.get(
                OutboxEventModel,
                (intent.tenant_id, intent.request_id),
            )
            if old_outbox is None:
                raise SQLiteMigrationHistoryConflict(
                    "SQLite legacy Observer intent outbox is unavailable"
                )
            old_outbox.state = "dead"
            old_outbox.available_at = intent.updated_at

        for target_key, unsafe_intents in unsafe_by_target.items():
            target = session.get(ObserverSubscriptionTargetModel, target_key)
            if target is None:
                raise SQLiteMigrationHistoryConflict(
                    "SQLite legacy Observer intent target is unavailable"
                )
            desired_type = _legacy_target_recovery_type(target)
            if desired_type is None or (*target_key, desired_type) in safe_pending:
                continue
            superseded_candidates = tuple(
                intent
                for intent in unsafe_intents
                if intent.message_type == desired_type
            ) or tuple(unsafe_intents)
            superseded = max(
                superseded_candidates,
                key=lambda candidate: (
                    int(candidate.intent_sequence),
                    str(candidate.request_id),
                ),
            )
            _append_observer_recovery_intent(
                session,
                target=target,
                message_type=desired_type,
                supersedes_request_id=UUID(str(superseded.request_id)),
                migration_version=8,
            )


def _legacy_intent_was_never_dispatched(
    intent: ObserverSubscriptionIntentModel,
) -> bool:
    return (
        intent.state == "pending"
        and intent.dispatch_connection_id is None
        and intent.dispatch_sequence is None
        and intent.dispatch_attempts == 0
        and intent.dispatched_at is None
        and intent.settled_at is None
    )


def _legacy_target_recovery_type(
    target: ObserverSubscriptionTargetModel,
) -> str | None:
    if target.state == "active" and target.active_ref_count > 0:
        return "session.observe.open"
    if target.state == "closing" and target.active_ref_count == 0:
        return "session.observe.close"
    if target.state == "closed" and target.active_ref_count == 0:
        return None
    raise SQLiteMigrationHistoryConflict(
        "SQLite legacy Observer target state is inconsistent"
    )


def _append_observer_recovery_intent(
    session: Session,
    *,
    target: ObserverSubscriptionTargetModel,
    message_type: str,
    supersedes_request_id: UUID,
    migration_version: int,
) -> None:
    intent_sequence = int(target.next_intent_sequence)
    request_id = uuid5(
        NAMESPACE_URL,
        (
            f"https://hermes.local/sqlite/v{migration_version}/observer-intent/"
            f"{target.tenant_id}/{target.target_subscription_id}/"
            f"{intent_sequence}/{message_type}"
        ),
    )
    if session.get(ObserverSubscriptionIntentModel, (target.tenant_id, request_id)):
        raise SQLiteMigrationHistoryConflict(
            "SQLite Observer recovery intent identity conflicts"
        )
    when = target.updated_at
    payload: dict[str, object] = {
        "request_id": str(request_id),
        "subscription_id": str(target.target_subscription_id),
        "profile": target.profile,
        "session_key": target.session_key,
        "target_source": "cloud_authorized_binding",
    }
    if message_type == "session.observe.open":
        payload["requested_at"] = _utc_timestamp(when)
    else:
        payload["reason"] = "reconciliation"
        payload["closed_at"] = _utc_timestamp(when)
    target.next_intent_sequence = intent_sequence + 1
    session.add(
        ObserverSubscriptionIntentModel(
            tenant_id=target.tenant_id,
            request_id=request_id,
            supersedes_request_id=supersedes_request_id,
            target_subscription_id=target.target_subscription_id,
            intent_sequence=intent_sequence,
            workspace_id=target.workspace_id,
            agent_id=target.agent_id,
            device_id=target.device_id,
            message_type=message_type,
            payload=payload,
            state="pending",
            dispatch_connection_id=None,
            dispatch_sequence=None,
            dispatch_attempts=0,
            dispatched_at=None,
            settled_at=None,
            created_at=when,
            updated_at=when,
            observer_contract=None,
            wire_message_type=None,
            wire_payload_digest=None,
        )
    )
    session.add(
        OutboxEventModel(
            tenant_id=target.tenant_id,
            event_id=request_id,
            workspace_id=target.workspace_id,
            aggregate_type="observer_subscription",
            aggregate_id=target.target_subscription_id,
            event_type=message_type,
            payload={
                "request_id": str(request_id),
                "subscription_id": str(target.target_subscription_id),
                "agent_id": str(target.agent_id),
                "device_id": str(target.device_id),
                "profile": target.profile,
                "session_key": target.session_key,
            },
            state="pending",
            publish_attempts=0,
            available_at=when,
            published_at=None,
            created_at=when,
        )
    )


def _utc_timestamp(value: datetime) -> str:
    if value.utcoffset() is None:
        raise SQLiteMigrationHistoryConflict(
            "SQLite legacy Observer timestamp is invalid"
        )
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _create_v9_observer_inbox_retention(operations: Operations) -> None:
    """Bound Observer transport idempotency rows to an explicit retention window."""

    operations.drop_index(
        "observer_inbox_received_idx",
        table_name=ObserverInboxV7Model.__table__.name,
    )
    operations.rename_table(
        ObserverInboxV7Model.__table__.name,
        ObserverInboxV8Rows.__table__.name,
    )

    with Session(bind=operations.get_bind()) as session:
        legacy_rows = tuple(session.scalars(select(ObserverInboxV8Rows)).all())

    table = ObserverInboxV9Model.__table__
    operations.invoke(_deterministic_create_table_op(table))
    for index in sorted(table.indexes, key=lambda candidate: candidate.name or ""):
        operations.invoke(CreateIndexOp.from_index(index))

    if legacy_rows:
        operations.bulk_insert(
            table,
            [
                {
                    "tenant_id": row.tenant_id,
                    "message_id": row.message_id,
                    "workspace_id": row.workspace_id,
                    "agent_id": row.agent_id,
                    "device_id": row.device_id,
                    "connector_instance_id": row.connector_instance_id,
                    "runtime_generation": row.runtime_generation,
                    "connector_sequence": row.connector_sequence,
                    "message_type": row.message_type,
                    "payload_digest": row.payload_digest,
                    "binding_digest": row.binding_digest,
                    "received_at": row.received_at,
                    "retention_until": (
                        row.received_at + OBSERVER_INBOX_LEGACY_RETENTION
                    ),
                }
                for row in legacy_rows
            ],
        )
    operations.drop_table(ObserverInboxV8Rows.__table__.name)


def _create_v10_observer_subscription_legacy_wire_repair(
    operations: Operations,
) -> None:
    """Repair identities guessed by the originally published SQLite v8 data step."""

    bind = operations.get_bind()
    if not inspect(bind).has_table(SQLITE_MIGRATION_TABLE):
        return
    with Session(bind=bind) as session, session.begin():
        v8_ledger = session.get(SQLiteSchemaMigration, 8)
        if v8_ledger is None:
            return
        v8_applied_at = _normalized_utc_datetime(v8_ledger.applied_at)
        intents = tuple(session.scalars(select(ObserverSubscriptionIntentModel)).all())
        unsafe_by_target: dict[
            tuple[UUID, UUID], list[ObserverSubscriptionIntentModel]
        ] = {}
        safe_by_target: set[tuple[UUID, UUID, str]] = set()
        for intent in intents:
            target_key = (
                UUID(str(intent.tenant_id)),
                UUID(str(intent.target_subscription_id)),
            )
            intent_key = (*target_key, str(intent.message_type))
            if not _matches_original_v8_guess(intent):
                if _is_usable_subscription_intent(intent):
                    safe_by_target.add(intent_key)
                continue
            outbox = session.get(
                OutboxEventModel,
                (intent.tenant_id, intent.request_id),
            )
            if _legacy_intent_was_never_dispatched(intent):
                _clear_wire_binding(intent)
                safe_by_target.add(intent_key)
                continue
            if intent.state == "cancelled":
                _clear_wire_binding(intent)
                if outbox is not None:
                    outbox.state = "dead"
                    outbox.available_at = intent.updated_at
                continue
            if _proves_first_v1_dispatch_after_v8(
                intent,
                outbox=outbox,
                v8_applied_at=v8_applied_at,
            ):
                safe_by_target.add(intent_key)
                continue
            if outbox is None:
                raise SQLiteMigrationHistoryConflict(
                    "SQLite legacy Observer intent outbox is unavailable"
                )
            intent.state = "cancelled"
            _clear_wire_binding(intent)
            outbox.state = "dead"
            outbox.available_at = intent.updated_at
            unsafe_by_target.setdefault(target_key, []).append(intent)

        for target_key, unsafe_intents in unsafe_by_target.items():
            target = session.get(ObserverSubscriptionTargetModel, target_key)
            if target is None:
                raise SQLiteMigrationHistoryConflict(
                    "SQLite legacy Observer intent target is unavailable"
                )
            desired_type = _legacy_target_recovery_type(target)
            if desired_type is None or (*target_key, desired_type) in safe_by_target:
                continue
            superseded_candidates = tuple(
                intent
                for intent in unsafe_intents
                if intent.message_type == desired_type
            ) or tuple(unsafe_intents)
            superseded = max(
                superseded_candidates,
                key=lambda candidate: (
                    int(candidate.intent_sequence),
                    str(candidate.request_id),
                ),
            )
            _append_observer_recovery_intent(
                session,
                target=target,
                message_type=desired_type,
                supersedes_request_id=UUID(str(superseded.request_id)),
                migration_version=10,
            )


_SESSION_PROJECTION_V10_TABLES: Final = (
    "session_messages",
    "session_events",
    "session_cursors",
    "websocket_tickets",
    "sessions",
)
_SESSION_PROJECTION_V10_INDEXES: Final = (
    ("session_messages", "session_messages_sequence_idx"),
    ("session_messages", "session_messages_retention_idx"),
    ("session_events", "session_events_sequence_idx"),
    ("session_events", "session_events_retention_idx"),
    ("session_cursors", "session_cursors_updated_idx"),
    ("websocket_tickets", "websocket_tickets_consume_idx"),
    ("websocket_tickets", "websocket_tickets_retention_idx"),
    ("sessions", "session_projection_acl_idx"),
    ("sessions", "session_projection_retention_idx"),
)


def _create_v11_session_projection_durable_identity(
    operations: Operations,
) -> None:
    """Rebuild session identity and ticket scope without guessing provenance."""

    for table_name, index_name in _SESSION_PROJECTION_V10_INDEXES:
        operations.drop_index(index_name, table_name=table_name)
    for table_name in _SESSION_PROJECTION_V10_TABLES:
        operations.rename_table(table_name, f"{table_name}_v10")

    bind = operations.get_bind()
    with Session(bind=bind) as session:
        legacy_sessions = tuple(
            session.scalars(select(SessionProjectionV10Rows)).all()
        )
        legacy_messages = tuple(
            session.scalars(select(SessionMessageProjectionV10Rows)).all()
        )
        legacy_events = tuple(
            session.scalars(select(SessionEventProjectionV10Rows)).all()
        )
        legacy_cursors = tuple(
            session.scalars(select(SessionProjectionCursorV10Rows)).all()
        )
        legacy_tickets = tuple(
            session.scalars(select(WebSocketTicketV10Rows)).all()
        )
        session_records, identity_by_legacy_key = _v11_session_records(
            session,
            legacy_sessions,
        )
        ticket_records = _v11_ticket_records(
            legacy_tickets,
            identity_by_legacy_key=identity_by_legacy_key,
        )

    current_metadata = build_sqlite_metadata()
    current_table_names = frozenset(
        {
            "sessions",
            "session_messages",
            "session_events",
            "session_cursors",
            "websocket_tickets",
        }
    )
    for table in current_metadata.sorted_tables:
        if table.name not in current_table_names:
            continue
        operations.invoke(_deterministic_create_table_op(table))
        for index in sorted(table.indexes, key=lambda candidate: candidate.name or ""):
            operations.invoke(CreateIndexOp.from_index(index))

    records_by_table: dict[str, list[dict[str, object]]] = {
        "sessions": session_records,
        "session_messages": [
            _record_from_v10_child(row) for row in legacy_messages
        ],
        "session_events": [_record_from_v10_child(row) for row in legacy_events],
        "session_cursors": [_record_from_v10_child(row) for row in legacy_cursors],
        "websocket_tickets": ticket_records,
    }
    for table in current_metadata.sorted_tables:
        records = records_by_table.get(table.name)
        if records:
            operations.bulk_insert(table, records)

    for table_name in _SESSION_PROJECTION_V10_TABLES:
        operations.drop_table(f"{table_name}_v10")


def _create_v12_session_catalog(operations: Operations) -> None:
    """Create the isolated authoritative Session Catalog v1 tables."""

    current_metadata = build_sqlite_v12_metadata()
    current_table_names = frozenset(
        table.name for table in SessionCatalogV12Base.metadata.sorted_tables
    )
    for table in current_metadata.sorted_tables:
        if table.name not in current_table_names:
            continue
        operations.invoke(_deterministic_create_table_op(table))
        for index in sorted(table.indexes, key=lambda candidate: candidate.name or ""):
            operations.invoke(CreateIndexOp.from_index(index))


def _create_v13_session_catalog_recovery(operations: Operations) -> None:
    """Add bounded recovery and replay retention without rewriting v12."""

    current_metadata = build_sqlite_metadata()

    authority_table = SessionCatalogAuthorityModel.__table__.name
    operations.drop_index(
        "session_catalog_authority_writer_idx",
        table_name=authority_table,
    )
    operations.rename_table(
        authority_table,
        SessionCatalogAuthorityV12Rows.__table__.name,
    )
    with Session(bind=operations.get_bind()) as session:
        authority_rows = tuple(
            session.scalars(select(SessionCatalogAuthorityV12Rows)).all()
        )
    current_authority = current_metadata.tables[authority_table]
    operations.invoke(_deterministic_create_table_op(current_authority))
    for index in sorted(
        current_authority.indexes,
        key=lambda candidate: candidate.name or "",
    ):
        operations.invoke(CreateIndexOp.from_index(index))
    if authority_rows:
        operations.bulk_insert(
            current_authority,
            [
                {
                    "tenant_id": row.tenant_id,
                    "agent_id": row.agent_id,
                    "profile": row.profile,
                    "workspace_id": row.workspace_id,
                    "writer_id": row.writer_id,
                    "writer_fence": row.writer_fence,
                    "runtime_generation": row.runtime_generation,
                    "catalog_revision": row.catalog_revision,
                    "catalog_sequence": row.catalog_sequence,
                    "staging_snapshot_id": row.staging_snapshot_id,
                    "staging_runtime_generation": row.staging_runtime_generation,
                    "staging_catalog_revision": row.staging_catalog_revision,
                    "staging_deadline": (
                        row.updated_at + SESSION_CATALOG_STAGING_TTL
                        if row.staging_snapshot_id is not None
                        else None
                    ),
                    "require_full_snapshot": False,
                    "expected_page_index": row.expected_page_index,
                    "updated_at": row.updated_at,
                }
                for row in authority_rows
            ],
        )
    operations.drop_table(SessionCatalogAuthorityV12Rows.__table__.name)

    inbox_table = SessionCatalogInboxModel.__table__.name
    operations.drop_index(
        "session_catalog_inbox_received_idx",
        table_name=inbox_table,
    )
    operations.rename_table(
        inbox_table,
        SessionCatalogInboxV12Rows.__table__.name,
    )
    with Session(bind=operations.get_bind()) as session:
        inbox_rows = tuple(session.scalars(select(SessionCatalogInboxV12Rows)).all())
    current_inbox = current_metadata.tables[inbox_table]
    operations.invoke(_deterministic_create_table_op(current_inbox))
    for index in sorted(
        current_inbox.indexes,
        key=lambda candidate: candidate.name or "",
    ):
        operations.invoke(CreateIndexOp.from_index(index))
    if inbox_rows:
        operations.bulk_insert(
            current_inbox,
            [
                {
                    "tenant_id": row.tenant_id,
                    "message_id": row.message_id,
                    "workspace_id": row.workspace_id,
                    "agent_id": row.agent_id,
                    "device_id": row.device_id,
                    "connector_instance_id": row.connector_instance_id,
                    "runtime_generation": row.runtime_generation,
                    "connector_sequence": row.connector_sequence,
                    "message_type": row.message_type,
                    "payload_digest": row.payload_digest,
                    "receipt_type": row.receipt_type,
                    "receipt_payload": row.receipt_payload,
                    "receipt_state": (
                        "settled" if row.receipt_type is not None else None
                    ),
                    "dispatch_connection_id": None,
                    "dispatch_message_id": None,
                    "dispatch_sequence": None,
                    "dispatch_attempts": 0,
                    "received_at": row.received_at,
                    "updated_at": row.received_at,
                    "receipt_sent_at": (
                        row.received_at if row.receipt_type is not None else None
                    ),
                    "receipt_settled_at": (
                        row.received_at if row.receipt_type is not None else None
                    ),
                    "receipt_retired_at": None,
                    "receipt_retirement_reason": None,
                    "retention_until": (
                        row.received_at + SESSION_CATALOG_INBOX_RETENTION
                    ),
                }
                for row in inbox_rows
            ],
        )
    operations.drop_table(SessionCatalogInboxV12Rows.__table__.name)

    snapshot_table = current_metadata.tables[
        SessionCatalogSnapshotPageModel.__table__.name
    ]
    retention_index = next(
        index
        for index in snapshot_table.indexes
        if index.name == "session_catalog_snapshot_page_retention_idx"
    )
    operations.invoke(CreateIndexOp.from_index(retention_index))


def _v11_session_records(
    session: Session,
    rows: tuple[SessionProjectionV10Rows, ...],
) -> tuple[list[dict[str, object]], dict[tuple[UUID, str], UUID]]:
    records: list[dict[str, object]] = []
    identity_by_legacy_key: dict[tuple[UUID, str], UUID] = {}
    for row in rows:
        tenant_id = UUID(str(row.tenant_id))
        session_id = UUID(str(row.session_id))
        legacy_key = (tenant_id, str(row.session_key))
        if legacy_key in identity_by_legacy_key:
            raise SQLiteMigrationHistoryConflict(
                "SQLite legacy session identity is ambiguous"
            )
        profile = _v11_profile_evidence(session, row)
        identity_by_legacy_key[legacy_key] = session_id
        records.append(
            {
                "tenant_id": tenant_id,
                "session_id": session_id,
                "session_key": row.session_key,
                "workspace_id": UUID(str(row.workspace_id)),
                "agent_id": (
                    UUID(str(row.agent_id)) if row.agent_id is not None else None
                ),
                "profile": profile,
                "title": row.title,
                "state": row.state,
                "revision": row.revision,
                "lineage_tip_message_id": (
                    UUID(str(row.lineage_tip_message_id))
                    if row.lineage_tip_message_id is not None
                    else None
                ),
                "lineage_tip_sequence": row.lineage_tip_sequence,
                "started_at": row.started_at,
                "updated_at": row.updated_at,
                "closed_at": row.closed_at,
                "retention_until": row.retention_until,
            }
        )
    return records, identity_by_legacy_key


def _v11_profile_evidence(
    session: Session,
    row: SessionProjectionV10Rows,
) -> str:
    if row.agent_id is None:
        raise SQLiteMigrationHistoryConflict(
            "SQLite legacy session has no authoritative Agent identity"
        )
    profiles = set(
        session.scalars(
            select(ObserverSessionModel.profile)
            .where(
                ObserverSessionModel.tenant_id == row.tenant_id,
                ObserverSessionModel.agent_id == row.agent_id,
                ObserverSessionModel.session_key == row.session_key,
            )
            .distinct()
            .limit(2)
        ).all()
    )
    profiles.update(
        session.scalars(
            select(ObserverSubscriptionTargetModel.profile)
            .where(
                ObserverSubscriptionTargetModel.tenant_id == row.tenant_id,
                ObserverSubscriptionTargetModel.agent_id == row.agent_id,
                ObserverSubscriptionTargetModel.session_key == row.session_key,
            )
            .distinct()
            .limit(2)
        ).all()
    )
    if len(profiles) != 1:
        raise SQLiteMigrationHistoryConflict(
            "SQLite legacy session profile evidence is unavailable or ambiguous"
        )
    profile = str(next(iter(profiles)))
    if not 1 <= len(profile) <= 128 or profile != profile.strip():
        raise SQLiteMigrationHistoryConflict(
            "SQLite legacy session profile evidence is invalid"
        )
    return profile


def _v11_ticket_records(
    rows: tuple[WebSocketTicketV10Rows, ...],
    *,
    identity_by_legacy_key: dict[tuple[UUID, str], UUID],
) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for row in rows:
        tenant_id = UUID(str(row.tenant_id))
        session_id = None
        if row.session_key is not None:
            session_id = identity_by_legacy_key.get(
                (tenant_id, str(row.session_key))
            )
            if session_id is None:
                raise SQLiteMigrationHistoryConflict(
                    "SQLite legacy control ticket session is unavailable"
                )
        records.append(
            {
                "tenant_id": tenant_id,
                "ticket_id": UUID(str(row.ticket_id)),
                "ticket_digest": row.ticket_digest,
                "principal_type": row.principal_type,
                "principal_id": UUID(str(row.principal_id)),
                "refresh_session_id": UUID(str(row.refresh_session_id)),
                "session_id": session_id,
                "observer_scope": row.observer_scope,
                "issued_at": row.issued_at,
                "expires_at": row.expires_at,
                "consumed_at": row.consumed_at,
                "retention_until": row.retention_until,
            }
        )
    return records


def _record_from_v10_child(row: object) -> dict[str, object]:
    return {
        column.name: getattr(row, column.name)
        for column in row.__table__.columns  # type: ignore[attr-defined]
    }


def _matches_original_v8_guess(
    intent: ObserverSubscriptionIntentModel,
) -> bool:
    return (
        intent.observer_contract == 1
        and intent.wire_message_type == intent.message_type
        and intent.wire_payload_digest == canonical_payload_digest(intent.payload)
    )


def _is_usable_subscription_intent(
    intent: ObserverSubscriptionIntentModel,
) -> bool:
    if intent.state == "cancelled":
        return False
    if _legacy_intent_was_never_dispatched(intent):
        return (
            intent.observer_contract,
            intent.wire_message_type,
            intent.wire_payload_digest,
        ) == (None, None, None)
    if intent.state not in {"dispatching", "settled"}:
        return False
    if intent.observer_contract not in {1, 2}:
        return False
    expected_type = (
        f"{intent.message_type}.v2"
        if intent.observer_contract == 2
        else intent.message_type
    )
    expected_payload = {
        **dict(intent.payload),
        **({"observer_contract": 2} if intent.observer_contract == 2 else {}),
    }
    return (
        intent.wire_message_type == expected_type
        and intent.wire_payload_digest == canonical_payload_digest(expected_payload)
    )


def _proves_first_v1_dispatch_after_v8(
    intent: ObserverSubscriptionIntentModel,
    *,
    outbox: OutboxEventModel | None,
    v8_applied_at: datetime,
) -> bool:
    if (
        intent.state not in {"dispatching", "settled"}
        or intent.dispatch_attempts != 1
        or intent.dispatch_connection_id is None
        or intent.dispatch_sequence is None
        or intent.dispatched_at is None
        or outbox is None
        or outbox.publish_attempts != 1
    ):
        return False
    created_at = _normalized_utc_datetime(intent.created_at)
    dispatched_at = _normalized_utc_datetime(intent.dispatched_at)
    if (
        created_at <= v8_applied_at
        or dispatched_at < created_at
        or _normalized_utc_datetime(outbox.created_at) != created_at
        or _normalized_utc_datetime(outbox.available_at) != dispatched_at
    ):
        return False
    if intent.state == "dispatching":
        return (
            intent.settled_at is None
            and outbox.state == "publishing"
            and outbox.published_at is None
        )
    return (
        intent.settled_at is not None
        and _normalized_utc_datetime(intent.settled_at) >= dispatched_at
        and outbox.state == "published"
        and outbox.published_at is not None
        and _normalized_utc_datetime(outbox.published_at)
        == _normalized_utc_datetime(intent.settled_at)
    )


def _clear_wire_binding(intent: ObserverSubscriptionIntentModel) -> None:
    intent.observer_contract = None
    intent.wire_message_type = None
    intent.wire_payload_digest = None


def _normalized_utc_datetime(value: datetime) -> datetime:
    if value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


PUBLISHED_SQLITE_MIGRATIONS: Final = (
    PublishedSQLiteMigration(
        version=1,
        name="0001_current_sqlite_baseline",
        checksum="37ecb4d593935f36145e4ad47f96e72ded8df1b4388cccb95fa864e17b738cf4",
        upgrade=_create_v1_schema,
    ),
    PublishedSQLiteMigration(
        version=2,
        name="0002_observer_projection",
        checksum="af29e4578d3a4292d7a42005cf22302f17ed527664d7f797a673739131e5af3b",
        upgrade=_create_v2_observer_projection,
    ),
    PublishedSQLiteMigration(
        version=3,
        name="0003_observer_authority_and_encryption",
        checksum="390849f9f1de943bcbe612fc465d043609f0a95e8d9e436f61ebabbd1bfbf255",
        upgrade=_create_v3_observer_authority,
    ),
    PublishedSQLiteMigration(
        version=4,
        name="0004_connector_transport_cursor",
        checksum="78613cdbf54520c55c30ab4f0d7e1c09722b14bbcf4577358ce738905c29d02d",
        upgrade=_create_v4_connector_transport_cursor,
    ),
    PublishedSQLiteMigration(
        version=5,
        name="0005_connector_handshake_ownership",
        checksum="4568039d452469ed3b60c7dd525366770af22cbde8a607ab4c909c42605d22ad",
        upgrade=_create_v5_connector_handshake_ownership,
    ),
    PublishedSQLiteMigration(
        version=6,
        name="0006_observer_output_parity",
        checksum="de8fcd85247ac676de822658a5633bd8a08b4115a32dceb369a9685afc2089cf",
        upgrade=_create_v6_observer_output_parity,
    ),
    PublishedSQLiteMigration(
        version=7,
        name="0007_observer_inbox_runtime_epoch",
        checksum="82087977133534a69e563dc4430cb4788bf6cd8136d379842ec6067a2b86e764",
        upgrade=_create_v7_observer_inbox_runtime_epoch,
    ),
    PublishedSQLiteMigration(
        version=8,
        name="0008_observer_subscription_wire_contract",
        checksum="294e5303bc88fded09ed5f5a0f510de7fdd1d187c4f8e2d6085dee4344fd458d",
        upgrade=_create_v8_observer_subscription_wire_contract,
    ),
    PublishedSQLiteMigration(
        version=9,
        name="0009_observer_inbox_retention",
        checksum="d57b2cf6017e6f9733722897ce82783f8630ac23248a5ad5366e9c914c22ba15",
        upgrade=_create_v9_observer_inbox_retention,
    ),
    PublishedSQLiteMigration(
        version=10,
        name="0010_observer_subscription_legacy_wire_repair",
        checksum="d57b2cf6017e6f9733722897ce82783f8630ac23248a5ad5366e9c914c22ba15",
        upgrade=_create_v10_observer_subscription_legacy_wire_repair,
    ),
    PublishedSQLiteMigration(
        version=11,
        name="0011_session_projection_durable_identity",
        checksum="61bce41e2da426e5c36c8d7b4587d3ac0d45bd0bde1731358187c42bed040e22",
        upgrade=_create_v11_session_projection_durable_identity,
    ),
    PublishedSQLiteMigration(
        version=12,
        name="0012_session_catalog_v1",
        checksum="d1f732311486fa6e438b7cd46fa3478b92aeb3b2bd21e69aa1a1aedbff21e050",
        upgrade=_create_v12_session_catalog,
    ),
    PublishedSQLiteMigration(
        version=13,
        name="0013_session_catalog_recovery",
        checksum="f4928c39728e333e37cc88fae7e82748abce55286156f538c5af3bc1b5c34cfd",
        upgrade=_create_v13_session_catalog_recovery,
    ),
)
# These signatures are release evidence, not values to recompute from the
# executable migration functions.  Keeping both the canonical manifest and the
# exact sqlite_master manifest prevents an edited historical upgrade from
# redefining its own expected schema during validation.
PUBLISHED_SQLITE_VERSIONED_DATABASE_SIGNATURES: Final[
    tuple[tuple[int, str, str], ...]
] = (
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
PUBLISHED_SQLITE_CURRENT_SESSION_IDENTITY_SIGNATURE: Final[tuple[str, str]] = (
    "363985e16c9dba16d0cd328039a3034968d281762bef5f2066fe256c6fbf7146",
    "29220c8b67daa35a12d0c4f16299f12688d6e7c748b6796cfea6a8bdbca53018",
)
PUBLISHED_SQLITE_CURRENT_OBSERVER_SIGNATURE: Final[tuple[str, str]] = (
    "71f149ed1dfe729ad77f0e4eacd55a69120ea7ae385ec39d37bed64dffaa9b09",
    "d2f3683da3d5a00d1f04aa1df495024e9c8502e6939c150781f0e1978f82808f",
)
PUBLISHED_SQLITE_CONNECTOR_TRANSPORT_SIGNATURE: Final[tuple[str, str]] = (
    "f5b029f741077ccaff7a7c5a2c040b18537b623de5f6294bf8f1b07f938bbda1",
    "4aeeafd605c7023a55d7df8e1971dc491642907724eb50360782835c7d66fe77",
)
PUBLISHED_SQLITE_CONNECTOR_HANDSHAKE_SIGNATURE: Final[tuple[str, str]] = (
    "bb55ec1899fcb8d7d93c8f1a486e115603ad4c4a34c35da259b6da904a6575d3",
    "6899eb241728655d8a48fe2d1948fdda312fbb7fb3eab8f2b873408701dc90f3",
)
PUBLISHED_SQLITE_SESSION_CATALOG_SIGNATURE: Final[tuple[str, str]] = (
    "b4e13440f9c0cba3ed999e588c7f065f8667d22346f5190087659b9cbd363184",
    "4233877360d79737f7d70ce44ec0a32924ede3de2a4358066d03207687bdf499",
)
PUBLISHED_SQLITE_LEGACY_SOURCES: Final = (
    PublishedSQLiteLegacySource(
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
PUBLISHED_SQLITE_V1_LEDGER_SIGNATURE: Final[tuple[str, str]] = (
    "ead3fa7217c8da81251761ff11a28d7faac48924581a965c49ef3adc57c92491",
    "4429c9c258d7dda582c3f2ccf2a5c27cc109ec614df31e2b0da9ebb5c8264501",
)
PUBLISHED_SQLITE_DEPLOYED_LEGACY_BASE_SIGNATURE: Final[tuple[str, str]] = (
    "43616337e19b7a4bbb70f2d2887d68dd8222d329f891f02553c40d84523ca89e",
    "cda09c49697e5bc711cbb536ea5505c61f822bd9f7e24ab0cda4685a5ab5f656",
)
PUBLISHED_SQLITE_VERSIONED_COMPATIBILITY_SOURCES: Final = (
    PublishedSQLiteVersionedCompatibilitySource(
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
PUBLISHED_SQLITE_VERSIONED_DATABASE_COMPATIBILITY_SOURCES: Final = (
    PublishedSQLiteVersionedDatabaseCompatibilitySource(
        release="20260801T131728Z",
        version=10,
        source="versioned-10",
        schema_fingerprint=(
            "f43658517c47ec0336e7e061ec4ee04aa976f3ee9d91b659a8c35720bb3944be"
        ),
        raw_manifest_checksum=(
            "df2b0f97389e0844c7e0f665b2d4a3caf52b460d4b94551a74bd34ccebd54820"
        ),
    ),
)
CURRENT_SQLITE_SCHEMA_VERSION: Final = PUBLISHED_SQLITE_MIGRATIONS[-1].version
# A source version is added only after a real immutable fixture has passed its
# upgrade-to-current plus ORM read/write gate. Catalog length alone is not proof.
VERIFIED_SQLITE_UPGRADE_SOURCE_VERSIONS: Final[tuple[int, ...]] = (
    1,
    2,
    3,
    4,
    5,
    6,
    7,
    8,
    9,
    10,
    11,
    12,
)


class SQLiteMigrationHistoryConflict(RuntimeError):
    """Raised when a database cannot be proven to match published history."""


def sqlite_upgrade_coverage() -> SQLiteUpgradeCoverage:
    """Describe only upgrade sources backed by immutable SQLite revisions."""

    published_versions = tuple(
        migration.version for migration in PUBLISHED_SQLITE_MIGRATIONS
    )
    recent_historical_versions = VERIFIED_SQLITE_UPGRADE_SOURCE_VERSIONS[-2:]
    expected_recent_versions = published_versions[:-1][-2:]
    return SQLiteUpgradeCoverage(
        published_versions=published_versions,
        recent_historical_versions=recent_historical_versions,
        recent_two_covered=(
            len(recent_historical_versions) == 2
            and recent_historical_versions == expected_recent_versions
        ),
    )


def _stable_value(value: object) -> object:
    return json.loads(json.dumps(value, default=str, sort_keys=True))


def _normalized_sql(value: object) -> str | None:
    if value is None:
        return None
    sql = str(value)
    result: list[str] = []
    quote_end: str | None = None
    pending_space = False
    index = 0
    while index < len(sql):
        character = sql[index]
        if quote_end is not None:
            result.append(character)
            if character == quote_end:
                if (
                    quote_end != "]"
                    and index + 1 < len(sql)
                    and sql[index + 1] == quote_end
                ):
                    result.append(sql[index + 1])
                    index += 2
                    continue
                quote_end = None
            index += 1
            continue
        if character in {"'", '"', "`"}:
            if pending_space and result:
                result.append(" ")
            pending_space = False
            quote_end = character
            result.append(character)
        elif character == "[":
            if pending_space and result:
                result.append(" ")
            pending_space = False
            quote_end = "]"
            result.append(character)
        elif character in SQLITE_SQL_WHITESPACE:
            pending_space = True
        else:
            if pending_space and result:
                result.append(" ")
            pending_space = False
            result.append(character)
        index += 1
    return "".join(result).strip()


def _compiled_type(column_type: object, dialect: object) -> str:
    try:
        return str(column_type.compile(dialect=dialect))  # type: ignore[attr-defined]
    except CompileError:
        type_class = type(column_type)
        return f"<uncompilable:{type_class.__module__}.{type_class.__qualname__}>"


def _table_shape(inspector: Inspector, table_name: str) -> dict[str, object]:
    dialect = inspector.bind.dialect
    columns = []
    for column in inspector.get_columns(table_name):
        columns.append(
            {
                "name": str(column["name"]),
                "type": _compiled_type(column["type"], dialect),
                "nullable": bool(column["nullable"]),
                "default": _normalized_sql(column.get("default")),
                "computed": _stable_value(column.get("computed")),
                "identity": _stable_value(column.get("identity")),
            }
        )
    primary_key = inspector.get_pk_constraint(table_name)
    foreign_keys = [
        {
            "name": constraint.get("name"),
            "columns": tuple(constraint.get("constrained_columns") or ()),
            "referred_schema": constraint.get("referred_schema"),
            "referred_table": constraint.get("referred_table"),
            "referred_columns": tuple(constraint.get("referred_columns") or ()),
            "options": _stable_value(constraint.get("options") or {}),
        }
        for constraint in inspector.get_foreign_keys(table_name)
    ]
    unique_constraints = [
        {
            "name": constraint.get("name"),
            "columns": tuple(constraint.get("column_names") or ()),
        }
        for constraint in inspector.get_unique_constraints(table_name)
    ]
    indexes = [
        {
            "name": index.get("name"),
            "columns": tuple(index.get("column_names") or ()),
            "expressions": tuple(index.get("expressions") or ()),
            "unique": bool(index.get("unique")),
            "dialect_options": _stable_value(index.get("dialect_options") or {}),
        }
        for index in inspector.get_indexes(table_name)
    ]
    checks = [
        {
            "name": constraint.get("name"),
            "sqltext": _normalized_sql(constraint.get("sqltext")),
        }
        for constraint in inspector.get_check_constraints(table_name)
    ]
    return {
        "name": table_name,
        "columns": columns,
        "primary_key": {
            "name": primary_key.get("name"),
            "columns": tuple(primary_key.get("constrained_columns") or ()),
        },
        "foreign_keys": sorted(
            foreign_keys,
            key=lambda item: json.dumps(item, sort_keys=True),
        ),
        "unique_constraints": sorted(
            unique_constraints,
            key=lambda item: json.dumps(item, sort_keys=True),
        ),
        "indexes": sorted(
            indexes,
            key=lambda item: json.dumps(item, sort_keys=True),
        ),
        "checks": sorted(
            checks,
            key=lambda item: json.dumps(item, sort_keys=True),
        ),
    }


def _sql_without_comments(sql: str) -> str:
    result: list[str] = []
    quote_end: str | None = None
    index = 0
    while index < len(sql):
        character = sql[index]
        if quote_end is not None:
            result.append(character)
            if character == quote_end:
                if (
                    quote_end != "]"
                    and index + 1 < len(sql)
                    and sql[index + 1] == quote_end
                ):
                    result.append(sql[index + 1])
                    index += 2
                    continue
                quote_end = None
            index += 1
            continue
        if character == "/" and index + 1 < len(sql) and sql[index + 1] == "*":
            comment_end = sql.find("*/", index + 2)
            if comment_end == -1:
                raise SQLiteMigrationHistoryConflict(
                    "SQLite schema contains an unterminated SQLite block comment"
                )
            result.append(" ")
            index = comment_end + 2
            continue
        if character == "-" and index + 1 < len(sql) and sql[index + 1] == "-":
            result.append(" ")
            index += 2
            while index < len(sql) and sql[index] != "\n":
                index += 1
            continue
        if character == "\v":
            raise SQLiteMigrationHistoryConflict(
                "SQLite schema contains an invalid vertical-tab token"
            )
        if character in {"'", '"', "`"}:
            quote_end = character
            result.append(character)
        elif character == "[":
            quote_end = "]"
            result.append(character)
        else:
            result.append(character)
        index += 1
    if quote_end is not None:
        raise SQLiteMigrationHistoryConflict(
            "SQLite schema contains an unterminated quoted token"
        )
    return "".join(result)


def _sql_without_quoted_or_commented_content(sql: str) -> str:
    sql_without_comments = _sql_without_comments(sql)
    result: list[str] = []
    quote_end: str | None = None
    index = 0
    while index < len(sql_without_comments):
        character = sql_without_comments[index]
        if quote_end is not None:
            result.append(" ")
            if character == quote_end:
                if (
                    quote_end != "]"
                    and index + 1 < len(sql_without_comments)
                    and sql_without_comments[index + 1] == quote_end
                ):
                    result.append(" ")
                    index += 2
                    continue
                quote_end = None
            index += 1
            continue
        if character in {"'", '"', "`"}:
            quote_end = character
            result.append(" ")
        elif character == "[":
            quote_end = "]"
            result.append(" ")
        else:
            result.append(character)
        index += 1
    return "".join(result)


def _table_body_bounds(sql: str) -> tuple[int, int]:
    quote_end: str | None = None
    opening: int | None = None
    depth = 0
    index = 0
    while index < len(sql):
        character = sql[index]
        if quote_end is not None:
            if character == quote_end:
                if (
                    quote_end != "]"
                    and index + 1 < len(sql)
                    and sql[index + 1] == quote_end
                ):
                    index += 2
                    continue
                quote_end = None
            index += 1
            continue
        if character in {"'", '"', "`"}:
            quote_end = character
        elif character == "[":
            quote_end = "]"
        elif character == "(":
            if opening is None:
                opening = index
            depth += 1
        elif character == ")":
            if opening is None or depth == 0:
                break
            depth -= 1
            if depth == 0:
                return opening, index
        index += 1
    raise SQLiteMigrationHistoryConflict(
        "SQLite CREATE TABLE body has unbalanced parentheses"
    )


def _split_table_body(body: str) -> list[str]:
    segments: list[str] = []
    quote_end: str | None = None
    depth = 0
    segment_start = 0
    index = 0
    while index < len(body):
        character = body[index]
        if quote_end is not None:
            if character == quote_end:
                if (
                    quote_end != "]"
                    and index + 1 < len(body)
                    and body[index + 1] == quote_end
                ):
                    index += 2
                    continue
                quote_end = None
            index += 1
            continue
        if character in {"'", '"', "`"}:
            quote_end = character
        elif character == "[":
            quote_end = "]"
        elif character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
            if depth < 0:
                break
        elif character == "," and depth == 0:
            segments.append(body[segment_start:index])
            segment_start = index + 1
        index += 1
    if quote_end is not None or depth != 0:
        raise SQLiteMigrationHistoryConflict(
            "SQLite CREATE TABLE segment has ambiguous structure"
        )
    segments.append(body[segment_start:])
    if any(not segment.strip() for segment in segments):
        raise SQLiteMigrationHistoryConflict(
            "SQLite CREATE TABLE contains an empty segment"
        )
    return segments


def _leading_clause_tokens(
    segment: str,
    *,
    limit: int | None = 4,
) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    index = 0
    while index < len(segment) and (limit is None or len(tokens) < limit):
        character = segment[index]
        if character in SQLITE_SQL_WHITESPACE:
            index += 1
            continue
        if character in {"'", '"', "`", "["}:
            quote_end = "]" if character == "[" else character
            index += 1
            while index < len(segment):
                if segment[index] == quote_end:
                    if (
                        quote_end != "]"
                        and index + 1 < len(segment)
                        and segment[index + 1] == quote_end
                    ):
                        index += 2
                        continue
                    index += 1
                    break
                index += 1
            tokens.append(("quoted", ""))
            continue
        if character.isalpha() or character == "_" or ord(character) >= 0x80:
            token_start = index
            index += 1
            while index < len(segment) and (
                segment[index].isalnum()
                or segment[index] in {"_", "$"}
                or ord(segment[index]) >= 0x80
            ):
                index += 1
            tokens.append(("word", segment[token_start:index].upper()))
            continue
        tokens.append(("symbol", character))
        index += 1
    return tokens


def _is_table_constraint(segment: str) -> bool:
    tokens = _leading_clause_tokens(segment)
    if not tokens:
        raise SQLiteMigrationHistoryConflict(
            "SQLite CREATE TABLE contains an empty segment"
        )
    position = 0
    if tokens[0] == ("word", "CONSTRAINT"):
        if (
            len(tokens) < 3
            or tokens[1][0] not in {"word", "quoted"}
            or tokens[2][0] != "word"
        ):
            raise SQLiteMigrationHistoryConflict(
                "SQLite named table constraint is ambiguous"
            )
        position = 2
    keyword = tokens[position]
    next_keyword = tokens[position + 1] if position + 1 < len(tokens) else None
    if keyword == ("word", "PRIMARY"):
        if next_keyword != ("word", "KEY"):
            raise SQLiteMigrationHistoryConflict(
                "SQLite PRIMARY table constraint is ambiguous"
            )
        return True
    if keyword == ("word", "FOREIGN"):
        if next_keyword != ("word", "KEY"):
            raise SQLiteMigrationHistoryConflict(
                "SQLite FOREIGN table constraint is ambiguous"
            )
        return True
    if keyword in {("word", "UNIQUE"), ("word", "CHECK")}:
        return True
    if (
        position
        or keyword[0] == "word"
        and keyword[1]
        in {
            "PRIMARY",
            "FOREIGN",
            "UNIQUE",
            "CHECK",
        }
    ):
        raise SQLiteMigrationHistoryConflict(
            "SQLite table constraint cannot be classified"
        )
    return False


def _validate_table_options(suffix: str) -> None:
    tokens = _leading_clause_tokens(suffix, limit=None)
    if not tokens:
        return

    position = 0
    seen: set[str] = set()
    while position < len(tokens):
        if tokens[position] == ("word", "STRICT"):
            option = "strict"
            position += 1
        elif tokens[position : position + 2] == [
            ("word", "WITHOUT"),
            ("word", "ROWID"),
        ]:
            option = "without_rowid"
            position += 2
        else:
            raise SQLiteMigrationHistoryConflict(
                "SQLite CREATE TABLE suffix contains an unknown table option"
            )

        if option in seen:
            raise SQLiteMigrationHistoryConflict(
                "SQLite CREATE TABLE suffix repeats a table option"
            )
        seen.add(option)
        if position == len(tokens):
            return
        if tokens[position] != ("symbol", ","):
            raise SQLiteMigrationHistoryConflict(
                "SQLite CREATE TABLE options must be comma-separated"
            )
        position += 1
        if position == len(tokens):
            raise SQLiteMigrationHistoryConflict(
                "SQLite CREATE TABLE suffix has a trailing comma"
            )


def _canonical_table_ddl(sql: str | None) -> str:
    if sql is None:
        raise SQLiteMigrationHistoryConflict(
            "SQLite table is missing its CREATE definition"
        )
    sql_without_comments = _sql_without_comments(sql)
    visible_sql = _sql_without_quoted_or_commented_content(sql)
    if ";" in visible_sql:
        raise SQLiteMigrationHistoryConflict(
            "SQLite schema contains a semicolon or multiple statements"
        )
    leading_tokens = _leading_clause_tokens(visible_sql)
    if leading_tokens[:3] == [
        ("word", "CREATE"),
        ("word", "VIRTUAL"),
        ("word", "TABLE"),
    ]:
        normalized_virtual = _normalized_sql(sql_without_comments)
        if normalized_virtual is None:
            raise SQLiteMigrationHistoryConflict(
                "SQLite virtual table is missing its CREATE definition"
            )
        return normalized_virtual
    if leading_tokens[:2] != [
        ("word", "CREATE"),
        ("word", "TABLE"),
    ]:
        raise SQLiteMigrationHistoryConflict(
            "SQLite schema object is not a canonical CREATE TABLE"
        )
    opening, closing = _table_body_bounds(sql_without_comments)
    prefix_tokens = _leading_clause_tokens(
        sql_without_comments[:opening],
        limit=None,
    )
    valid_prefix = (
        len(prefix_tokens) == 3
        and prefix_tokens[:2]
        == [
            ("word", "CREATE"),
            ("word", "TABLE"),
        ]
        and prefix_tokens[2][0] in {"word", "quoted"}
    )
    valid_if_not_exists_prefix = (
        len(prefix_tokens) == 6
        and prefix_tokens[:5]
        == [
            ("word", "CREATE"),
            ("word", "TABLE"),
            ("word", "IF"),
            ("word", "NOT"),
            ("word", "EXISTS"),
        ]
        and prefix_tokens[5][0] in {"word", "quoted"}
    )
    if not valid_prefix and not valid_if_not_exists_prefix:
        raise SQLiteMigrationHistoryConflict(
            "SQLite CREATE TABLE prefix has ambiguous structure"
        )
    if any(
        character in {"(", ")"}
        for character in _sql_without_quoted_or_commented_content(
            sql_without_comments[closing + 1 :]
        )
    ):
        raise SQLiteMigrationHistoryConflict(
            "SQLite CREATE TABLE suffix has ambiguous parentheses"
        )
    _validate_table_options(sql_without_comments[closing + 1 :])
    columns: list[str] = []
    constraints: list[str] = []
    saw_table_constraint = False
    for segment in _split_table_body(sql_without_comments[opening + 1 : closing]):
        normalized_segment = _normalized_sql(segment)
        if normalized_segment is None:
            raise SQLiteMigrationHistoryConflict(
                "SQLite CREATE TABLE contains an empty segment"
            )
        is_table_constraint = _is_table_constraint(segment)
        if not is_table_constraint and saw_table_constraint:
            raise SQLiteMigrationHistoryConflict(
                "SQLite column appears after a table constraint"
            )
        saw_table_constraint = saw_table_constraint or is_table_constraint
        target = constraints if is_table_constraint else columns
        target.append(normalized_segment)
    if not columns:
        raise SQLiteMigrationHistoryConflict(
            "SQLite CREATE TABLE contains no column definition"
        )
    return json.dumps(
        {
            "prefix": _normalized_sql(sql_without_comments[:opening]),
            "columns": columns,
            "constraints": constraints,
            "suffix": _normalized_sql(sql_without_comments[closing + 1 :]),
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def _table_ddl_features(sql: str | None) -> tuple[tuple[str, int], ...]:
    if sql is None:
        return ()
    visible_sql = _sql_without_quoted_or_commented_content(sql).upper()
    patterns = (
        ("autoincrement", r"\bAUTOINCREMENT\b"),
        ("collate", r"\bCOLLATE\b"),
        ("on_conflict", r"\bON\s+CONFLICT\b"),
        ("strict", r"\bSTRICT\b"),
        ("virtual_table", r"\bCREATE\s+VIRTUAL\s+TABLE\b"),
        ("without_rowid", r"\bWITHOUT\s+ROWID\b"),
    )
    return tuple(
        (name, len(matches))
        for name, pattern in patterns
        if (matches := re.findall(pattern, visible_sql))
    )


def _schema_ddl(
    engine: Engine | Connection,
    *,
    excluded_table_names: frozenset[str] = frozenset(),
) -> list[dict[str, object]]:
    with Session(engine) as session:
        objects = (
            session.query(SQLiteSchemaObject)
            .order_by(
                SQLiteSchemaObject.object_type,
                SQLiteSchemaObject.name,
            )
            .all()
        )
    schema: list[dict[str, object]] = []
    for schema_object in objects:
        if (
            schema_object.name.startswith("sqlite_")
            or schema_object.name in excluded_table_names
            or schema_object.table_name in excluded_table_names
        ):
            continue
        record: dict[str, object] = {
            "type": schema_object.object_type,
            "name": schema_object.name,
            "table_name": schema_object.table_name,
        }
        if schema_object.object_type == "table":
            record["sql"] = _canonical_table_ddl(schema_object.definition)
        else:
            record["sql"] = _normalized_sql(schema_object.definition)
        schema.append(record)
    return schema


def sqlite_schema_fingerprint(
    engine: Engine | Connection,
    *,
    excluded_table_names: frozenset[str] = frozenset(),
) -> str:
    """Hash the normalized complete SQLite structure exposed by Inspector."""

    inspector = inspect(engine)
    table_shapes = [
        _table_shape(inspector, table_name)
        for table_name in sorted(
            name
            for name in inspector.get_table_names()
            if not name.startswith("sqlite_") and name not in excluded_table_names
        )
    ]
    canonical = json.dumps(
        {
            "ddl": _schema_ddl(
                engine,
                excluded_table_names=excluded_table_names,
            ),
            "tables": table_shapes,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _raw_schema_manifest(
    engine: Engine | Connection,
    *,
    excluded_table_names: frozenset[str] = frozenset(),
) -> list[dict[str, str | None]]:
    with Session(engine) as session:
        objects = (
            session.query(SQLiteSchemaObject)
            .order_by(
                SQLiteSchemaObject.object_type,
                SQLiteSchemaObject.name,
            )
            .all()
        )
    return [
        {
            "type": schema_object.object_type,
            "name": schema_object.name,
            "table_name": schema_object.table_name,
            "definition": schema_object.definition,
        }
        for schema_object in objects
        if not schema_object.name.startswith("sqlite_")
        and schema_object.name not in excluded_table_names
        and schema_object.table_name not in excluded_table_names
    ]


def sqlite_raw_manifest_checksum(
    engine: Engine | Connection,
    *,
    excluded_table_names: frozenset[str] = frozenset(),
) -> str:
    """Hash exact sqlite_master bytes independently of the canonicalizer."""

    canonical = json.dumps(
        _raw_schema_manifest(
            engine,
            excluded_table_names=excluded_table_names,
        ),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


@lru_cache(maxsize=1)
def expected_sqlite_schema_fingerprint() -> str:
    """Return the current canonical business-schema fingerprint."""

    reference = create_engine("sqlite+pysqlite:///:memory:")
    try:
        with reference.begin() as connection:
            operations = Operations(MigrationContext.configure(connection))
            for migration in PUBLISHED_SQLITE_MIGRATIONS:
                migration.upgrade(operations)
        return sqlite_schema_fingerprint(reference)
    finally:
        reference.dispose()


@lru_cache(maxsize=1)
def expected_current_database_fingerprint() -> str:
    """Return the canonical business plus migration-ledger structure."""

    reference = create_engine("sqlite+pysqlite:///:memory:")
    try:
        with reference.begin() as connection:
            operations = Operations(MigrationContext.configure(connection))
            for migration in PUBLISHED_SQLITE_MIGRATIONS:
                migration.upgrade(operations)
            operations.invoke(
                _deterministic_create_table_op(
                    SQLiteSchemaMigration.__table__,
                )
            )
        return sqlite_schema_fingerprint(reference)
    finally:
        reference.dispose()


@cache
def _expected_versioned_database_fingerprint(version: int) -> str:
    """Dynamically replay one version for diagnostics and regression tests only."""

    reference = create_engine("sqlite+pysqlite:///:memory:")
    try:
        with reference.begin() as connection:
            operations = Operations(MigrationContext.configure(connection))
            for migration in PUBLISHED_SQLITE_MIGRATIONS:
                if migration.version > version:
                    break
                migration.upgrade(operations)
            operations.invoke(
                _deterministic_create_table_op(SQLiteSchemaMigration.__table__)
            )
        return sqlite_schema_fingerprint(reference)
    finally:
        reference.dispose()


def _published_versioned_database_signature(version: int) -> tuple[str, str]:
    for published_version, canonical_fingerprint, raw_manifest_checksum in (
        PUBLISHED_SQLITE_VERSIONED_DATABASE_SIGNATURES
    ):
        if published_version == version:
            return canonical_fingerprint, raw_manifest_checksum
    raise SQLiteMigrationHistoryConflict(
        "SQLite migration history conflicts with the published catalog"
    )


@lru_cache(maxsize=1)
def expected_sqlite_ledger_fingerprint() -> str:
    """Return the exact structure of the deterministic SQLite ledger table."""

    reference = create_engine("sqlite+pysqlite:///:memory:")
    try:
        with reference.begin() as connection:
            operations = Operations(MigrationContext.configure(connection))
            operations.invoke(
                _deterministic_create_table_op(
                    SQLiteSchemaMigration.__table__,
                )
            )
        return sqlite_schema_fingerprint(reference)
    finally:
        reference.dispose()


@lru_cache(maxsize=1)
def expected_sqlite_ledger_raw_manifest_checksum() -> str:
    """Return the exact raw DDL checksum of the published SQLite v1 ledger."""

    reference = create_engine("sqlite+pysqlite:///:memory:")
    try:
        SQLiteSchemaMigration.__table__.create(reference)
        return sqlite_raw_manifest_checksum(reference)
    finally:
        reference.dispose()


def _schema_signature(
    engine: Engine | Connection,
    *,
    excluded_table_names: frozenset[str] = frozenset(),
) -> tuple[str, str]:
    return (
        sqlite_schema_fingerprint(
            engine,
            excluded_table_names=excluded_table_names,
        ),
        sqlite_raw_manifest_checksum(
            engine,
            excluded_table_names=excluded_table_names,
        ),
    )


def _ledger_signature(engine: Engine | Connection) -> tuple[str, str]:
    schema_manifest = _raw_schema_manifest(engine)
    non_ledger_objects = frozenset(
        str(schema_object["name"])
        for schema_object in schema_manifest
        if schema_object["name"] != SQLITE_MIGRATION_TABLE
        and schema_object["table_name"] != SQLITE_MIGRATION_TABLE
    )
    return _schema_signature(
        engine,
        excluded_table_names=non_ledger_objects,
    )


def _session_identity_schema_signature(
    engine: Engine | Connection,
) -> tuple[str, str] | None:
    table_names = frozenset(inspect(engine).get_table_names())
    identity_tables = frozenset(_SESSION_PROJECTION_V10_TABLES)
    if not identity_tables <= table_names:
        return None
    return _schema_signature(
        engine,
        excluded_table_names=table_names - identity_tables,
    )


@lru_cache(maxsize=1)
def _expected_current_session_identity_signature() -> tuple[str, str]:
    """Dynamically rebuild the component for diagnostics and tests only."""

    reference = create_engine("sqlite+pysqlite:///:memory:")
    try:
        with reference.begin() as connection:
            operations = Operations(MigrationContext.configure(connection))
            for migration in PUBLISHED_SQLITE_MIGRATIONS:
                migration.upgrade(operations)
        signature = _session_identity_schema_signature(reference)
        if signature is None:
            raise RuntimeError("current SQLite session identity schema is incomplete")
        return signature
    finally:
        reference.dispose()


def _validate_current_session_identity_rows(
    engine: Engine | Connection,
) -> None:
    with Session(engine) as session:
        projections = tuple(session.scalars(select(SessionProjectionV11Rows)).all())
        tickets = tuple(session.scalars(select(WebSocketTicketV11Rows)).all())
        by_stable_id: dict[tuple[UUID, UUID], SessionProjectionV11Rows] = {}
        durable_identities: set[tuple[UUID, UUID | None, str, str]] = set()
        for projection in projections:
            profile = projection.profile
            if (
                not isinstance(profile, str)
                or not 1 <= len(profile) <= 128
                or profile != profile.strip()
            ):
                raise SQLiteMigrationHistoryConflict(
                    "SQLite current session profile is invalid"
                )
            if projection.agent_id is None:
                raise SQLiteMigrationHistoryConflict(
                    "SQLite current session Agent identity is unavailable"
                )
            identity = (
                UUID(str(projection.tenant_id)),
                (
                    UUID(str(projection.agent_id))
                    if projection.agent_id is not None
                    else None
                ),
                profile,
                str(projection.session_key),
            )
            if identity in durable_identities:
                raise SQLiteMigrationHistoryConflict(
                    "SQLite current session identity is ambiguous"
                )
            durable_identities.add(identity)
            by_stable_id[
                (UUID(str(projection.tenant_id)), UUID(str(projection.session_id)))
            ] = projection

        for ticket in tickets:
            scope = ticket.observer_scope
            if not isinstance(scope, list) or any(
                not isinstance(item, str) for item in scope
            ):
                raise SQLiteMigrationHistoryConflict(
                    "SQLite current ticket scope is invalid"
                )
            is_control = "session.control" in scope
            if not is_control:
                if ticket.session_id is not None:
                    raise SQLiteMigrationHistoryConflict(
                        "SQLite observer ticket cannot bind a session"
                    )
                continue
            if ticket.session_id is None:
                raise SQLiteMigrationHistoryConflict(
                    "SQLite control ticket has no stable session identity"
                )
            projection = by_stable_id.get(
                (UUID(str(ticket.tenant_id)), UUID(str(ticket.session_id)))
            )
            if projection is None:
                raise SQLiteMigrationHistoryConflict(
                    "SQLite control ticket session is unavailable"
                )
            profile_scopes = tuple(
                item for item in scope if item.startswith("profile=")
            )
            agent_scopes = tuple(
                item for item in scope if item.startswith("agent_id=")
            )
            if profile_scopes != (f"profile={projection.profile}",) or agent_scopes != (
                f"agent_id={projection.agent_id}",
            ):
                raise SQLiteMigrationHistoryConflict(
                    "SQLite control ticket scope does not match its session"
                )


def _matches_published_legacy_v1_with_exact_ledger(
    engine: Engine | Connection,
) -> bool:
    business_signature = _schema_signature(
        engine,
        excluded_table_names=frozenset({SQLITE_MIGRATION_TABLE}),
    )
    business_matches = any(
        business_signature == (source.schema_fingerprint, source.raw_manifest_checksum)
        for source in PUBLISHED_SQLITE_LEGACY_SOURCES
    )
    return (
        business_matches
        and _ledger_signature(engine) == PUBLISHED_SQLITE_V1_LEDGER_SIGNATURE
    )


def _has_exact_published_ledger_prefix(
    engine: Engine | Connection,
    *,
    version: int,
) -> bool:
    try:
        with Session(engine) as session:
            applied = tuple(
                session.query(SQLiteSchemaMigration)
                .order_by(SQLiteSchemaMigration.version)
                .all()
            )
    except OperationalError:
        return False
    actual = tuple(
        (migration.version, migration.name, migration.checksum) for migration in applied
    )
    expected = tuple(
        (migration.version, migration.name, migration.checksum)
        for migration in PUBLISHED_SQLITE_MIGRATIONS[:version]
    )
    return actual == expected


def _legacy_v5_overlay_tables() -> tuple[
    frozenset[str],
    frozenset[str],
    frozenset[str],
]:
    observer_tables = frozenset(
        {
            ObserverEventModel.__table__.name,
            ObserverSessionModel.__table__.name,
            ObserverInboxV6Model.__table__.name,
            ObserverDeletionLedgerModel.__table__.name,
            *(
                table.name
                for table in ObserverSubscriptionV7Base.metadata.tables.values()
            ),
        }
    )
    transport_tables = frozenset({ConnectorTransportCursorModel.__table__.name})
    handshake_tables = frozenset(
        {
            ConnectorTransportHandshakeOwnershipModel.__table__.name,
            ConnectorObserverReceiptModel.__table__.name,
        }
    )
    return observer_tables, transport_tables, handshake_tables


def _matching_versioned_compatibility_source(
    engine: Engine | Connection,
    *,
    version: int,
) -> PublishedSQLiteVersionedCompatibilitySource | None:
    candidates = tuple(
        source
        for source in PUBLISHED_SQLITE_VERSIONED_COMPATIBILITY_SOURCES
        if source.version == version
    )
    if not candidates or not _has_exact_published_ledger_prefix(
        engine,
        version=version,
    ):
        return None
    table_names = frozenset(inspect(engine).get_table_names())
    observer_tables, transport_tables, handshake_tables = _legacy_v5_overlay_tables()
    overlay_tables = observer_tables | transport_tables | handshake_tables
    if not overlay_tables <= table_names:
        return None
    complete_signature = _schema_signature(engine)
    base_signature = _schema_signature(
        engine,
        excluded_table_names=frozenset({SQLITE_MIGRATION_TABLE, *overlay_tables}),
    )
    ledger_signature = _ledger_signature(engine)
    observer_signature = _schema_signature(
        engine,
        excluded_table_names=table_names - observer_tables,
    )
    transport_signature = _schema_signature(
        engine,
        excluded_table_names=table_names - transport_tables,
    )
    handshake_signature = _schema_signature(
        engine,
        excluded_table_names=table_names - handshake_tables,
    )
    for source in candidates:
        if (
            complete_signature
            == (source.schema_fingerprint, source.raw_manifest_checksum)
            and base_signature
            == (
                source.legacy_base_schema_fingerprint,
                source.legacy_base_raw_manifest_checksum,
            )
            and ledger_signature
            == (
                source.ledger_schema_fingerprint,
                source.ledger_raw_manifest_checksum,
            )
            and observer_signature
            == (
                source.observer_schema_fingerprint,
                source.observer_raw_manifest_checksum,
            )
            and transport_signature
            == (
                source.transport_schema_fingerprint,
                source.transport_raw_manifest_checksum,
            )
            and handshake_signature
            == (
                source.handshake_schema_fingerprint,
                source.handshake_raw_manifest_checksum,
            )
        ):
            return source
    return None


def _matching_versioned_database_compatibility_source(
    engine: Engine | Connection,
    *,
    version: int,
) -> PublishedSQLiteVersionedDatabaseCompatibilitySource | None:
    candidates = tuple(
        source
        for source in PUBLISHED_SQLITE_VERSIONED_DATABASE_COMPATIBILITY_SOURCES
        if source.version == version
    )
    if not candidates or not _has_exact_published_ledger_prefix(
        engine,
        version=version,
    ):
        return None
    signature = _schema_signature(engine)
    return next(
        (
            source
            for source in candidates
            if signature
            == (source.schema_fingerprint, source.raw_manifest_checksum)
        ),
        None,
    )


@lru_cache(maxsize=1)
def _expected_observer_projection_signature() -> tuple[str, str]:
    """Dynamically rebuild the component for diagnostics and tests only."""

    reference = create_engine("sqlite+pysqlite:///:memory:")
    try:
        with reference.begin() as connection:
            operations = Operations(MigrationContext.configure(connection))
            _create_v2_observer_projection(operations)
            _create_v3_observer_authority(operations)
            _create_v6_observer_output_parity(operations)
            _create_v7_observer_inbox_runtime_epoch(operations)
            _create_v8_observer_subscription_wire_contract(operations)
            _create_v9_observer_inbox_retention(operations)
        return _schema_signature(reference)
    finally:
        reference.dispose()


@lru_cache(maxsize=1)
def _expected_observer_projection_fingerprint() -> str:
    return _expected_observer_projection_signature()[0]


@lru_cache(maxsize=1)
def _expected_connector_transport_signature() -> tuple[str, str]:
    """Dynamically rebuild the component for diagnostics and tests only."""

    reference = create_engine("sqlite+pysqlite:///:memory:")
    try:
        with reference.begin() as connection:
            operations = Operations(MigrationContext.configure(connection))
            _create_v4_connector_transport_cursor(operations)
        return _schema_signature(reference)
    finally:
        reference.dispose()


@lru_cache(maxsize=1)
def _expected_connector_transport_fingerprint() -> str:
    return _expected_connector_transport_signature()[0]


@lru_cache(maxsize=1)
def _expected_connector_handshake_signature() -> tuple[str, str]:
    """Dynamically rebuild the component for diagnostics and tests only."""

    reference = create_engine("sqlite+pysqlite:///:memory:")
    try:
        with reference.begin() as connection:
            operations = Operations(MigrationContext.configure(connection))
            _create_v5_connector_handshake_ownership(operations)
        return _schema_signature(reference)
    finally:
        reference.dispose()


@lru_cache(maxsize=1)
def _expected_connector_handshake_fingerprint() -> str:
    return _expected_connector_handshake_signature()[0]


def verify_published_sqlite_catalog() -> None:
    migration_versions = tuple(
        migration.version for migration in PUBLISHED_SQLITE_MIGRATIONS
    )
    signature_versions = tuple(
        version
        for version, _canonical, _raw in (
            PUBLISHED_SQLITE_VERSIONED_DATABASE_SIGNATURES
        )
    )
    versioned_digests = tuple(
        (canonical, raw)
        for _version, canonical, raw in (
            PUBLISHED_SQLITE_VERSIONED_DATABASE_SIGNATURES
        )
    )
    current_component_signatures = (
        PUBLISHED_SQLITE_CURRENT_SESSION_IDENTITY_SIGNATURE,
        PUBLISHED_SQLITE_CURRENT_OBSERVER_SIGNATURE,
        PUBLISHED_SQLITE_CONNECTOR_TRANSPORT_SIGNATURE,
        PUBLISHED_SQLITE_CONNECTOR_HANDSHAKE_SIGNATURE,
        PUBLISHED_SQLITE_SESSION_CATALOG_SIGNATURE,
    )
    legacy_releases = tuple(
        source.release for source in PUBLISHED_SQLITE_LEGACY_SOURCES
    )
    legacy_signatures = tuple(
        (
            source.schema_fingerprint,
            source.raw_manifest_checksum,
            source.session_identity_remainder_schema_fingerprint,
            source.session_identity_remainder_raw_manifest_checksum,
        )
        for source in PUBLISHED_SQLITE_LEGACY_SOURCES
    )
    compatibility_catalog = tuple(
        (source.release, source.version, source.source)
        for source in PUBLISHED_SQLITE_VERSIONED_COMPATIBILITY_SOURCES
    )
    compatibility_digests = tuple(
        (
            source.schema_fingerprint,
            source.raw_manifest_checksum,
            source.legacy_base_schema_fingerprint,
            source.legacy_base_raw_manifest_checksum,
            source.ledger_schema_fingerprint,
            source.ledger_raw_manifest_checksum,
            source.observer_schema_fingerprint,
            source.observer_raw_manifest_checksum,
            source.transport_schema_fingerprint,
            source.transport_raw_manifest_checksum,
            source.handshake_schema_fingerprint,
            source.handshake_raw_manifest_checksum,
        )
        for source in PUBLISHED_SQLITE_VERSIONED_COMPATIBILITY_SOURCES
    )
    database_compatibility_catalog = tuple(
        (source.release, source.version, source.source)
        for source in PUBLISHED_SQLITE_VERSIONED_DATABASE_COMPATIBILITY_SOURCES
    )
    database_compatibility_digests = tuple(
        (source.schema_fingerprint, source.raw_manifest_checksum)
        for source in PUBLISHED_SQLITE_VERSIONED_DATABASE_COMPATIBILITY_SOURCES
    )
    if (
        tuple(
            (migration.version, migration.name)
            for migration in PUBLISHED_SQLITE_MIGRATIONS
        )
        != (
            (1, "0001_current_sqlite_baseline"),
            (2, "0002_observer_projection"),
            (3, "0003_observer_authority_and_encryption"),
            (4, "0004_connector_transport_cursor"),
            (5, "0005_connector_handshake_ownership"),
            (6, "0006_observer_output_parity"),
            (7, "0007_observer_inbox_runtime_epoch"),
            (8, "0008_observer_subscription_wire_contract"),
            (9, "0009_observer_inbox_retention"),
            (10, "0010_observer_subscription_legacy_wire_repair"),
            (11, "0011_session_projection_durable_identity"),
            (12, "0012_session_catalog_v1"),
            (13, "0013_session_catalog_recovery"),
        )
        or signature_versions != migration_versions
        or any(
            re.fullmatch(r"[0-9a-f]{64}", digest) is None
            for digests in versioned_digests
            for digest in digests
        )
        or any(
            re.fullmatch(r"[0-9a-f]{64}", digest) is None
            for signature in current_component_signatures
            for digest in signature
        )
        or VERIFIED_SQLITE_UPGRADE_SOURCE_VERSIONS
        and VERIFIED_SQLITE_UPGRADE_SOURCE_VERSIONS
        != tuple(sorted(set(VERIFIED_SQLITE_UPGRADE_SOURCE_VERSIONS)))
        or any(
            version
            not in {migration.version for migration in PUBLISHED_SQLITE_MIGRATIONS[:-1]}
            for version in VERIFIED_SQLITE_UPGRADE_SOURCE_VERSIONS
        )
        or PUBLISHED_SQLITE_MIGRATIONS[-1].checksum
        != expected_sqlite_schema_fingerprint()
        or PUBLISHED_SQLITE_V1_LEDGER_SIGNATURE
        != (
            expected_sqlite_ledger_fingerprint(),
            expected_sqlite_ledger_raw_manifest_checksum(),
        )
        or legacy_releases != tuple(dict.fromkeys(legacy_releases))
        or legacy_signatures != tuple(dict.fromkeys(legacy_signatures))
        or compatibility_catalog
        != (("20260731T084500Z-legacy-v1-to-v5", 5, "versioned-5-compatible"),)
        or database_compatibility_catalog
        != (("20260801T131728Z", 10, "versioned-10"),)
        or any(
            (
                source.legacy_base_schema_fingerprint,
                source.legacy_base_raw_manifest_checksum,
            )
            != PUBLISHED_SQLITE_DEPLOYED_LEGACY_BASE_SIGNATURE
            or (
                source.ledger_schema_fingerprint,
                source.ledger_raw_manifest_checksum,
            )
            != PUBLISHED_SQLITE_V1_LEDGER_SIGNATURE
            or (
                source.transport_schema_fingerprint,
                source.transport_raw_manifest_checksum,
            )
            != PUBLISHED_SQLITE_CONNECTOR_TRANSPORT_SIGNATURE
            or (
                source.handshake_schema_fingerprint,
                source.handshake_raw_manifest_checksum,
            )
            != PUBLISHED_SQLITE_CONNECTOR_HANDSHAKE_SIGNATURE
            for source in PUBLISHED_SQLITE_VERSIONED_COMPATIBILITY_SOURCES
        )
        or any(
            re.fullmatch(r"[0-9a-f]{64}", digest) is None
            for digests in compatibility_digests
            for digest in digests
        )
        or any(
            re.fullmatch(r"[0-9a-f]{64}", digest) is None
            for digests in database_compatibility_digests
            for digest in digests
        )
        or any(
            not source.release
            or any(
                re.fullmatch(r"[0-9a-f]{64}", digest) is None
                for digest in (
                    source.schema_fingerprint,
                    source.raw_manifest_checksum,
                    source.session_identity_remainder_schema_fingerprint,
                    source.session_identity_remainder_raw_manifest_checksum,
                )
            )
            for source in PUBLISHED_SQLITE_LEGACY_SOURCES
        )
    ):
        raise SQLiteMigrationHistoryConflict(
            "SQLite migration history conflicts with the published catalog"
        )


def _validate_versioned_history(engine: Engine | Connection) -> int:
    try:
        with Session(engine) as session:
            applied = (
                session.query(SQLiteSchemaMigration)
                .order_by(SQLiteSchemaMigration.version)
                .all()
            )
    except OperationalError as error:
        raise SQLiteMigrationHistoryConflict(
            "SQLite migration history conflicts with the published catalog"
        ) from error
    published = [
        (migration.version, migration.name, migration.checksum)
        for migration in PUBLISHED_SQLITE_MIGRATIONS
    ]
    actual = [
        (migration.version, migration.name, migration.checksum) for migration in applied
    ]
    if not actual or actual != published[: len(actual)]:
        raise SQLiteMigrationHistoryConflict(
            "SQLite migration history conflicts with the published catalog"
        )
    version = applied[-1].version
    if _schema_signature(engine) != _published_versioned_database_signature(version):
        if version == 1 and _matches_published_legacy_v1_with_exact_ledger(engine):
            return version
        if (
            version == 5
            and _matching_versioned_compatibility_source(
                engine,
                version=version,
            )
            is not None
        ):
            return version
        if (
            _matching_versioned_database_compatibility_source(
                engine,
                version=version,
            )
            is not None
        ):
            return version
        if version != CURRENT_SQLITE_SCHEMA_VERSION:
            raise SQLiteMigrationHistoryConflict(
                "SQLite migration history conflicts with the published catalog"
            )
        table_names = frozenset(inspect(engine).get_table_names())
        observer_tables = frozenset(
            table.name for table in ObserverProjectionBase.metadata.tables.values()
        ) | frozenset(
            table.name for table in ObserverSubscriptionBase.metadata.tables.values()
        )
        transport_table = ConnectorTransportCursorModel.__table__.name
        handshake_tables = frozenset(
            {
                ConnectorTransportHandshakeOwnershipModel.__table__.name,
                ConnectorObserverReceiptModel.__table__.name,
            }
        )
        catalog_tables = frozenset(
            table.name for table in SessionCatalogBase.metadata.tables.values()
        )
        session_identity_tables = frozenset(_SESSION_PROJECTION_V10_TABLES)
        legacy_remainder_signature = _schema_signature(
            engine,
            excluded_table_names=frozenset(
                {
                    SQLITE_MIGRATION_TABLE,
                    *session_identity_tables,
                    *observer_tables,
                        transport_table,
                        *handshake_tables,
                        *catalog_tables,
                }
            ),
        )
        source_matches = any(
            legacy_remainder_signature
            == (
                source.session_identity_remainder_schema_fingerprint,
                source.session_identity_remainder_raw_manifest_checksum,
            )
            for source in PUBLISHED_SQLITE_LEGACY_SOURCES
        )
        session_identity_matches = (
            _session_identity_schema_signature(engine)
            == PUBLISHED_SQLITE_CURRENT_SESSION_IDENTITY_SIGNATURE
        )
        observer_signature = _schema_signature(
            engine,
            excluded_table_names=table_names - observer_tables,
        )
        observer_matches = (
            observer_signature == PUBLISHED_SQLITE_CURRENT_OBSERVER_SIGNATURE
        )
        transport_signature = _schema_signature(
            engine,
            excluded_table_names=table_names - {transport_table},
        )
        transport_matches = (
            transport_signature == PUBLISHED_SQLITE_CONNECTOR_TRANSPORT_SIGNATURE
        )
        handshake_signature = _schema_signature(
            engine,
            excluded_table_names=table_names - handshake_tables,
        )
        handshake_matches = (
            handshake_signature == PUBLISHED_SQLITE_CONNECTOR_HANDSHAKE_SIGNATURE
        )
        catalog_signature = _schema_signature(
            engine,
            excluded_table_names=table_names - catalog_tables,
        )
        catalog_matches = (
            catalog_signature == PUBLISHED_SQLITE_SESSION_CATALOG_SIGNATURE
        )
        ledger_matches = _ledger_signature(engine) == (
            PUBLISHED_SQLITE_V1_LEDGER_SIGNATURE
        )
        if (
            not source_matches
            or not session_identity_matches
            or not observer_matches
            or not transport_matches
                or not handshake_matches
                or not catalog_matches
                or not ledger_matches
        ):
            raise SQLiteMigrationHistoryConflict(
                "SQLite migration history conflicts with the published catalog"
            )
    if version == CURRENT_SQLITE_SCHEMA_VERSION:
        _validate_current_session_identity_rows(engine)
    return version


def _validate_current_history(engine: Engine | Connection) -> None:
    if _validate_versioned_history(engine) != CURRENT_SQLITE_SCHEMA_VERSION:
        raise SQLiteMigrationHistoryConflict(
            "SQLite migration history conflicts with the published catalog"
        )


def plan_sqlite_schema(engine: Engine) -> SQLiteUpgradeResult:
    """Validate one existing database without changing schema or history."""

    verify_published_sqlite_catalog()
    schema_manifest = _raw_schema_manifest(engine)
    if not schema_manifest:
        return SQLiteUpgradeResult(
            schema_version=CURRENT_SQLITE_SCHEMA_VERSION,
            source="empty",
        )
    has_migration_ledger = any(
        schema_object["type"] == "table"
        and schema_object["name"] == SQLITE_MIGRATION_TABLE
        for schema_object in schema_manifest
    )
    if not has_migration_ledger:
        schema_fingerprint = sqlite_schema_fingerprint(engine)
        raw_manifest_checksum = sqlite_raw_manifest_checksum(engine)
        if not any(
            source.schema_fingerprint == schema_fingerprint
            and source.raw_manifest_checksum == raw_manifest_checksum
            for source in PUBLISHED_SQLITE_LEGACY_SOURCES
        ):
            raise SQLiteMigrationHistoryConflict(
                "SQLite migration history conflicts with the published catalog"
            )
        return SQLiteUpgradeResult(
            schema_version=CURRENT_SQLITE_SCHEMA_VERSION,
            source="legacy-current",
        )
    version = _validate_versioned_history(engine)
    compatibility_source = _matching_versioned_compatibility_source(
        engine,
        version=version,
    )
    database_compatibility_source = (
        _matching_versioned_database_compatibility_source(
            engine,
            version=version,
        )
    )
    return SQLiteUpgradeResult(
        schema_version=version,
        source=(
            "current"
            if version == CURRENT_SQLITE_SCHEMA_VERSION
            else database_compatibility_source.source
            if database_compatibility_source is not None
            else compatibility_source.source
            if compatibility_source is not None
            else f"versioned-{version}"
        ),
    )


def _observer_encryption_context(
    stored: ObserverSessionModel,
    *,
    field: str,
) -> ObserverEncryptionContext:
    return ObserverEncryptionContext(
        tenant_id=stored.tenant_id,
        agent_id=stored.agent_id,
        profile=stored.profile,
        session_key=stored.session_key,
        field=field,
        schema_version=1,
    )


def _encrypt_v2_observer_projection(
    session: Session,
    cipher: TenantObserverCipher | None,
) -> None:
    stored_sessions = session.scalars(select(ObserverSessionModel)).all()
    stored_events = session.scalars(select(ObserverEventModel)).all()
    if not stored_sessions and not stored_events:
        return
    if cipher is None:
        raise SQLiteMigrationHistoryConflict(
            "SQLite Observer v2 plaintext requires a tenant key migration"
        )
    sessions_by_id = {
        (stored.tenant_id, stored.session_id): stored for stored in stored_sessions
    }
    for stored in stored_sessions:
        try:
            CloudEnvelopeV1Adapter().decode_session_snapshot(
                {
                    "profile": stored.profile,
                    "runtime_generation": stored.runtime_generation,
                    "session_key": stored.session_key,
                    "runtime_session_id": stored.runtime_session_id,
                    "running": stored.running,
                    "status": stored.status,
                    "event_sequence": stored.snapshot_head_sequence,
                    "snapshot_event_sequence": stored.snapshot_event_sequence,
                    "messages": stored.messages,
                    "inflight": stored.inflight,
                    "replay_events": stored.replay_events,
                }
            )
        except ContractConformanceError as error:
            raise SQLiteMigrationHistoryConflict(
                "SQLite Observer v2 plaintext violates the published schema"
            ) from error
        stored.messages = cipher.encrypt_json(
            stored.messages,
            context=_observer_encryption_context(stored, field="messages"),
        )
        stored.inflight = cipher.encrypt_json(
            stored.inflight,
            context=_observer_encryption_context(stored, field="inflight"),
        )
        stored.replay_events = cipher.encrypt_json(
            stored.replay_events,
            context=_observer_encryption_context(stored, field="replay_events"),
        )
    for event in stored_events:
        stored = sessions_by_id.get((event.tenant_id, event.session_id))
        if stored is None:
            raise SQLiteMigrationHistoryConflict(
                "SQLite Observer v2 event has no authoritative session"
            )
        raw_event: dict[str, object] = {
            "profile": stored.profile,
            "runtime_generation": stored.runtime_generation,
            "session_key": event.session_key,
            "session_id": event.runtime_session_id,
            "type": event.event_type,
            "event_sequence": event.event_sequence,
            "payload": event.payload,
        }
        if event.event_sequence_start != event.event_sequence:
            raw_event["event_sequence_start"] = event.event_sequence_start
        try:
            CloudEnvelopeV1Adapter().decode_session_event(raw_event)
        except ContractConformanceError as error:
            raise SQLiteMigrationHistoryConflict(
                "SQLite Observer v2 plaintext violates the published schema"
            ) from error
        event.payload = cipher.encrypt_json(
            event.payload,
            context=_observer_encryption_context(
                stored,
                field=f"event.payload:{event.event_sequence}",
            ),
        )
    session.flush()


def _apply_migration_transaction(
    engine: Engine,
    migration: PublishedSQLiteMigration,
    *,
    create_business_schema: bool,
    planned_source: str,
    observer_cipher: TenantObserverCipher | None,
) -> None:
    del migration, create_business_schema
    with (
        engine.connect() as connection,
        connection.begin(),
        connection.begin_nested(),
    ):
        operations = Operations(MigrationContext.configure(connection))
        if not inspect(connection).has_table(SQLITE_MIGRATION_TABLE):
            operations.invoke(
                _deterministic_create_table_op(
                    SQLiteSchemaMigration.__table__,
                )
            )
        _revalidate_planned_source_after_guard(connection, planned_source)
        with Session(
            bind=connection,
            join_transaction_mode="create_savepoint",
        ) as session:
            if planned_source == "empty":
                version = 0
            elif planned_source == "legacy-current":
                baseline = PUBLISHED_SQLITE_MIGRATIONS[0]
                session.add(
                    SQLiteSchemaMigration(
                        version=baseline.version,
                        name=baseline.name,
                        checksum=baseline.checksum,
                        applied_at=datetime.now(UTC),
                    )
                )
                version = baseline.version
            elif planned_source == "versioned-5-compatible":
                version = 5
            elif planned_source.startswith("versioned-"):
                version = int(planned_source.removeprefix("versioned-"))
            else:
                raise SQLiteMigrationHistoryConflict(
                    "SQLite migration history conflicts with the published catalog"
                )
            for pending in PUBLISHED_SQLITE_MIGRATIONS:
                if pending.version <= version:
                    continue
                legacy_tables_already_present = planned_source == "legacy-current" and (
                    (
                        pending.version == 4
                        and inspect(connection).has_table(
                            ConnectorTransportCursorModel.__table__.name
                        )
                    )
                    or (
                        pending.version == 5
                        and all(
                            inspect(connection).has_table(model.__table__.name)
                            for model in (
                                ConnectorTransportHandshakeOwnershipModel,
                                ConnectorObserverReceiptModel,
                            )
                        )
                    )
                )
                if not legacy_tables_already_present:
                    pending.upgrade(operations)
                if pending.version == 3 and version == 2:
                    _encrypt_v2_observer_projection(session, observer_cipher)
                session.add(
                    SQLiteSchemaMigration(
                        version=pending.version,
                        name=pending.name,
                        checksum=pending.checksum,
                        applied_at=datetime.now(UTC),
                    )
                )
            session.flush()
            session.commit()
        _validate_current_history(connection)


def _revalidate_planned_source_after_guard(
    connection: Connection,
    planned_source: str,
) -> None:
    excluded_ledger = frozenset({SQLITE_MIGRATION_TABLE})
    if planned_source == "empty":
        source_matches = not _raw_schema_manifest(
            connection,
            excluded_table_names=excluded_ledger,
        )
    elif planned_source == "legacy-current":
        schema_fingerprint = sqlite_schema_fingerprint(
            connection,
            excluded_table_names=excluded_ledger,
        )
        raw_manifest_checksum = sqlite_raw_manifest_checksum(
            connection,
            excluded_table_names=excluded_ledger,
        )
        source_matches = any(
            source.schema_fingerprint == schema_fingerprint
            and source.raw_manifest_checksum == raw_manifest_checksum
            for source in PUBLISHED_SQLITE_LEGACY_SOURCES
        )
    elif planned_source == "versioned-5-compatible":
        compatibility_source = _matching_versioned_compatibility_source(
            connection,
            version=5,
        )
        source_matches = (
            _validate_versioned_history(connection) == 5
            and compatibility_source is not None
            and compatibility_source.source == planned_source
        )
    elif planned_source.startswith("versioned-"):
        try:
            expected_version = int(planned_source.removeprefix("versioned-"))
        except ValueError:
            source_matches = False
        else:
            source_matches = _validate_versioned_history(connection) == expected_version
    else:
        source_matches = False
    if not source_matches:
        raise SQLiteMigrationHistoryConflict(
            "SQLite migration history conflicts with the published catalog"
        )


def _is_concurrent_migration_collision(
    error: IntegrityError | OperationalError,
) -> bool:
    message = str(error.orig).casefold()
    return any(
        marker in message
        for marker in (
            "already exists",
            "database is locked",
            "database schema is locked",
            "unique constraint failed: hermes_sqlite_schema_migrations.",
        )
    )


def _apply_or_observe_concurrent_current(
    engine: Engine,
    migration: PublishedSQLiteMigration,
    *,
    create_business_schema: bool,
    planned_source: str,
    observer_cipher: TenantObserverCipher | None,
) -> SQLiteUpgradeResult | None:
    try:
        _apply_migration_transaction(
            engine,
            migration,
            create_business_schema=create_business_schema,
            planned_source=planned_source,
            observer_cipher=observer_cipher,
        )
    except (IntegrityError, OperationalError) as error:
        if not _is_concurrent_migration_collision(error):
            raise
        deadline = monotonic() + _CONCURRENT_CONVERGENCE_TIMEOUT_SECONDS
        while True:
            try:
                revalidated = plan_sqlite_schema(engine)
            except Exception:  # noqa: BLE001 - preserve the original collision.
                revalidated = None
            if revalidated is not None and revalidated.source == "current":
                return revalidated
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise error from None
            sleep(min(_CONCURRENT_CONVERGENCE_POLL_SECONDS, remaining))
    return None


def upgrade_sqlite_schema(
    engine: Engine,
    *,
    observer_cipher: TenantObserverCipher | None = None,
) -> SQLiteUpgradeResult:
    """Upgrade an empty, legacy-v1, or versioned SQLite store to current."""

    plan = plan_sqlite_schema(engine)
    original_source = plan.source
    if plan.source == "current":
        return plan
    if not (
        plan.source in {"empty", "legacy-current"}
        or plan.source.startswith("versioned-")
    ):
        raise SQLiteMigrationHistoryConflict(
            "SQLite migration history conflicts with the published catalog"
        )
    concurrent_result = _apply_or_observe_concurrent_current(
        engine,
        PUBLISHED_SQLITE_MIGRATIONS[0],
        create_business_schema=True,
        planned_source=plan.source,
        observer_cipher=observer_cipher,
    )
    if concurrent_result is not None:
        return concurrent_result
    return SQLiteUpgradeResult(
        schema_version=CURRENT_SQLITE_SCHEMA_VERSION,
        source=original_source,
    )
