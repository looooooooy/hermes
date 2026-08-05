"""Connector Gateway process bootstrap."""

import asyncio
import os
from collections.abc import Awaitable, Callable, Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

from hermes_cloud.adapters.connector_contract_v1 import CloudEnvelopeV1Adapter
from hermes_cloud.adapters.connector_gateway_runtime import (
    build_production_connector_gateway_application as _build_production_application,
)
from hermes_cloud.application.connector_frames import ConnectorFrameService
from hermes_cloud.application.connector_gateway import ConnectorGatewaySettings
from hermes_cloud.ports.connector_frame import ConnectorFrameDecoder
from hermes_cloud.ports.connector_gateway import (
    ConnectorAuthenticator,
    ConnectorCommandRouter,
    ConnectorObserverIngress,
    ConnectorObserverReceiptRouter,
    ConnectorObserverSubscriptionRouter,
    ConnectorOwnerControlRouter,
    ConnectorProtocolCodec,
    ConnectorResumeResolver,
    ConnectorSessionCatalogIngress,
    ConnectorTransportCursorAuthority,
)
from hermes_cloud.ports.dependency_probe import DependencyProbe

from .app import ConnectorGatewayApplication


def create_app(
    dependency_probes: Iterable[DependencyProbe] = (),
    frame_decoder: ConnectorFrameDecoder | None = None,
    *,
    authenticator: ConnectorAuthenticator | None = None,
    protocol_codec: ConnectorProtocolCodec | None = None,
    resume_resolver: ConnectorResumeResolver | None = None,
    transport_cursor_authority: ConnectorTransportCursorAuthority | None = None,
    command_router: ConnectorCommandRouter | None = None,
    owner_control_router: ConnectorOwnerControlRouter | None = None,
    observer_ingress: ConnectorObserverIngress | None = None,
    session_catalog_ingress: ConnectorSessionCatalogIngress | None = None,
    observer_receipt_router: ConnectorObserverReceiptRouter | None = None,
    observer_subscription_router: ConnectorObserverSubscriptionRouter | None = None,
    owner_control_bridge: Any | None = None,
    available_capabilities: tuple[str, ...] = (
        "session.catalog.v1",
        "session.observe",
        "session.observe.output-parity.v1",
    ),
    settings: ConnectorGatewaySettings | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> ConnectorGatewayApplication:
    return ConnectorGatewayApplication(
        dependency_probes,
        frame_decoder,
        authenticator=authenticator,
        protocol_codec=protocol_codec,
        resume_resolver=resume_resolver,
        transport_cursor_authority=transport_cursor_authority,
        command_router=command_router,
        owner_control_router=owner_control_router,
        observer_ingress=observer_ingress,
        session_catalog_ingress=session_catalog_ingress,
        observer_receipt_router=observer_receipt_router,
        observer_subscription_router=observer_subscription_router,
        owner_control_bridge=owner_control_bridge,
        settings=(
            settings
            or ConnectorGatewaySettings(
                available_capabilities=available_capabilities,
            )
        ),
        sleep=sleep,
    )


_default_frame_service = ConnectorFrameService(CloudEnvelopeV1Adapter())


def decode_connector_frame(raw: object):
    """Decode a frame without opening a WebSocket or causing business effects."""

    return _default_frame_service.decode_connector_frame(raw)


def build_production_connector_gateway_application(
    dependency_probes: Iterable[DependencyProbe] = (),
    *,
    environment: Mapping[str, str],
    utc_now: Callable[[], datetime] = lambda: datetime.now(UTC),
    settings: ConnectorGatewaySettings | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> object:
    return _build_production_application(
        dependency_probes,
        environment=environment,
        application_factory=create_app,
        utc_now=utc_now,
        settings=settings,
        sleep=sleep,
    )


app = build_production_connector_gateway_application(environment=os.environ)
