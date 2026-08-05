"""PostgreSQL bindings for the neutral operation-scoped runtime."""

from __future__ import annotations

from hermes_cloud.platform.postgres.repositories.device import (
    SqlAlchemyPairingRepository,
)
from hermes_cloud.platform.postgres.repositories.identity import (
    SqlAlchemyIdentityRepository,
)
from hermes_cloud.platform.postgres.repositories.projection import (
    SqlAlchemySessionProjectionRepository,
)
from hermes_cloud.platform.sqlalchemy.runtime import (
    OperationScopedIdentityRepository as _OperationScopedIdentityRepository,
)
from hermes_cloud.platform.sqlalchemy.runtime import (
    OperationScopedPairingRepository as _OperationScopedPairingRepository,
)
from hermes_cloud.platform.sqlalchemy.runtime import (
    OperationScopedSessionProjectionRepository as _ScopedProjectionRepository,
)
from hermes_cloud.platform.sqlalchemy.runtime import (
    SessionFactory,
    SqlAlchemyLoginTenantResolver,
)
from hermes_cloud.platform.sqlalchemy.runtime import (
    SqlAlchemyDatabaseProbe as _SqlAlchemyDatabaseProbe,
)


class OperationScopedIdentityRepository(_OperationScopedIdentityRepository):
    """Bind PostgreSQL identity writes to neutral transaction scopes."""

    def __init__(self, session_factory: SessionFactory) -> None:
        super().__init__(session_factory, SqlAlchemyIdentityRepository)


class OperationScopedPairingRepository(_OperationScopedPairingRepository):
    """Bind PostgreSQL pairing writes to neutral transaction scopes."""

    def __init__(self, session_factory: SessionFactory) -> None:
        super().__init__(session_factory, SqlAlchemyPairingRepository)


class OperationScopedSessionProjectionRepository(_ScopedProjectionRepository):
    """Bind PostgreSQL projection writes to neutral transaction scopes."""

    def __init__(self, session_factory: SessionFactory) -> None:
        super().__init__(session_factory, SqlAlchemySessionProjectionRepository)


class SqlAlchemyDatabaseProbe(_SqlAlchemyDatabaseProbe):
    """PostgreSQL-named ORM readiness probe."""

    def __init__(self, session_factory: SessionFactory) -> None:
        super().__init__(session_factory, name="postgresql")


__all__ = (
    "OperationScopedIdentityRepository",
    "OperationScopedPairingRepository",
    "OperationScopedSessionProjectionRepository",
    "SessionFactory",
    "SqlAlchemyDatabaseProbe",
    "SqlAlchemyLoginTenantResolver",
)
