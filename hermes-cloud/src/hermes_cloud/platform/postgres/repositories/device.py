"""PostgreSQL binding for the provider-neutral pairing ORM repository."""

from hermes_cloud.platform.sqlalchemy.repositories.device import (
    SqlAlchemyPairingRepositoryBase,
)


class SqlAlchemyPairingRepository(SqlAlchemyPairingRepositoryBase):
    """Use shared mapped-entity pairing behavior with PostgreSQL."""
