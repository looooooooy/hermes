"""Explicit formal Windows Connector runtime assembly.

This module keeps the final service-runner choice Windows-native while the generic
platform selector intentionally remains fail-closed. Once product-entry gates are
green, the legacy bootstrap.windows builder can delegate here.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from hermes_connector.adapters.cloud.codec import ConnectorProtocolCodec
from hermes_connector.adapters.cloud.pairing_http import DevicePairingHttpClient
from hermes_connector.adapters.foundation_projection import (
    FoundationNoOpLocalProjectionInvalidator,
)
from hermes_connector.adapters.local_runtime_preflight import LocalRuntimePreflight
from hermes_connector.adapters.platform.windows.agent_discovery import (
    WindowsAgentDiscovery,
)
from hermes_connector.adapters.platform.windows.dpapi_secret_store import (
    WindowsDPAPISecretStore,
)
from hermes_connector.adapters.platform.windows.instance_identity import (
    WindowsInstanceIdentityStore,
)
from hermes_connector.adapters.platform.windows.local_gateway_transport import (
    WindowsLocalGatewayTransport,
)
from hermes_connector.adapters.platform.windows.observer_client import (
    WindowsObserverClient,
)
from hermes_connector.adapters.platform.windows.pairing_projection import (
    WindowsPairedProjectionStore,
)
from hermes_connector.adapters.platform.windows.plugin_control_relay import (
    WindowsPluginControlRelay,
    WindowsPluginOwnerControlChannelFactory,
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
from hermes_connector.adapters.secure_store_cloud_token import (
    SecureStoreCloudTokenProvider,
)
from hermes_connector.adapters.secure_store_device_identity import (
    SecureStoreDeviceIdentity,
)
from hermes_connector.adapters.sqlite_storage import SQLiteStorageComponent
from hermes_connector.application.cloud_wss_client import CloudClientConfig
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
from hermes_connector.application.readiness_status import ReadinessStatusComponent
from hermes_connector.application.session_catalog_outbound_lane import (
    SessionCatalogOutboundLane,
)
from hermes_connector.application.session_catalog_sync import SessionCatalogSync
from hermes_connector.bootstrap.cloud import build_cloud_wss_client
from hermes_connector.bootstrap.config import ConnectorConfig
from hermes_connector.bootstrap.settings import ConnectorRuntimeSettings
from hermes_connector.bootstrap.windows import (
    UnpairedConnector,
    WindowsConnectorRuntime,
    check_windows_runtime,
)
from hermes_connector.bootstrap.windows_service import build_windows_service_runner
from hermes_connector.ports.component import ComponentPort
from hermes_connector.ports.logging import SafeLogPort

_REQUIRED_CAPABILITIES = ("session.observe",)
_OPTIONAL_CAPABILITIES = (
    "session.control",
    "session.observe.output-parity.v1",
    "session.catalog.v1",
)
_DEVICE_KEY_SERVICE = "wiki.seaotter.hermes.connector.device-key.v1"
_CLOUD_TOKEN_SERVICE = "wiki.seaotter.hermes.connector.cloud-token.v1"


def build_windows_formal_runtime(
    settings: ConnectorRuntimeSettings,
    *,
    release_id: str,
    config: ConnectorConfig,
    logger: SafeLogPort,
) -> WindowsConnectorRuntime:
    """Compose the verified Windows formal runtime without platform selection."""

    check_windows_runtime(settings)
    paired_store = WindowsPairedProjectionStore(settings.paired_projection_file)
    paired = paired_store.load_sync()
    if paired is None or paired.lifecycle_state != "active":
        raise UnpairedConnector("active paired projection is required")

    discovery = WindowsAgentDiscovery(
        settings.local_gateway_registry_directory,
        settings.local_gateway_socket_directory,
        timeout_seconds=config.local_connect_timeout_seconds,
    )
    transport = WindowsLocalGatewayTransport(
        connect_timeout_seconds=config.local_connect_timeout_seconds,
        io_timeout_seconds=config.local_rpc_deadline_seconds,
    )
    preflight = LocalRuntimePreflight(
        discovery=discovery,
        transport=transport,
        timeout_seconds=config.local_connect_timeout_seconds,
    )
    expected_endpoint = preflight.verify(settings.profile)
    if expected_endpoint is None:
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
    pairing_http = DevicePairingHttpClient(settings.cloud_api_endpoint)
    token_cache = SecureStoreCloudTokenProvider(
        WindowsDPAPISecretStore(
            root_directory=settings.state_directory,
            service=_CLOUD_TOKEN_SERVICE,
            account=account,
        )
    )
    token_provider = DeviceBoundCloudTokenProvider(
        projection_store=paired_store,
        token_store=token_cache,
        identity=device_identity,
        cloud=pairing_http,
        now=lambda: datetime.now(UTC),
        refresh_before=timedelta(seconds=30),
        new_idempotency_key=uuid4,
        initial_lifecycle_state=paired.lifecycle_state,
    )

    tenant_id = str(paired.tenant_id)
    device_id = str(paired.device_id)
    storage = SQLiteStorageComponent(settings.database_file, config)
    codec = ConnectorProtocolCodec()
    foundation_invalidator = FoundationNoOpLocalProjectionInvalidator()
    local_gateway = LocalGatewayClient(
        profile=settings.profile,
        client_instance_id=identities.client_instance_id,
        required_capabilities=_REQUIRED_CAPABILITIES,
        optional_capabilities=_OPTIONAL_CAPABILITIES,
        discovery=discovery,
        transport=transport,
        session_state=foundation_invalidator,
        config=config,
        expected_endpoint=expected_endpoint,
    )
    observer_client = WindowsObserverClient(
        authority=local_gateway.current_runtime_authority,
        rpc_timeout_seconds=config.local_rpc_deadline_seconds,
    )
    catalog_client = WindowsSessionCatalogClient(
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
        codec=codec,
    )
    owner_factory = WindowsPluginOwnerControlChannelFactory(
        profile=settings.profile,
        provider="hermes-cloud",
        authority=local_gateway.current_runtime_authority,
    )
    owner_lane = OwnerControlLane(factory=owner_factory)
    observer_outbound = ObserverOutboundLane(
        storage=storage,
        codec=codec,
        tenant_id=tenant_id,
        device_id=device_id,
    )
    catalog_outbound = SessionCatalogOutboundLane(
        storage=storage,
        codec=codec,
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
        owner_control_lane=owner_lane,
        observer_outbound_lane=observer_outbound,
        session_catalog_outbound_lane=catalog_outbound,
        codec=codec,
    )
    observer_intent = ObserverIntentLane(
        local_client=observer_client,
        publisher=cloud_wss,
    )
    cloud_wss.bind_observer_intent_lane(observer_intent)
    catalog_sync = SessionCatalogSync(
        profile=settings.profile,
        local_client=catalog_client,
        publisher=cloud_wss,
        runtime_authority=local_gateway.current_runtime_authority,
    )
    cloud_wss.bind_session_catalog_sync(catalog_sync)
    status = ReadinessStatusComponent(
        store=WindowsStatusReceiptStore(settings.status_receipt_file),
        release_id=release_id,
        local_authority=local_gateway.current_runtime_authority,
        storage=storage,
        directory=catalog_sync,
        cloud=cloud_wss,
        pid=os.getpid(),
        process_identity_provider=current_process_identity,
    )
    components: tuple[ComponentPort, ...] = (
        storage,
        local_gateway,
        pairing_http,
        cloud_wss,
        catalog_sync,
        status,
    )
    runner = build_windows_service_runner(
        lock_path=settings.lock_file,
        components=components,
        config=config,
        logger=logger,
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
        owner_control_factory=owner_factory,
        owner_control_lane=owner_lane,
        observer_client=observer_client,
        observer_outbound_lane=observer_outbound,
        observer_intent_lane=observer_intent,
        session_catalog_client=catalog_client,
        session_catalog_outbound_lane=catalog_outbound,
        session_catalog_sync=catalog_sync,
        status_receipt=status,
        foundation_invalidator=foundation_invalidator,
        components=components,
        pairing_http=pairing_http,
    )


__all__ = ["build_windows_formal_runtime"]
