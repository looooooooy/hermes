from __future__ import annotations

from dataclasses import replace
from types import MappingProxyType

import pytest
from sqlalchemy import CheckConstraint, ForeignKeyConstraint, PrimaryKeyConstraint
from sqlalchemy.schema import CreateSchema

import hermes_cloud.platform.postgres.catalog as catalog_module
from hermes_cloud.domain.migrations import PUBLISHED_POSTGRES_MIGRATIONS
from hermes_cloud.platform.postgres.catalog import (
    POSTGRES_V1_MIGRATIONS,
    MigrationPhase,
    verify_migration_catalog,
)
from hermes_cloud.platform.postgres.models import ALL_TENANT_MODELS

EXPECTED_PUBLISHED_CHECKSUMS = (
    "208bcc25a35cb7e083d57586825209456512278c254a56df40d15cab233dc104",
    "748b905dadff5f7fe3ee22325cff7265c1f3136eac586de62bd4a4591db98476",
    "35354dbd6825de200ad9c00adea614c672be216c8aa75b8cec197c6bb11a8bf7",
    "f1ca950f5c7ef07664312759e811ed2dd0c4284b2ac7fd9d88f96e65860e416f",
    "c1033cb8f78520cb4ed6c7bf28f3fcc6341cd4a2a2f2a0c7110ed78ecd01e679",
    "2e587b795e300b9a042158d2fe68dafb16066ee0de8e02cf6dd6de9f2414684a",
    "1f4cca3ebc4599f1c3d1c2cec79bffa722147af1f2f375f784aa8e4e0abbea7d",
    "8413b68009185ac947bbd6cdb810578b81d2679876bc9fe71543cfb743c856d3",
    "110554cd52e1fe0f7524552f61d4e5e67c19ce11be7e632522682cf84accb415",
    "14c09070c8ed0dee01eb3b1a87a6e3623b6241f7462ef8975dabbd87489d3737",
    "61bce41e2da426e5c36c8d7b4587d3ac0d45bd0bde1731358187c42bed040e22",
    "f170a2f8a43c8b17d9c705d8354cc868ce7d5a4763fa7949031e6c5bc8414154",
    "ce3810b7aac562fc71a9996e7bda0444664bae4a5f4f5930a62d18c0cb5bd58d",
)


def test_catalog_is_numbered_immutable_and_preserves_published_checksums() -> None:
    assert tuple(item.version for item in POSTGRES_V1_MIGRATIONS) == tuple(
        range(1, len(POSTGRES_V1_MIGRATIONS) + 1)
    )
    assert tuple(item.checksum for item in POSTGRES_V1_MIGRATIONS) == (
        EXPECTED_PUBLISHED_CHECKSUMS
    )
    assert all(not hasattr(item, "sql") for item in POSTGRES_V1_MIGRATIONS)
    assert all(item.checksum == item.plan.checksum for item in POSTGRES_V1_MIGRATIONS)
    assert all(
        operation.phase
        in (
            MigrationPhase.EXPAND,
            MigrationPhase.MIGRATE,
            MigrationPhase.CONTRACT,
        )
        for item in POSTGRES_V1_MIGRATIONS
        for operation in item.plan.operations
    )
    assert all(item.plan.structural_digest for item in POSTGRES_V1_MIGRATIONS)
    verify_migration_catalog()


def test_catalog_rejects_typed_statement_drift_even_when_operation_key_is_unchanged() -> (
    None
):
    migration = POSTGRES_V1_MIGRATIONS[0]
    operation = migration.plan.expand[0]
    changed_operation = replace(
        operation,
        _factory=lambda _: CreateSchema("changed_identity"),
    )
    changed_plan = replace(
        migration.plan,
        expand=(changed_operation, *migration.plan.expand[1:]),
    )
    changed_catalog = (
        replace(migration, plan=changed_plan),
        *POSTGRES_V1_MIGRATIONS[1:],
    )

    with pytest.raises(ValueError, match="typed plan mismatch"):
        verify_migration_catalog(changed_catalog)


def test_published_typed_fingerprints_are_runtime_immutable() -> None:
    assert isinstance(catalog_module._PUBLISHED_CATALOG, MappingProxyType)
    with pytest.raises(TypeError):
        catalog_module._PUBLISHED_CATALOG[1] = ()  # type: ignore[index]


def test_plan_resolver_rejects_registered_metadata_with_typed_statement_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = POSTGRES_V1_MIGRATIONS[0]
    operation = migration.plan.expand[0]
    changed_operation = replace(
        operation,
        _factory=lambda _: CreateSchema("changed_identity"),
    )
    changed_plan = replace(
        migration.plan,
        expand=(changed_operation, *migration.plan.expand[1:]),
    )
    monkeypatch.setattr(
        catalog_module,
        "POSTGRES_V1_MIGRATIONS",
        (replace(migration, plan=changed_plan), *POSTGRES_V1_MIGRATIONS[1:]),
    )

    with pytest.raises(ValueError, match="typed plan mismatch"):
        catalog_module.migration_plan_for(PUBLISHED_POSTGRES_MIGRATIONS[0])


@pytest.mark.parametrize(
    "catalog",
    (
        (),
        POSTGRES_V1_MIGRATIONS[:1],
        POSTGRES_V1_MIGRATIONS[:-1],
        POSTGRES_V1_MIGRATIONS[1:],
        (
            *POSTGRES_V1_MIGRATIONS,
            replace(
                POSTGRES_V1_MIGRATIONS[-1],
                version=7,
                name="0007_unregistered",
            ),
        ),
    ),
)
def test_catalog_must_exactly_match_the_frozen_published_registry(
    catalog: tuple[object, ...],
) -> None:
    with pytest.raises(ValueError, match="published migration registry"):
        verify_migration_catalog(catalog)  # type: ignore[arg-type]


def test_all_v1_tenant_tables_have_orm_integrity_and_tenant_scope() -> None:
    assert len(ALL_TENANT_MODELS) == 33
    for model in ALL_TENANT_MODELS:
        table = model.__table__
        assert "tenant_id" in table.columns
        assert table.c.tenant_id.nullable is False
        assert any(
            isinstance(constraint, PrimaryKeyConstraint)
            for constraint in table.constraints
        )
        if model is not ALL_TENANT_MODELS[0]:
            assert any(
                isinstance(constraint, ForeignKeyConstraint)
                for constraint in table.constraints
            )

    assert any(
        isinstance(constraint, CheckConstraint)
        and "pending" in str(constraint.sqltext)
        and "confirmed" in str(constraint.sqltext)
        for constraint in ALL_TENANT_MODELS[9].__table__.constraints
    )


def test_catalog_declares_role_bindings_without_embedding_identifiers() -> None:
    role_migration = POSTGRES_V1_MIGRATIONS[2]
    public_hardening = POSTGRES_V1_MIGRATIONS[4]
    workspace_boundaries = POSTGRES_V1_MIGRATIONS[5]
    identity_projection = POSTGRES_V1_MIGRATIONS[6]

    assert role_migration.variables == (
        "database_name",
        "migration_role",
        "runtime_role",
    )
    assert public_hardening.variables == ("database_name",)
    assert workspace_boundaries.variables == (
        "migration_role",
        "runtime_role",
    )
    assert identity_projection.variables == (
        "migration_role",
        "runtime_role",
    )
