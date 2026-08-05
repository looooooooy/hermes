"""Domain records and fail-closed errors for schema migration history."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Final

_CHECKSUM = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class PublishedMigration:
    """Stable migration metadata shared without database implementation types."""

    version: int
    name: str
    checksum: str
    variables: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.version < 1:
            raise ValueError("published migration version must be positive")
        if not self.name.startswith(f"{self.version:04d}_"):
            raise ValueError("published migration name must start with its version")
        if _CHECKSUM.fullmatch(self.checksum) is None:
            raise ValueError("published migration checksum must be SHA-256")
        if len(set(self.variables)) != len(self.variables):
            raise ValueError("published migration variables must be unique")


PUBLISHED_POSTGRES_MIGRATIONS: Final = (
    PublishedMigration(
        version=1,
        name="0001_foundation",
        checksum=("208bcc25a35cb7e083d57586825209456512278c254a56df40d15cab233dc104"),
    ),
    PublishedMigration(
        version=2,
        name="0002_tenant_storage",
        checksum=("748b905dadff5f7fe3ee22325cff7265c1f3136eac586de62bd4a4591db98476"),
    ),
    PublishedMigration(
        version=3,
        name="0003_runtime_role_boundaries",
        checksum=("35354dbd6825de200ad9c00adea614c672be216c8aa75b8cec197c6bb11a8bf7"),
        variables=("database_name", "migration_role", "runtime_role"),
    ),
    PublishedMigration(
        version=4,
        name="0004_foundation_gap_tables",
        checksum=("f1ca950f5c7ef07664312759e811ed2dd0c4284b2ac7fd9d88f96e65860e416f"),
    ),
    PublishedMigration(
        version=5,
        name="0005_public_privilege_hardening",
        checksum=("c1033cb8f78520cb4ed6c7bf28f3fcc6341cd4a2a2f2a0c7110ed78ecd01e679"),
        variables=("database_name",),
    ),
    PublishedMigration(
        version=6,
        name="0006_workspace_role_boundaries",
        checksum=("2e587b795e300b9a042158d2fe68dafb16066ee0de8e02cf6dd6de9f2414684a"),
        variables=("migration_role", "runtime_role"),
    ),
    PublishedMigration(
        version=7,
        name="0007_cloud_client_identity_and_session_projection",
        checksum=("1f4cca3ebc4599f1c3d1c2cec79bffa722147af1f2f375f784aa8e4e0abbea7d"),
        variables=("migration_role", "runtime_role"),
    ),
    PublishedMigration(
        version=8,
        name="0008_device_pairing_and_credentials",
        checksum=("8413b68009185ac947bbd6cdb810578b81d2679876bc9fe71543cfb743c856d3"),
    ),
    PublishedMigration(
        version=9,
        name="0009_connector_transport_cursor",
        checksum=("110554cd52e1fe0f7524552f61d4e5e67c19ce11be7e632522682cf84accb415"),
    ),
    PublishedMigration(
        version=10,
        name="0010_connector_handshake_ownership",
        checksum=("14c09070c8ed0dee01eb3b1a87a6e3623b6241f7462ef8975dabbd87489d3737"),
    ),
    PublishedMigration(
        version=11,
        name="0011_session_projection_durable_identity",
        checksum=("61bce41e2da426e5c36c8d7b4587d3ac0d45bd0bde1731358187c42bed040e22"),
    ),
    PublishedMigration(
        version=12,
        name="0012_session_catalog_v1",
        checksum=("f170a2f8a43c8b17d9c705d8354cc868ce7d5a4763fa7949031e6c5bc8414154"),
    ),
    PublishedMigration(
        version=13,
        name="0013_session_catalog_recovery",
        checksum=("ce3810b7aac562fc71a9996e7bda0444664bae4a5f4f5930a62d18c0cb5bd58d"),
    ),
)

_PUBLISHED_REGISTRY: Final = tuple(
    (
        migration.version,
        migration.name,
        migration.checksum,
        migration.variables,
    )
    for migration in PUBLISHED_POSTGRES_MIGRATIONS
)


def verify_published_migration_registry(
    migrations: Iterable[PublishedMigration] = PUBLISHED_POSTGRES_MIGRATIONS,
) -> None:
    """Require the complete, registered immutable migration sequence."""

    actual = tuple(
        (
            migration.version,
            migration.name,
            migration.checksum,
            migration.variables,
        )
        for migration in migrations
    )
    if actual != _PUBLISHED_REGISTRY:
        raise ValueError("published migration registry must match exactly")


@dataclass(frozen=True, slots=True)
class AppliedMigration:
    version: int
    name: str
    checksum: str
    applied_at: datetime

    def __post_init__(self) -> None:
        if self.version < 1:
            raise ValueError("applied migration version must be positive")
        if not self.name:
            raise ValueError("applied migration name must not be empty")
        if _CHECKSUM.fullmatch(self.checksum) is None:
            raise ValueError("applied migration checksum must be SHA-256")
        if self.applied_at.utcoffset() is None:
            raise ValueError("applied migration time must include a timezone")


class MigrationLockUnavailable(RuntimeError):
    """Raised when another migration session owns the advisory lock."""


class MigrationHistoryConflict(RuntimeError):
    """Raised when stored history is unknown, noncontiguous, or mutated."""


class UnsafeMigrationIdentifier(ValueError):
    """Raised before SQL execution when identifier bindings are unsafe."""
