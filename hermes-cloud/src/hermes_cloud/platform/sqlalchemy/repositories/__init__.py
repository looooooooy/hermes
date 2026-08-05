"""Shared SQLAlchemy repository behavior."""

from hermes_cloud.platform.sqlalchemy.repositories.identity import (
    SqlAlchemyIdentityRepositoryBase,
)
from hermes_cloud.platform.sqlalchemy.repositories.projection import (
    SqlAlchemySessionProjectionRepositoryBase,
)

__all__ = (
    "SqlAlchemyIdentityRepositoryBase",
    "SqlAlchemySessionProjectionRepositoryBase",
)
