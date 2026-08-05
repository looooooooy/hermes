"""Typed PostgreSQL statements used at the platform adapter boundary."""

from __future__ import annotations

import re
from enum import Enum
from typing import Any

from sqlalchemy import BigInteger, Column, Integer
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.schema import ExecutableDDLElement
from sqlalchemy.sql.base import Executable
from sqlalchemy.sql.elements import ClauseElement

_POSTGRESQL_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*\Z", re.ASCII)
_POSTGRESQL_BIGINT_MIN = -(2**63)
_POSTGRESQL_BIGINT_MAX = 2**63 - 1
_POSTGRESQL_TIMEOUT_MAX = 2**31 - 1


def _validate_advisory_key(key: object) -> int:
    if type(key) is not int:
        raise TypeError("advisory lock key must be a signed 64-bit integer")
    if not _POSTGRESQL_BIGINT_MIN <= key <= _POSTGRESQL_BIGINT_MAX:
        raise ValueError("advisory lock key must be within the signed 64-bit range")
    return key


def _validate_identifier(identifier: object, *, kind: str) -> str:
    if (
        not isinstance(identifier, str)
        or _POSTGRESQL_IDENTIFIER.fullmatch(identifier) is None
        or len(identifier.encode("utf-8")) > 63
    ):
        raise ValueError(f"{kind} must be a valid PostgreSQL identifier")
    return identifier


class _AdvisoryLockStatement(Executable, ClauseElement):
    inherit_cache = False

    def __init__(self, key: int) -> None:
        self.key = _validate_advisory_key(key)


class TryAdvisoryLock(_AdvisoryLockStatement):
    """Attempt to acquire a PostgreSQL session advisory lock."""


class ReleaseAdvisoryLock(_AdvisoryLockStatement):
    """Release a PostgreSQL session advisory lock."""


class _LocalTimeoutStatement(Executable, ClauseElement):
    inherit_cache = False

    def __init__(self, milliseconds: int) -> None:
        if type(milliseconds) is not int:
            raise TypeError("timeout must be a positive PostgreSQL integer")
        if not 1 <= milliseconds <= _POSTGRESQL_TIMEOUT_MAX:
            raise ValueError("timeout must be within the PostgreSQL integer range")
        self.milliseconds = milliseconds


class SetLocalStatementTimeout(_LocalTimeoutStatement):
    """Bound statement execution time inside the current transaction."""


class SetLocalLockTimeout(_LocalTimeoutStatement):
    """Bound lock acquisition time inside the current transaction."""


class _QualifiedTableDDL(ExecutableDDLElement):
    def __init__(self, schema: str, table: str) -> None:
        self.schema = _validate_identifier(schema, kind="schema")
        self.table = _validate_identifier(table, kind="table")


class AssertTablesEmpty(ExecutableDDLElement):
    """Fail before a migration would guess identity for existing rows."""

    def __init__(self, tables: tuple[tuple[str, str], ...]) -> None:
        if not tables:
            raise ValueError("at least one table is required")
        self.tables = tuple(
            (
                _validate_identifier(schema, kind="schema"),
                _validate_identifier(table, kind="table"),
            )
            for schema, table in tables
        )


class AddTableColumn(_QualifiedTableDDL):
    """Add one SQLAlchemy-typed column to an existing table."""

    def __init__(self, schema: str, table: str, column: Column[Any]) -> None:
        super().__init__(schema, table)
        if column.table is not None:
            raise ValueError("migration column must be unattached")
        self.column = column


class DropTableColumn(_QualifiedTableDDL):
    """Drop one validated column from an existing table."""

    def __init__(self, schema: str, table: str, column: str) -> None:
        super().__init__(schema, table)
        self.column = _validate_identifier(column, kind="column")


class DropTableConstraint(_QualifiedTableDDL):
    """Drop one validated PostgreSQL table constraint."""

    def __init__(self, schema: str, table: str, constraint: str) -> None:
        super().__init__(schema, table)
        self.constraint = _validate_identifier(constraint, kind="constraint")


class SetTableColumnNotNull(_QualifiedTableDDL):
    """Contract a populated column to NOT NULL."""

    def __init__(self, schema: str, table: str, column: str) -> None:
        super().__init__(schema, table)
        self.column = _validate_identifier(column, kind="column")


class ForceRowLevelSecurity(_QualifiedTableDDL):
    """Force row-level security for every table access path."""


class EnableRowLevelSecurity(_QualifiedTableDDL):
    """Enable row-level security for a tenant-owned table."""


class CreateTenantIsolationPolicy(_QualifiedTableDDL):
    """Create the standard tenant isolation policy for a tenant table."""


class AlterRuntimeRole(ExecutableDDLElement):
    """Remove PostgreSQL privilege-escalation capabilities from a runtime role."""

    def __init__(self, role: str) -> None:
        self.role = _validate_identifier(role, kind="role")


class RevokePublicDatabaseTemporary(ExecutableDDLElement):
    """Remove the default PUBLIC temporary-table database privilege."""

    def __init__(self, database: str) -> None:
        self.database = _validate_identifier(database, kind="database")


class RevokePublicSchemaCreate(ExecutableDDLElement):
    """Remove the default PUBLIC create privilege from the public schema."""


class CreateTransactionContextFunction(ExecutableDDLElement):
    """Create the transaction-local request-context function."""


class DatabasePrivilege(str, Enum):
    """Privilege tokens valid only for PostgreSQL database objects."""

    CREATE = "CREATE"
    TEMPORARY = "TEMPORARY"


class TablePrivilege(str, Enum):
    """Privilege tokens valid only for PostgreSQL table objects."""

    DELETE = "DELETE"
    INSERT = "INSERT"
    REFERENCES = "REFERENCES"
    SELECT = "SELECT"
    TRIGGER = "TRIGGER"
    TRUNCATE = "TRUNCATE"
    UPDATE = "UPDATE"


def _validate_privileges(
    privileges: object,
    *,
    privilege_type: type[DatabasePrivilege | TablePrivilege],
    kind: str,
) -> tuple[DatabasePrivilege | TablePrivilege, ...]:
    if (
        not isinstance(privileges, tuple)
        or not privileges
        or any(not isinstance(item, privilege_type) for item in privileges)
        or len(set(privileges)) != len(privileges)
    ):
        raise ValueError(
            f"{kind} privileges must be a non-empty tuple of unique values"
        )
    return privileges


class _SchemaRoleDDL(ExecutableDDLElement):
    def __init__(self, schema: str, role: str) -> None:
        self.schema = _validate_identifier(schema, kind="schema")
        self.role = _validate_identifier(role, kind="role")


class RevokeSchemaCreate(_SchemaRoleDDL):
    """Remove schema object-creation rights from a runtime role."""


class GrantSchemaUsage(_SchemaRoleDDL):
    """Grant schema name-resolution rights to a runtime role."""


class _PrivilegesDDL(ExecutableDDLElement):
    def __init__(
        self,
        role: str,
        privileges: tuple[DatabasePrivilege | TablePrivilege, ...],
        *,
        privilege_type: type[DatabasePrivilege | TablePrivilege],
        kind: str,
    ) -> None:
        self.role = _validate_identifier(role, kind="role")
        self.privileges = _validate_privileges(
            privileges,
            privilege_type=privilege_type,
            kind=kind,
        )


class RevokeDatabasePrivileges(_PrivilegesDDL):
    """Remove selected database privileges from a role."""

    def __init__(
        self,
        database: str,
        role: str,
        privileges: tuple[DatabasePrivilege, ...],
    ) -> None:
        super().__init__(
            role,
            privileges,
            privilege_type=DatabasePrivilege,
            kind="database",
        )
        self.database = _validate_identifier(database, kind="database")


class _SchemaPrivilegesDDL(_PrivilegesDDL):
    def __init__(
        self,
        schema: str,
        role: str,
        privileges: tuple[TablePrivilege, ...],
    ) -> None:
        super().__init__(
            role,
            privileges,
            privilege_type=TablePrivilege,
            kind="table",
        )
        self.schema = _validate_identifier(schema, kind="schema")


class GrantAllTablesPrivileges(_SchemaPrivilegesDDL):
    """Grant selected privileges on all current tables in a schema."""


class RevokeAllTablesPrivileges(_SchemaPrivilegesDDL):
    """Remove selected privileges from all current tables in a schema."""


class _TablePrivilegesDDL(_SchemaPrivilegesDDL):
    def __init__(
        self,
        schema: str,
        table: str,
        role: str,
        privileges: tuple[TablePrivilege, ...],
    ) -> None:
        super().__init__(schema, role, privileges)
        self.table = _validate_identifier(table, kind="table")


class GrantTablePrivileges(_TablePrivilegesDDL):
    """Grant selected privileges on one table."""


class RevokeTablePrivileges(_TablePrivilegesDDL):
    """Remove selected privileges from one table."""


class GrantFunctionExecute(ExecutableDDLElement):
    """Grant execution of the transaction-context function."""

    def __init__(self, role: str) -> None:
        self.role = _validate_identifier(role, kind="role")


class AlterDefaultTablePrivileges(_SchemaPrivilegesDDL):
    """Grant future-table privileges created by one migration owner."""

    def __init__(
        self,
        owner: str,
        schema: str,
        role: str,
        privileges: tuple[TablePrivilege, ...],
    ) -> None:
        super().__init__(schema, role, privileges)
        self.owner = _validate_identifier(owner, kind="owner")


def _compile_advisory_key(statement: _AdvisoryLockStatement, compiler: Any) -> str:
    return compiler.render_literal_value(statement.key, BigInteger())


def _compile_timeout(
    statement: _LocalTimeoutStatement,
    compiler: Any,
) -> str:
    return compiler.render_literal_value(statement.milliseconds, Integer())


def _qualified_table(statement: _QualifiedTableDDL, compiler: Any) -> str:
    quote = compiler.preparer.quote
    return f"{quote(statement.schema)}.{quote(statement.table)}"


@compiles(AssertTablesEmpty, "postgresql")
def _compile_assert_tables_empty(
    statement: AssertTablesEmpty,
    compiler: Any,
    **_: Any,
) -> str:
    quote = compiler.preparer.quote
    predicates = " OR ".join(
        f"EXISTS (SELECT 1 FROM {quote(schema)}.{quote(table)} LIMIT 1)"
        for schema, table in statement.tables
    )
    return (
        "DO $migration$ BEGIN IF "
        f"{predicates} THEN RAISE EXCEPTION "
        "'revision 11 requires externally reconciled session identity'; "
        "END IF; END $migration$"
    )


@compiles(AddTableColumn, "postgresql")
def _compile_add_table_column(
    statement: AddTableColumn,
    compiler: Any,
    **_: Any,
) -> str:
    specification = compiler.get_column_specification(statement.column)
    return f"ALTER TABLE {_qualified_table(statement, compiler)} ADD COLUMN {specification}"


@compiles(DropTableColumn, "postgresql")
def _compile_drop_table_column(
    statement: DropTableColumn,
    compiler: Any,
    **_: Any,
) -> str:
    column = compiler.preparer.quote(statement.column)
    return f"ALTER TABLE {_qualified_table(statement, compiler)} DROP COLUMN {column}"


@compiles(DropTableConstraint, "postgresql")
def _compile_drop_table_constraint(
    statement: DropTableConstraint,
    compiler: Any,
    **_: Any,
) -> str:
    constraint = compiler.preparer.quote(statement.constraint)
    return f"ALTER TABLE {_qualified_table(statement, compiler)} DROP CONSTRAINT {constraint}"


@compiles(SetTableColumnNotNull, "postgresql")
def _compile_set_table_column_not_null(
    statement: SetTableColumnNotNull,
    compiler: Any,
    **_: Any,
) -> str:
    column = compiler.preparer.quote(statement.column)
    return (
        f"ALTER TABLE {_qualified_table(statement, compiler)} "
        f"ALTER COLUMN {column} SET NOT NULL"
    )


@compiles(TryAdvisoryLock, "postgresql")
def _compile_try_advisory_lock(
    statement: TryAdvisoryLock,
    compiler: Any,
    **_: Any,
) -> str:
    key = _compile_advisory_key(statement, compiler)
    return f"SELECT pg_try_advisory_lock({key})"


@compiles(ReleaseAdvisoryLock, "postgresql")
def _compile_release_advisory_lock(
    statement: ReleaseAdvisoryLock,
    compiler: Any,
    **_: Any,
) -> str:
    key = _compile_advisory_key(statement, compiler)
    return f"SELECT pg_advisory_unlock({key})"


@compiles(SetLocalStatementTimeout, "postgresql")
def _compile_set_local_statement_timeout(
    statement: SetLocalStatementTimeout,
    compiler: Any,
    **_: Any,
) -> str:
    timeout = _compile_timeout(statement, compiler)
    return f"SET LOCAL statement_timeout = {timeout}"


@compiles(SetLocalLockTimeout, "postgresql")
def _compile_set_local_lock_timeout(
    statement: SetLocalLockTimeout,
    compiler: Any,
    **_: Any,
) -> str:
    timeout = _compile_timeout(statement, compiler)
    return f"SET LOCAL lock_timeout = {timeout}"


@compiles(ForceRowLevelSecurity, "postgresql")
def _compile_force_row_level_security(
    statement: ForceRowLevelSecurity,
    compiler: Any,
    **_: Any,
) -> str:
    return (
        f"ALTER TABLE {_qualified_table(statement, compiler)} FORCE ROW LEVEL SECURITY"
    )


@compiles(EnableRowLevelSecurity, "postgresql")
def _compile_enable_row_level_security(
    statement: EnableRowLevelSecurity,
    compiler: Any,
    **_: Any,
) -> str:
    return (
        f"ALTER TABLE {_qualified_table(statement, compiler)} ENABLE ROW LEVEL SECURITY"
    )


@compiles(CreateTenantIsolationPolicy, "postgresql")
def _compile_create_tenant_isolation_policy(
    statement: CreateTenantIsolationPolicy,
    compiler: Any,
    **_: Any,
) -> str:
    table = _qualified_table(statement, compiler)
    tenant_context = "NULLIF(current_setting('hermes.tenant_id', true), '')::uuid"
    return (
        f"CREATE POLICY tenant_isolation ON {table} "
        f"USING (tenant_id = {tenant_context}) "
        f"WITH CHECK (tenant_id = {tenant_context})"
    )


@compiles(AlterRuntimeRole, "postgresql")
def _compile_alter_runtime_role(
    statement: AlterRuntimeRole,
    compiler: Any,
    **_: Any,
) -> str:
    role = compiler.preparer.quote(statement.role)
    return f"ALTER ROLE {role} NOBYPASSRLS NOSUPERUSER NOCREATEDB NOCREATEROLE"


@compiles(RevokePublicDatabaseTemporary, "postgresql")
def _compile_revoke_public_database_temporary(
    statement: RevokePublicDatabaseTemporary,
    compiler: Any,
    **_: Any,
) -> str:
    database = compiler.preparer.quote(statement.database)
    return f"REVOKE TEMPORARY ON DATABASE {database} FROM PUBLIC"


@compiles(RevokePublicSchemaCreate, "postgresql")
def _compile_revoke_public_schema_create(
    _: RevokePublicSchemaCreate,
    __: Any,
    **___: Any,
) -> str:
    return "REVOKE CREATE ON SCHEMA public FROM PUBLIC"


def _compiled_privileges(statement: _PrivilegesDDL) -> str:
    return ", ".join(item.value for item in statement.privileges)


def _quoted_schema_role(statement: _SchemaRoleDDL, compiler: Any) -> tuple[str, str]:
    quote = compiler.preparer.quote
    return quote(statement.schema), quote(statement.role)


@compiles(RevokeSchemaCreate, "postgresql")
def _compile_revoke_schema_create(
    statement: RevokeSchemaCreate,
    compiler: Any,
    **_: Any,
) -> str:
    schema, role = _quoted_schema_role(statement, compiler)
    return f"REVOKE CREATE ON SCHEMA {schema} FROM {role}"


@compiles(GrantSchemaUsage, "postgresql")
def _compile_grant_schema_usage(
    statement: GrantSchemaUsage,
    compiler: Any,
    **_: Any,
) -> str:
    schema, role = _quoted_schema_role(statement, compiler)
    return f"GRANT USAGE ON SCHEMA {schema} TO {role}"


@compiles(RevokeDatabasePrivileges, "postgresql")
def _compile_revoke_database_privileges(
    statement: RevokeDatabasePrivileges,
    compiler: Any,
    **_: Any,
) -> str:
    quote = compiler.preparer.quote
    privileges = _compiled_privileges(statement)
    return (
        f"REVOKE {privileges} ON DATABASE {quote(statement.database)} "
        f"FROM {quote(statement.role)}"
    )


def _compile_all_tables_privileges(
    statement: _SchemaPrivilegesDDL,
    compiler: Any,
    *,
    action: str,
    preposition: str,
) -> str:
    quote = compiler.preparer.quote
    privileges = _compiled_privileges(statement)
    return (
        f"{action} {privileges} ON ALL TABLES "
        f"IN SCHEMA {quote(statement.schema)} "
        f"{preposition} {quote(statement.role)}"
    )


@compiles(GrantAllTablesPrivileges, "postgresql")
def _compile_grant_all_tables_privileges(
    statement: GrantAllTablesPrivileges,
    compiler: Any,
    **_: Any,
) -> str:
    return _compile_all_tables_privileges(
        statement,
        compiler,
        action="GRANT",
        preposition="TO",
    )


@compiles(RevokeAllTablesPrivileges, "postgresql")
def _compile_revoke_all_tables_privileges(
    statement: RevokeAllTablesPrivileges,
    compiler: Any,
    **_: Any,
) -> str:
    return _compile_all_tables_privileges(
        statement,
        compiler,
        action="REVOKE",
        preposition="FROM",
    )


def _compile_table_privileges(
    statement: _TablePrivilegesDDL,
    compiler: Any,
    *,
    action: str,
    preposition: str,
) -> str:
    quote = compiler.preparer.quote
    privileges = _compiled_privileges(statement)
    table = f"{quote(statement.schema)}.{quote(statement.table)}"
    return (
        f"{action} {privileges} ON TABLE {table} {preposition} {quote(statement.role)}"
    )


@compiles(GrantTablePrivileges, "postgresql")
def _compile_grant_table_privileges(
    statement: GrantTablePrivileges,
    compiler: Any,
    **_: Any,
) -> str:
    return _compile_table_privileges(
        statement,
        compiler,
        action="GRANT",
        preposition="TO",
    )


@compiles(RevokeTablePrivileges, "postgresql")
def _compile_revoke_table_privileges(
    statement: RevokeTablePrivileges,
    compiler: Any,
    **_: Any,
) -> str:
    return _compile_table_privileges(
        statement,
        compiler,
        action="REVOKE",
        preposition="FROM",
    )


@compiles(GrantFunctionExecute, "postgresql")
def _compile_grant_function_execute(
    statement: GrantFunctionExecute,
    compiler: Any,
    **_: Any,
) -> str:
    role = compiler.preparer.quote(statement.role)
    return (
        "GRANT EXECUTE ON FUNCTION "
        "platform.set_transaction_context(uuid, uuid, text) "
        f"TO {role}"
    )


@compiles(AlterDefaultTablePrivileges, "postgresql")
def _compile_alter_default_table_privileges(
    statement: AlterDefaultTablePrivileges,
    compiler: Any,
    **_: Any,
) -> str:
    quote = compiler.preparer.quote
    privileges = _compiled_privileges(statement)
    return (
        f"ALTER DEFAULT PRIVILEGES FOR ROLE {quote(statement.owner)} "
        f"IN SCHEMA {quote(statement.schema)} "
        f"GRANT {privileges} ON TABLES TO {quote(statement.role)}"
    )


@compiles(CreateTransactionContextFunction, "postgresql")
def _compile_create_transaction_context_function(
    _: CreateTransactionContextFunction,
    __: Any,
    **___: Any,
) -> str:
    return """CREATE FUNCTION platform.set_transaction_context(
    p_tenant_id uuid,
    p_workspace_id uuid,
    p_purpose text
) RETURNS void
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = pg_catalog
AS $function$
BEGIN
    IF p_purpose IS NULL OR btrim(p_purpose) = ''
        OR octet_length(p_purpose) > 128 THEN
        RAISE EXCEPTION 'transaction purpose must contain 1 to 128 bytes';
    END IF;
    PERFORM set_config('hermes.tenant_id', p_tenant_id::text, true);
    PERFORM set_config('hermes.workspace_id', p_workspace_id::text, true);
    PERFORM set_config('hermes.purpose', p_purpose, true);
END;
$function$"""
