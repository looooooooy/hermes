"""Production composition for the Business API without exposing secrets."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable, Mapping
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import sessionmaker

from hermes_cloud.adapters.connector_auth import read_connector_signing_secret
from hermes_cloud.application.business_api import (
    BusinessApiApplicationPort,
    build_business_api_application,
)
from hermes_cloud.configuration import ConfigurationError, DsnFileReference
from hermes_cloud.modules.cloud_api.domain import CloudApiSettings
from hermes_cloud.modules.control.broker import OwnerControlBroker
from hermes_cloud.modules.control.runtime import BrokeredControlRuntime
from hermes_cloud.modules.device.application import DevicePairingService
from hermes_cloud.platform.postgres.runtime import (
    OperationScopedIdentityRepository,
    OperationScopedPairingRepository,
    OperationScopedSessionProjectionRepository,
    SessionFactory,
    SqlAlchemyDatabaseProbe,
    SqlAlchemyLoginTenantResolver,
)
from hermes_cloud.platform.sqlalchemy.observer_encryption import (
    AesGcmTenantEnvelopeCipher,
    read_tenant_kek_registry,
)
from hermes_cloud.platform.sqlalchemy.observer_projection import (
    ObserverProjectionEventSource,
    SqlAlchemyObserverProjectionRepository,
)
from hermes_cloud.platform.sqlalchemy.observer_subscription import (
    SqlAlchemyObserverSubscriptionRouter,
)
from hermes_cloud.platform.sqlalchemy.pairing_context import (
    SqlAlchemyPairingContextResolver,
)
from hermes_cloud.platform.sqlalchemy.session_catalog import (
    SqlAlchemySessionCatalogRepository,
)
from hermes_cloud.platform.sqlite.engine import build_sqlite_engine
from hermes_cloud.platform.sqlite.runtime import (
    SQLiteDatabaseProbe,
    SQLiteLoginTenantResolver,
    SQLiteOperationScopedIdentityRepository,
    SQLiteOperationScopedPairingRepository,
    SQLiteOperationScopedSessionProjectionRepository,
)
from hermes_cloud.ports.dependency_probe import DependencyProbe

_SIGNING_SECRET_REFERENCE = "systemd-credential:business-api-signing"
_POSTGRESQL_DRIVERS = frozenset({"postgresql", "postgresql+psycopg"})
_SQLITE_DRIVERS = frozenset({"sqlite", "sqlite+pysqlite"})


class _UnavailableBusinessApiRuntimeProbe:
    name = "business-api-runtime-configuration"
    critical = True
    deadline_seconds = 1.0

    async def check(self) -> None:
        raise RuntimeError("business API runtime configuration is unavailable")


class _CredentialSecretResolver:
    __slots__ = ("_secret",)

    def __init__(self, secret: bytes) -> None:
        if len(secret) < 32:
            raise ConfigurationError("signing credential is too short")
        self._secret = secret

    def resolve(self, reference: str) -> bytes:
        if reference != _SIGNING_SECRET_REFERENCE:
            raise KeyError("unknown signing secret reference")
        return self._secret

    def __repr__(self) -> str:
        return "_CredentialSecretResolver(<redacted>)"


class _ManagedBusinessApiApplication:
    def __init__(
        self,
        application: BusinessApiApplicationPort,
        engine: Engine,
        *,
        async_closers: Iterable[Callable[[], Awaitable[None]]] = (),
    ) -> None:
        self._application = application
        self._engine = engine
        self._disposed = False
        self._async_closers = tuple(async_closers)
        self._resources_closed = False

    async def startup(self) -> None:
        await self._application.startup()

    async def shutdown(self) -> None:
        try:
            await self._application.shutdown()
        finally:
            await self._close_resources()
            self._dispose()

    def snapshot(self) -> dict[str, object]:
        return self._application.snapshot()

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Callable[[], Awaitable[dict[str, Any]]],
        send: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        try:
            await self._application(scope, receive, send)
        finally:
            if scope["type"] == "lifespan":
                await self._close_resources()
                self._dispose()

    def __getattr__(self, name: str) -> object:
        return getattr(self._application, name)

    def _dispose(self) -> None:
        if not self._disposed:
            self._engine.dispose()
            self._disposed = True

    async def _close_resources(self) -> None:
        if self._resources_closed:
            return
        self._resources_closed = True
        for close in reversed(self._async_closers):
            await close()


def build_production_business_api_application(
    dependency_probes: Iterable[DependencyProbe] = (),
    *,
    environment: Mapping[str, str],
    engine_factory: Callable[..., Engine] = create_engine,
    session_factory_builder: Callable[..., SessionFactory] = sessionmaker,
) -> BusinessApiApplicationPort:
    engine: Engine | None = None
    bridge_client = None
    try:
        database_url = DsnFileReference(environment["HERMES_RUNTIME_DSN_FILE"]).read()
        signing_secret = (
            DsnFileReference(environment["HERMES_SIGNING_SECRET_FILE"])
            .read()
            .encode("utf-8")
        )
        secret_resolver = _CredentialSecretResolver(signing_secret)
        drivername = make_url(database_url).drivername
        if drivername in _POSTGRESQL_DRIVERS:
            engine = engine_factory(
                database_url,
                pool_pre_ping=True,
            )
            identity_repository_type = OperationScopedIdentityRepository
            projection_repository_type = OperationScopedSessionProjectionRepository
            pairing_repository_type = OperationScopedPairingRepository
            tenant_resolver_type = SqlAlchemyLoginTenantResolver
            database_probe_type = SqlAlchemyDatabaseProbe
        elif drivername in _SQLITE_DRIVERS:
            engine = build_sqlite_engine(
                database_url,
                engine_factory=engine_factory,
            )
            identity_repository_type = SQLiteOperationScopedIdentityRepository
            projection_repository_type = (
                SQLiteOperationScopedSessionProjectionRepository
            )
            pairing_repository_type = SQLiteOperationScopedPairingRepository
            tenant_resolver_type = SQLiteLoginTenantResolver
            database_probe_type = SQLiteDatabaseProbe
        else:
            raise ConfigurationError("database provider is unsupported")
        session_factory = session_factory_builder(
            bind=engine,
            expire_on_commit=False,
        )
        observer_projection_repository = None
        projection_event_source = None
        observer_subscription_manager = None
        if drivername in _SQLITE_DRIVERS:
            observer_cipher = AesGcmTenantEnvelopeCipher(
                read_tenant_kek_registry(environment["HERMES_OBSERVER_KEYRING_FILE"])
            )
            observer_projection_repository = SqlAlchemyObserverProjectionRepository(
                session_factory,
                cipher=observer_cipher,
            )
            projection_event_source = ObserverProjectionEventSource(
                session_factory,
                cipher=observer_cipher,
            )
            observer_subscription_manager = SqlAlchemyObserverSubscriptionRouter(
                session_factory
            )
        identity_repository = identity_repository_type(session_factory)
        projection_repository = projection_repository_type(session_factory)
        session_catalog_repository = SqlAlchemySessionCatalogRepository(
            session_factory
        )
        pairing_context_resolver = SqlAlchemyPairingContextResolver(session_factory)
        pairing_service = None
        connector_signing_path = environment.get("HERMES_CONNECTOR_SIGNING_SECRET_FILE")
        if connector_signing_path is not None:
            pairing_service = DevicePairingService(
                repository=pairing_repository_type(session_factory),
                signing_secret=read_connector_signing_secret(connector_signing_path),
            )
        tenant_resolver = tenant_resolver_type(session_factory)
        database_probe = database_probe_type(session_factory)
        control_runtime = None
        bridge_socket = environment.get("HERMES_OWNER_CONTROL_SOCKET")
        if bridge_socket:
            from hermes_cloud.adapters.owner_control_bridge import (
                BridgeRegisteringRouteResolver,
                OwnerControlBridgeClient,
            )
            from hermes_cloud.platform.sqlalchemy.control_route import (
                SqlAlchemyControlRouteResolver,
            )

            broker = OwnerControlBroker()
            bridge_client = OwnerControlBridgeClient(
                socket_path=Path(bridge_socket),
            )
            bridge_connection_id = str(uuid4())
            control_runtime = BrokeredControlRuntime(
                broker=broker,
                route_resolver=BridgeRegisteringRouteResolver(
                    delegate=SqlAlchemyControlRouteResolver(session_factory),
                    broker=broker,
                    client=bridge_client,
                    broker_connection_id=bridge_connection_id,
                ),
            )
        application = build_business_api_application(
            (*dependency_probes, database_probe),
            identity_repository=identity_repository,
            projection_repository=projection_repository,
            session_catalog_repository=session_catalog_repository,
            observer_projection_repository=observer_projection_repository,
            projection_event_source=projection_event_source,
            observer_subscription_manager=observer_subscription_manager,
            tenant_resolver=tenant_resolver,
            secret_resolver=secret_resolver,
            settings=CloudApiSettings(
                signing_secret_ref=_SIGNING_SECRET_REFERENCE,
            ),
            control_runtime=control_runtime,
            pairing_service=pairing_service,
            pairing_context_resolver=pairing_context_resolver,
        )
    except asyncio.CancelledError:
        if engine is not None:
            engine.dispose()
        raise
    except Exception:  # noqa: BLE001 - production composition must fail closed
        if engine is not None:
            engine.dispose()
        return build_business_api_application(
            (*dependency_probes, _UnavailableBusinessApiRuntimeProbe())
        )

    return _ManagedBusinessApiApplication(
        application,
        engine,
        async_closers=((bridge_client.close,) if bridge_client is not None else ()),
    )
