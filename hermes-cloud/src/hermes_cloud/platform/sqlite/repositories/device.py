"""SQLite binding for the neutral pairing ORM repository."""

from hermes_cloud.platform.sqlalchemy.repositories.recoverable_device import (
    RecoverablePairingRepositoryBase,
)


class SQLitePairingRepository(RecoverablePairingRepositoryBase):
    """SQLite pairing operations use the shared recoverable ORM behavior."""
