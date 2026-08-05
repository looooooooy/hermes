from __future__ import annotations

from sqlalchemy import CheckConstraint, Index, UniqueConstraint

from hermes_cloud.platform.postgres.catalog import POSTGRES_V1_MIGRATIONS
from hermes_cloud.platform.postgres.models import (
    PasswordCredentialModel,
    RefreshSessionModel,
    SessionEventProjectionModel,
    SessionMessageProjectionModel,
    SessionProjectionCursorModel,
    SessionProjectionModel,
)
from hermes_cloud.platform.sqlalchemy.session_projection_migration_models import (
    SessionEventProjectionV10Model,
    SessionMessageProjectionV10Model,
    SessionProjectionCursorV10Model,
    SessionProjectionV10Model,
    WebSocketTicketV10Model,
)

HISTORICAL_V7_TENANT_MODELS = (
    SessionProjectionV10Model,
    SessionMessageProjectionV10Model,
    SessionEventProjectionV10Model,
    SessionProjectionCursorV10Model,
    PasswordCredentialModel,
    RefreshSessionModel,
    WebSocketTicketV10Model,
)


def _constraint_sql(model: type[object]) -> tuple[str, ...]:
    return tuple(
        str(constraint.sqltext)
        for constraint in model.__table__.constraints  # type: ignore[attr-defined]
        if isinstance(constraint, CheckConstraint)
    )


def test_v7_catalog_is_frozen_and_covers_tables_indexes_and_rls() -> None:
    migration = POSTGRES_V1_MIGRATIONS[6]

    assert migration.version == 7
    assert migration.name == "0007_cloud_client_identity_and_session_projection"
    assert migration.checksum == (
        "1f4cca3ebc4599f1c3d1c2cec79bffa722147af1f2f375f784aa8e4e0abbea7d"
    )
    assert migration.plan.structural_digest == (
        "118a820ded9c3e209026eb01e5f7f1f03b864991d58996d7b27e3a0e5eb2d854"
    )
    assert migration.variables == ("migration_role", "runtime_role")
    keys = {operation.key for operation in migration.plan.operations}
    assert "schema:projection" in keys
    assert "schema:projection:runtime-grant-usage" in keys
    assert "schema:projection:runtime-grant-dml" in keys
    assert "schema:projection:runtime-default-dml" in keys
    for model in HISTORICAL_V7_TENANT_MODELS:
        table = model.__table__
        qualified = f"{table.schema}.{table.name}"
        assert f"table:{qualified}" in keys
        assert f"rls-enable:{qualified}" in keys
        assert f"rls-force:{qualified}" in keys
        assert f"rls-policy:{qualified}" in keys
        for index in table.indexes:
            assert f"index:{index.name}" in keys


def test_identity_models_store_only_argon2_and_token_digests() -> None:
    password_columns = set(PasswordCredentialModel.__table__.columns.keys())
    refresh_columns = set(RefreshSessionModel.__table__.columns.keys())
    ticket_columns = set(WebSocketTicketV10Model.__table__.columns.keys())

    assert "password_hash" in password_columns
    assert "password" not in password_columns
    assert "token_digest" in refresh_columns
    assert "token" not in refresh_columns
    assert "ticket_digest" in ticket_columns
    assert "ticket" not in ticket_columns
    assert WebSocketTicketV10Model.__table__.c.session_key.nullable is True
    assert any("$argon2id$" in sql for sql in _constraint_sql(PasswordCredentialModel))
    assert any("rotation" in sql for sql in _constraint_sql(RefreshSessionModel))
    assert any("consumed_at" in sql for sql in _constraint_sql(WebSocketTicketV10Model))
    assert any(
        "retention_until >= expires_at" in sql
        for sql in _constraint_sql(RefreshSessionModel)
    )
    assert any(
        "retention_until >= expires_at" in sql
        for sql in _constraint_sql(WebSocketTicketV10Model)
    )


def test_projection_models_separate_session_key_lineage_and_freeze_integrity() -> None:
    session_columns = set(SessionProjectionModel.__table__.columns.keys())
    assert {"session_key", "lineage_tip_message_id"}.issubset(session_columns)
    assert SessionProjectionModel.session_key is not (
        SessionProjectionModel.lineage_tip_message_id
    )

    for model in (
        SessionProjectionModel,
        SessionMessageProjectionModel,
        SessionEventProjectionModel,
        SessionProjectionCursorModel,
    ):
        table = model.__table__
        assert "tenant_id" in table.columns
        assert any(
            isinstance(constraint, UniqueConstraint) for constraint in table.constraints
        )
        assert any(isinstance(index, Index) for index in table.indexes)

    assert "retention_until" in SessionProjectionModel.__table__.columns
    assert "retention_until" in SessionMessageProjectionModel.__table__.columns
    assert "retention_until" in SessionEventProjectionModel.__table__.columns
