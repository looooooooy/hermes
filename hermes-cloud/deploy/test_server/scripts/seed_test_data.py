"""Explicit, dry-run-first ORM seed process for the test server."""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID, uuid5

from sqlalchemy import and_, create_engine, or_, select
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from hermes_cloud.configuration import ConfigurationError, DsnFileReference
from hermes_cloud.modules.identity.domain import Argon2PasswordHasher
from hermes_cloud.platform.postgres.models import (
    AgentModel,
    DeviceModel,
    PasswordCredentialModel,
    RoleModel,
    TenantModel,
    UserModel,
    WorkspaceMembershipModel,
    WorkspaceModel,
)
from hermes_cloud.platform.sqlite.engine import build_sqlite_engine

_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_NAMESPACE = UUID("ba84c827-b174-47f8-bbbd-52cbaf7232b9")
_SEED_TIME = datetime(2026, 7, 31, 0, 0, tzinfo=UTC)
_BASE_SEED_RESOURCE_COUNT = 6
_SENSITIVE_ARGUMENT_PREFIXES = (
    "--credential",
    "--dsn",
    "--password",
    "--secret",
    "--token",
)


class SeedConfigurationError(ValueError):
    """Raised when explicit seed configuration is absent or unsafe."""


class SeedConflict(RuntimeError):
    """Raised when an existing row differs from the deterministic seed."""


class SeedCommitOutcomeUnknown(RuntimeError):
    """Raised when an apply commit may have reached PostgreSQL."""


class _RedactingArgumentParser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        self.print_usage(sys.stderr)
        self.exit(2, f"{self.prog}: error: invalid arguments\n")


class _PasswordHasher(Protocol):
    def hash(self, password: str) -> str: ...

    def verify(self, password_hash: str, password: str) -> bool: ...


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
        except IntegrityError:
            raise
        except Exception:
            if self._apply:
                raise SeedCommitOutcomeUnknown from None
            raise


@dataclass(frozen=True, slots=True)
class SeedConfig:
    tenant_slug: str
    tenant_display_name: str
    username: str
    user_display_name: str
    workspace_key: str
    workspace_display_name: str
    owner_control_enabled: bool = False
    agent_key: str | None = None
    device_key: str | None = None

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
        if type(self.owner_control_enabled) is not bool:
            raise SeedConfigurationError("owner-control seed opt-in is invalid")
        if self.agent_key is None:
            raise SeedConfigurationError("agent seed identifier is required")
        _require_slug(self.agent_key, "agent key")
        if self.owner_control_enabled:
            if self.device_key is None:
                raise SeedConfigurationError("owner-control device identifier is required")
            _require_slug(self.device_key, "device key")
        elif self.device_key is not None:
            raise SeedConfigurationError(
                "owner-control device identifier requires opt-in"
            )

    @classmethod
    def from_environment(cls, environment: Mapping[str, str]) -> SeedConfig:
        names = (
            "HERMES_SEED_TENANT_SLUG",
            "HERMES_SEED_TENANT_DISPLAY_NAME",
            "HERMES_SEED_USERNAME",
            "HERMES_SEED_USER_DISPLAY_NAME",
            "HERMES_SEED_WORKSPACE_KEY",
            "HERMES_SEED_WORKSPACE_DISPLAY_NAME",
        )
        values: list[str] = []
        for name in names:
            value = environment.get(name)
            if value is None:
                raise SeedConfigurationError(f"{name} is required")
            values.append(value)
        enabled = environment.get(
            "HERMES_SEED_OWNER_CONTROL_ENABLED",
            "false",
        )
        if enabled not in {"false", "true"}:
            raise SeedConfigurationError("HERMES_SEED_OWNER_CONTROL_ENABLED is invalid")
        agent_key = environment.get("HERMES_SEED_AGENT_KEY")
        device_key = None
        if enabled == "true":
            device_key = environment.get("HERMES_SEED_DEVICE_KEY")
        return cls(
            *values,
            owner_control_enabled=enabled == "true",
            agent_key=agent_key,
            device_key=device_key,
        )

    def as_environment(self) -> dict[str, str]:
        values = {
            "HERMES_SEED_TENANT_SLUG": self.tenant_slug,
            "HERMES_SEED_TENANT_DISPLAY_NAME": self.tenant_display_name,
            "HERMES_SEED_USERNAME": self.username,
            "HERMES_SEED_USER_DISPLAY_NAME": self.user_display_name,
            "HERMES_SEED_WORKSPACE_KEY": self.workspace_key,
            "HERMES_SEED_WORKSPACE_DISPLAY_NAME": self.workspace_display_name,
            "HERMES_SEED_OWNER_CONTROL_ENABLED": (
                "true" if self.owner_control_enabled else "false"
            ),
        }
        assert self.agent_key is not None
        values["HERMES_SEED_AGENT_KEY"] = self.agent_key
        if self.owner_control_enabled:
            assert self.device_key is not None
            values["HERMES_SEED_DEVICE_KEY"] = self.device_key
        return values


@dataclass(frozen=True, slots=True)
class SeedResult:
    mode: str
    created: int
    existing: int
    updated: int


def _require_slug(value: str, name: str) -> None:
    if not isinstance(value, str) or _SLUG.fullmatch(value) is None:
        raise SeedConfigurationError(f"{name} is invalid")


def _require_text(value: str, name: str, *, maximum: int) -> None:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= maximum
        or value != value.strip()
        or "\r" in value
        or "\n" in value
    ):
        raise SeedConfigurationError(f"{name} is invalid")


def read_secret_file(path: str, *, name: str) -> str:
    if not isinstance(path, str) or not os.path.isabs(path):
        raise SeedConfigurationError(f"{name} credential path must be absolute")
    try:
        return DsnFileReference(path).read()
    except ConfigurationError:
        raise SeedConfigurationError(f"{name} credential is invalid") from None


def _stable_id(kind: str, *parts: str) -> UUID:
    return uuid5(_NAMESPACE, "\x1f".join((kind, *parts)))


def _one_or_none(
    session: Session,
    model: type[object],
    identity: object,
    *,
    resource: str,
) -> object | None:
    rows = session.query(model).filter(identity).limit(2).all()
    if len(rows) > 1:
        raise SeedConflict(f"{resource} identity is ambiguous")
    return rows[0] if rows else None


def _matches(model: object, values: Mapping[str, object]) -> bool:
    return all(getattr(model, field) == value for field, value in values.items())


def _ensure_model(
    session: Session,
    *,
    model: type[object],
    identity: object,
    values: Mapping[str, object],
    resource: str,
    apply: bool,
) -> bool:
    existing = _one_or_none(
        session,
        model,
        identity,
        resource=resource,
    )
    if existing is not None:
        if not _matches(existing, values):
            raise SeedConflict(f"{resource} content conflicts with seed")
        return False
    if apply:
        session.add(model(**values))
    return True


def _ensure_agent(
    session: Session,
    *,
    tenant_id: UUID,
    agent_id: UUID,
    workspace_id: UUID,
    agent_key: str,
    apply: bool,
) -> bool:
    rows = session.scalars(
        select(AgentModel)
        .where(
            AgentModel.tenant_id == tenant_id,
            or_(
                AgentModel.agent_id == agent_id,
                AgentModel.agent_key == agent_key,
            ),
        )
        .limit(2)
    ).all()
    if len(rows) > 1:
        raise SeedConflict("agent identity is ambiguous")
    values = {
        "tenant_id": tenant_id,
        "agent_id": agent_id,
        "workspace_id": workspace_id,
        "agent_key": agent_key,
        "status": "active",
        "last_seen_at": None,
        "created_at": _SEED_TIME,
    }
    if rows:
        if not _matches(rows[0], values):
            raise SeedConflict("agent content conflicts with seed")
        return False
    if apply:
        session.add(AgentModel(**values))
    return True


def _resource_count(config: SeedConfig) -> int:
    return _BASE_SEED_RESOURCE_COUNT + 1 + (
        1 if config.owner_control_enabled else 0
    )


def _seed_test_data_once(
    *,
    session_factory: object,
    config: SeedConfig,
    initial_password: str,
    apply: bool,
    password_hasher: _PasswordHasher | None = None,
) -> SeedResult:
    _require_text(initial_password, "initial password", maximum=1024)
    hasher = password_hasher or Argon2PasswordHasher()

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
    agent_key = config.agent_key
    if agent_key is None:
        raise SeedConfigurationError("agent seed identifier is required")
    agent_id = _stable_id(
        "agent",
        config.tenant_slug,
        config.workspace_key,
        agent_key,
    )
    device_id = (
        _stable_id(
            "device",
            config.tenant_slug,
            config.device_key,
        )
        if config.device_key is not None
        else None
    )

    tenant_values = {
        "tenant_id": tenant_id,
        "slug": config.tenant_slug,
        "display_name": config.tenant_display_name,
        "status": "active",
        "created_at": _SEED_TIME,
    }
    user_values = {
        "tenant_id": tenant_id,
        "user_id": user_id,
        "subject": config.username,
        "display_name": config.user_display_name,
        "email": None,
        "status": "active",
        "created_at": _SEED_TIME,
    }
    role_values = {
        "tenant_id": tenant_id,
        "role_id": role_id,
        "role_key": "test-user",
        "display_name": "Test user",
        "scope_type": "workspace",
        "permissions": [],
        "status": "active",
        "version": 1,
        "created_at": _SEED_TIME,
    }
    workspace_values = {
        "tenant_id": tenant_id,
        "workspace_id": workspace_id,
        "workspace_key": config.workspace_key,
        "display_name": config.workspace_display_name,
        "status": "active",
        "created_by": user_id,
        "created_at": _SEED_TIME,
    }
    membership_values = {
        "tenant_id": tenant_id,
        "workspace_membership_id": membership_id,
        "workspace_id": workspace_id,
        "user_id": user_id,
        "role_id": role_id,
        "status": "active",
        "joined_at": _SEED_TIME,
        "revoked_at": None,
    }
    created = 0
    existing = 0
    updated = 0
    with _CommitAwareTransaction(
        session_factory.begin(),
        apply=apply,
    ) as session:
        resources = [
            (
                TenantModel,
                or_(
                    TenantModel.tenant_id == tenant_id,
                    TenantModel.slug == config.tenant_slug,
                ),
                tenant_values,
                "tenant",
            ),
            (
                UserModel,
                and_(
                    UserModel.tenant_id == tenant_id,
                    or_(
                        UserModel.user_id == user_id,
                        UserModel.subject == config.username,
                    ),
                ),
                user_values,
                "user",
            ),
            (
                RoleModel,
                and_(
                    RoleModel.tenant_id == tenant_id,
                    or_(
                        RoleModel.role_id == role_id,
                        and_(
                            RoleModel.role_key == "test-user",
                            RoleModel.version == 1,
                        ),
                    ),
                ),
                role_values,
                "workspace role",
            ),
            (
                WorkspaceModel,
                and_(
                    WorkspaceModel.tenant_id == tenant_id,
                    or_(
                        WorkspaceModel.workspace_id == workspace_id,
                        WorkspaceModel.workspace_key == config.workspace_key,
                    ),
                ),
                workspace_values,
                "workspace",
            ),
            (
                WorkspaceMembershipModel,
                and_(
                    WorkspaceMembershipModel.tenant_id == tenant_id,
                    or_(
                        WorkspaceMembershipModel.workspace_membership_id
                        == membership_id,
                        and_(
                            WorkspaceMembershipModel.workspace_id == workspace_id,
                            WorkspaceMembershipModel.user_id == user_id,
                            WorkspaceMembershipModel.role_id == role_id,
                        ),
                    ),
                ),
                membership_values,
                "workspace membership",
            ),
        ]
        for model, identity, values, resource in resources:
            if _ensure_model(
                session,
                model=model,
                identity=identity,
                values=values,
                resource=resource,
                apply=apply,
            ):
                created += 1
            else:
                existing += 1

        if _ensure_agent(
            session,
            tenant_id=tenant_id,
            agent_id=agent_id,
            workspace_id=workspace_id,
            agent_key=agent_key,
            apply=apply,
        ):
            created += 1
        else:
            existing += 1

        if config.owner_control_enabled:
            assert device_id is not None
            assert config.device_key is not None
            if _ensure_model(
                session,
                model=DeviceModel,
                identity=and_(
                    DeviceModel.tenant_id == tenant_id,
                    or_(
                        DeviceModel.device_id == device_id,
                        DeviceModel.device_key == config.device_key,
                    ),
                ),
                values={
                    "tenant_id": tenant_id,
                    "device_id": device_id,
                    "agent_id": agent_id,
                    "workspace_id": workspace_id,
                    "device_key": config.device_key,
                    "status": "active",
                    "created_at": _SEED_TIME,
                },
                resource="owner-control device",
                apply=apply,
            ):
                created += 1
            else:
                existing += 1

        credential_identity = or_(
            PasswordCredentialModel.subject == config.username,
            and_(
                PasswordCredentialModel.tenant_id == tenant_id,
                or_(
                    PasswordCredentialModel.credential_id == credential_id,
                    PasswordCredentialModel.user_id == user_id,
                ),
            ),
        )
        credential = _one_or_none(
            session,
            PasswordCredentialModel,
            credential_identity,
            resource="password credential",
        )
        credential_values = {
            "tenant_id": tenant_id,
            "credential_id": credential_id,
            "user_id": user_id,
            "subject": config.username,
            "status": "active",
            "created_at": _SEED_TIME,
            "updated_at": _SEED_TIME,
        }
        if credential is None:
            created += 1
            if apply:
                session.add(
                    PasswordCredentialModel(
                        **credential_values,
                        password_hash=hasher.hash(initial_password),
                    )
                )
        else:
            if not _matches(credential, credential_values) or not hasher.verify(
                credential.password_hash,
                initial_password,
            ):
                raise SeedConflict("password credential conflicts with seed")
            existing += 1

    return SeedResult(
        mode="apply" if apply else "plan",
        created=created,
        existing=existing,
        updated=updated,
    )


def seed_test_data(
    *,
    session_factory: object,
    config: SeedConfig,
    initial_password: str,
    apply: bool,
    password_hasher: _PasswordHasher | None = None,
) -> SeedResult:
    hasher = password_hasher or Argon2PasswordHasher()
    try:
        return _seed_test_data_once(
            session_factory=session_factory,
            config=config,
            initial_password=initial_password,
            apply=apply,
            password_hasher=hasher,
        )
    except IntegrityError:
        if not apply:
            raise

    validation = _seed_test_data_once(
        session_factory=session_factory,
        config=config,
        initial_password=initial_password,
        apply=False,
        password_hasher=hasher,
    )
    if (
        validation.created != 0
        or validation.existing != _resource_count(config)
        or validation.updated != 0
    ):
        raise SeedConflict("concurrent seed content is incomplete")
    return SeedResult(
        mode="apply",
        created=0,
        existing=_resource_count(config),
        updated=0,
    )


def _arguments(argv: list[str] | None) -> argparse.Namespace:
    parser = _RedactingArgumentParser(
        prog="seed_test_data.py",
        usage="%(prog)s [-h] [--apply]",
        description="Plan or explicitly apply Hermes Cloud test data",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write the reviewed seed plan in one ORM transaction",
    )
    arguments = list(sys.argv[1:] if argv is None else argv)
    if any(
        argument.startswith(prefix)
        for argument in arguments
        for prefix in _SENSITIVE_ARGUMENT_PREFIXES
    ):
        parser.error("sensitive command-line arguments are forbidden")
    return parser.parse_args(arguments)


def _dispose_engine(engine: Engine | None) -> BaseException | None:
    if engine is None:
        return None
    try:
        engine.dispose()
    except BaseException as error:  # noqa: BLE001 - Preserve the primary failure.
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
        config = SeedConfig.from_environment(environment)
        dsn_path = environment.get("HERMES_BOOTSTRAP_DSN_FILE", "")
        password_path = environment.get(
            "HERMES_INITIAL_USER_PASSWORD_FILE",
            "",
        )
        database_url = read_secret_file(dsn_path, name="bootstrap database")
        initial_user_secret = read_secret_file(
            password_path,
            name="initial password",
        )
        drivername = make_url(database_url).drivername
        if drivername in {"postgresql", "postgresql+psycopg"}:
            engine = engine_factory(
                database_url,
                pool_pre_ping=True,
                poolclass=NullPool,
            )
        elif drivername in {"sqlite", "sqlite+pysqlite"}:
            engine = build_sqlite_engine(
                database_url,
                engine_factory=engine_factory,
            )
        else:
            raise SeedConfigurationError("database provider is unsupported")
        factory = session_factory_builder(
            bind=engine,
            expire_on_commit=False,
        )
        result = seed_test_data(
            session_factory=factory,
            config=config,
            initial_password=initial_user_secret,
            apply=arguments.apply,
        )
    except SeedCommitOutcomeUnknown:
        _dispose_engine(engine)
        raise SystemExit("seed apply outcome unknown; rerun plan") from None
    except Exception:  # noqa: BLE001 - CLI boundary must redact secret-bearing errors.
        _dispose_engine(engine)
        raise SystemExit("seed failed; database unchanged") from None
    except BaseException:
        _dispose_engine(engine)
        raise

    dispose_error = _dispose_engine(engine)
    if dispose_error is not None:
        if not isinstance(dispose_error, Exception):
            raise dispose_error
        if result.mode == "apply":
            raise SystemExit("seed committed; cleanup failed") from None
        raise SystemExit("seed failed; database unchanged") from None

    output = (
        f"seed_mode={result.mode} created={result.created} existing={result.existing}"
    )
    if result.updated:
        output += f" updated={result.updated}"
    print(output)


if __name__ == "__main__":
    main()
