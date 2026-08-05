from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID

import pytest

from hermes_cloud.adapters.owner_control_bridge import (
    BridgeRegisteringRouteResolver,
    OwnerControlBridgeBeforeEffect,
    OwnerControlBridgeClient,
    OwnerControlBridgeProtocolError,
    OwnerControlBridgeServer,
)
from hermes_cloud.domain.connector_gateway import ConnectorIdentity
from hermes_cloud.modules.cloud_api.domain import (
    Principal,
    WebSocketTicketAuthentication,
)
from hermes_cloud.modules.control.broker import OwnerControlBroker
from hermes_cloud.modules.control.domain import (
    ControlConnectorRoute,
    ControlRequestContext,
)
from hermes_cloud.modules.control.gateway import GatewayOwnerControlRouter
from hermes_cloud.modules.control.runtime import BrokeredControlRuntime

ROUTE = ControlConnectorRoute(tenant_id="tenant-test", device_id="device-test")


def _request(request_id: str) -> dict[str, object]:
    return {
        "request_id": request_id,
        "control_transport_id": "22222222-2222-4222-8222-222222222222",
        "operation": "session.control.status",
        "issued_at": "2026-07-31T02:00:00Z",
        "expires_at": "2026-07-31T02:00:03Z",
        "body": {},
    }


class _Handler:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ControlConnectorRoute, dict[str, object]]] = []
        self.disconnected: list[str] = []

    async def handle_bridge_request(
        self,
        *,
        peer_id: str,
        route: ControlConnectorRoute,
        payload: Mapping[str, object],
    ) -> Mapping[str, object]:
        copied = dict(payload)
        self.calls.append((peer_id, route, copied))
        await asyncio.sleep(0)
        return {
            "request_id": copied["request_id"],
            "control_transport_id": copied["control_transport_id"],
            "operation": copied["operation"],
            "state": "succeeded",
            "completed_at": "2026-07-31T02:00:01Z",
            "result": {
                "controller_kind": "none",
                "controller_label": None,
                "control_revision": 0,
                "lease_expires_at_epoch_ms": 0,
                "pending_input": None,
            },
        }

    async def bridge_disconnected(self, *, peer_id: str) -> None:
        self.disconnected.append(peer_id)


class _BlockingHandler(_Handler):
    def __init__(self, *, expected_concurrency: int) -> None:
        super().__init__()
        self._expected_concurrency = expected_concurrency
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def handle_bridge_request(
        self,
        *,
        peer_id: str,
        route: ControlConnectorRoute,
        payload: Mapping[str, object],
    ) -> Mapping[str, object]:
        copied = dict(payload)
        self.calls.append((peer_id, route, copied))
        if len(self.calls) == self._expected_concurrency:
            self.started.set()
        await self.release.wait()
        return await super().handle_bridge_request(
            peer_id=peer_id,
            route=route,
            payload=payload,
        )


@pytest.mark.asyncio
async def test_private_uds_bridge_multiplexes_bounded_requests_and_cleans_peer() -> (
    None
):
    with TemporaryDirectory(prefix="hc-", dir="/tmp") as temporary:
        runtime_directory = Path(temporary)
        runtime_directory.chmod(0o700)
        socket_path = runtime_directory / "owner-control.sock"
        handler = _Handler()
        server = OwnerControlBridgeServer(
            socket_path=socket_path,
            handler=handler,
            max_frame_bytes=4_096,
            max_in_flight=2,
            peer_id_factory=iter(
                [UUID("11111111-1111-4111-8111-111111111111")]
            ).__next__,
        )
        client = OwnerControlBridgeClient(
            socket_path=socket_path,
            max_frame_bytes=4_096,
            request_timeout_seconds=0.5,
        )

        await server.start()
        try:
            assert socket_path.stat().st_mode & 0o777 == 0o600
            first, second = await asyncio.gather(
                client.exchange(
                    route=ROUTE,
                    payload=_request("33333333-3333-4333-8333-333333333333"),
                ),
                client.exchange(
                    route=ROUTE,
                    payload=_request("44444444-4444-4444-8444-444444444444"),
                ),
            )
            assert first["state"] == "succeeded"
            assert second["state"] == "succeeded"
            assert len({call[0] for call in handler.calls}) == 1
            assert [call[1] for call in handler.calls] == [ROUTE, ROUTE]
        finally:
            await client.close()
            await server.stop()

        assert handler.disconnected == ["11111111-1111-4111-8111-111111111111"]
        assert not socket_path.exists()


@pytest.mark.asyncio
async def test_server_backpressures_before_creating_more_than_bounded_tasks() -> None:
    with TemporaryDirectory(prefix="hc-", dir="/tmp") as temporary:
        runtime_directory = Path(temporary)
        runtime_directory.chmod(0o700)
        socket_path = runtime_directory / "owner-control.sock"
        handler = _BlockingHandler(expected_concurrency=2)
        server = OwnerControlBridgeServer(
            socket_path=socket_path,
            handler=handler,
            max_in_flight=2,
        )
        client = OwnerControlBridgeClient(
            socket_path=socket_path,
            max_in_flight=3,
            request_timeout_seconds=1.0,
        )
        await server.start()
        exchanges = [
            asyncio.create_task(
                client.exchange(
                    route=ROUTE,
                    payload=_request(f"00000000-0000-4000-8000-{index:012d}"),
                )
            )
            for index in range(1, 4)
        ]
        try:
            await asyncio.wait_for(handler.started.wait(), timeout=0.5)
            await asyncio.sleep(0)
            assert len(handler.calls) == 2
            assert server.snapshot() == {
                "active_connections": 1,
                "active_request_tasks": 2,
                "max_in_flight": 2,
            }
            handler.release.set()
            await asyncio.gather(*exchanges)
        finally:
            handler.release.set()
            for exchange in exchanges:
                exchange.cancel()
            await asyncio.gather(*exchanges, return_exceptions=True)
            await client.close()
            await server.stop()


@pytest.mark.asyncio
async def test_bridge_rejects_unsafe_permissions_and_oversized_frame() -> None:
    with TemporaryDirectory(prefix="hc-", dir="/tmp") as temporary:
        unsafe = Path(temporary)
        unsafe.chmod(0o755)
        socket_path = unsafe / "owner-control.sock"
        handler = _Handler()
        server = OwnerControlBridgeServer(
            socket_path=socket_path,
            handler=handler,
            max_frame_bytes=256,
        )

        with pytest.raises(ValueError, match="permissions"):
            await server.start()

        unsafe.chmod(0o700)
        await server.start()
        client = OwnerControlBridgeClient(
            socket_path=socket_path,
            max_frame_bytes=256,
            request_timeout_seconds=0.5,
        )
        try:
            with pytest.raises(
                OwnerControlBridgeProtocolError,
                match="frame",
            ):
                await client.exchange(
                    route=ROUTE,
                    payload={
                        **_request("55555555-5555-4555-8555-555555555555"),
                        "body": {"padding": "x" * 1_024},
                    },
                )
        finally:
            await client.close()
            await server.stop()


@pytest.mark.asyncio
async def test_client_fails_closed_then_reconnects_after_gateway_restart() -> None:
    with TemporaryDirectory(prefix="hc-", dir="/tmp") as temporary:
        runtime_directory = Path(temporary)
        runtime_directory.chmod(0o700)
        socket_path = runtime_directory / "owner-control.sock"
        handler = _Handler()
        server = OwnerControlBridgeServer(
            socket_path=socket_path,
            handler=handler,
        )
        client = OwnerControlBridgeClient(
            socket_path=socket_path,
            request_timeout_seconds=0.5,
        )

        await server.start()
        assert (
            await client.exchange(
                route=ROUTE,
                payload=_request("66666666-6666-4666-8666-666666666666"),
            )
        )["state"] == "succeeded"
        await server.stop()
        await asyncio.sleep(0)

        with pytest.raises(OwnerControlBridgeBeforeEffect):
            await client.exchange(
                route=ROUTE,
                payload=_request("77777777-7777-4777-8777-777777777777"),
            )

        await server.start()
        try:
            assert (
                await client.exchange(
                    route=ROUTE,
                    payload=_request("88888888-8888-4888-8888-888888888888"),
                )
            )["state"] == "succeeded"
        finally:
            await client.close()
            await server.stop()


@pytest.mark.asyncio
async def test_brokered_runtime_crosses_real_uds_to_exact_gateway_connector() -> None:
    with TemporaryDirectory(prefix="hc-", dir="/tmp") as temporary:
        runtime_directory = Path(temporary)
        runtime_directory.chmod(0o700)
        socket_path = runtime_directory / "owner-control.sock"
        gateway = GatewayOwnerControlRouter()
        identity = ConnectorIdentity("tenant-test", "device-test")
        connector_connection_id = "66666666-6666-4666-8666-666666666666"
        connector_instance_id = "77777777-7777-4777-8777-777777777777"
        await gateway.connector_connected(
            identity=identity,
            connection_id=connector_connection_id,
            connector_instance_id=connector_instance_id,
            runtime_generation="runtime-1",
        )
        server = OwnerControlBridgeServer(
            socket_path=socket_path,
            handler=gateway,
        )
        client = OwnerControlBridgeClient(
            socket_path=socket_path,
            request_timeout_seconds=0.5,
        )
        broker = OwnerControlBroker(
            control_transport_id_factory=lambda: UUID(
                "88888888-8888-4888-8888-888888888888"
            )
        )

        class RouteResolver:
            async def resolve(
                self,
                _context: ControlRequestContext,
            ) -> ControlConnectorRoute:
                return ControlConnectorRoute(
                    tenant_id="tenant-test",
                    device_id="device-test",
                    principal_tenant_id=("11111111-1111-4111-8111-111111111111"),
                )

        request_ids = iter(
            [
                UUID("99999999-9999-4999-8999-999999999999"),
                UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
                UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
            ]
        )
        runtime = BrokeredControlRuntime(
            broker=broker,
            route_resolver=BridgeRegisteringRouteResolver(
                delegate=RouteResolver(),
                broker=broker,
                client=client,
                broker_connection_id=("cccccccc-cccc-4ccc-8ccc-cccccccccccc"),
            ),
            request_id_factory=lambda: next(request_ids),
            timeout_seconds=0.5,
        )
        context = ControlRequestContext(
            authentication=WebSocketTicketAuthentication(
                principal=Principal(
                    tenant_id=UUID("11111111-1111-4111-8111-111111111111"),
                    user_id=UUID("22222222-2222-4222-8222-222222222222"),
                    provider="basic",
                    refresh_session_id=UUID("33333333-3333-4333-8333-333333333333"),
                ),
                connection_role="control",
                client_instance_id=("44444444-4444-4444-8444-444444444444"),
                session_id=UUID("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"),
                session_key="session-root-1",
                profile="default",
                agent_id=UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd"),
            ),
            connection_id="55555555-5555-4555-8555-555555555555",
        )

        async def connector() -> None:
            while True:
                delivered = await gateway.wait_for_control_request(
                    identity=identity,
                    connection_id=connector_connection_id,
                    connector_instance_id=connector_instance_id,
                    runtime_generation="runtime-1",
                )
                assert delivered is not None
                request_id = str(delivered["request_id"])
                await gateway.control_request_effect_started(
                    identity=identity,
                    connection_id=connector_connection_id,
                    request_id=request_id,
                )
                operation = str(delivered["operation"])
                result = (
                    {"attached": True, "connection_role": "control"}
                    if operation == "control.transport.open"
                    else (
                        {
                            "controller_kind": "none",
                            "controller_label": None,
                            "control_revision": 0,
                            "lease_expires_at_epoch_ms": 0,
                            "pending_input": None,
                        }
                        if operation == "session.control.status"
                        else {"closed": True}
                    )
                )
                await gateway.accept_control_response(
                    identity=identity,
                    connection_id=connector_connection_id,
                    connector_instance_id=connector_instance_id,
                    runtime_generation="runtime-1",
                    payload={
                        "request_id": request_id,
                        "control_transport_id": delivered["control_transport_id"],
                        "operation": operation,
                        "state": "succeeded",
                        "completed_at": "2026-07-31T02:00:01Z",
                        "result": result,
                    },
                )
                if operation == "control.transport.close":
                    return

        await server.start()
        connector_task = asyncio.create_task(connector())
        try:
            await runtime.open(context=context)
            status = await runtime.execute(
                    context=context,
                    method="session.control.status",
                    params={
                        "session_id": "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
                    },
            )
            await runtime.close(
                context=context,
                reason="client_disconnected",
            )
            await connector_task
        finally:
            connector_task.cancel()
            await asyncio.gather(connector_task, return_exceptions=True)
            await client.close()
            await server.stop()

        assert status["controller_kind"] == "none"
