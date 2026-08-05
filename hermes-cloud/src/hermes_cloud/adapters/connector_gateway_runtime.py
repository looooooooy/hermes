"""Dependency-lazy production composition for the Connector Gateway."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hermes_cloud.application.connector_gateway import ConnectorGatewaySettings
from hermes_cloud.ports.dependency_probe import DependencyProbe


class ManagedConnectorGatewayApplication:
    """Dispose the provider engine exactly once with the ASGI lifecycle."""

    def __init__(self, application: Any, engine: Any) -> None:
        self._application = application
        self._engine = engine
        self._disposed = False

    async def startup(self) -> None:
        await self._application.startup()

    async def shutdown(self) -> None:
        try:
            await self._application.shutdown()
        finally:
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
                self._dispose()

    def __getattr__(self, name: str) -> object:
        return getattr(self._application, name)

    def _dispose(self) -> None:
        if not self._disposed:
            self._engine.dispose()
            self._disposed = True


def build_production_connector_gateway_application(
    dependency_probes: Iterable[DependencyProbe] = (),
    *,
    environment: Mapping[str, str],
    application_factory: Callable[..., Any],
    utc_now: Callable[[], datetime] = lambda: datetime.now(UTC),
    settings: ConnectorGatewaySettings | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> Any:
    """Compose SQLite persistence only when every dependency is available."""

    probes = tuple(dependency_probes)
    required = {
        "HERMES_CONNECTOR_SIGNING_SECRET_FILE",
        "HERMES_OBSERVER_KEYRING_FILE",
        "HERMES_RUNTIME_DSN_FILE",
    }
    if not required <= set(environment):
        return application_factory(probes)

    engine = None
    try:
        from sqlalchemy.orm import sessionmaker

        from hermes_cloud.adapters.connector_auth import (
            build_connector_authenticator,
        )
        from hermes_cloud.adapters.owner_control_bridge import (
            OwnerControlBridgeServer,
        )
        from hermes_cloud.configuration import DsnFileReference
        from hermes_cloud.modules.control.gateway import (
            GatewayOwnerControlRouter,
        )
        from hermes_cloud.platform.sqlalchemy.connector_command_router import (
            SqlAlchemyConnectorCommandRouter,
        )
        from hermes_cloud.platform.sqlalchemy.connector_transport_cursor import (
            SqlAlchemyConnectorTransportCursorAuthority,
        )
        from hermes_cloud.platform.sqlalchemy.observer_encryption import (
            AesGcmTenantEnvelopeCipher,
            read_tenant_kek_registry,
        )
        from hermes_cloud.platform.sqlalchemy.observer_projection import (
            SqlAlchemyObserverIngress,
        )
        from hermes_cloud.platform.sqlalchemy.observer_receipt import (
            SqlAlchemyObserverReceiptRouter,
        )
        from hermes_cloud.platform.sqlalchemy.observer_subscription import (
            SqlAlchemyObserverSubscriptionRouter,
        )
        from hermes_cloud.platform.sqlalchemy.session_catalog import (
            SqlAlchemySessionCatalogIngress,
        )
        from hermes_cloud.platform.sqlite.engine import build_sqlite_engine
        from hermes_cloud.platform.sqlite.runtime import SQLiteDatabaseProbe

        database_url = DsnFileReference(environment["HERMES_RUNTIME_DSN_FILE"]).read()
        base_settings = settings or ConnectorGatewaySettings()
        engine = build_sqlite_engine(database_url)
        session_factory = sessionmaker(
            bind=engine,
            expire_on_commit=False,
        )
        from hermes_cloud.platform.sqlite.runtime import (
            SQLiteOperationScopedPairingRepository,
        )

        pairing_repository = SQLiteOperationScopedPairingRepository(session_factory)
        authenticator = build_connector_authenticator(
            environment,
            utc_now=utc_now,
            device_authority=pairing_repository,
        )
        database_probe = SQLiteDatabaseProbe(session_factory)
        command_router = SqlAlchemyConnectorCommandRouter(
            session_factory,
            now=utc_now,
        )
        transport_cursor_authority = SqlAlchemyConnectorTransportCursorAuthority(
            session_factory,
            now=utc_now,
            ownership_lease_seconds=(base_settings.transport_ownership_lease_seconds),
        )
        observer_cipher = AesGcmTenantEnvelopeCipher(
            read_tenant_kek_registry(environment["HERMES_OBSERVER_KEYRING_FILE"])
        )
        observer_ingress = SqlAlchemyObserverIngress(
            session_factory,
            cipher=observer_cipher,
            now=utc_now,
        )
        session_catalog_ingress = SqlAlchemySessionCatalogIngress(
            session_factory,
            now=utc_now,
            ownership_lease_seconds=(base_settings.transport_ownership_lease_seconds),
        )
        observer_receipt_router = SqlAlchemyObserverReceiptRouter(
            session_factory,
            now=utc_now,
        )
        observer_subscription_router = SqlAlchemyObserverSubscriptionRouter(
            session_factory,
            now=utc_now,
        )
        owner_control_router = None
        owner_control_bridge = None
        bridge_socket = environment.get("HERMES_OWNER_CONTROL_SOCKET")
        if bridge_socket:
            owner_control_router = GatewayOwnerControlRouter(now=utc_now)
            owner_control_bridge = OwnerControlBridgeServer(
                socket_path=Path(bridge_socket),
                handler=owner_control_router,
            )
        effective_settings = settings
        if effective_settings is None and owner_control_bridge is not None:
            effective_settings = ConnectorGatewaySettings(
                available_capabilities=(
                    "session.catalog.v1",
                    "session.observe",
                    "session.observe.output-parity.v1",
                    "session.control",
                )
            )
        application = application_factory(
            (*probes, authenticator, database_probe),
            authenticator=authenticator,
            resume_resolver=transport_cursor_authority,
            transport_cursor_authority=transport_cursor_authority,
            command_router=command_router,
            observer_ingress=observer_ingress,
            session_catalog_ingress=session_catalog_ingress,
            observer_receipt_router=observer_receipt_router,
            observer_subscription_router=observer_subscription_router,
            owner_control_router=owner_control_router,
            owner_control_bridge=owner_control_bridge,
            settings=effective_settings,
            sleep=sleep,
        )
    except asyncio.CancelledError:
        if engine is not None:
            engine.dispose()
        raise
    except Exception:  # noqa: BLE001 - production composition fails closed
        if engine is not None:
            engine.dispose()
        return application_factory(probes)
    return ManagedConnectorGatewayApplication(application, engine)
