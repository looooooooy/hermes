"""PostgreSQL binding for the provider-neutral pairing ORM repository."""

from hermes_cloud.platform.sqlalchemy.repositories.recoverable_device import (
    RecoverablePairingRepositoryBase,
)


class SqlAlchemyPairingRepository(RecoverablePairingRepositoryBase):
    """Use shared recoverable pairing behavior with PostgreSQL."""
