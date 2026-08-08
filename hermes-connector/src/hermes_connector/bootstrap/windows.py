"""Side-effect-bounded Windows Connector runtime composition."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import uuid4

from hermes_connector.adapters.cloud.codec import ConnectorProtocolCodec
from hermes_connector.adapters.cloud.pairing_http import DevicePairingHttpClient
from hermes_connector.adapters.foundation_projection import (
    FoundationNoOpLocalProjectionInvalidator,
)
from hermes_connector.adapters.local_runtime_preflight import LocalRuntimePreflight
from hermes_connector.adapters.platform.windows.agent_discovery import WindowsAgentDiscovery
from hermes_connector.adapters.platform.windows.dpapi_secret_store import (
    WindowsDPAPISecretStore,
)
from hermes_connector.adapters.platform.windows.instance_identity import (
    InstanceIdentities,
    WindowsInstanceIdentityStore,
)
from hermes_connector.adapters.platform.windows.instance_lock import WindowsInstanceLock
from hermes_connector.adapters.platform.windows.local_gateway_transport import (
    WindowsLocalGatewayTransport,
)
from hermes_connector.adapters.platform.windows.observer_client import WindowsObserverClient
from hermes_connector.adapters.platform.windows.pairing_command_lock import (
    WindowsPairingCommandLock,
)
from hermes_connector.adapters.platform.windows.pairing_projection import (
    WindowsPairedProjectionStore,
    WindowsPairingOfferProjectionStore,
)
from hermes_connector.adapters.platform.windows.plugin_control_relay import (
    WindowsPluginControlRelay,
    WindowsPluginOwnerControlChannelFactory,
)
from hermes_connector.adapters.platform.windows.private_state import (
    private_file_exists,
    validate_private_directory,
    validate_private_file,
)
from hermes_connector.adapters.platform.windows.process_identity import (
    current_process_identity,
)
from hermes_connector.adapters.platform.windows.session_catalog_client import (
    WindowsSessionCatalogClient,
)
from hermes_connector.adapters.platform.windows.status_receipt import (
    WindowsStatusReceiptStore,
)
from hermes_connector.adapters.secure_store_cloud_token import SecureStoreCloudTokenProvider
from hermes_connector.adapters.secure_store_device_identity import SecureStoreDeviceIdentity
from hermes_connector.adapters.sqlite_storage import SQLiteStorageComponent
from hermes_connector.application.cloud_wss_client import (
    CloudClientConfig,
    CloudWSSClient,
)
from hermes_connector.application.command_lane import CommandLane, CommandScope
from hermes_connector.application.device_bound_token_provider import (
    DeviceBoundCloudTokenProvider,
)
from hermes_connector.application.local_gateway_client import (
    LocalGatewayClient,
    LocalRuntimeUnavailable,
)
from hermes_connector.application.observer_intent_lane import ObserverIntentLane
from hermes_connector.application.observer_outbound_lane import ObserverOutboundLane
from hermes_connector.application.owner_control_lane import OwnerControlLane
from hermes_connector.application.pairing_coordinator import PairingCoordinator
from hermes_connector.application.readiness_status import ReadinessStatusComponent
from hermes_connector.application.service_runner import ServiceRunner
from hermes_connector.application.session_catalog_outbound_lane import (
    SessionCatalogOutboundLane,
)
from hermes_connector.application.session_catalog_sync import SessionCatalogSync
from hermes_connector.bootstrap.cloud import build_cloud_wss_client
from hermes_connector.bootstrap.config import ConnectorConfig
from hermes_connector.bootstrap.runtime import build_service_runner
from hermes_connector.bootstrap.settings import ConnectorRuntimeSettings
from hermes_connector.domain.local_gateway import AgentEndpoint
from hermes_connector.ports.cloud import CloudTokenProviderPort
from hermes_connector.ports.component import ComponentPort
from hermes_connector.ports.device_identity import DeviceIdentityPort
from hermes_connector.ports.logging import SafeLogPort

_REQUIRED_CAPABILITIES = ("session.observe",)
_OPTIONAL_CAPABILITIES = (
    "session.control",
    "session.observe.output-parity.v1",
    "session.catalog.v1",
)
_DEVICE_KEY_SERVICE = "wiki.seaotter.hermes.connector.device-key.v1"
_CLOUD_TOKEN_SERVICE = "wiki.seaotter.hermes.connector.cloud-token.v1"
_PAIRING_OFFER_SERVICE = "wiki.seaotter.hermes.connector.pairing-offer.v1"


class UnpairedConnector(ValueError):
    """Formal runtime cannot start without an active server binding."""


class WindowsCredentialRuntimeForbidden(ValueError):
    """Windows formal runtime requires current-user DPAPI credentials."""


class LocalRuntimePreflightPort(Protocol):
    def verify(self, profile: str) -> AgentEndpoint | None: ...


@dataclass(frozen=True, slots=True)
class WindowsConnectorRuntime:
    runner: ServiceRunner
    identities: InstanceIdentities
    device_identity: DeviceIdentityPort
    cloud_token_provider: CloudTokenProviderPort
    storage: SQLiteStorageComponent
    local_gateway: LocalGatewayClient
    cloud_wss: CloudWSSClient
    control_relay: WindowsPluginControlRelay
    command_lane: CommandLane
    owner_control_factory: WindowsPluginOwnerControlChannelFactory
    owner_control_lane: OwnerControlLane
    observer_client: WindowsObserverClient
    observer_outbound_lane: ObserverOutboundLane
    observer_intent_lane: ObserverIntentLane
    session_catalog_client: WindowsSessionCatalogClient
    session_catalog_outbound_lane: SessionCatalogOutboundLane
    session_catalog_sync: SessionCatalogSync
    status_receipt: ReadinessStatusComponent
    foundation_invalidator: FoundationNoOpLocalProjectionInvalidator
    components: tuple[ComponentPort, ...]
    pairing_http: DevicePairingHttpClient


@dataclass(frozen=True, slots=True)
class WindowsPairingRuntime:
    coordinator: PairingCoordinator
    pairing_http: DevicePairingHttpClient

    async def aclose(self) -> None:
        await self.pairing_http.aclose()


def check_windows_runtime(settings: ConnectorRuntimeSettings) -> None:
    """Validate Windows private state and DPAPI availability without mutation."""

    if settings.credential_store != "dpapi":
        raise WindowsCredentialRuntimeForbidden(
            "Windows formal runtime requires DPAPI credentials"
        )
    try:
        validate_private_directory(settings.state_directory)
        for path in (
            settings.database_file,
            settings.lock_file,
            settings.paired_projection_file,
            settings.pairing_offer_projection_file,
            settings.pairing_command_lock_file,
            settings.status_receipt_file,
        ):
            if private_file_exists(path):
                validate_private_file(path)
        WindowsInstanceIdentityStore(settings.instance_state_file).check_path()
        WindowsPairedProjectionStore(settings.paired_projection_file).check_path()
        WindowsPairingOfferProjectionStore(
            settings.pairing_offer_projection_file
        ).check_path()
        WindowsDPAPISecretStore(
            root_directory=settings.state_directory,
            service=_DEVICE_KEY_SERVICE,
            account="preflight",
        ).check_available()
    except WindowsCredentialRuntimeForbidden:
        raise
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        raise ValueError("Windows runtime private state is unsafe") from error


def build_windows_runtime(
    settings: ConnectorRuntimeSettings,
    *,
    release_id: str,
    config: ConnectorConfig,
    logger: SafeLogPort,
    local_runtime_preflight: LocalRuntimePreflightPort | None = None,
) -> WindowsConnectorRuntime:
    """Compose all Windows components without starting local or network I/O."""

    check_windows_runtime(settings)
    paired_store = WindowsPairedProjectionStore(settings.paired_projection_file)
    paired = paired_store.load_sync()
    if paired is None or paired.lifecycle_state != "active":
        raise UnpairedConnector("active paired projection is required")

    local_discovery = WindowsAgentDiscovery(
        settings.local_gateway_registry_directory,
        settings.local_gateway_socket_directory,
        timeout_seconds=config.local_connect_timeout_seconds,
    )
    local_transport = WindowsLocalGatewayTransport(
        connect_timeout_seconds=config.local_connect_timeout_seconds,
        io_timeout_seconds=config.local_rpc_deadline_seconds,
    )
    preflight = local_runtime_preflight or LocalRuntimePreflight(
        discovery=local_discovery,
        transport=local_transport,
        timeout_seconds=config.local_connect_timeout_seconds,
    )
    expected_local_endpoint = preflight.verify(settings.profile)
    if expected_local_endpoint is None:
        raise LocalRuntimeUnavailable()

    identities = WindowsInstanceIdentityStore(settings.instance_state_file).load_or_create()
    account = f"connector-instance:{identities.connector_instance_id}"
    device_identity = SecureStoreDeviceIdentity(
        WindowsDPAPISecretStore(
            root_directory=settings.state_directory,
            service=_DEVICE_KEY_SERVICE,
            account=account,
        )
    )
    tenant_id = str(paired.tenant_id)
    device_id = str(paired.device_id)
    token_cache = SecureStoreCloudTokenProvider(
        WindowsDPAPISecretStore(
            root_directory=settings.state_directory,
            service=_CLOUD_TOKEN_SERVICE,
            account=account,
        )
    )
    pairing_http = DevicePairingHttpClient(settings.cloud_api_endpoint)
    token_provider: CloudTokenProviderPort = DeviceBoundCloudTokenProvider(
        projection_store=paired_store,
        token_store=token_cache,
        identity=device_identity,
        cloud=pairing_http,
        now=lambda: datetime.now(UTC),
        refresh_before=timedelta(seconds=30),
        new_idempotency_key=uuid4,
        initial_lifecycle_state=paired.lifecycle_state,
    )

    storage = SQLiteStorageComponent(settings.database_file, config)
    protocol_codec = ConnectorProtocolCodec()
    foundation_invalidator = FoundationNoOpLocalProjectionInvalidator()
    local_gateway = LocalGatewayClient(
        profile=settings.profile,
        client_instance_id=identities.client_instance_id,
        required_capabilities=_REQUIRED_CAPABILITIES,
        optional_capabilities=_OPTIONAL_CAPABILITIES,
        discovery=local_discovery,
        transport=local_transport,
        session_state=foundation_invalidator,
        config=config,
        expected_endpoint=expected_local_endpoint,
    )
    observer_client = WindowsObserverClient(
        authority=local_gateway.current_runtime_authority,
        rpc_timeout_seconds=config.local_rpc_deadline_seconds,
    )
    session_catalog_client = WindowsSessionCatalogClient(
        authority=local_gateway.current_runtime_authority,
        rpc_timeout_seconds=config.local_rpc_deadline_seconds,
    )
    control_relay = WindowsPluginControlRelay(
        profile=settings.profile,
        user_id=device_id,
        provider="hermes-cloud",
        authority=local_gateway.current_runtime_authority,
        timeout_seconds=config.local_rpc_deadline_seconds,
    )
    command_lane = CommandLane(
        storage=storage,
        relay=control_relay,
        scope=CommandScope(
            tenant_id=tenant_id,
            device_id=device_id,
            connector_instance_id=identities.connector_instance_id,
            profile=settings.profile,
            allowed_session_keys=None,
        ),
        codec=protocol_codec,
    )
    owner_control_factory = WindowsPluginOwnerControlChannelFactory(
        profile=settings.profile,
        provider="hermes-cloud",
        authority=local_gateway.current_runtime_authority,
    )
    owner_control_lane = OwnerControlLane(factory=owner_control_factory)
    observer_outbound_lane = ObserverOutboundLane(
        storage=storage,
        codec=protocol_codec,
        tenant_id=tenant_id,
        device_id=device_id,
    )
    session_catalog_outbound_lane = SessionCatalogOutboundLane(
        storage=storage,
        codec=protocol_codec,
        tenant_id=tenant_id,
        device_id=device_id,
    )
    cloud_wss = build_cloud_wss_client(
        config=CloudClientConfig(
            endpoint=settings.cloud_endpoint,
            tenant_id=tenant_id,
            device_id=device_id,
            connector_instance_id=identities.connector_instance_id,
            connector_version=settings.connector_version,
            command_outbox_batch_size=config.command_retention_entries * 2,
        ),
        token_provider=token_provider,
        storage=storage,
        runtime_authority=local_gateway,
        command_lane=command_lane,
        owner_control_lane=owner_control_lane,
        observer_outbound_lane=observer_outbound_lane,
        session_catalog_outbound_lane=session_catalog_outbound_lane,
        codec=protocol_codec,
    )
    observer_intent_lane = ObserverIntentLane(
        local_client=observer_client,
        publisher=cloud_wss,
    )
    cloud_wss.bind_observer_intent_lane(observer_intent_lane)
    session_catalog_sync = SessionCatalogSync(
        profile=settings.profile,
        local_client=session_catalog_client,
        publisher=cloud_wss,
        runtime_authority=local_gateway.current_runtime_authority,
    )
    cloud_wss.bind_session_catalog_sync(session_catalog_sync)
    status_receipt = ReadinessStatusComponent(
        store=WindowsStatusReceiptStore(settings.status_receipt_file),
        release_id=release_id,
        local_authority=local_gateway.current_runtime_authority,
        storage=storage,
        directory=session_catalog_sync,
        cloud=cloud_wss,
        pid=os.getpid(),
        process_identity_provider=current_process_identity,
    )
    components: tuple[ComponentPort, ...] = (
        storage,
        local_gateway,
        pairing_http,
        cloud_wss,
        session_catalog_sync,
        status_receipt,
    )
    runner = build_service_runner(
        lock_path=settings.lock_file,
        components=components,
        config=config,
        logger=logger,
        instance_lock_type=WindowsInstanceLock,
    )
    return WindowsConnectorRuntime(
        runner=runner,
        identities=identities,
        device_identity=device_identity,
        cloud_token_provider=token_provider,
        storage=storage,
        local_gateway=local_gateway,
        cloud_wss=cloud_wss,
        control_relay=control_relay,
        command_lane=command_lane,
        owner_control_factory=owner_control_factory,
        owner_control_lane=owner_control_lane,
        observer_client=observer_client,
        observer_outbound_lane=observer_outbound_lane,
        observer_intent_lane=observer_intent_lane,
        session_catalog_client=session_catalog_client,
        session_catalog_outbound_lane=session_catalog_outbound_lane,
        session_catalog_sync=session_catalog_sync,
        status_receipt=status_receipt,
        foundation_invalidator=foundation_invalidator,
        components=components,
        pairing_http=pairing_http,
    )


def build_windows_pairing_runtime(
    settings: ConnectorRuntimeSettings,
) -> WindowsPairingRuntime:
    """Compose one explicit Windows pairing command without starting the service."""

    check_windows_runtime(settings)
    identities = WindowsInstanceIdentityStore(settings.instance_state_file).load_or_create()
    account = f"connector-instance:{identities.connector_instance_id}"
    device_identity = SecureStoreDeviceIdentity(
        WindowsDPAPISecretStore(
            root_directory=settings.state_directory,
            service=_DEVICE_KEY_SERVICE,
            account=account,
        )
    )
    offer_secret_store = WindowsDPAPISecretStore(
        root_directory=settings.state_directory,
        service=_PAIRING_OFFER_SERVICE,
        account=account,
    )
    token_store = SecureStoreCloudTokenProvider(
        WindowsDPAPISecretStore(
            root_directory=settings.state_directory,
            service=_CLOUD_TOKEN_SERVICE,
            account=account,
        )
    )
    pairing_http = DevicePairingHttpClient(settings.cloud_api_endpoint)
    coordinator = PairingCoordinator(
        connector_instance_id=identities.connector_instance_id,
        display_name=settings.display_name,
        connector_version=settings.connector_version,
        identity=device_identity,
        cloud=pairing_http,
        offer_secret_store=offer_secret_store,
        offer_projection_store=WindowsPairingOfferProjectionStore(
            settings.pairing_offer_projection_file
        ),
        paired_projection_store=WindowsPairedProjectionStore(
            settings.paired_projection_file
        ),
        token_store=token_store,
        now=lambda: datetime.now(UTC),
        new_idempotency_key=uuid4,
        command_lock=WindowsPairingCommandLock(settings.pairing_command_lock_file),
    )
    return WindowsPairingRuntime(coordinator=coordinator, pairing_http=pairing_http)


def read_windows_status(
    settings: ConnectorRuntimeSettings,
) -> object | None:
    return WindowsStatusReceiptStore(settings.status_receipt_file).read(
        now=datetime.now(UTC),
        process_identity_provider=current_process_identity,
    )


__all__ = [
    "UnpairedConnector",
    "WindowsConnectorRuntime",
    "WindowsCredentialRuntimeForbidden",
    "WindowsPairingRuntime",
    "build_windows_pairing_runtime",
    "build_windows_runtime",
    "check_windows_runtime",
    "read_windows_status",
]
