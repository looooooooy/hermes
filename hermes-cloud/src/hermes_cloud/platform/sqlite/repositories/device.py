"""SQLite binding for the neutral pairing ORM repository."""

from hermes_cloud.platform.sqlalchemy.repositories.device import (
    SqlAlchemyPairingRepositoryBase,
)


class SQLitePairingRepository(SqlAlchemyPairingRepositoryBase):
    """SQLite pairing operations use the shared mapped-entity behavior."""
