from __future__ import annotations

import pytest
from sqlalchemy.dialects import postgresql

from hermes_cloud.platform.postgres.ddl import (
    AlterDefaultTablePrivileges,
    AlterRuntimeRole,
    CreateTenantIsolationPolicy,
    CreateTransactionContextFunction,
    DatabasePrivilege,
    EnableRowLevelSecurity,
    ForceRowLevelSecurity,
    GrantAllTablesPrivileges,
    GrantFunctionExecute,
    GrantSchemaUsage,
    GrantTablePrivileges,
    ReleaseAdvisoryLock,
    RevokeAllTablesPrivileges,
    RevokeDatabasePrivileges,
    RevokePublicDatabaseTemporary,
    RevokePublicSchemaCreate,
    RevokeSchemaCreate,
    RevokeTablePrivileges,
    SetLocalLockTimeout,
    SetLocalStatementTimeout,
    TablePrivilege,
    TryAdvisoryLock,
)

DIALECT = postgresql.dialect()


def _compiled(statement: object) -> str:
    return str(statement.compile(dialect=DIALECT))


def test_advisory_lock_is_a_typed_executable_with_integer_literal_only() -> None:
    assert _compiled(TryAdvisoryLock(42)) == "SELECT pg_try_advisory_lock(42)"
    assert _compiled(ReleaseAdvisoryLock(42)) == "SELECT pg_advisory_unlock(42)"


def test_execution_deadlines_use_typed_database_local_timeouts() -> None:
    assert _compiled(SetLocalStatementTimeout(2500)) == (
        "SET LOCAL statement_timeout = 2500"
    )
    assert _compiled(SetLocalLockTimeout(1500)) == ("SET LOCAL lock_timeout = 1500")


@pytest.mark.parametrize(
    "statement_type",
    [SetLocalStatementTimeout, SetLocalLockTimeout],
)
@pytest.mark.parametrize("milliseconds", [True, 0, -1, 2**31])
def test_database_local_timeouts_require_positive_postgresql_integer_range(
    statement_type: type[SetLocalStatementTimeout | SetLocalLockTimeout],
    milliseconds: object,
) -> None:
    with pytest.raises((TypeError, ValueError), match="timeout"):
        statement_type(milliseconds)  # type: ignore[arg-type]


def test_rls_policy_and_force_are_typed_and_quote_schema_table_names() -> None:
    assert _compiled(EnableRowLevelSecurity("identity", "tenants")) == (
        "ALTER TABLE identity.tenants ENABLE ROW LEVEL SECURITY"
    )
    assert _compiled(ForceRowLevelSecurity("identity", "tenants")) == (
        "ALTER TABLE identity.tenants FORCE ROW LEVEL SECURITY"
    )
    policy = _compiled(CreateTenantIsolationPolicy("identity", "tenants"))
    assert "CREATE POLICY tenant_isolation ON identity.tenants" in policy
    assert "NULLIF(current_setting('hermes.tenant_id', true), '')::uuid" in policy


def test_role_and_public_hardening_quote_validated_identifiers() -> None:
    assert _compiled(AlterRuntimeRole("hermes_runtime")) == (
        "ALTER ROLE hermes_runtime NOBYPASSRLS NOSUPERUSER NOCREATEDB NOCREATEROLE"
    )
    assert (
        _compiled(RevokePublicDatabaseTemporary("hermes_cloud"))
        == "REVOKE TEMPORARY ON DATABASE hermes_cloud FROM PUBLIC"
    )
    assert _compiled(RevokePublicSchemaCreate()) == (
        "REVOKE CREATE ON SCHEMA public FROM PUBLIC"
    )


def test_runtime_privileges_are_individual_typed_statements() -> None:
    assert _compiled(
        RevokeDatabasePrivileges(
            "hermes_cloud",
            "hermes_runtime",
            (DatabasePrivilege.CREATE, DatabasePrivilege.TEMPORARY),
        )
    ) == ("REVOKE CREATE, TEMPORARY ON DATABASE hermes_cloud FROM hermes_runtime")
    assert _compiled(RevokeSchemaCreate("identity", "hermes_runtime")) == (
        "REVOKE CREATE ON SCHEMA identity FROM hermes_runtime"
    )
    assert _compiled(GrantSchemaUsage("identity", "hermes_runtime")) == (
        "GRANT USAGE ON SCHEMA identity TO hermes_runtime"
    )
    dml = (
        TablePrivilege.SELECT,
        TablePrivilege.INSERT,
        TablePrivilege.UPDATE,
        TablePrivilege.DELETE,
    )
    unsafe = (
        TablePrivilege.TRUNCATE,
        TablePrivilege.REFERENCES,
        TablePrivilege.TRIGGER,
    )
    assert _compiled(GrantAllTablesPrivileges("identity", "hermes_runtime", dml)) == (
        "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES "
        "IN SCHEMA identity TO hermes_runtime"
    )
    assert _compiled(
        RevokeAllTablesPrivileges("identity", "hermes_runtime", unsafe)
    ) == (
        "REVOKE TRUNCATE, REFERENCES, TRIGGER ON ALL TABLES "
        "IN SCHEMA identity FROM hermes_runtime"
    )
    assert _compiled(
        GrantTablePrivileges(
            "audit",
            "audit_events",
            "hermes_runtime",
            (TablePrivilege.SELECT, TablePrivilege.INSERT),
        )
    ) == ("GRANT SELECT, INSERT ON TABLE audit.audit_events TO hermes_runtime")
    assert _compiled(
        RevokeTablePrivileges(
            "audit",
            "audit_events",
            "hermes_runtime",
            (
                TablePrivilege.UPDATE,
                TablePrivilege.DELETE,
                TablePrivilege.TRUNCATE,
                TablePrivilege.REFERENCES,
                TablePrivilege.TRIGGER,
            ),
        )
    ) == (
        "REVOKE UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER "
        "ON TABLE audit.audit_events FROM hermes_runtime"
    )
    assert _compiled(GrantFunctionExecute("hermes_runtime")) == (
        "GRANT EXECUTE ON FUNCTION "
        "platform.set_transaction_context(uuid, uuid, text) TO hermes_runtime"
    )
    assert _compiled(
        AlterDefaultTablePrivileges(
            "hermes_migration",
            "identity",
            "hermes_runtime",
            dml,
        )
    ) == (
        "ALTER DEFAULT PRIVILEGES FOR ROLE hermes_migration "
        "IN SCHEMA identity GRANT SELECT, INSERT, UPDATE, DELETE "
        "ON TABLES TO hermes_runtime"
    )


def test_privileges_cannot_cross_database_and_table_object_types() -> None:
    with pytest.raises(ValueError, match="database privileges"):
        RevokeDatabasePrivileges(
            "hermes_cloud",
            "hermes_runtime",
            (TablePrivilege.SELECT,),  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="table privileges"):
        GrantAllTablesPrivileges(
            "identity",
            "hermes_runtime",
            (DatabasePrivilege.CREATE,),  # type: ignore[arg-type]
        )


def test_valid_identifiers_use_postgresql_dialect_quoting() -> None:
    assert _compiled(ForceRowLevelSecurity("Identity", "select")) == (
        'ALTER TABLE "Identity"."select" FORCE ROW LEVEL SECURITY'
    )
    assert _compiled(AlterRuntimeRole("select")) == (
        'ALTER ROLE "select" NOBYPASSRLS NOSUPERUSER NOCREATEDB NOCREATEROLE'
    )


def test_transaction_context_function_remains_a_typed_dialect_boundary() -> None:
    compiled = _compiled(CreateTransactionContextFunction())
    assert "CREATE FUNCTION platform.set_transaction_context" in compiled
    assert "p_purpose IS NULL OR btrim(p_purpose) = ''" in compiled
    assert "octet_length(p_purpose) > 128" in compiled
    assert (
        "RAISE EXCEPTION 'transaction purpose must contain 1 to 128 bytes'" in compiled
    )
    assert compiled.count("set_config(") == 3
    for context_name in ("tenant_id", "workspace_id", "purpose"):
        assert f"set_config('hermes.{context_name}'" in compiled
    assert ", false)" not in compiled


@pytest.mark.parametrize("statement_type", [TryAdvisoryLock, ReleaseAdvisoryLock])
@pytest.mark.parametrize("key", ["42", 42.0, True, None])
def test_advisory_lock_rejects_non_integer_keys(
    statement_type: type[TryAdvisoryLock | ReleaseAdvisoryLock],
    key: object,
) -> None:
    with pytest.raises(TypeError, match="signed 64-bit integer"):
        statement_type(key)  # type: ignore[arg-type]


@pytest.mark.parametrize("statement_type", [TryAdvisoryLock, ReleaseAdvisoryLock])
@pytest.mark.parametrize("key", [-(2**63) - 1, 2**63])
def test_advisory_lock_rejects_keys_outside_postgresql_bigint(
    statement_type: type[TryAdvisoryLock | ReleaseAdvisoryLock],
    key: int,
) -> None:
    with pytest.raises(ValueError, match="signed 64-bit range"):
        statement_type(key)


@pytest.mark.parametrize(
    ("statement_type", "args"),
    [
        (ForceRowLevelSecurity, ("", "tenants")),
        (EnableRowLevelSecurity, ("identity", "")),
        (ForceRowLevelSecurity, ("identity", "")),
        (CreateTenantIsolationPolicy, ("identity.tenants", "tenants")),
        (CreateTenantIsolationPolicy, ("identity", "tenants --")),
        (AlterRuntimeRole, ("runtime; ALTER ROLE admin",)),
        (RevokePublicDatabaseTemporary, ('cloud" FROM PUBLIC; --',)),
        (RevokePublicDatabaseTemporary, ("a" * 64,)),
    ],
)
def test_database_identifiers_reject_empty_illegal_and_injection_values(
    statement_type: type[
        ForceRowLevelSecurity
        | CreateTenantIsolationPolicy
        | AlterRuntimeRole
        | RevokePublicDatabaseTemporary
    ],
    args: tuple[str, ...],
) -> None:
    with pytest.raises(ValueError, match="valid PostgreSQL identifier"):
        statement_type(*args)
