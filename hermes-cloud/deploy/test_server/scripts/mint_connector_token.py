"""Explicit, dry-run-first Connector test token mint."""

from __future__ import annotations

import argparse
import os
import stat
import sys
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import jwt
from sqlalchemy import DateTime, TypeDecorator, and_, create_engine, or_, select
from sqlalchemy.dialects.sqlite import DATETIME
from sqlalchemy.engine import URL, Engine, make_url
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from hermes_cloud.adapters.connector_auth import (
    CONNECTOR_TOKEN_SCOPE,
    MAX_CONNECTOR_TOKEN_TTL_SECONDS,
    read_connector_signing_secret,
    validate_connector_identity,
)
from hermes_cloud.configuration import ConfigurationError, DsnFileReference
from hermes_cloud.platform.postgres.models import (
    AgentModel,
    DeviceCredentialModel,
    DeviceModel,
    TenantModel,
)
from hermes_cloud.platform.sqlalchemy.repositories.device import (
    SqlAlchemyPairingRepositoryBase,
)
from hermes_cloud.platform.sqlite.engine import (
    require_sqlite_version,
    sqlite_database_path,
)
from hermes_cloud.platform.sqlite.schema import SQLITE_SCHEMA_TRANSLATE_MAP


class ConnectorTokenMintError(ValueError):
    """Raised when an explicit token mint request is unsafe."""


class _RedactingArgumentParser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        self.print_usage(sys.stderr)
        self.exit(2, f"{self.prog}: error: invalid arguments\n")


@dataclass(frozen=True, slots=True)
class ConnectorTokenMintConfig:
    tenant_id: str
    device_id: str
    ttl_seconds: int
    owner_control_enabled: bool
    tenant_slug: str | None
    agent_key: str | None
    device_key: str | None

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str],
    ) -> ConnectorTokenMintConfig:
        try:
            tenant_id = _canonical_uuid(environment["HERMES_CONNECTOR_TOKEN_TENANT_ID"])
            device_id = _canonical_uuid(environment["HERMES_CONNECTOR_TOKEN_DEVICE_ID"])
            raw_ttl = environment.get(
                "HERMES_CONNECTOR_TOKEN_TTL_SECONDS",
                "300",
            )
            if not raw_ttl.isascii() or not raw_ttl.isdecimal():
                raise ValueError
            ttl_seconds = int(raw_ttl)
            if not 1 <= ttl_seconds <= MAX_CONNECTOR_TOKEN_TTL_SECONDS:
                raise ValueError
            owner_control_enabled = environment.get(
                "HERMES_SEED_OWNER_CONTROL_ENABLED",
                "false",
            )
            if owner_control_enabled not in {"false", "true"}:
                raise ValueError
            if owner_control_enabled == "true":
                tenant_slug = validate_connector_identity(
                    environment["HERMES_SEED_TENANT_SLUG"]
                )
                device_key = validate_connector_identity(
                    environment["HERMES_SEED_DEVICE_KEY"]
                )
                agent_key = validate_connector_identity(
                    environment["HERMES_SEED_AGENT_KEY"]
                )
            else:
                tenant_slug = None
                agent_key = None
                device_key = None
        except (KeyError, TypeError, ValueError):
            raise ConnectorTokenMintError(
                "connector token configuration is invalid"
            ) from None
        return cls(
            tenant_id=tenant_id,
            device_id=device_id,
            ttl_seconds=ttl_seconds,
            owner_control_enabled=owner_control_enabled == "true",
            tenant_slug=tenant_slug,
            agent_key=agent_key,
            device_key=device_key,
        )


@dataclass(frozen=True, slots=True)
class OwnerControlSelector:
    tenant_slug: str
    agent_key: str
    device_key: str

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str],
    ) -> OwnerControlSelector:
        try:
            if environment.get("HERMES_SEED_OWNER_CONTROL_ENABLED") != "true":
                raise ValueError
            return cls(
                tenant_slug=validate_connector_identity(
                    environment["HERMES_SEED_TENANT_SLUG"]
                ),
                agent_key=validate_connector_identity(
                    environment["HERMES_SEED_AGENT_KEY"]
                ),
                device_key=validate_connector_identity(
                    environment["HERMES_SEED_DEVICE_KEY"]
                ),
            )
        except (KeyError, TypeError, ValueError):
            raise ConnectorTokenMintError("owner-control selector is invalid") from None


@dataclass(frozen=True, slots=True)
class ConnectorTokenBinding:
    tenant_id: UUID
    device_id: UUID
    credential_id: UUID | None
    agent_id: UUID | None
    scopes: tuple[str, ...]
    credential_expires_at: datetime | None = None


class _MintSQLiteUtcDateTime(TypeDecorator[datetime]):
    impl = DATETIME
    cache_ok = True

    def process_bind_param(
        self,
        value: datetime | None,
        _dialect: object,
    ) -> datetime | None:
        if value is None:
            return None
        if value.utcoffset() is None:
            raise ValueError("SQLite datetime must include a timezone")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(
        self,
        value: datetime | None,
        _dialect: object,
    ) -> datetime | None:
        if value is None or value.utcoffset() is not None:
            return value
        return value.replace(tzinfo=UTC)


def _canonical_uuid(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError
    parsed = UUID(value)
    if str(parsed) != value:
        raise ValueError
    return value


def _read_runtime_database_url(path: str) -> str:
    if not isinstance(path, str) or not os.path.isabs(path):
        raise ConnectorTokenMintError("runtime database reference is invalid")
    try:
        return DsnFileReference(path).read()
    except ConfigurationError:
        raise ConnectorTokenMintError("runtime database reference is invalid") from None


def _read_only_sqlite_default_isolation_level(_connection: object) -> str:
    return "SERIALIZABLE"


def _build_database_engine(database_url: str) -> Engine:
    parsed_url = make_url(database_url)
    drivername = parsed_url.drivername
    if drivername in {"postgresql", "postgresql+psycopg"}:
        if drivername == "postgresql":
            parsed_url = parsed_url.set(drivername="postgresql+psycopg")
        return create_engine(
            parsed_url,
            connect_args={
                "options": "-c default_transaction_read_only=on",
            },
            execution_options={"postgresql_readonly": True},
            poolclass=NullPool,
        )
    if drivername in {"sqlite", "sqlite+pysqlite"}:
        require_sqlite_version()
        database = sqlite_database_path(database_url)
        read_only_url = URL.create(
            "sqlite+pysqlite",
            database=f"file:{database.as_posix()}",
            query={"mode": "ro", "uri": "true"},
        )
        engine = create_engine(
            read_only_url,
            connect_args={
                "check_same_thread": False,
                "timeout": 5.0,
            },
            execution_options={
                "schema_translate_map": SQLITE_SCHEMA_TRANSLATE_MAP,
            },
            poolclass=NullPool,
        )
        engine.dialect.colspecs[DateTime] = _MintSQLiteUtcDateTime
        engine.dialect.get_default_isolation_level = (
            _read_only_sqlite_default_isolation_level
        )
        return engine
    raise ConnectorTokenMintError("runtime database provider is unsupported")


def _resolve_owner_control_binding(
    *,
    selector: OwnerControlSelector,
    dsn_path: str,
    now: datetime,
    expected_tenant_id: UUID | None,
    expected_device_id: UUID | None,
) -> ConnectorTokenBinding:
    database_url = _read_runtime_database_url(dsn_path)
    engine: Engine | None = None
    try:
        engine = _build_database_engine(database_url)
        factory = sessionmaker(bind=engine, expire_on_commit=False)
        with factory.begin() as session:
            candidates = session.execute(
                select(
                    TenantModel.tenant_id,
                    DeviceModel.device_id,
                    DeviceCredentialModel.credential_id,
                    AgentModel.agent_id,
                )
                .join(
                    DeviceModel,
                    DeviceModel.tenant_id == TenantModel.tenant_id,
                )
                .join(
                    AgentModel,
                    and_(
                        AgentModel.tenant_id == DeviceModel.tenant_id,
                        AgentModel.agent_id == DeviceModel.agent_id,
                        AgentModel.workspace_id == DeviceModel.workspace_id,
                    ),
                )
                .join(
                    DeviceCredentialModel,
                    and_(
                        DeviceCredentialModel.tenant_id == DeviceModel.tenant_id,
                        DeviceCredentialModel.device_id == DeviceModel.device_id,
                    ),
                )
                .where(
                    TenantModel.slug == selector.tenant_slug,
                    TenantModel.status == "active",
                    DeviceModel.device_key == selector.device_key,
                    DeviceModel.status == "active",
                    AgentModel.agent_key == selector.agent_key,
                    AgentModel.status == "active",
                    DeviceCredentialModel.status == "active",
                    DeviceCredentialModel.revoked_at.is_(None),
                    or_(
                        DeviceCredentialModel.expires_at.is_(None),
                        DeviceCredentialModel.expires_at > now,
                    ),
                )
                .limit(2)
            ).all()
            if len(candidates) != 1:
                raise ConnectorTokenMintError("owner-control binding is unavailable")
            tenant_id, device_id, credential_id, agent_id = candidates[0]
            if (expected_tenant_id is not None and tenant_id != expected_tenant_id) or (
                expected_device_id is not None and device_id != expected_device_id
            ):
                raise ConnectorTokenMintError("owner-control binding is unavailable")
            snapshot = SqlAlchemyPairingRepositoryBase(session).active_device_binding(
                tenant_id=tenant_id,
                device_id=device_id,
                credential_id=credential_id,
                now=now,
            )
            binding = snapshot.binding
            if (
                binding.tenant_id != tenant_id
                or binding.device_id != device_id
                or binding.credential_id != credential_id
                or binding.agent_id != agent_id
            ):
                raise ConnectorTokenMintError("owner-control binding is unavailable")
            return ConnectorTokenBinding(
                tenant_id=tenant_id,
                device_id=device_id,
                credential_id=credential_id,
                agent_id=agent_id,
                scopes=tuple(binding.scopes),
                credential_expires_at=binding.credential_expires_at,
            )
    finally:
        if engine is not None:
            engine.dispose()


def _arguments(argv: list[str] | None) -> argparse.Namespace:
    parser = _RedactingArgumentParser(
        prog="mint_connector_token.py",
        usage=("%(prog)s [-h] [--inspect-binding] [--apply] [--output OUTPUT]"),
        description="Plan or explicitly mint one short-lived Connector token",
    )
    parser.add_argument(
        "--inspect-binding",
        action="store_true",
        help="print one active owner-control ORM binding without minting",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="mint and atomically write a token",
    )
    parser.add_argument(
        "--output",
        help="absolute private token file path; valid only with --apply",
    )
    return parser.parse_args(argv)


def _aware_utc_now(utc_now: Callable[[], datetime]) -> datetime:
    current = utc_now()
    if current.utcoffset() is None:
        raise ConnectorTokenMintError("token mint clock is invalid")
    return current.astimezone(UTC)


def _selector(config: ConnectorTokenMintConfig) -> OwnerControlSelector:
    if (
        not config.owner_control_enabled
        or config.tenant_slug is None
        or config.agent_key is None
        or config.device_key is None
    ):
        raise ConnectorTokenMintError("owner-control binding is unavailable")
    return OwnerControlSelector(
        tenant_slug=config.tenant_slug,
        agent_key=config.agent_key,
        device_key=config.device_key,
    )


def _require_same_binding(
    planned: ConnectorTokenBinding,
    current: ConnectorTokenBinding,
) -> ConnectorTokenBinding:
    if current != planned:
        raise ConnectorTokenMintError("owner-control binding changed during mint")
    return current


def _require_binding_active_at(
    binding: ConnectorTokenBinding,
    now: datetime,
) -> None:
    expires_at = binding.credential_expires_at
    if expires_at is not None and expires_at <= now:
        raise ConnectorTokenMintError("owner-control binding is unavailable")


def _validate_output(path: str | None) -> Path:
    if not isinstance(path, str) or not os.path.isabs(path):
        raise ConnectorTokenMintError("token output path must be absolute")
    output = Path(path)
    try:
        parent_metadata = os.lstat(output.parent)
    except OSError:
        raise ConnectorTokenMintError("token output directory is unavailable") from None
    if not stat.S_ISDIR(parent_metadata.st_mode) or stat.S_ISLNK(
        parent_metadata.st_mode
    ):
        raise ConnectorTokenMintError("token output directory is unsafe")
    try:
        output_metadata = os.lstat(output)
    except FileNotFoundError:
        return output
    except OSError:
        raise ConnectorTokenMintError("token output path is unavailable") from None
    if (
        not stat.S_ISREG(output_metadata.st_mode)
        or stat.S_ISLNK(output_metadata.st_mode)
        or stat.S_IMODE(output_metadata.st_mode) & ~0o600
    ):
        raise ConnectorTokenMintError("token output path is unsafe")
    return output


def _write_private_token(output: Path, encoded_value: str) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".connector-token-",
        dir=output.parent,
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="ascii") as stream:
            descriptor = -1
            stream.write(encoded_value)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, output)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def main(
    argv: list[str] | None = None,
    *,
    environment: Mapping[str, str] = os.environ,
    utc_now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> None:
    arguments = _arguments(argv)
    try:
        if arguments.inspect_binding:
            if arguments.apply or arguments.output is not None:
                raise ConnectorTokenMintError(
                    "binding inspection arguments are invalid"
                )
            selector = OwnerControlSelector.from_environment(environment)
            binding = _resolve_owner_control_binding(
                selector=selector,
                dsn_path=environment.get("HERMES_RUNTIME_DSN_FILE", ""),
                now=_aware_utc_now(utc_now),
                expected_tenant_id=None,
                expected_device_id=None,
            )
            if binding.credential_id is None or binding.agent_id is None:
                raise ConnectorTokenMintError("owner-control binding is unavailable")
            print(
                "binding "
                f"tenant_id={binding.tenant_id} "
                f"device_id={binding.device_id} "
                f"credential_id={binding.credential_id} "
                f"agent_id={binding.agent_id} "
                f"scopes={','.join(binding.scopes)}"
            )
            return
        config = ConnectorTokenMintConfig.from_environment(environment)
        secret_path = environment.get(
            "HERMES_CONNECTOR_SIGNING_SECRET_FILE",
            "",
        )
        signing_secret = read_connector_signing_secret(secret_path)
        if config.owner_control_enabled:
            selector = _selector(config)
            binding = _resolve_owner_control_binding(
                selector=selector,
                dsn_path=environment.get("HERMES_RUNTIME_DSN_FILE", ""),
                now=_aware_utc_now(utc_now),
                expected_tenant_id=UUID(config.tenant_id),
                expected_device_id=UUID(config.device_id),
            )
        else:
            selector = None
            binding = ConnectorTokenBinding(
                tenant_id=UUID(config.tenant_id),
                device_id=UUID(config.device_id),
                credential_id=None,
                agent_id=None,
                scopes=(),
            )
        if not arguments.apply:
            print(f"mint_mode=plan ttl_seconds={config.ttl_seconds}")
            return
        output = _validate_output(arguments.output)
        if config.owner_control_enabled:
            assert selector is not None
            binding = _require_same_binding(
                binding,
                _resolve_owner_control_binding(
                    selector=selector,
                    dsn_path=environment.get("HERMES_RUNTIME_DSN_FILE", ""),
                    now=_aware_utc_now(utc_now),
                    expected_tenant_id=UUID(config.tenant_id),
                    expected_device_id=UUID(config.device_id),
                ),
            )
            current = _aware_utc_now(utc_now)
            _require_binding_active_at(binding, current)
            if binding.credential_id is None or binding.agent_id is None:
                raise ConnectorTokenMintError("owner-control binding is unavailable")
            issued_at = int(current.timestamp())
            claims: dict[str, object] = {
                "tenant_id": str(binding.tenant_id),
                "device_id": str(binding.device_id),
                "credential_id": str(binding.credential_id),
                "agent_id": str(binding.agent_id),
                "scopes": list(binding.scopes),
                "jti": str(uuid4()),
                "iat": issued_at,
                "nbf": issued_at,
                "exp": issued_at + config.ttl_seconds,
            }
        else:
            issued_at = int(_aware_utc_now(utc_now).timestamp())
            claims = {
                "tenant_id": str(binding.tenant_id),
                "device_id": str(binding.device_id),
                "scope": CONNECTOR_TOKEN_SCOPE,
                "iat": issued_at,
                "nbf": issued_at,
                "exp": issued_at + config.ttl_seconds,
            }
        encoded_connector_credential = jwt.encode(
            claims,
            signing_secret,
            algorithm="HS256",
            headers={"typ": "JWT"},
        )
        _write_private_token(output, encoded_connector_credential)
    except Exception:  # noqa: BLE001 - CLI boundary must redact token material.
        raise SystemExit("mint failed; no token written") from None

    print("mint_mode=apply token_written=true")


if __name__ == "__main__":
    main()
