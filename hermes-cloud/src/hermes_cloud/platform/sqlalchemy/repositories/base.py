"""Shared SQLAlchemy Session ownership for provider-neutral repositories."""

from sqlalchemy.orm import Session


class SqlAlchemySessionRepositoryBase:
    """Hold one caller-owned ORM Session; transaction scope stays outside."""

    def __init__(self, session: Session) -> None:
        self._session = session
