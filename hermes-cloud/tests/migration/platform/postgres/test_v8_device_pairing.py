from __future__ import annotations

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, Index, UniqueConstraint

from hermes_cloud.platform.postgres.catalog import POSTGRES_V1_MIGRATIONS
from hermes_cloud.platform.postgres.models import (
    PAIRING_V8_MODELS,
    PAIRING_V8_TENANT_MODELS,
    DeviceAuthenticationChallengeModel,
    DeviceCredentialPublicKeyModel,
    DeviceLifecycleModel,
    PairingClaimLimitModel,
    PairingEnrollmentProofModel,
    PairingIdempotencyModel,
    PairingOfferModel,
)


def _check_sql(model: type[object]) -> tuple[str, ...]:
    return tuple(
        str(constraint.sqltext)
        for constraint in model.__table__.constraints  # type: ignore[attr-defined]
        if isinstance(constraint, CheckConstraint)
    )


def test_v8_adds_typed_pairing_tables_without_altering_published_tables() -> None:
    migration = POSTGRES_V1_MIGRATIONS[7]

    assert migration.version == 8
    assert migration.name == "0008_device_pairing_and_credentials"
    assert migration.variables == ()
    keys = {operation.key for operation in migration.plan.operations}
    for model in PAIRING_V8_MODELS:
        table = model.__table__
        qualified = f"{table.schema}.{table.name}"
        assert f"table:{qualified}" in keys
        for index in table.indexes:
            assert f"index:{index.name}" in keys
    for model in PAIRING_V8_TENANT_MODELS:
        qualified = f"{model.__table__.schema}.{model.__table__.name}"
        assert f"rls-enable:{qualified}" in keys
        assert f"rls-force:{qualified}" in keys
        assert f"rls-policy:{qualified}" in keys
    assert "rls-enable:device.pairing_offers" not in keys
    assert all("alter" not in operation.key for operation in migration.plan.operations)


def test_pairing_offer_is_global_bootstrap_state_without_authorized_scope() -> None:
    columns = set(PairingOfferModel.__table__.columns.keys())
    assert {
        "pairing_offer_id",
        "pairing_code_digest",
        "bootstrap_secret_digest",
        "public_key",
        "credential_fingerprint",
        "state",
        "expires_at",
        "revision",
    }.issubset(columns)
    assert {
        "tenant_id",
        "workspace_id",
        "agent_id",
        "device_id",
        "owner_user_id",
        "pairing_code",
        "bootstrap_secret",
        "private_key",
    }.isdisjoint(columns)
    assert "confirmed_at" not in columns
    assert PairingOfferModel.__table__.schema == "device"
    assert any(
        isinstance(constraint, UniqueConstraint)
        and {"pairing_code_digest"} == {column.name for column in constraint.columns}
        for constraint in PairingOfferModel.__table__.constraints
    )
    checks = _check_sql(PairingOfferModel)
    assert any("octet_length(public_key) = 32" in sql for sql in checks)
    assert "failed_attempts" not in columns


def test_owner_claim_proof_binds_offer_to_tenant_scoped_pairing_session() -> None:
    table = PairingEnrollmentProofModel.__table__
    columns = set(table.columns.keys())
    assert {
        "tenant_id",
        "pairing_session_id",
        "pairing_offer_id",
        "owner_user_id",
        "device_display_name",
        "claim_id",
        "challenge_digest",
        "challenge_id",
        "challenge_expires_at",
        "owner_confirmed_at",
        "scopes",
        "confirmation_digest",
        "revision",
    }.issubset(columns)
    assert {"challenge", "signature", "pairing_code", "private_key"}.isdisjoint(columns)
    foreign_targets = {
        tuple(element.target_fullname for element in constraint.elements)
        for constraint in table.constraints
        if isinstance(constraint, ForeignKeyConstraint)
    }
    assert (
        "device.pairing_sessions.tenant_id",
        "device.pairing_sessions.pairing_session_id",
    ) in foreign_targets
    assert ("device.pairing_offers.pairing_offer_id",) in foreign_targets
    assert ("identity.users.tenant_id", "identity.users.user_id") in foreign_targets


def test_lifecycle_and_credential_key_material_are_separate_tenant_records() -> None:
    lifecycle_checks = _check_sql(DeviceLifecycleModel)
    assert any(
        "pending" in sql
        and "active" in sql
        and "suspended" in sql
        and "revoked" in sql
        and "retired" in sql
        for sql in lifecycle_checks
    )
    assert "offline" not in " ".join(lifecycle_checks)
    assert {"workspace_id", "agent_id"}.issubset(
        DeviceLifecycleModel.__table__.columns.keys()
    )

    public_key_columns = set(DeviceCredentialPublicKeyModel.__table__.columns.keys())
    assert {
        "tenant_id",
        "credential_id",
        "algorithm",
        "public_key",
        "credential_fingerprint",
    }.issubset(public_key_columns)
    assert {"private_key", "secret"}.isdisjoint(public_key_columns)
    assert any(
        isinstance(index, Index)
        for index in DeviceCredentialPublicKeyModel.__table__.indexes
    )


def test_repeated_device_challenges_store_only_single_use_digests() -> None:
    table = DeviceAuthenticationChallengeModel.__table__
    assert {
        "tenant_id",
        "challenge_id",
        "device_id",
        "credential_id",
        "pairing_mutation_id",
        "challenge_digest",
        "proof_digest",
        "issued_at",
        "expires_at",
        "consumed_at",
        "revision",
    } == set(table.columns.keys())
    assert {"challenge", "signing_payload", "signature", "token"}.isdisjoint(
        table.columns.keys()
    )
    checks = _check_sql(DeviceAuthenticationChallengeModel)
    assert any("challenge_digest ~ '^[0-9a-f]{64}$'" in sql for sql in checks)
    assert any("proof_digest IS NULL" in sql for sql in checks)


def test_pairing_idempotency_ledger_stores_only_canonical_digests() -> None:
    table = PairingIdempotencyModel.__table__
    columns = set(table.columns.keys())
    assert {
        "pairing_mutation_id",
        "pairing_offer_id",
        "operation",
        "idempotency_key_digest",
        "principal_digest",
        "request_digest",
        "expected_revision",
        "result_revision",
        "result_state",
        "result_code",
        "retry_after_seconds",
    }.issubset(columns)
    assert {
        "idempotency_key",
        "principal",
        "request_body",
        "pairing_code",
        "bootstrap_secret",
        "challenge",
        "signature",
    }.isdisjoint(columns)
    assert any(
        isinstance(constraint, UniqueConstraint)
        and {
            "operation",
            "idempotency_key_digest",
            "principal_digest",
        }
        == {column.name for column in constraint.columns}
        for constraint in table.constraints
    )
    assert table.c.pairing_offer_id.nullable


def test_claim_limit_is_tenant_scoped_without_offer_mutation_authority() -> None:
    table = PairingClaimLimitModel.__table__
    assert {
        "tenant_id",
        "owner_user_id",
        "failed_attempts",
        "revision",
        "window_started_at",
        "window_expires_at",
        "updated_at",
    } == set(table.columns.keys())
    assert "pairing_offer_id" not in table.columns
    checks = _check_sql(PairingClaimLimitModel)
    assert any("failed_attempts BETWEEN 0 AND 5" in sql for sql in checks)
