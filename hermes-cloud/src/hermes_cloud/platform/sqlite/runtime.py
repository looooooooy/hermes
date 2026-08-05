"""SQLite bindings for the neutral operation-scoped runtime."""

from __future__ import annotations

from hermes_cloud.platform.sqlalchemy.runtime import (
    OperationScopedIdentityRepository,
    OperationScopedPairingRepository,
    OperationScopedSessionProjectionRepository,
    SessionFactory,
    SqlAlchemyDatabaseProbe,
    SqlAlchemyLoginTenantResolver,
)
from hermes_cloud.platform.sqlite.repositories.device import (
    SQLitePairingRepository,
)
from hermes_cloud.platform.sqlite.repositories.identity import (
    SQLiteIdentityRepository,
)
from hermes_cloud.platform.sqlite.repositories.projection import (
    SQLiteSessionProjectionRepository,
)


class SQLiteOperationScopedIdentityRepository(OperationScopedIdentityRepository):
    """Bind SQLite identity writes to neutral transaction scopes."""

    def __init__(self, session_factory: SessionFactory) -> None:
        super().__init__(session_factory, SQLiteIdentityRepository)


class SQLiteOperationScopedPairingRepository(OperationScopedPairingRepository):
    """Bind SQLite pairing writes to neutral transaction scopes."""

    def __init__(self, session_factory: SessionFactory) -> None:
        super().__init__(session_factory, SQLitePairingRepository)


class SQLiteOperationScopedSessionProjectionRepository(
    OperationScopedSessionProjectionRepository
):
    """Bind SQLite projection writes to neutral transaction scopes."""

    def __init__(self, session_factory: SessionFactory) -> None:
        super().__init__(session_factory, SQLiteSessionProjectionRepository)


class SQLiteLoginTenantResolver(SqlAlchemyLoginTenantResolver):
    """Resolve login tenants through the neutral ORM query."""


class SQLiteDatabaseProbe(SqlAlchemyDatabaseProbe):
    """SQLite-named ORM readiness probe."""

    def __init__(self, session_factory: SessionFactory) -> None:
        super().__init__(session_factory, name="sqlite")
