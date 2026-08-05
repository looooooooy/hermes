"""Connector Gateway ASGI application."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from hermes_cloud.adapters.connector_asgi import (
    CONNECTOR_SUBPROTOCOL,
    ASGIConnectorConnection,
    FailClosedConnectorAuthenticator,
    bearer_token_from_headers,
)
from hermes_cloud.adapters.connector_contract_v1 import CloudEnvelopeV1Adapter
from hermes_cloud.adapters.connector_protocol_codec import (
    AuthoritativeFrameProtocolCodec,
)
from hermes_cloud.application.asgi_health import HealthApplication
from hermes_cloud.application.connector_frames import ConnectorFrameService
from hermes_cloud.application.connector_gateway import (
    ConnectorGatewayService,
    ConnectorGatewaySettings,
)
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


class ConnectorGatewayApplication(HealthApplication):
    """Expose health and the authenticated Connector Protocol WebSocket."""

    def __init__(
        self,
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
        settings: ConnectorGatewaySettings | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        probes = tuple(dependency_probes)
        if authenticator is None:
            fail_closed_authenticator = FailClosedConnectorAuthenticator()
            authenticator = fail_closed_authenticator
            probes = (*probes, fail_closed_authenticator)
        super().__init__("connector-gateway", probes)
        codec = protocol_codec or CloudEnvelopeV1Adapter()
        authoritative_frame_decoder = frame_decoder or codec
        if authoritative_frame_decoder is not codec:
            codec = AuthoritativeFrameProtocolCodec(
                authoritative_frame_decoder,
                codec,
            )
        self._frame_service = ConnectorFrameService(authoritative_frame_decoder)
        self._owner_control_bridge = owner_control_bridge
        self._gateway_service = ConnectorGatewayService(
            authenticator=authenticator,
            codec=codec,
            settings=settings or ConnectorGatewaySettings(),
            resume_resolver=resume_resolver,
            transport_cursor_authority=transport_cursor_authority,
            command_router=command_router,
            owner_control_router=owner_control_router,
            observer_ingress=observer_ingress,
            session_catalog_ingress=session_catalog_ingress,
            observer_receipt_router=observer_receipt_router,
            observer_subscription_router=observer_subscription_router,
            sleep=sleep,
        )

    async def startup(self) -> None:
        if self._owner_control_bridge is not None:
            await self._owner_control_bridge.start()
        try:
            await super().startup()
        except BaseException:
            if self._owner_control_bridge is not None:
                await self._owner_control_bridge.stop()
            raise

    async def shutdown(self) -> None:
        try:
            await super().shutdown()
        finally:
            if self._owner_control_bridge is not None:
                await self._owner_control_bridge.stop()

    def decode_connector_frame(self, raw: object):
        return self._frame_service.decode_connector_frame(raw)

    async def __call__(
        self,
        scope: dict[str, Any],
        receive,
        send,
    ) -> None:
        if scope.get("type") != "websocket":
            await super().__call__(scope, receive, send)
            return
        if scope.get("path") != "/api/ws":
            await send({"type": "websocket.close", "code": 1008})
            return
        subprotocols = scope.get("subprotocols")
        if not isinstance(subprotocols, (list, tuple)) or tuple(subprotocols) != (
            CONNECTOR_SUBPROTOCOL,
        ):
            await send(
                {
                    "type": "websocket.close",
                    "code": 1002,
                    "reason": "subprotocol_required",
                }
            )
            return
        if not self.snapshot()["ready"]:
            await send(
                {
                    "type": "websocket.close",
                    "code": 1013,
                    "reason": "gateway_not_ready",
                }
            )
            return
        connection = ASGIConnectorConnection(receive, send)
        token = bearer_token_from_headers(scope.get("headers", ()))
        await self._gateway_service.handle(token, connection)
