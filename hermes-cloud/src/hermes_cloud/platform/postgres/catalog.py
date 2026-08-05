"""Immutable PostgreSQL v1 catalog built from typed SQLAlchemy operations."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from enum import Enum
from hashlib import sha256
from types import MappingProxyType
from typing import Final

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    MetaData,
    String,
    Table,
    case,
    false,
    literal,
    or_,
    update,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.schema import (
    AddConstraint,
    CreateIndex,
    CreateSchema,
    CreateTable,
    ForeignKeyConstraint,
    UniqueConstraint,
)
from sqlalchemy.sql.base import Executable

from hermes_cloud.domain.migrations import (
    PUBLISHED_POSTGRES_MIGRATIONS,
    PublishedMigration,
    verify_published_migration_registry,
)
from hermes_cloud.platform.postgres.ddl import (
    AddTableColumn,
    AlterDefaultTablePrivileges,
    AlterRuntimeRole,
    AssertTablesEmpty,
    CreateTenantIsolationPolicy,
    CreateTransactionContextFunction,
    DatabasePrivilege,
    DropTableColumn,
    DropTableConstraint,
    EnableRowLevelSecurity,
    ForceRowLevelSecurity,
    GrantAllTablesPrivileges,
    GrantFunctionExecute,
    GrantSchemaUsage,
    GrantTablePrivileges,
    RevokeAllTablesPrivileges,
    RevokeDatabasePrivileges,
    RevokePublicDatabaseTemporary,
    RevokePublicSchemaCreate,
    RevokeSchemaCreate,
    RevokeTablePrivileges,
    SetTableColumnNotNull,
    TablePrivilege,
)
from hermes_cloud.platform.postgres.models import (
    AccessGrantModel,
    AgentModel,
    AuditEventModel,
    CommandAttemptModel,
    CommandModel,
    CommandTransitionModel,
    ConnectorObserverReceiptModel,
    ConnectorTransportCursorModel,
    ConnectorTransportHandshakeOwnershipModel,
    DeviceAuthenticationChallengeModel,
    DeviceCredentialModel,
    DeviceCredentialPublicKeyModel,
    DeviceLifecycleModel,
    DeviceModel,
    InboxMessageModel,
    MembershipModel,
    OutboxEventModel,
    PairingClaimLimitModel,
    PairingEnrollmentProofModel,
    PairingIdempotencyModel,
    PairingOfferModel,
    PairingSessionModel,
    PasswordCredentialModel,
    PolicyModel,
    RefreshSessionModel,
    RoleModel,
    SessionProjectionModel,
    TenantModel,
    UserModel,
    WebSocketTicketModel,
    WorkspaceMembershipModel,
    WorkspaceModel,
)
from hermes_cloud.platform.sqlalchemy.session_catalog_migration_models import (
    SessionCatalogAuthorityV12Model,
    SessionCatalogEntryV12Model,
    SessionCatalogGenerationV12Model,
    SessionCatalogInboxV12Model,
    SessionCatalogSnapshotPageV12Model,
)
from hermes_cloud.platform.sqlalchemy.session_catalog_models import (
    SessionCatalogAuthorityModel,
    SessionCatalogInboxModel,
    SessionCatalogSnapshotPageModel,
)
from hermes_cloud.platform.sqlalchemy.session_projection_migration_models import (
    SessionEventProjectionV10Model,
    SessionMessageProjectionV10Model,
    SessionProjectionCursorV10Model,
    SessionProjectionV10Model,
    WebSocketTicketV10Model,
)

_CHECKSUM = re.compile(r"^[0-9a-f]{64}$")
_VARIABLE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_POSTGRESQL_DIALECT = postgresql.dialect()
_StatementFactory = Callable[[Mapping[str, str]], Executable]


class MigrationPhase(str, Enum):
    """The fixed order for safe online schema evolution."""

    EXPAND = "expand"
    MIGRATE = "migrate"
    CONTRACT = "contract"


@dataclass(frozen=True, slots=True)
class MigrationOperation:
    """One typed statement in an immutable migration plan."""

    phase: MigrationPhase
    key: str
    variables: tuple[str, ...]
    _factory: _StatementFactory = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if not self.key or "\n" in self.key or "|" in self.key:
            raise ValueError("migration operation key must be stable and non-empty")
        if len(set(self.variables)) != len(self.variables):
            raise ValueError("migration operation variables must be unique")
        if any(_VARIABLE.fullmatch(variable) is None for variable in self.variables):
            raise ValueError("migration operation variable names must be stable")

    def statement(self, identifiers: Mapping[str, str]) -> Executable:
        missing = set(self.variables).difference(identifiers)
        if missing:
            raise ValueError("migration operation identifier bindings are incomplete")
        return self._factory(identifiers)

    @property
    def canonical_key(self) -> str:
        variables = ",".join(self.variables)
        identifiers = {variable: variable for variable in self.variables}
        compiled = self.statement(identifiers).compile(dialect=_POSTGRESQL_DIALECT)
        return f"{self.phase.value}|{self.key}|{variables}|{compiled}"


@dataclass(frozen=True, slots=True)
class MigrationPlan:
    """An ordered expand, migrate, contract plan with a frozen ledger checksum."""

    checksum: str
    expand: tuple[MigrationOperation, ...] = ()
    migrate: tuple[MigrationOperation, ...] = ()
    contract: tuple[MigrationOperation, ...] = ()

    def __post_init__(self) -> None:
        if _CHECKSUM.fullmatch(self.checksum) is None:
            raise ValueError("migration plan checksum must be SHA-256")
        phase_groups = (
            (MigrationPhase.EXPAND, self.expand),
            (MigrationPhase.MIGRATE, self.migrate),
            (MigrationPhase.CONTRACT, self.contract),
        )
        for phase, operations in phase_groups:
            if any(operation.phase is not phase for operation in operations):
                raise ValueError("migration operation is in the wrong phase")
        keys = tuple(operation.key for operation in self.operations)
        if len(set(keys)) != len(keys):
            raise ValueError("migration operation keys must be unique within a plan")

    @property
    def operations(self) -> tuple[MigrationOperation, ...]:
        return self.expand + self.migrate + self.contract

    @property
    def structural_digest(self) -> str:
        payload = "\n".join(operation.canonical_key for operation in self.operations)
        return sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class Migration:
    """One immutable, ordered PostgreSQL migration."""

    version: int
    name: str
    plan: MigrationPlan
    variables: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.version < 1:
            raise ValueError("migration version must be positive")
        if len(set(self.variables)) != len(self.variables):
            raise ValueError("migration variables must be unique")
        operation_variables = {
            variable
            for operation in self.plan.operations
            for variable in operation.variables
        }
        if operation_variables != set(self.variables):
            raise ValueError("migration variables must match its typed operations")

    @property
    def checksum(self) -> str:
        return self.plan.checksum

    @property
    def metadata(self) -> PublishedMigration:
        return PublishedMigration(
            version=self.version,
            name=self.name,
            checksum=self.checksum,
            variables=self.variables,
        )


def _static_operation(
    phase: MigrationPhase,
    key: str,
    statement: Executable,
) -> MigrationOperation:
    return MigrationOperation(
        phase=phase,
        key=key,
        variables=(),
        _factory=lambda _: statement,
    )


def _bound_operation(
    phase: MigrationPhase,
    key: str,
    variables: tuple[str, ...],
    factory: _StatementFactory,
) -> MigrationOperation:
    return MigrationOperation(
        phase=phase,
        key=key,
        variables=variables,
        _factory=factory,
    )


def _schema_operation(schema: str) -> MigrationOperation:
    return _static_operation(
        MigrationPhase.EXPAND,
        f"schema:{schema}",
        CreateSchema(schema),
    )


def _table_operations(models: Sequence[type[object]]) -> tuple[MigrationOperation, ...]:
    operations: list[MigrationOperation] = []
    for model in models:
        table = model.__table__  # type: ignore[attr-defined]
        qualified_name = f"{table.schema}.{table.name}"
        operations.append(
            _static_operation(
                MigrationPhase.EXPAND,
                f"table:{qualified_name}",
                CreateTable(table),
            )
        )
        for index in sorted(table.indexes, key=lambda item: item.name or ""):
            operations.append(
                _static_operation(
                    MigrationPhase.EXPAND,
                    f"index:{index.name}",
                    CreateIndex(index),
                )
            )
    return tuple(operations)


def _tenant_security_operations(
    models: Sequence[type[object]],
) -> tuple[MigrationOperation, ...]:
    operations: list[MigrationOperation] = []
    for model in models:
        table = model.__table__  # type: ignore[attr-defined]
        schema = str(table.schema)
        name = str(table.name)
        qualified_name = f"{schema}.{name}"
        operations.extend(
            (
                _static_operation(
                    MigrationPhase.CONTRACT,
                    f"rls-enable:{qualified_name}",
                    EnableRowLevelSecurity(schema, name),
                ),
                _static_operation(
                    MigrationPhase.CONTRACT,
                    f"rls-force:{qualified_name}",
                    ForceRowLevelSecurity(schema, name),
                ),
                _static_operation(
                    MigrationPhase.CONTRACT,
                    f"rls-policy:{qualified_name}",
                    CreateTenantIsolationPolicy(schema, name),
                ),
            )
        )
    return tuple(operations)


_FOUNDATION_MODELS: Final = (
    TenantModel,
    UserModel,
    MembershipModel,
    AgentModel,
    DeviceModel,
    CommandModel,
    CommandAttemptModel,
    CommandTransitionModel,
    PolicyModel,
    AccessGrantModel,
    OutboxEventModel,
    InboxMessageModel,
    AuditEventModel,
)

_GAP_MODELS: Final = (
    RoleModel,
    WorkspaceModel,
    WorkspaceMembershipModel,
    DeviceCredentialModel,
    PairingSessionModel,
)

_BASE_SCHEMAS: Final = (
    "identity",
    "device",
    "command",
    "authorization",
    "platform",
    "audit",
)

_DATA_SCHEMAS: Final = (
    "identity",
    "device",
    "command",
    "authorization",
    "platform",
)

_DML_PRIVILEGES: Final = (
    TablePrivilege.SELECT,
    TablePrivilege.INSERT,
    TablePrivilege.UPDATE,
    TablePrivilege.DELETE,
)

_APPEND_PRIVILEGES: Final = (
    TablePrivilege.SELECT,
    TablePrivilege.INSERT,
)

_UNSAFE_PRIVILEGES: Final = (
    TablePrivilege.TRUNCATE,
    TablePrivilege.REFERENCES,
    TablePrivilege.TRIGGER,
)

_AUDIT_MUTATION_PRIVILEGES: Final = (
    TablePrivilege.UPDATE,
    TablePrivilege.DELETE,
    TablePrivilege.TRUNCATE,
    TablePrivilege.REFERENCES,
    TablePrivilege.TRIGGER,
)


def _runtime_role_operations() -> tuple[MigrationOperation, ...]:
    contract: list[MigrationOperation] = [
        _bound_operation(
            MigrationPhase.CONTRACT,
            "role:runtime-hardening",
            ("runtime_role",),
            lambda values: AlterRuntimeRole(values["runtime_role"]),
        ),
        _bound_operation(
            MigrationPhase.CONTRACT,
            "database:runtime-revoke-create-temporary",
            ("database_name", "runtime_role"),
            lambda values: RevokeDatabasePrivileges(
                values["database_name"],
                values["runtime_role"],
                (DatabasePrivilege.CREATE, DatabasePrivilege.TEMPORARY),
            ),
        ),
    ]
    for schema in _BASE_SCHEMAS:
        contract.extend(
            (
                _bound_operation(
                    MigrationPhase.CONTRACT,
                    f"schema:{schema}:runtime-revoke-create",
                    ("runtime_role",),
                    lambda values, schema=schema: RevokeSchemaCreate(
                        schema,
                        values["runtime_role"],
                    ),
                ),
                _bound_operation(
                    MigrationPhase.CONTRACT,
                    f"schema:{schema}:runtime-grant-usage",
                    ("runtime_role",),
                    lambda values, schema=schema: GrantSchemaUsage(
                        schema,
                        values["runtime_role"],
                    ),
                ),
            )
        )
    for schema in _DATA_SCHEMAS:
        contract.extend(
            (
                _bound_operation(
                    MigrationPhase.CONTRACT,
                    f"schema:{schema}:runtime-grant-dml",
                    ("runtime_role",),
                    lambda values, schema=schema: GrantAllTablesPrivileges(
                        schema,
                        values["runtime_role"],
                        _DML_PRIVILEGES,
                    ),
                ),
                _bound_operation(
                    MigrationPhase.CONTRACT,
                    f"schema:{schema}:runtime-revoke-unsafe",
                    ("runtime_role",),
                    lambda values, schema=schema: RevokeAllTablesPrivileges(
                        schema,
                        values["runtime_role"],
                        _UNSAFE_PRIVILEGES,
                    ),
                ),
                _bound_operation(
                    MigrationPhase.CONTRACT,
                    f"schema:{schema}:runtime-default-dml",
                    ("migration_role", "runtime_role"),
                    lambda values, schema=schema: AlterDefaultTablePrivileges(
                        values["migration_role"],
                        schema,
                        values["runtime_role"],
                        _DML_PRIVILEGES,
                    ),
                ),
            )
        )
    contract.extend(
        (
            _bound_operation(
                MigrationPhase.CONTRACT,
                "table:audit.audit_events:runtime-grant-append",
                ("runtime_role",),
                lambda values: GrantTablePrivileges(
                    "audit",
                    "audit_events",
                    values["runtime_role"],
                    _APPEND_PRIVILEGES,
                ),
            ),
            _bound_operation(
                MigrationPhase.CONTRACT,
                "table:audit.audit_events:runtime-revoke-mutation",
                ("runtime_role",),
                lambda values: RevokeTablePrivileges(
                    "audit",
                    "audit_events",
                    values["runtime_role"],
                    _AUDIT_MUTATION_PRIVILEGES,
                ),
            ),
            _bound_operation(
                MigrationPhase.CONTRACT,
                "function:transaction-context:runtime-grant-execute",
                ("runtime_role",),
                lambda values: GrantFunctionExecute(values["runtime_role"]),
            ),
            _bound_operation(
                MigrationPhase.CONTRACT,
                "schema:audit:runtime-default-append",
                ("migration_role", "runtime_role"),
                lambda values: AlterDefaultTablePrivileges(
                    values["migration_role"],
                    "audit",
                    values["runtime_role"],
                    _APPEND_PRIVILEGES,
                ),
            ),
        )
    )
    return tuple(contract)


def _workspace_role_operations() -> tuple[MigrationOperation, ...]:
    return (
        _bound_operation(
            MigrationPhase.CONTRACT,
            "schema:workspace:runtime-revoke-create",
            ("runtime_role",),
            lambda values: RevokeSchemaCreate(
                "workspace",
                values["runtime_role"],
            ),
        ),
        _bound_operation(
            MigrationPhase.CONTRACT,
            "schema:workspace:runtime-grant-usage",
            ("runtime_role",),
            lambda values: GrantSchemaUsage(
                "workspace",
                values["runtime_role"],
            ),
        ),
        _bound_operation(
            MigrationPhase.CONTRACT,
            "schema:workspace:runtime-grant-dml",
            ("runtime_role",),
            lambda values: GrantAllTablesPrivileges(
                "workspace",
                values["runtime_role"],
                _DML_PRIVILEGES,
            ),
        ),
        _bound_operation(
            MigrationPhase.CONTRACT,
            "schema:workspace:runtime-revoke-unsafe",
            ("runtime_role",),
            lambda values: RevokeAllTablesPrivileges(
                "workspace",
                values["runtime_role"],
                _UNSAFE_PRIVILEGES,
            ),
        ),
        _bound_operation(
            MigrationPhase.CONTRACT,
            "schema:workspace:runtime-default-dml",
            ("migration_role", "runtime_role"),
            lambda values: AlterDefaultTablePrivileges(
                values["migration_role"],
                "workspace",
                values["runtime_role"],
                _DML_PRIVILEGES,
            ),
        ),
    )


def _projection_role_operations() -> tuple[MigrationOperation, ...]:
    return (
        _bound_operation(
            MigrationPhase.CONTRACT,
            "schema:projection:runtime-revoke-create",
            ("runtime_role",),
            lambda values: RevokeSchemaCreate(
                "projection",
                values["runtime_role"],
            ),
        ),
        _bound_operation(
            MigrationPhase.CONTRACT,
            "schema:projection:runtime-grant-usage",
            ("runtime_role",),
            lambda values: GrantSchemaUsage(
                "projection",
                values["runtime_role"],
            ),
        ),
        _bound_operation(
            MigrationPhase.CONTRACT,
            "schema:projection:runtime-grant-dml",
            ("runtime_role",),
            lambda values: GrantAllTablesPrivileges(
                "projection",
                values["runtime_role"],
                _DML_PRIVILEGES,
            ),
        ),
        _bound_operation(
            MigrationPhase.CONTRACT,
            "schema:projection:runtime-revoke-unsafe",
            ("runtime_role",),
            lambda values: RevokeAllTablesPrivileges(
                "projection",
                values["runtime_role"],
                _UNSAFE_PRIVILEGES,
            ),
        ),
        _bound_operation(
            MigrationPhase.CONTRACT,
            "schema:projection:runtime-default-dml",
            ("migration_role", "runtime_role"),
            lambda values: AlterDefaultTablePrivileges(
                values["migration_role"],
                "projection",
                values["runtime_role"],
                _DML_PRIVILEGES,
            ),
        ),
    )


_FOUNDATION_EXPAND = tuple(_schema_operation(schema) for schema in _BASE_SCHEMAS) + (
    _static_operation(
        MigrationPhase.EXPAND,
        "function:platform.set_transaction_context",
        CreateTransactionContextFunction(),
    ),
)

_TENANT_STORAGE_EXPAND = _table_operations(_FOUNDATION_MODELS)
_TENANT_STORAGE_CONTRACT = _tenant_security_operations(_FOUNDATION_MODELS)
_GAP_EXPAND = (_schema_operation("workspace"),) + _table_operations(_GAP_MODELS)
_GAP_CONTRACT = _tenant_security_operations(_GAP_MODELS)
_V7_MODELS: Final = (
    SessionProjectionV10Model,
    SessionMessageProjectionV10Model,
    SessionEventProjectionV10Model,
    SessionProjectionCursorV10Model,
    PasswordCredentialModel,
    RefreshSessionModel,
    WebSocketTicketV10Model,
)
_V7_EXPAND = (_schema_operation("projection"),) + _table_operations(_V7_MODELS)
_V7_CONTRACT = _tenant_security_operations(_V7_MODELS) + _projection_role_operations()
_V8_MODELS: Final = (
    PairingOfferModel,
    DeviceLifecycleModel,
    PairingEnrollmentProofModel,
    PairingClaimLimitModel,
    PairingIdempotencyModel,
    DeviceCredentialPublicKeyModel,
    DeviceAuthenticationChallengeModel,
)
_V8_TENANT_MODELS: Final = (
    DeviceLifecycleModel,
    PairingEnrollmentProofModel,
    PairingClaimLimitModel,
    DeviceCredentialPublicKeyModel,
    DeviceAuthenticationChallengeModel,
)
_V8_EXPAND = _table_operations(_V8_MODELS)
_V8_CONTRACT = _tenant_security_operations(_V8_TENANT_MODELS)
_V9_MODELS: Final = (ConnectorTransportCursorModel,)
_V9_EXPAND = _table_operations(_V9_MODELS)
_V9_CONTRACT = _tenant_security_operations(_V9_MODELS)
_V10_MODELS: Final = (
    ConnectorTransportHandshakeOwnershipModel,
    ConnectorObserverReceiptModel,
)
_V10_EXPAND = _table_operations(_V10_MODELS)
_V10_CONTRACT = _tenant_security_operations(_V10_MODELS)
_V12_MODELS: Final = (
    SessionCatalogAuthorityV12Model,
    SessionCatalogGenerationV12Model,
    SessionCatalogSnapshotPageV12Model,
    SessionCatalogEntryV12Model,
    SessionCatalogInboxV12Model,
)
_V12_EXPAND = _table_operations(_V12_MODELS)
_V12_CONTRACT = _tenant_security_operations(_V12_MODELS)


def _index_named(model: type[object], name: str):
    table = model.__table__  # type: ignore[attr-defined]
    matches = tuple(index for index in table.indexes if index.name == name)
    if len(matches) != 1:
        raise RuntimeError("current model index is not uniquely defined")
    return matches[0]


def _detached_session_catalog_inbox_check(name: str) -> CheckConstraint:
    table = Table(
        "session_catalog_inbox",
        MetaData(),
        Column("receipt_state", String(16)),
        Column("dispatch_sequence", BigInteger()),
        Column("dispatch_attempts", BigInteger()),
        schema="projection",
    )
    expressions = {
        "session_catalog_inbox_receipt_state_check": or_(
            table.c.receipt_state.is_(None),
            table.c.receipt_state.in_(("pending", "settled", "retired")),
        ),
        "session_catalog_inbox_dispatch_sequence_check": or_(
            table.c.dispatch_sequence.is_(None),
            table.c.dispatch_sequence >= 0,
        ),
        "session_catalog_inbox_dispatch_attempts_check": (
            table.c.dispatch_attempts >= 0
        ),
    }
    try:
        constraint = CheckConstraint(expressions[name], name=name)
    except KeyError:
        raise RuntimeError("current model constraint is not uniquely defined") from None
    table.append_constraint(constraint)
    return constraint


_V13_SESSION_CATALOG_EXPAND: Final = (
    _static_operation(
        MigrationPhase.EXPAND,
        "column:projection.session_catalog_authorities.staging_deadline:add",
        AddTableColumn(
            "projection",
            "session_catalog_authorities",
            Column("staging_deadline", DateTime(timezone=True), nullable=True),
        ),
    ),
    _static_operation(
        MigrationPhase.EXPAND,
        "column:projection.session_catalog_authorities.require_full_snapshot:add",
        AddTableColumn(
            "projection",
            "session_catalog_authorities",
            Column(
                "require_full_snapshot",
                Boolean(),
                nullable=False,
                server_default=false(),
            ),
        ),
    ),
    _static_operation(
        MigrationPhase.EXPAND,
        "column:projection.session_catalog_inbox.retention_until:add",
        AddTableColumn(
            "projection",
            "session_catalog_inbox",
            Column("retention_until", DateTime(timezone=True), nullable=True),
        ),
    ),
    _static_operation(
        MigrationPhase.EXPAND,
        "column:projection.session_catalog_inbox.receipt_state:add",
        AddTableColumn(
            "projection",
            "session_catalog_inbox",
            Column("receipt_state", String(16), nullable=True),
        ),
    ),
    _static_operation(
        MigrationPhase.EXPAND,
        "column:projection.session_catalog_inbox.dispatch_connection_id:add",
        AddTableColumn(
            "projection",
            "session_catalog_inbox",
            Column(
                "dispatch_connection_id",
                PG_UUID(as_uuid=True),
                nullable=True,
            ),
        ),
    ),
    _static_operation(
        MigrationPhase.EXPAND,
        "column:projection.session_catalog_inbox.dispatch_message_id:add",
        AddTableColumn(
            "projection",
            "session_catalog_inbox",
            Column("dispatch_message_id", PG_UUID(as_uuid=True), nullable=True),
        ),
    ),
    _static_operation(
        MigrationPhase.EXPAND,
        "column:projection.session_catalog_inbox.dispatch_sequence:add",
        AddTableColumn(
            "projection",
            "session_catalog_inbox",
            Column("dispatch_sequence", BigInteger(), nullable=True),
        ),
    ),
    _static_operation(
        MigrationPhase.EXPAND,
        "column:projection.session_catalog_inbox.dispatch_attempts:add",
        AddTableColumn(
            "projection",
            "session_catalog_inbox",
            Column("dispatch_attempts", BigInteger(), nullable=True),
        ),
    ),
    _static_operation(
        MigrationPhase.EXPAND,
        "column:projection.session_catalog_inbox.updated_at:add",
        AddTableColumn(
            "projection",
            "session_catalog_inbox",
            Column("updated_at", DateTime(timezone=True), nullable=True),
        ),
    ),
    _static_operation(
        MigrationPhase.EXPAND,
        "column:projection.session_catalog_inbox.receipt_sent_at:add",
        AddTableColumn(
            "projection",
            "session_catalog_inbox",
            Column("receipt_sent_at", DateTime(timezone=True), nullable=True),
        ),
    ),
    _static_operation(
        MigrationPhase.EXPAND,
        "column:projection.session_catalog_inbox.receipt_settled_at:add",
        AddTableColumn(
            "projection",
            "session_catalog_inbox",
            Column("receipt_settled_at", DateTime(timezone=True), nullable=True),
        ),
    ),
    _static_operation(
        MigrationPhase.EXPAND,
        "column:projection.session_catalog_inbox.receipt_retired_at:add",
        AddTableColumn(
            "projection",
            "session_catalog_inbox",
            Column("receipt_retired_at", DateTime(timezone=True), nullable=True),
        ),
    ),
    _static_operation(
        MigrationPhase.EXPAND,
        "column:projection.session_catalog_inbox.receipt_retirement_reason:add",
        AddTableColumn(
            "projection",
            "session_catalog_inbox",
            Column("receipt_retirement_reason", String(64), nullable=True),
        ),
    ),
)
_V13_SESSION_CATALOG_MIGRATE: Final = (
    _static_operation(
        MigrationPhase.MIGRATE,
        "data:projection.session_catalog_authorities.staging_deadline:backfill",
        update(SessionCatalogAuthorityModel.__table__)
        .where(
            SessionCatalogAuthorityModel.staging_snapshot_id.is_not(None),
            SessionCatalogAuthorityModel.staging_deadline.is_(None),
        )
        .values(
            staging_deadline=(
                SessionCatalogAuthorityModel.updated_at
                + literal(timedelta(minutes=10))
            )
        ),
    ),
    _static_operation(
        MigrationPhase.MIGRATE,
        "data:projection.session_catalog_inbox.retention_until:backfill",
        update(SessionCatalogInboxModel.__table__)
        .where(SessionCatalogInboxModel.retention_until.is_(None))
        .values(
            retention_until=(
                SessionCatalogInboxModel.received_at
                + literal(timedelta(days=7))
            )
        ),
    ),
    _static_operation(
        MigrationPhase.MIGRATE,
        "data:projection.session_catalog_inbox.receipt_dispatch:backfill",
        update(SessionCatalogInboxModel.__table__).values(
            receipt_state=case(
                (SessionCatalogInboxModel.receipt_type.is_not(None), "settled"),
                else_=None,
            ),
            dispatch_attempts=0,
            updated_at=SessionCatalogInboxModel.received_at,
            receipt_sent_at=case(
                (
                    SessionCatalogInboxModel.receipt_type.is_not(None),
                    SessionCatalogInboxModel.received_at,
                ),
                else_=None,
            ),
            receipt_settled_at=case(
                (
                    SessionCatalogInboxModel.receipt_type.is_not(None),
                    SessionCatalogInboxModel.received_at,
                ),
                else_=None,
            ),
        ),
    ),
)
_V13_SESSION_CATALOG_CONTRACT: Final = (
    _static_operation(
        MigrationPhase.CONTRACT,
        "column:projection.session_catalog_inbox.retention_until:not-null",
        SetTableColumnNotNull(
            "projection",
            "session_catalog_inbox",
            "retention_until",
        ),
    ),
    _static_operation(
        MigrationPhase.CONTRACT,
        "column:projection.session_catalog_inbox.dispatch_attempts:not-null",
        SetTableColumnNotNull(
            "projection",
            "session_catalog_inbox",
            "dispatch_attempts",
        ),
    ),
    _static_operation(
        MigrationPhase.CONTRACT,
        "column:projection.session_catalog_inbox.updated_at:not-null",
        SetTableColumnNotNull(
            "projection",
            "session_catalog_inbox",
            "updated_at",
        ),
    ),
    _static_operation(
        MigrationPhase.CONTRACT,
        "constraint:projection.session_catalog_inbox.receipt_state:add",
        AddConstraint(
            _detached_session_catalog_inbox_check(
                "session_catalog_inbox_receipt_state_check",
            )
        ),
    ),
    _static_operation(
        MigrationPhase.CONTRACT,
        "constraint:projection.session_catalog_inbox.dispatch_sequence:add",
        AddConstraint(
            _detached_session_catalog_inbox_check(
                "session_catalog_inbox_dispatch_sequence_check",
            )
        ),
    ),
    _static_operation(
        MigrationPhase.CONTRACT,
        "constraint:projection.session_catalog_inbox.dispatch_attempts:add",
        AddConstraint(
            _detached_session_catalog_inbox_check(
                "session_catalog_inbox_dispatch_attempts_check",
            )
        ),
    ),
    _static_operation(
        MigrationPhase.CONTRACT,
        "index:session_catalog_authority_recovery_idx",
        CreateIndex(
            _index_named(
                SessionCatalogAuthorityModel,
                "session_catalog_authority_recovery_idx",
            )
        ),
    ),
    _static_operation(
        MigrationPhase.CONTRACT,
        "index:session_catalog_inbox_retention_idx",
        CreateIndex(
            _index_named(
                SessionCatalogInboxModel,
                "session_catalog_inbox_retention_idx",
            )
        ),
    ),
    _static_operation(
        MigrationPhase.CONTRACT,
        "index:session_catalog_inbox_pending_receipt_idx",
        CreateIndex(
            _index_named(
                SessionCatalogInboxModel,
                "session_catalog_inbox_pending_receipt_idx",
            )
        ),
    ),
    _static_operation(
        MigrationPhase.CONTRACT,
        "index:session_catalog_snapshot_page_retention_idx",
        CreateIndex(
            _index_named(
                SessionCatalogSnapshotPageModel,
                "session_catalog_snapshot_page_retention_idx",
            )
        ),
    ),
)


def _constraint_for_columns(
    model: type[object],
    constraint_type: type[ForeignKeyConstraint | UniqueConstraint],
    columns: tuple[str, ...],
) -> ForeignKeyConstraint | UniqueConstraint:
    table = model.__table__  # type: ignore[attr-defined]
    matches = tuple(
        constraint
        for constraint in table.constraints
        if isinstance(constraint, constraint_type)
        and tuple(column.name for column in constraint.columns) == columns
    )
    if len(matches) != 1:
        raise RuntimeError("current model constraint is not uniquely defined")
    return matches[0]


_V11_SESSION_IDENTITY_EXPAND: Final = (
    _static_operation(
        MigrationPhase.EXPAND,
        "assert-empty:session-identity-v10",
        AssertTablesEmpty(
            (("projection", "sessions"), ("identity", "websocket_tickets"))
        ),
    ),
    _static_operation(
        MigrationPhase.EXPAND,
        "column:projection.sessions.profile:add",
        AddTableColumn(
            "projection",
            "sessions",
            Column("profile", String(128), nullable=True),
        ),
    ),
    _static_operation(
        MigrationPhase.EXPAND,
        "column:identity.websocket_tickets.session_id:add",
        AddTableColumn(
            "identity",
            "websocket_tickets",
            Column("session_id", PG_UUID(as_uuid=True), nullable=True),
        ),
    ),
)
_V11_SESSION_IDENTITY_CONTRACT: Final = (
    _static_operation(
        MigrationPhase.CONTRACT,
        "constraint:projection.sessions.tenant-session-key:drop",
        DropTableConstraint(
            "projection",
            "sessions",
            "sessions_tenant_id_session_key_key",
        ),
    ),
    _static_operation(
        MigrationPhase.CONTRACT,
        "constraint:identity.websocket_tickets.tenant-session-key:drop",
        DropTableConstraint(
            "identity",
            "websocket_tickets",
            "websocket_tickets_tenant_id_session_key_fkey",
        ),
    ),
    _static_operation(
        MigrationPhase.CONTRACT,
        "column:projection.sessions.profile:not-null",
        SetTableColumnNotNull("projection", "sessions", "profile"),
    ),
    _static_operation(
        MigrationPhase.CONTRACT,
        "column:identity.websocket_tickets.session_key:drop",
        DropTableColumn("identity", "websocket_tickets", "session_key"),
    ),
    _static_operation(
        MigrationPhase.CONTRACT,
        "constraint:projection.sessions.durable-identity:add",
        AddConstraint(
            _constraint_for_columns(
                SessionProjectionModel,
                UniqueConstraint,
                ("tenant_id", "agent_id", "profile", "session_key"),
            )
        ),
    ),
    _static_operation(
        MigrationPhase.CONTRACT,
        "constraint:identity.websocket_tickets.stable-session:add",
        AddConstraint(
            _constraint_for_columns(
                WebSocketTicketModel,
                ForeignKeyConstraint,
                ("tenant_id", "session_id"),
            )
        ),
    ),
    _static_operation(
        MigrationPhase.CONTRACT,
        "index:session_projection_identity_idx",
        CreateIndex(
            _index_named(SessionProjectionModel, "session_projection_identity_idx")
        ),
    ),
    _static_operation(
        MigrationPhase.CONTRACT,
        "index:session_projection_legacy_identity_uq",
        CreateIndex(
            _index_named(
                SessionProjectionModel,
                "session_projection_legacy_identity_uq",
            )
        ),
    ),
)

POSTGRES_V1_MIGRATIONS: Final = (
    Migration(
        version=1,
        name="0001_foundation",
        plan=MigrationPlan(
            checksum=(
                "208bcc25a35cb7e083d57586825209456512278c254a56df40d15cab233dc104"
            ),
            expand=_FOUNDATION_EXPAND,
        ),
    ),
    Migration(
        version=2,
        name="0002_tenant_storage",
        plan=MigrationPlan(
            checksum=(
                "748b905dadff5f7fe3ee22325cff7265c1f3136eac586de62bd4a4591db98476"
            ),
            expand=_TENANT_STORAGE_EXPAND,
            contract=_TENANT_STORAGE_CONTRACT,
        ),
    ),
    Migration(
        version=3,
        name="0003_runtime_role_boundaries",
        plan=MigrationPlan(
            checksum=(
                "35354dbd6825de200ad9c00adea614c672be216c8aa75b8cec197c6bb11a8bf7"
            ),
            contract=_runtime_role_operations(),
        ),
        variables=("database_name", "migration_role", "runtime_role"),
    ),
    Migration(
        version=4,
        name="0004_foundation_gap_tables",
        plan=MigrationPlan(
            checksum=(
                "f1ca950f5c7ef07664312759e811ed2dd0c4284b2ac7fd9d88f96e65860e416f"
            ),
            expand=_GAP_EXPAND,
            contract=_GAP_CONTRACT,
        ),
    ),
    Migration(
        version=5,
        name="0005_public_privilege_hardening",
        plan=MigrationPlan(
            checksum=(
                "c1033cb8f78520cb4ed6c7bf28f3fcc6341cd4a2a2f2a0c7110ed78ecd01e679"
            ),
            contract=(
                _static_operation(
                    MigrationPhase.CONTRACT,
                    "schema:public:public-revoke-create",
                    RevokePublicSchemaCreate(),
                ),
                _bound_operation(
                    MigrationPhase.CONTRACT,
                    "database:public-revoke-temporary",
                    ("database_name",),
                    lambda values: RevokePublicDatabaseTemporary(
                        values["database_name"]
                    ),
                ),
            ),
        ),
        variables=("database_name",),
    ),
    Migration(
        version=6,
        name="0006_workspace_role_boundaries",
        plan=MigrationPlan(
            checksum=(
                "2e587b795e300b9a042158d2fe68dafb16066ee0de8e02cf6dd6de9f2414684a"
            ),
            contract=_workspace_role_operations(),
        ),
        variables=("migration_role", "runtime_role"),
    ),
    Migration(
        version=7,
        name="0007_cloud_client_identity_and_session_projection",
        plan=MigrationPlan(
            checksum=(
                "1f4cca3ebc4599f1c3d1c2cec79bffa722147af1f2f375f784aa8e4e0abbea7d"
            ),
            expand=_V7_EXPAND,
            contract=_V7_CONTRACT,
        ),
        variables=("migration_role", "runtime_role"),
    ),
    Migration(
        version=8,
        name="0008_device_pairing_and_credentials",
        plan=MigrationPlan(
            checksum=(
                "8413b68009185ac947bbd6cdb810578b81d2679876bc9fe71543cfb743c856d3"
            ),
            expand=_V8_EXPAND,
            contract=_V8_CONTRACT,
        ),
    ),
    Migration(
        version=9,
        name="0009_connector_transport_cursor",
        plan=MigrationPlan(
            checksum=(
                "110554cd52e1fe0f7524552f61d4e5e67c19ce11be7e632522682cf84accb415"
            ),
            expand=_V9_EXPAND,
            contract=_V9_CONTRACT,
        ),
    ),
    Migration(
        version=10,
        name="0010_connector_handshake_ownership",
        plan=MigrationPlan(
            checksum=(
                "14c09070c8ed0dee01eb3b1a87a6e3623b6241f7462ef8975dabbd87489d3737"
            ),
            expand=_V10_EXPAND,
            contract=_V10_CONTRACT,
        ),
    ),
    Migration(
        version=11,
        name="0011_session_projection_durable_identity",
        plan=MigrationPlan(
            checksum=(
                "61bce41e2da426e5c36c8d7b4587d3ac0d45bd0bde1731358187c42bed040e22"
            ),
            expand=_V11_SESSION_IDENTITY_EXPAND,
            contract=_V11_SESSION_IDENTITY_CONTRACT,
        ),
    ),
    Migration(
        version=12,
        name="0012_session_catalog_v1",
        plan=MigrationPlan(
            checksum=(
                "f170a2f8a43c8b17d9c705d8354cc868ce7d5a4763fa7949031e6c5bc8414154"
            ),
            expand=_V12_EXPAND,
            contract=_V12_CONTRACT,
        ),
    ),
    Migration(
        version=13,
        name="0013_session_catalog_recovery",
        plan=MigrationPlan(
            checksum=(
                "ce3810b7aac562fc71a9996e7bda0444664bae4a5f4f5930a62d18c0cb5bd58d"
            ),
            expand=_V13_SESSION_CATALOG_EXPAND,
            migrate=_V13_SESSION_CATALOG_MIGRATE,
            contract=_V13_SESSION_CATALOG_CONTRACT,
        ),
    ),
)


_PUBLISHED_CATALOG: Final = MappingProxyType(
    {
        1: (
            "0001_foundation",
            "208bcc25a35cb7e083d57586825209456512278c254a56df40d15cab233dc104",
            "5f171dfe01696324ec7ff34201adc7054f3bfb1bf90d8d9434d14e4a54965c70",
            (),
        ),
        2: (
            "0002_tenant_storage",
            "748b905dadff5f7fe3ee22325cff7265c1f3136eac586de62bd4a4591db98476",
            "eee9cc203092e0bb5a673aaeb6e7a67c36f96a8e6015413a05d71c4866de5345",
            (),
        ),
        3: (
            "0003_runtime_role_boundaries",
            "35354dbd6825de200ad9c00adea614c672be216c8aa75b8cec197c6bb11a8bf7",
            "8142924694fb451de4f14e46d24d2471e8a30fae4af3d6c7307eb92ab22a5ca2",
            ("database_name", "migration_role", "runtime_role"),
        ),
        4: (
            "0004_foundation_gap_tables",
            "f1ca950f5c7ef07664312759e811ed2dd0c4284b2ac7fd9d88f96e65860e416f",
            "245c30f720405524ed0f2067197476a979d92b2bcc14e65b4d62238d48fb2206",
            (),
        ),
        5: (
            "0005_public_privilege_hardening",
            "c1033cb8f78520cb4ed6c7bf28f3fcc6341cd4a2a2f2a0c7110ed78ecd01e679",
            "646622b2b8d9890508b7d41dcdd3d3de04d387b16c9b3f46d1314cdef8a49b2e",
            ("database_name",),
        ),
        6: (
            "0006_workspace_role_boundaries",
            "2e587b795e300b9a042158d2fe68dafb16066ee0de8e02cf6dd6de9f2414684a",
            "75c7566b9d55a6ac7872ef27220ea3cfe70c6a795a81fd602f4d6e97b17e1509",
            ("migration_role", "runtime_role"),
        ),
        7: (
            "0007_cloud_client_identity_and_session_projection",
            "1f4cca3ebc4599f1c3d1c2cec79bffa722147af1f2f375f784aa8e4e0abbea7d",
            "118a820ded9c3e209026eb01e5f7f1f03b864991d58996d7b27e3a0e5eb2d854",
            ("migration_role", "runtime_role"),
        ),
        8: (
            "0008_device_pairing_and_credentials",
            "8413b68009185ac947bbd6cdb810578b81d2679876bc9fe71543cfb743c856d3",
            "dabd59411502bea470e9ec70d7ebdcecba4fa0e07ad9d33688801f39e1dba5c3",
            (),
        ),
        9: (
            "0009_connector_transport_cursor",
            "110554cd52e1fe0f7524552f61d4e5e67c19ce11be7e632522682cf84accb415",
            "9011504c296ef03fe0d6ca3968971600ab5467626e00bed3019c5d20c149e35f",
            (),
        ),
        10: (
            "0010_connector_handshake_ownership",
            "14c09070c8ed0dee01eb3b1a87a6e3623b6241f7462ef8975dabbd87489d3737",
            "5a3b5056ef00a1167f87c72e82aa3ed52412bcb5e91e8d5efcac577888244d7b",
            (),
        ),
        11: (
            "0011_session_projection_durable_identity",
            "61bce41e2da426e5c36c8d7b4587d3ac0d45bd0bde1731358187c42bed040e22",
            "202d43ea6e50f6779074462ea64a0d47f8e70025577196de4fb551451fc4c959",
            (),
        ),
        12: (
            "0012_session_catalog_v1",
            "f170a2f8a43c8b17d9c705d8354cc868ce7d5a4763fa7949031e6c5bc8414154",
            "7bde3908ab4b209dc1dc83b9206af47cd3a5cd79a1f39c570e83cb4cc1ed2fb1",
            (),
        ),
        13: (
            "0013_session_catalog_recovery",
            "ce3810b7aac562fc71a9996e7bda0444664bae4a5f4f5930a62d18c0cb5bd58d",
            "ce3810b7aac562fc71a9996e7bda0444664bae4a5f4f5930a62d18c0cb5bd58d",
            (),
        ),
    }
)


def verify_migration_catalog(
    migrations: Iterable[Migration] = POSTGRES_V1_MIGRATIONS,
) -> None:
    """Reject reordered, renamed, duplicated, or structurally mutated entries."""

    catalog = tuple(migrations)
    verify_published_migration_registry(migration.metadata for migration in catalog)
    for migration in catalog:
        _verify_typed_migration(migration)


def _verify_typed_migration(migration: Migration) -> None:
    published = _PUBLISHED_CATALOG.get(migration.version)
    actual = (
        migration.name,
        migration.checksum,
        migration.plan.structural_digest,
        migration.variables,
    )
    if published is None or actual != published:
        raise ValueError(f"migration typed plan mismatch: {migration.name}")


def migration_plan_for(migration: PublishedMigration) -> MigrationPlan:
    """Resolve one registered neutral migration to its typed PostgreSQL plan."""

    verify_published_migration_registry(PUBLISHED_POSTGRES_MIGRATIONS)
    if (
        migration.version < 1
        or migration.version > len(POSTGRES_V1_MIGRATIONS)
        or PUBLISHED_POSTGRES_MIGRATIONS[migration.version - 1] != migration
    ):
        raise ValueError("published migration registry entry is not registered")
    registered = POSTGRES_V1_MIGRATIONS[migration.version - 1]
    if registered.metadata != migration:
        raise ValueError("PostgreSQL migration metadata does not match registry")
    _verify_typed_migration(registered)
    return registered.plan


verify_migration_catalog()
