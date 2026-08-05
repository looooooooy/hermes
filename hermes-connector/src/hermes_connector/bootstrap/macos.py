"""Side-effect-bounded macOS Connector runtime composition."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Protocol
from uuid import uuid4

from hermes_connector.adapters.cloud.codec import ConnectorProtocolCodec
from hermes_connector.adapters.cloud.pairing_http import DevicePairingHttpClient
from hermes_connector.adapters.platform.macos import (
    MacOSAgentDiscovery,
    MacOSLocalGatewayTransport,
    MacOSLocalRuntimePreflight,
    MacOSObserverClient,
    MacOSObserverEndpointDiscovery,
    MacOSSessionCatalogClient,
)
from hermes_connector.adapters.platform.macos.credentials import (
    MacOSFileCloudTokenProvider,
    MacOSKeychainCloudTokenProvider,
)
from hermes_connector.adapters.platform.macos.device_identity import (
    MacOSKeychainDeviceIdentity,
)
from hermes_connector.adapters.platform.macos.foundation_projection import (
    FoundationNoOpLocalProjectionInvalidator,
)
from hermes_connector.adapters.platform.macos.instance_identity import (
    InstanceIdentities,
    MacOSInstanceIdentityStore,
)
from hermes_connector.adapters.platform.macos.keychain import (
    MacOSKeychainSecretStore,
)
from hermes_connector.adapters.platform.macos.keychain_broker import (
    MacOSKeychainBroker,
)
from hermes_connector.adapters.platform.macos.pairing_command_lock import (
    MacOSPairingCommandLock,
)
from hermes_connector.adapters.platform.macos.pairing_projection import (
    MacOSPairedProjectionStore,
    MacOSPairingOfferProjectionStore,
)
from hermes_connector.adapters.platform.macos.plugin_control_relay import (
    MacOSPluginControlRelay,
    MacOSPluginOwnerControlChannelFactory,
)
from hermes_connector.adapters.platform.macos.process_identity import (
    current_process_identity,
)
from hermes_connector.adapters.platform.macos.status_receipt import (
    MacOSStatusReceiptStore,
)
from hermes_connector.adapters.sqlite_storage import SQLiteStorageComponent
from hermes_connector.application.cloud_wss_client import (
    CloudClientConfig,
    CloudWSSClient,
)
from hermes_connector.application.command_lane import CommandLane, CommandScope
from hermes_connector.application.device_bound_token_provider import (
    DeviceBoundCloudTokenProvider,
)
from hermes_connector.application.file_credential_migration import (
    FileCredentialMigration,
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

if TYPE_CHECKING:
    from hermes_connector.adapters.platform.macos.security_framework import (
        SecurityFrameworkAPIPort,
    )

_REQUIRED_CAPABILITIES = ("session.observe",)
_OPTIONAL_CAPABILITIES = (
    "session.control",
    "session.observe.output-parity.v1",
    "session.catalog.v1",
)
_DEVICE_KEY_SERVICE = "wiki.seaotter.hermes.connector.device-key.v1"
_CLOUD_TOKEN_SERVICE = "wiki.seaotter.hermes.connector.cloud-token.v1"
_PAIRING_OFFER_SERVICE = "wiki.seaotter.hermes.connector.pairing-offer.v1"
_MAX_PATH_BYTES = int(os.pathconf("/", "PC_PATH_MAX"))
_MAX_NAME_BYTES = int(os.pathconf("/", "PC_NAME_MAX"))


class UnpairedConnector(ValueError):
    """Formal runtime cannot start without an active server binding."""


class FileCredentialRuntimeForbidden(ValueError):
    """File credentials are restricted to the one-shot migration command."""


class LocalRuntimePreflightPort(Protocol):
    def verify(self, profile: str) -> AgentEndpoint | None: ...


@dataclass(frozen=True, slots=True)
class MacOSConnectorRuntime:
    runner: ServiceRunner
    identities: InstanceIdentities
    device_identity: DeviceIdentityPort
    cloud_token_provider: CloudTokenProviderPort
    storage: SQLiteStorageComponent
    local_gateway: LocalGatewayClient
    cloud_wss: CloudWSSClient
    control_relay: MacOSPluginControlRelay
    command_lane: CommandLane
    owner_control_factory: MacOSPluginOwnerControlChannelFactory
    owner_control_lane: OwnerControlLane
    observer_client: MacOSObserverClient
    observer_outbound_lane: ObserverOutboundLane
    observer_intent_lane: ObserverIntentLane
    session_catalog_client: MacOSSessionCatalogClient
    session_catalog_outbound_lane: SessionCatalogOutboundLane
    session_catalog_sync: SessionCatalogSync
    status_receipt: ReadinessStatusComponent
    foundation_invalidator: FoundationNoOpLocalProjectionInvalidator
    components: tuple[ComponentPort, ...]
    pairing_http: DevicePairingHttpClient | None
    keychain_broker: MacOSKeychainBroker


@dataclass(frozen=True, slots=True)
class MacOSPairingRuntime:
    coordinator: PairingCoordinator
    pairing_http: DevicePairingHttpClient
    keychain_broker: MacOSKeychainBroker

    async def aclose(self) -> None:
        try:
            await self.pairing_http.aclose()
        finally:
            await self.keychain_broker.aclose()


@dataclass(frozen=True, slots=True)
class MacOSFileCredentialMigrationRuntime:
    migration: FileCredentialMigration
    pairing_http: DevicePairingHttpClient
    keychain_broker: MacOSKeychainBroker

    async def aclose(self) -> None:
        try:
            await self.pairing_http.aclose()
        finally:
            await self.keychain_broker.aclose()


def check_macos_runtime(
    settings: ConnectorRuntimeSettings,
    *,
    security_api: SecurityFrameworkAPIPort | None = None,
) -> None:
    """Validate macOS paths and credentials without creating files or connecting."""

    role_directories = (
        settings.local_gateway_registry_directory,
        settings.local_gateway_socket_directory,
        settings.control_registry_directory,
        settings.control_socket_directory,
        settings.observer_registry_directory,
        settings.observer_socket_directory,
    )
    for directory in (*role_directories, settings.state_directory):
        _validate_private_directory(directory)
    identities = {
        (directory.stat().st_dev, directory.stat().st_ino)
        for directory in role_directories
    }
    if len(identities) != len(role_directories):
        raise ValueError("gateway role paths must be physically distinct")
    _validate_managed_file_path(settings.database_file)
    _validate_managed_file_path(settings.lock_file)
    _validate_managed_file_path(settings.paired_projection_file)
    _validate_managed_file_path(settings.pairing_offer_projection_file)
    _validate_managed_file_path(settings.pairing_command_lock_file)
    _validate_managed_file_path(settings.status_receipt_file)
    MacOSInstanceIdentityStore(settings.instance_state_file).check_path()
    if security_api is not None:
        security_api.check_available()
    else:
        MacOSKeychainBroker().check_available()
    if settings.credential_store == "file":
        if settings.token_file is None:
            raise ValueError("file credential reference is unavailable")
        MacOSFileCloudTokenProvider(settings.token_file).check_reference()


def build_macos_runtime(
    settings: ConnectorRuntimeSettings,
    *,
    release_id: str,
    config: ConnectorConfig,
    logger: SafeLogPort,
    security_api: SecurityFrameworkAPIPort | None = None,
    local_runtime_preflight: LocalRuntimePreflightPort | None = None,
) -> MacOSConnectorRuntime:
    """Compose all macOS components without starting local or network I/O."""

    if settings.credential_store != "keychain":
        raise FileCredentialRuntimeForbidden(
            "formal runtime requires paired Keychain credentials"
        )
    check_macos_runtime(settings, security_api=security_api)
    paired_store = MacOSPairedProjectionStore(settings.paired_projection_file)
    paired = paired_store.load_sync()
    if paired is None or paired.lifecycle_state != "active":
        raise UnpairedConnector("active paired projection is required")
    local_discovery = MacOSAgentDiscovery(
        settings.local_gateway_registry_directory,
        settings.local_gateway_socket_directory,
    )
    local_transport = MacOSLocalGatewayTransport()
    preflight = local_runtime_preflight or MacOSLocalRuntimePreflight(
        discovery=local_discovery,
        transport=local_transport,
        timeout_seconds=config.local_connect_timeout_seconds,
    )
    expected_local_endpoint = preflight.verify(settings.profile)
    if expected_local_endpoint is None:
        raise LocalRuntimeUnavailable()
    keychain_broker = MacOSKeychainBroker()
    identities = MacOSInstanceIdentityStore(
        settings.instance_state_file
    ).load_or_create()
    device_identity = MacOSKeychainDeviceIdentity(
        MacOSKeychainSecretStore(
            service=_DEVICE_KEY_SERVICE,
            account=f"connector-instance:{identities.connector_instance_id}",
            broker=keychain_broker,
        )
    )
    tenant_id = str(paired.tenant_id)
    device_id = str(paired.device_id)
    token_cache = MacOSKeychainCloudTokenProvider(
        MacOSKeychainSecretStore(
            service=_CLOUD_TOKEN_SERVICE,
            account=f"connector-instance:{identities.connector_instance_id}",
            broker=keychain_broker,
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
    observer_client = MacOSObserverClient(
        discovery=MacOSObserverEndpointDiscovery(
            settings.observer_registry_directory,
            settings.observer_socket_directory,
        ),
        authority=local_gateway.current_runtime_authority,
        rpc_timeout_seconds=config.local_rpc_deadline_seconds,
    )
    session_catalog_client = MacOSSessionCatalogClient(
        discovery=MacOSObserverEndpointDiscovery(
            settings.observer_registry_directory,
            settings.observer_socket_directory,
        ),
        authority=local_gateway.current_runtime_authority,
        rpc_timeout_seconds=config.local_rpc_deadline_seconds,
    )
    control_relay = MacOSPluginControlRelay(
        registry_directory=settings.control_registry_directory,
        socket_directory=settings.control_socket_directory,
        profile=settings.profile,
        user_id=device_id,
        provider="hermes-cloud",
        authority=local_gateway.current_runtime_authority,
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
    owner_control_factory = MacOSPluginOwnerControlChannelFactory(
        registry_directory=settings.control_registry_directory,
        socket_directory=settings.control_socket_directory,
        profile=settings.profile,
        provider="hermes-cloud",
        authority=local_gateway.current_runtime_authority,
    )
    owner_control_lane = OwnerControlLane(
        factory=owner_control_factory,
    )
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
        store=MacOSStatusReceiptStore(settings.status_receipt_file),
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
        keychain_broker,
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
        platform_name="darwin",
    )
    return MacOSConnectorRuntime(
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
        keychain_broker=keychain_broker,
    )


def build_macos_pairing_runtime(
    settings: ConnectorRuntimeSettings,
    *,
    security_api: SecurityFrameworkAPIPort | None = None,
) -> MacOSPairingRuntime:
    """Compose one explicit pairing command without starting the service."""

    if settings.credential_store != "keychain":
        raise ValueError("pairing is unavailable in file migration mode")
    check_macos_runtime(settings, security_api=security_api)
    keychain_broker = MacOSKeychainBroker()
    identities = MacOSInstanceIdentityStore(
        settings.instance_state_file
    ).load_or_create()
    account = f"connector-instance:{identities.connector_instance_id}"
    device_identity = MacOSKeychainDeviceIdentity(
        MacOSKeychainSecretStore(
            service=_DEVICE_KEY_SERVICE,
            account=account,
            broker=keychain_broker,
        )
    )
    offer_secret_store = MacOSKeychainSecretStore(
        service=_PAIRING_OFFER_SERVICE,
        account=account,
        broker=keychain_broker,
    )
    token_store = MacOSKeychainCloudTokenProvider(
        MacOSKeychainSecretStore(
            service=_CLOUD_TOKEN_SERVICE,
            account=account,
            broker=keychain_broker,
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
        offer_projection_store=MacOSPairingOfferProjectionStore(
            settings.pairing_offer_projection_file
        ),
        paired_projection_store=MacOSPairedProjectionStore(
            settings.paired_projection_file
        ),
        token_store=token_store,
        now=lambda: datetime.now(UTC),
        new_idempotency_key=uuid4,
        command_lock=MacOSPairingCommandLock(settings.pairing_command_lock_file),
    )
    return MacOSPairingRuntime(
        coordinator=coordinator,
        pairing_http=pairing_http,
        keychain_broker=keychain_broker,
    )


def build_macos_file_credential_migration(
    settings: ConnectorRuntimeSettings,
    *,
    security_api: SecurityFrameworkAPIPort | None = None,
) -> MacOSFileCredentialMigrationRuntime:
    """Compose the explicit one-shot legacy file-to-Keychain migration."""

    if settings.credential_store != "file" or settings.token_file is None:
        raise ValueError("file credential migration mode is required")
    check_macos_runtime(settings, security_api=security_api)
    keychain_broker = MacOSKeychainBroker()
    identities = MacOSInstanceIdentityStore(
        settings.instance_state_file
    ).load_or_create()
    account = f"connector-instance:{identities.connector_instance_id}"
    device_identity = MacOSKeychainDeviceIdentity(
        MacOSKeychainSecretStore(
            service=_DEVICE_KEY_SERVICE,
            account=account,
            broker=keychain_broker,
        )
    )
    pairing_http = DevicePairingHttpClient(settings.cloud_api_endpoint)
    migration = FileCredentialMigration(
        projection_store=MacOSPairedProjectionStore(settings.paired_projection_file),
        identity=device_identity,
        cloud=pairing_http,
        target=MacOSKeychainCloudTokenProvider(
            MacOSKeychainSecretStore(
                service=_CLOUD_TOKEN_SERVICE,
                account=account,
                broker=keychain_broker,
            )
        ),
        now=lambda: datetime.now(UTC),
        new_idempotency_key=uuid4,
        command_lock=MacOSPairingCommandLock(settings.pairing_command_lock_file),
    )
    return MacOSFileCredentialMigrationRuntime(
        migration=migration,
        pairing_http=pairing_http,
        keychain_broker=keychain_broker,
    )


def _validate_private_directory(path) -> None:
    _validate_runtime_path(path)
    if _has_symlink_component(path):
        raise ValueError("required private directory contains a symlink")
    try:
        metadata = path.lstat()
    except OSError:
        raise ValueError("required private directory is unavailable") from None
    if (
        not path.is_absolute()
        or stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise ValueError("required private directory is unsafe")


def _validate_managed_file_path(path) -> None:
    if not path.is_absolute() or "\x00" in str(path):
        raise ValueError("managed file reference is unsafe")
    _validate_runtime_path(path)
    _validate_private_directory(path.parent)
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    except OSError:
        raise ValueError("managed file is unavailable") from None
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise ValueError("managed file metadata is unsafe")


def _validate_runtime_path(path) -> None:
    try:
        encoded = os.fsencode(path)
        components = tuple(
            os.fsencode(component)
            for component in path.parts
            if component not in {path.anchor, os.sep}
        )
    except (TypeError, UnicodeEncodeError):
        raise ValueError("runtime path exceeds the platform budget") from None
    if len(encoded) > _MAX_PATH_BYTES or any(
        len(component) > _MAX_NAME_BYTES for component in components
    ):
        raise ValueError("runtime path exceeds the platform budget")


def _has_symlink_component(path) -> bool:
    current = type(path)(path.anchor)
    for component in path.parts[1:]:
        current /= component
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            return False
        except OSError:
            return True
        if stat.S_ISLNK(metadata.st_mode):
            return True
    return False
