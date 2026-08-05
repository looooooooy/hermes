"""Dry-run-first ORM cleanup for the explicit legacy test-server session."""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid5

from sqlalchemy import create_engine, delete, or_, select, update
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.orm.exc import StaleDataError

from hermes_cloud.configuration import ConfigurationError, DsnFileReference
from hermes_cloud.platform.postgres.models import (
    AgentModel,
    PasswordCredentialModel,
    RoleModel,
    TenantModel,
    UserModel,
    WorkspaceMembershipModel,
    WorkspaceModel,
)
from hermes_cloud.platform.sqlalchemy.observer_projection_models import (
    ObserverDeletionLedgerModel,
    ObserverEventModel,
    ObserverSessionModel,
)
from hermes_cloud.platform.sqlalchemy.observer_subscription_models import (
    ObserverSubscriptionTargetModel,
)
from hermes_cloud.platform.sqlalchemy.session_projection_migration_models import (
    SessionEventProjectionV10Model,
    SessionMessageProjectionV10Model,
    SessionProjectionCursorV10Model,
    SessionProjectionV10Model,
    WebSocketTicketV10Model,
)
from hermes_cloud.platform.sqlite.engine import build_sqlite_engine
from hermes_cloud.platform.sqlite.migrations import (
    PUBLISHED_SQLITE_MIGRATIONS,
    SQLiteSchemaMigration,
)

_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_NAMESPACE = UUID("ba84c827-b174-47f8-bbbd-52cbaf7232b9")
_SEED_TIME = datetime(2026, 7, 31, 0, 0, tzinfo=UTC)
_RETENTION_UNTIL = _SEED_TIME + timedelta(days=365)
_SESSION_KEY = "android-bootstrap"
_SESSION_TITLE = "Hermes Cloud test session"
_INITIAL_MESSAGE = {"text": "Hermes Cloud is connected."}
_MAX_SEED_DEPENDENCY_ROWS = 1_024
_SENSITIVE_ARGUMENT_PREFIXES = (
    "--credential",
    "--dsn",
    "--password",
    "--secret",
    "--token",
)


class CleanupConfigurationError(ValueError):
    """Raised when the explicit cleanup selectors are absent or unsafe."""


class CleanupConflict(RuntimeError):
    """Raised when the database is not the exact removable seed graph."""


class CleanupCommitOutcomeUnknown(RuntimeError):
    """Raised when an apply commit may have reached durable storage."""


class _RedactingArgumentParser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        self.print_usage(sys.stderr)
        self.exit(2, f"{self.prog}: error: invalid arguments\n")


class _CommitAwareTransaction:
    def __init__(self, transaction: object, *, apply: bool) -> None:
        self._transaction = transaction
        self._apply = apply

    def __enter__(self) -> Session:
        return self._transaction.__enter__()

    def __exit__(self, exc_type, exc, traceback) -> bool | None:
        if exc_type is not None:
            return self._transaction.__exit__(exc_type, exc, traceback)
        try:
            return self._transaction.__exit__(None, None, None)
        except Exception:
            if self._apply:
                raise CleanupCommitOutcomeUnknown from None
            raise


@dataclass(frozen=True, slots=True)
class CleanupConfig:
    tenant_slug: str
    tenant_display_name: str
    username: str
    user_display_name: str
    workspace_key: str
    workspace_display_name: str
    agent_key: str

    def __post_init__(self) -> None:
        _require_slug(self.tenant_slug, "tenant slug")
        _require_text(self.tenant_display_name, "tenant display name", maximum=128)
        _require_text(self.username, "username", maximum=254)
        _require_text(self.user_display_name, "user display name", maximum=128)
        _require_slug(self.workspace_key, "workspace key")
        _require_text(
            self.workspace_display_name,
            "workspace display name",
            maximum=128,
        )
        _require_slug(self.agent_key, "agent key")

    @classmethod
    def from_environment(cls, environment: Mapping[str, str]) -> CleanupConfig:
        names = (
            "HERMES_SEED_TENANT_SLUG",
            "HERMES_SEED_TENANT_DISPLAY_NAME",
            "HERMES_SEED_USERNAME",
            "HERMES_SEED_USER_DISPLAY_NAME",
            "HERMES_SEED_WORKSPACE_KEY",
            "HERMES_SEED_WORKSPACE_DISPLAY_NAME",
            "HERMES_SEED_AGENT_KEY",
        )
        values: list[str] = []
        for name in names:
            value = environment.get(name)
            if value is None:
                raise CleanupConfigurationError(f"{name} is required")
            values.append(value)
        return cls(*values)


@dataclass(frozen=True, slots=True)
class CleanupCounts:
    sessions: int = 0
    messages: int = 0
    events: int = 0
    cursors: int = 0
    tickets: int = 0


@dataclass(frozen=True, slots=True)
class CleanupResult:
    mode: str
    status: str
    session_id: UUID
    counts: CleanupCounts


@dataclass(frozen=True, slots=True)
class _SeedIdentity:
    tenant_id: UUID
    user_id: UUID
    role_id: UUID
    workspace_id: UUID
    membership_id: UUID
    credential_id: UUID
    agent_id: UUID
    session_id: UUID
    message_id: UUID


@dataclass(frozen=True, slots=True)
class _CleanupDependencies:
    messages: tuple[tuple[object, ...], ...]
    events: tuple[tuple[object, ...], ...]
    cursors: tuple[tuple[object, ...], ...]
    tickets: tuple[tuple[object, ...], ...]


def _require_slug(value: str, name: str) -> None:
    if not isinstance(value, str) or _SLUG.fullmatch(value) is None:
        raise CleanupConfigurationError(f"{name} is invalid")


def _require_text(value: str, name: str, *, maximum: int) -> None:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= maximum
        or value != value.strip()
        or "\r" in value
        or "\n" in value
    ):
        raise CleanupConfigurationError(f"{name} is invalid")


def _stable_id(kind: str, *parts: str) -> UUID:
    return uuid5(_NAMESPACE, "\x1f".join((kind, *parts)))


def _seed_identity(config: CleanupConfig) -> _SeedIdentity:
    tenant_id = _stable_id("tenant", config.tenant_slug)
    user_id = _stable_id("user", config.tenant_slug, config.username)
    role_id = _stable_id(
        "role",
        config.tenant_slug,
        config.workspace_key,
        "test-user",
    )
    workspace_id = _stable_id(
        "workspace",
        config.tenant_slug,
        config.workspace_key,
    )
    membership_id = _stable_id(
        "workspace-membership",
        str(tenant_id),
        str(workspace_id),
        str(user_id),
        str(role_id),
    )
    credential_id = _stable_id(
        "password-credential",
        config.tenant_slug,
        config.username,
    )
    agent_id = _stable_id(
        "agent",
        config.tenant_slug,
        config.workspace_key,
        config.agent_key,
    )
    session_id = _stable_id(
        "session",
        config.tenant_slug,
        config.workspace_key,
        _SESSION_KEY,
    )
    return _SeedIdentity(
        tenant_id=tenant_id,
        user_id=user_id,
        role_id=role_id,
        workspace_id=workspace_id,
        membership_id=membership_id,
        credential_id=credential_id,
        agent_id=agent_id,
        session_id=session_id,
        message_id=_stable_id("session-message", str(session_id), "1"),
    )


def _normalized(value: object) -> object:
    if isinstance(value, datetime):
        if value.utcoffset() is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
    if isinstance(value, UUID):
        return UUID(str(value))
    return value


def _matches(row: object, values: Mapping[str, object]) -> bool:
    return all(
        _normalized(getattr(row, field)) == _normalized(value)
        for field, value in values.items()
    )


def _one_or_conflict(
    session: Session,
    model: type[object],
    identity: object,
    *,
    resource: str,
) -> object | None:
    rows = tuple(session.scalars(select(model).where(identity).limit(2)).all())
    if len(rows) > 1:
        raise CleanupConflict(f"{resource} identity is ambiguous")
    return rows[0] if rows else None


def _require_exact_anchor(
    session: Session,
    *,
    model: type[object],
    identity: object,
    values: Mapping[str, object],
    resource: str,
) -> object:
    row = _one_or_conflict(session, model, identity, resource=resource)
    if row is None or not _matches(row, values):
        raise CleanupConflict(f"{resource} does not match the explicit seed")
    return row


def _require_revision_10(session: Session) -> None:
    rows = tuple(
        session.query(SQLiteSchemaMigration)
        .order_by(SQLiteSchemaMigration.version)
        .all()
    )
    expected = PUBLISHED_SQLITE_MIGRATIONS[:10]
    if len(rows) != len(expected) or any(
        (row.version, row.name, row.checksum)
        != (migration.version, migration.name, migration.checksum)
        for row, migration in zip(rows, expected, strict=True)
    ):
        raise CleanupConflict("cleanup requires the exact published SQLite revision 10")


def _require_seed_anchors(
    session: Session,
    config: CleanupConfig,
    identity: _SeedIdentity,
) -> None:
    _require_exact_anchor(
        session,
        model=TenantModel,
        identity=or_(
            TenantModel.tenant_id == identity.tenant_id,
            TenantModel.slug == config.tenant_slug,
        ),
        values={
            "tenant_id": identity.tenant_id,
            "slug": config.tenant_slug,
            "display_name": config.tenant_display_name,
            "status": "active",
            "created_at": _SEED_TIME,
        },
        resource="tenant",
    )
    _require_exact_anchor(
        session,
        model=UserModel,
        identity=(
            (UserModel.tenant_id == identity.tenant_id)
            & or_(
                UserModel.user_id == identity.user_id,
                UserModel.subject == config.username,
            )
        ),
        values={
            "tenant_id": identity.tenant_id,
            "user_id": identity.user_id,
            "subject": config.username,
            "display_name": config.user_display_name,
            "email": None,
            "status": "active",
            "created_at": _SEED_TIME,
        },
        resource="user",
    )
    _require_exact_anchor(
        session,
        model=RoleModel,
        identity=(
            (RoleModel.tenant_id == identity.tenant_id)
            & or_(
                RoleModel.role_id == identity.role_id,
                (RoleModel.role_key == "test-user") & (RoleModel.version == 1),
            )
        ),
        values={
            "tenant_id": identity.tenant_id,
            "role_id": identity.role_id,
            "role_key": "test-user",
            "display_name": "Test user",
            "scope_type": "workspace",
            "permissions": [],
            "status": "active",
            "version": 1,
            "created_at": _SEED_TIME,
        },
        resource="workspace role",
    )
    _require_exact_anchor(
        session,
        model=WorkspaceModel,
        identity=(
            (WorkspaceModel.tenant_id == identity.tenant_id)
            & or_(
                WorkspaceModel.workspace_id == identity.workspace_id,
                WorkspaceModel.workspace_key == config.workspace_key,
            )
        ),
        values={
            "tenant_id": identity.tenant_id,
            "workspace_id": identity.workspace_id,
            "workspace_key": config.workspace_key,
            "display_name": config.workspace_display_name,
            "status": "active",
            "created_by": identity.user_id,
            "created_at": _SEED_TIME,
        },
        resource="workspace",
    )
    _require_exact_anchor(
        session,
        model=WorkspaceMembershipModel,
        identity=(
            (WorkspaceMembershipModel.tenant_id == identity.tenant_id)
            & or_(
                WorkspaceMembershipModel.workspace_membership_id
                == identity.membership_id,
                (WorkspaceMembershipModel.workspace_id == identity.workspace_id)
                & (WorkspaceMembershipModel.user_id == identity.user_id)
                & (WorkspaceMembershipModel.role_id == identity.role_id),
            )
        ),
        values={
            "tenant_id": identity.tenant_id,
            "workspace_membership_id": identity.membership_id,
            "workspace_id": identity.workspace_id,
            "user_id": identity.user_id,
            "role_id": identity.role_id,
            "status": "active",
            "joined_at": _SEED_TIME,
            "revoked_at": None,
        },
        resource="workspace membership",
    )
    _require_exact_anchor(
        session,
        model=AgentModel,
        identity=(
            (AgentModel.tenant_id == identity.tenant_id)
            & or_(
                AgentModel.agent_id == identity.agent_id,
                AgentModel.agent_key == config.agent_key,
            )
        ),
        values={
            "tenant_id": identity.tenant_id,
            "agent_id": identity.agent_id,
            "workspace_id": identity.workspace_id,
            "agent_key": config.agent_key,
            "status": "active",
            "last_seen_at": None,
            "created_at": _SEED_TIME,
        },
        resource="agent",
    )
    credential = _require_exact_anchor(
        session,
        model=PasswordCredentialModel,
        identity=(
            (PasswordCredentialModel.tenant_id == identity.tenant_id)
            & or_(
                PasswordCredentialModel.credential_id == identity.credential_id,
                PasswordCredentialModel.user_id == identity.user_id,
                PasswordCredentialModel.subject == config.username,
            )
        ),
        values={
            "tenant_id": identity.tenant_id,
            "credential_id": identity.credential_id,
            "user_id": identity.user_id,
            "subject": config.username,
            "status": "active",
            "created_at": _SEED_TIME,
            "updated_at": _SEED_TIME,
        },
        resource="password credential",
    )
    if not str(credential.password_hash).startswith("$argon2id$"):
        raise CleanupConflict("password credential does not match the explicit seed")


def _seed_session_candidate(
    session: Session,
    identity: _SeedIdentity,
) -> SessionProjectionV10Model | None:
    candidate = _one_or_conflict(
        session,
        SessionProjectionV10Model,
        (SessionProjectionV10Model.tenant_id == identity.tenant_id)
        & or_(
            SessionProjectionV10Model.session_id == identity.session_id,
            SessionProjectionV10Model.session_key == _SESSION_KEY,
            SessionProjectionV10Model.title == _SESSION_TITLE,
        ),
        resource="session projection",
    )
    if candidate is None:
        return None
    if not _matches(
        candidate,
        {
            "tenant_id": identity.tenant_id,
            "session_id": identity.session_id,
            "session_key": _SESSION_KEY,
            "workspace_id": identity.workspace_id,
            "title": _SESSION_TITLE,
            "state": "active",
            "revision": 1,
            "lineage_tip_message_id": identity.message_id,
            "lineage_tip_sequence": 1,
            "started_at": _SEED_TIME,
            "updated_at": _SEED_TIME,
            "closed_at": None,
            "retention_until": _RETENTION_UNTIL,
        },
    ) or candidate.agent_id not in {None, identity.agent_id}:
        raise CleanupConflict("session projection does not match the explicit seed")
    return candidate


def _bounded_identity_rows(
    session: Session,
    statement: object,
    *,
    resource: str,
) -> tuple[tuple[object, ...], ...]:
    rows = tuple(
        tuple(row)
        for row in session.execute(statement.limit(_MAX_SEED_DEPENDENCY_ROWS + 1)).all()
    )
    if len(rows) > _MAX_SEED_DEPENDENCY_ROWS:
        raise CleanupConflict(f"{resource} exceeds bounded cleanup limit")
    return rows


def _rows_for_seed_session(
    session: Session,
    identity: _SeedIdentity,
) -> _CleanupDependencies:
    messages = _bounded_identity_rows(
        session,
        select(
            SessionMessageProjectionV10Model.tenant_id,
            SessionMessageProjectionV10Model.session_id,
            SessionMessageProjectionV10Model.message_id,
        )
        .where(
            SessionMessageProjectionV10Model.tenant_id == identity.tenant_id,
            SessionMessageProjectionV10Model.session_id == identity.session_id,
        )
        .order_by(
            SessionMessageProjectionV10Model.tenant_id,
            SessionMessageProjectionV10Model.session_id,
            SessionMessageProjectionV10Model.message_id,
        ),
        resource="session messages",
    )
    events = _bounded_identity_rows(
        session,
        select(
            SessionEventProjectionV10Model.tenant_id,
            SessionEventProjectionV10Model.session_id,
            SessionEventProjectionV10Model.event_id,
        )
        .where(
            SessionEventProjectionV10Model.tenant_id == identity.tenant_id,
            SessionEventProjectionV10Model.session_id == identity.session_id,
        )
        .order_by(
            SessionEventProjectionV10Model.tenant_id,
            SessionEventProjectionV10Model.session_id,
            SessionEventProjectionV10Model.event_id,
        ),
        resource="session events",
    )
    cursors = _bounded_identity_rows(
        session,
        select(
            SessionProjectionCursorV10Model.tenant_id,
            SessionProjectionCursorV10Model.session_id,
            SessionProjectionCursorV10Model.stream,
        )
        .where(
            SessionProjectionCursorV10Model.tenant_id == identity.tenant_id,
            SessionProjectionCursorV10Model.session_id == identity.session_id,
        )
        .order_by(
            SessionProjectionCursorV10Model.tenant_id,
            SessionProjectionCursorV10Model.session_id,
            SessionProjectionCursorV10Model.stream,
        ),
        resource="session cursors",
    )
    tickets = _bounded_identity_rows(
        session,
        select(
            WebSocketTicketV10Model.tenant_id,
            WebSocketTicketV10Model.ticket_id,
        )
        .where(
            WebSocketTicketV10Model.tenant_id == identity.tenant_id,
            WebSocketTicketV10Model.session_key == _SESSION_KEY,
        )
        .order_by(
            WebSocketTicketV10Model.tenant_id,
            WebSocketTicketV10Model.ticket_id,
        ),
        resource="WebSocket tickets",
    )
    return _CleanupDependencies(
        messages=messages,
        events=events,
        cursors=cursors,
        tickets=tickets,
    )


def _require_initial_seed_message(
    session: Session,
    identity: _SeedIdentity,
) -> None:
    message = session.get(
        SessionMessageProjectionV10Model,
        (identity.tenant_id, identity.session_id, identity.message_id),
    )
    if message is None or not _matches(
        message,
        {
            "tenant_id": identity.tenant_id,
            "session_id": identity.session_id,
            "message_id": identity.message_id,
            "sequence": 1,
            "role": "assistant",
            "content": _INITIAL_MESSAGE,
            "parent_message_id": None,
            "created_at": _SEED_TIME,
            "retention_until": _RETENTION_UNTIL,
        },
    ):
        raise CleanupConflict("initial message does not match the explicit seed")


def _require_no_authoritative_observer(
    session: Session,
    identity: _SeedIdentity,
) -> None:
    observer = session.execute(
        select(
            ObserverSessionModel.tenant_id,
            ObserverSessionModel.session_id,
        )
        .where(
            ObserverSessionModel.tenant_id == identity.tenant_id,
            ObserverSessionModel.session_key == _SESSION_KEY,
        )
        .limit(1)
    ).first()
    target = session.execute(
        select(
            ObserverSubscriptionTargetModel.tenant_id,
            ObserverSubscriptionTargetModel.target_subscription_id,
        )
        .where(
            ObserverSubscriptionTargetModel.tenant_id == identity.tenant_id,
            ObserverSubscriptionTargetModel.session_key == _SESSION_KEY,
        )
        .limit(1)
    ).first()
    deletion_ledger = session.execute(
        select(
            ObserverDeletionLedgerModel.tenant_id,
            ObserverDeletionLedgerModel.session_id,
        )
        .where(
            ObserverDeletionLedgerModel.tenant_id == identity.tenant_id,
            ObserverDeletionLedgerModel.session_key == _SESSION_KEY,
        )
        .limit(1)
    ).first()
    event = session.execute(
        select(
            ObserverEventModel.tenant_id,
            ObserverEventModel.session_id,
            ObserverEventModel.event_sequence,
        )
        .where(
            ObserverEventModel.tenant_id == identity.tenant_id,
            ObserverEventModel.session_key == _SESSION_KEY,
        )
        .limit(1)
    ).first()
    if any(row is not None for row in (observer, target, deletion_ledger, event)):
        raise CleanupConflict("authoritative Observer evidence exists")


def _acquire_sqlite_writer(session: Session, identity: _SeedIdentity) -> None:
    session.execute(
        update(SessionProjectionV10Model)
        .where(
            SessionProjectionV10Model.tenant_id == identity.tenant_id,
            SessionProjectionV10Model.session_id == identity.session_id,
        )
        .values(revision=SessionProjectionV10Model.revision)
        .execution_options(synchronize_session=False)
    )


def _require_deleted_count(result: object, expected: int, resource: str) -> None:
    if getattr(result, "rowcount", None) != expected:
        raise CleanupConflict(f"{resource} changed during bounded cleanup")


def _delete_seed_graph(
    session: Session,
    identity: _SeedIdentity,
    dependencies: _CleanupDependencies,
) -> None:
    _require_deleted_count(
        session.execute(
            delete(WebSocketTicketV10Model)
            .where(
                WebSocketTicketV10Model.tenant_id == identity.tenant_id,
                WebSocketTicketV10Model.session_key == _SESSION_KEY,
            )
            .execution_options(synchronize_session=False)
        ),
        len(dependencies.tickets),
        "WebSocket tickets",
    )
    for model, expected, resource in (
        (SessionProjectionCursorV10Model, len(dependencies.cursors), "cursors"),
        (SessionEventProjectionV10Model, len(dependencies.events), "events"),
        (SessionMessageProjectionV10Model, len(dependencies.messages), "messages"),
    ):
        _require_deleted_count(
            session.execute(
                delete(model)
                .where(
                    model.tenant_id == identity.tenant_id,
                    model.session_id == identity.session_id,
                )
                .execution_options(synchronize_session=False)
            ),
            expected,
            resource,
        )
    _require_deleted_count(
        session.execute(
            delete(SessionProjectionV10Model)
            .where(
                SessionProjectionV10Model.tenant_id == identity.tenant_id,
                SessionProjectionV10Model.session_id == identity.session_id,
            )
            .execution_options(synchronize_session=False)
        ),
        1,
        "session projection",
    )


def _cleanup_once(
    *,
    session_factory: object,
    config: CleanupConfig,
    apply: bool,
) -> CleanupResult:
    identity = _seed_identity(config)
    with _CommitAwareTransaction(session_factory.begin(), apply=apply) as session:
        if apply:
            _acquire_sqlite_writer(session, identity)
        _require_revision_10(session)
        _require_seed_anchors(session, config, identity)
        projection = _seed_session_candidate(session, identity)
        dependencies = _rows_for_seed_session(session, identity)
        if projection is None:
            if any(
                (
                    dependencies.messages,
                    dependencies.events,
                    dependencies.cursors,
                    dependencies.tickets,
                )
            ):
                raise CleanupConflict("seed session dependency graph is partial")
            return CleanupResult(
                mode="apply" if apply else "plan",
                status="absent",
                session_id=identity.session_id,
                counts=CleanupCounts(),
            )
        _require_initial_seed_message(session, identity)
        _require_no_authoritative_observer(session, identity)
        counts = CleanupCounts(
            sessions=1,
            messages=len(dependencies.messages),
            events=len(dependencies.events),
            cursors=len(dependencies.cursors),
            tickets=len(dependencies.tickets),
        )
        if apply:
            _delete_seed_graph(session, identity, dependencies)
        return CleanupResult(
            mode="apply" if apply else "plan",
            status="removed" if apply else "ready",
            session_id=identity.session_id,
            counts=counts,
        )


def cleanup_test_seed_session(
    *,
    session_factory: object,
    config: CleanupConfig,
    apply: bool,
) -> CleanupResult:
    try:
        return _cleanup_once(
            session_factory=session_factory,
            config=config,
            apply=apply,
        )
    except (IntegrityError, OperationalError, StaleDataError):
        if not apply:
            raise
    validation = _cleanup_once(
        session_factory=session_factory,
        config=config,
        apply=False,
    )
    if validation.status != "absent":
        raise CleanupConflict("concurrent cleanup did not converge")
    return CleanupResult(
        mode="apply",
        status="absent",
        session_id=validation.session_id,
        counts=CleanupCounts(),
    )


def _arguments(argv: list[str] | None) -> argparse.Namespace:
    parser = _RedactingArgumentParser(
        prog="cleanup_test_seed_session.py",
        usage="%(prog)s [-h] [--apply]",
        description="Plan or remove the explicit legacy test-server session graph",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="remove the reviewed session graph in one ORM transaction",
    )
    arguments = list(sys.argv[1:] if argv is None else argv)
    if any(
        argument.startswith(prefix)
        for argument in arguments
        for prefix in _SENSITIVE_ARGUMENT_PREFIXES
    ):
        parser.error("sensitive command-line arguments are forbidden")
    return parser.parse_args(arguments)


def _read_database_url(path: str) -> str:
    if not isinstance(path, str) or not os.path.isabs(path):
        raise CleanupConfigurationError("bootstrap database reference is invalid")
    try:
        return DsnFileReference(path).read()
    except ConfigurationError:
        raise CleanupConfigurationError(
            "bootstrap database reference is invalid"
        ) from None


def _dispose_engine(engine: Engine | None) -> BaseException | None:
    if engine is None:
        return None
    try:
        engine.dispose()
    except BaseException as error:  # noqa: BLE001 - Preserve primary failures.
        return error
    return None


def main(
    argv: list[str] | None = None,
    *,
    environment: Mapping[str, str] = os.environ,
    engine_factory: Callable[..., Engine] = create_engine,
    session_factory_builder: Callable[..., object] = sessionmaker,
) -> None:
    arguments = _arguments(argv)
    engine: Engine | None = None
    try:
        config = CleanupConfig.from_environment(environment)
        database_url = _read_database_url(
            environment.get("HERMES_BOOTSTRAP_DSN_FILE", "")
        )
        if make_url(database_url).drivername not in {"sqlite", "sqlite+pysqlite"}:
            raise CleanupConfigurationError("cleanup requires SQLite")
        engine = build_sqlite_engine(
            database_url,
            engine_factory=engine_factory,
        )
        factory = session_factory_builder(bind=engine, expire_on_commit=False)
        result = cleanup_test_seed_session(
            session_factory=factory,
            config=config,
            apply=arguments.apply,
        )
    except CleanupCommitOutcomeUnknown:
        _dispose_engine(engine)
        raise SystemExit("cleanup outcome unknown; rerun plan") from None
    except Exception:  # noqa: BLE001 - CLI boundary redacts storage details.
        _dispose_engine(engine)
        raise SystemExit("cleanup failed; database unchanged") from None
    except BaseException:
        _dispose_engine(engine)
        raise

    dispose_error = _dispose_engine(engine)
    if dispose_error is not None:
        if not isinstance(dispose_error, Exception):
            raise dispose_error
        if result.mode == "apply":
            raise SystemExit("cleanup committed; cleanup failed") from None
        raise SystemExit("cleanup failed; database unchanged") from None

    print(
        f"cleanup_mode={result.mode} status={result.status} "
        "schema_version=10 "
        f"session_id={result.session_id} sessions={result.counts.sessions} "
        f"messages={result.counts.messages} events={result.counts.events} "
        f"cursors={result.counts.cursors} tickets={result.counts.tickets}"
    )


if __name__ == "__main__":
    main()
