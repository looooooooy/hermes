from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from uuid import UUID, uuid4

from hermes_connector.adapters.cloud.codec import ConnectorProtocolCodec
from hermes_connector.adapters.cloud.websocket_transport import (
    WebSocketsCloudTransport,
)
from hermes_connector.application.cloud_wss_client import (
    CloudClientConfig,
    CloudWSSClient,
    ExponentialBackoff,
)
from hermes_connector.ports.cloud import (
    CloudTokenProviderPort,
    CloudTransportPort,
    ConnectorProtocolCodecPort,
)
from hermes_connector.ports.control_command import CommandLanePort
from hermes_connector.ports.local_gateway import LocalRuntimeAuthorityPort
from hermes_connector.ports.observer import (
    ObserverIntentLanePort,
    ObserverOutboundLanePort,
)
from hermes_connector.ports.owner_control import OwnerControlLanePort
from hermes_connector.ports.reliable_storage import ReliableStoragePort
from hermes_connector.ports.session_catalog import (
    SessionCatalogOutboundLanePort,
    SessionCatalogSyncPort,
)


def build_cloud_wss_client(
    *,
    config: CloudClientConfig,
    token_provider: CloudTokenProviderPort,
    storage: ReliableStoragePort,
    runtime_authority: LocalRuntimeAuthorityPort,
    command_lane: CommandLanePort | None = None,
    owner_control_lane: OwnerControlLanePort | None = None,
    observer_outbound_lane: ObserverOutboundLanePort | None = None,
    observer_intent_lane: ObserverIntentLanePort | None = None,
    session_catalog_outbound_lane: SessionCatalogOutboundLanePort | None = None,
    session_catalog_sync: SessionCatalogSyncPort | None = None,
    transport: CloudTransportPort | None = None,
    codec: ConnectorProtocolCodecPort | None = None,
    utc_now: Callable[[], datetime] = lambda: datetime.now(UTC),
    message_id_factory: Callable[[], UUID] = uuid4,
    backoff: ExponentialBackoff | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> CloudWSSClient:
    """Compose the platform-independent Connector Protocol cloud client."""

    return CloudWSSClient(
        config=config,
        transport=transport or WebSocketsCloudTransport(),
        token_provider=token_provider,
        storage=storage,
        runtime_authority=runtime_authority,
        codec=codec or ConnectorProtocolCodec(),
        command_lane=command_lane,
        owner_control_lane=owner_control_lane,
        observer_outbound_lane=observer_outbound_lane,
        observer_intent_lane=observer_intent_lane,
        session_catalog_outbound_lane=session_catalog_outbound_lane,
        session_catalog_sync=session_catalog_sync,
        utc_now=utc_now,
        message_id_factory=message_id_factory,
        backoff=backoff,
        sleep=sleep,
    )
