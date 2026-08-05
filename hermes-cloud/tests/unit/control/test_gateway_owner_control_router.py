from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from hermes_cloud.domain.connector_gateway import ConnectorIdentity
from hermes_cloud.modules.control.domain import ControlConnectorRoute
from hermes_cloud.modules.control.gateway import GatewayOwnerControlRouter

IDENTITY = ConnectorIdentity("tenant-test", "device-test")
ROUTE = ControlConnectorRoute("tenant-test", "device-test")
CONNECTION_1 = "11111111-1111-4111-8111-111111111111"
CONNECTION_2 = "22222222-2222-4222-8222-222222222222"
TRANSPORT_ID = "33333333-3333-4333-8333-333333333333"
REQUEST_ID = "44444444-4444-4444-8444-444444444444"
NOW = datetime(2026, 7, 31, 2, 0, tzinfo=UTC)


def _request(
    *,
    request_id: str = REQUEST_ID,
    control_transport_id: str = TRANSPORT_ID,
    operation: str = "control.transport.open",
) -> dict[str, object]:
    return {
        "request_id": request_id,
        "control_transport_id": control_transport_id,
        "operation": operation,
        "issued_at": "2026-07-31T02:00:00Z",
        "expires_at": "2026-07-31T02:00:03Z",
        "body": (
            {
                "principal_id": "principal-1",
                "client_instance_id": "55555555-5555-4555-8555-555555555555",
                "session_key": "session-root-1",
                "profile": "default",
            }
            if operation == "control.transport.open"
            else {}
        ),
    }


def _response(request: dict[str, object]) -> dict[str, object]:
    return {
        "request_id": request["request_id"],
        "control_transport_id": request["control_transport_id"],
        "operation": request["operation"],
        "state": "succeeded",
        "completed_at": "2026-07-31T02:00:01Z",
        "result": {"attached": True, "connection_role": "control"},
    }


@pytest.mark.asyncio
async def test_gateway_routes_to_exact_live_connector_and_correlates_response() -> None:
    router = GatewayOwnerControlRouter(now=lambda: NOW)
    await router.connector_connected(
        identity=IDENTITY,
        connection_id=CONNECTION_1,
        connector_instance_id="66666666-6666-4666-8666-666666666666",
        runtime_generation="runtime-1",
    )
    request = _request()
    exchange = asyncio.create_task(
        router.handle_bridge_request(
            peer_id="77777777-7777-4777-8777-777777777777",
            route=ROUTE,
            payload=request,
        )
    )

    delivery = await router.wait_for_control_request(
        identity=IDENTITY,
        connection_id=CONNECTION_1,
        connector_instance_id="66666666-6666-4666-8666-666666666666",
        runtime_generation="runtime-1",
    )
    assert delivery == request
    await router.control_request_effect_started(
        identity=IDENTITY,
        connection_id=CONNECTION_1,
        request_id=REQUEST_ID,
    )
    assert await router.accept_control_response(
        identity=IDENTITY,
        connection_id=CONNECTION_1,
        connector_instance_id="66666666-6666-4666-8666-666666666666",
        runtime_generation="runtime-1",
        payload=_response(request),
    )
    assert await exchange == _response(request)


@pytest.mark.asyncio
async def test_connector_replacement_fails_old_transport_closed_without_reroute() -> (
    None
):
    router = GatewayOwnerControlRouter(now=lambda: NOW)
    await router.connector_connected(
        identity=IDENTITY,
        connection_id=CONNECTION_1,
        connector_instance_id="66666666-6666-4666-8666-666666666666",
        runtime_generation="runtime-1",
    )
    request = _request()
    exchange = asyncio.create_task(
        router.handle_bridge_request(
            peer_id="77777777-7777-4777-8777-777777777777",
            route=ROUTE,
            payload=request,
        )
    )
    await router.wait_for_control_request(
        identity=IDENTITY,
        connection_id=CONNECTION_1,
        connector_instance_id="66666666-6666-4666-8666-666666666666",
        runtime_generation="runtime-1",
    )
    await router.control_request_effect_started(
        identity=IDENTITY,
        connection_id=CONNECTION_1,
        request_id=REQUEST_ID,
    )

    await router.connector_connected(
        identity=IDENTITY,
        connection_id=CONNECTION_2,
        connector_instance_id="88888888-8888-4888-8888-888888888888",
        runtime_generation="runtime-2",
    )
    response = await exchange
    assert response["state"] == "unknown"
    assert response["error"] == {"code": 4307, "reason": "effect_unknown"}

    stale = await router.handle_bridge_request(
        peer_id="77777777-7777-4777-8777-777777777777",
        route=ROUTE,
        payload=_request(
            request_id="99999999-9999-4999-8999-999999999999",
            operation="session.control.status",
        ),
    )
    assert stale["state"] == "failed"
    assert stale["error"] == {
        "code": 4202,
        "reason": "live_runtime_unavailable",
    }


@pytest.mark.asyncio
async def test_bridge_disconnect_enqueues_transport_cleanup_without_persistence() -> (
    None
):
    router = GatewayOwnerControlRouter(now=lambda: NOW)
    await router.connector_connected(
        identity=IDENTITY,
        connection_id=CONNECTION_1,
        connector_instance_id="66666666-6666-4666-8666-666666666666",
        runtime_generation="runtime-1",
    )
    peer_id = "77777777-7777-4777-8777-777777777777"
    open_request = _request()
    opened = asyncio.create_task(
        router.handle_bridge_request(
            peer_id=peer_id,
            route=ROUTE,
            payload=open_request,
        )
    )
    await router.wait_for_control_request(
        identity=IDENTITY,
        connection_id=CONNECTION_1,
        connector_instance_id="66666666-6666-4666-8666-666666666666",
        runtime_generation="runtime-1",
    )
    await router.control_request_effect_started(
        identity=IDENTITY,
        connection_id=CONNECTION_1,
        request_id=REQUEST_ID,
    )
    await router.accept_control_response(
        identity=IDENTITY,
        connection_id=CONNECTION_1,
        connector_instance_id="66666666-6666-4666-8666-666666666666",
        runtime_generation="runtime-1",
        payload=_response(open_request),
    )
    await opened

    await router.bridge_disconnected(peer_id=peer_id)
    cleanup = await router.wait_for_control_request(
        identity=IDENTITY,
        connection_id=CONNECTION_1,
        connector_instance_id="66666666-6666-4666-8666-666666666666",
        runtime_generation="runtime-1",
    )

    assert cleanup["control_transport_id"] == TRANSPORT_ID
    assert cleanup["operation"] == "control.transport.close"
    assert cleanup["body"] == {"reason": "gateway_shutdown"}


@pytest.mark.asyncio
async def test_completed_requests_are_removed_from_ephemeral_gateway_memory() -> None:
    router = GatewayOwnerControlRouter(now=lambda: NOW)
    await router.connector_connected(
        identity=IDENTITY,
        connection_id=CONNECTION_1,
        connector_instance_id="66666666-6666-4666-8666-666666666666",
        runtime_generation="runtime-1",
    )
    peer_id = "77777777-7777-4777-8777-777777777777"

    for index, operation in enumerate(
        (
            "control.transport.open",
            "session.control.status",
            "session.control.status",
            "control.transport.close",
        ),
        start=1,
    ):
        request = _request(
            request_id=f"00000000-0000-4000-8000-{index:012d}",
            operation=operation,
        )
        exchange = asyncio.create_task(
            router.handle_bridge_request(
                peer_id=peer_id,
                route=ROUTE,
                payload=request,
            )
        )
        delivered = await router.wait_for_control_request(
            identity=IDENTITY,
            connection_id=CONNECTION_1,
            connector_instance_id="66666666-6666-4666-8666-666666666666",
            runtime_generation="runtime-1",
        )
        assert delivered == request
        assert await router.accept_control_response(
            identity=IDENTITY,
            connection_id=CONNECTION_1,
            connector_instance_id="66666666-6666-4666-8666-666666666666",
            runtime_generation="runtime-1",
            payload=_response(request),
        )
        await exchange

    assert router.snapshot() == {
        "live_connectors": 1,
        "live_transports": 0,
        "tracked_requests": 0,
        "queued_requests": 0,
        "max_in_flight": 64,
        "max_transports": 64,
    }


@pytest.mark.asyncio
async def test_slow_connector_keeps_gateway_queue_requests_and_transports_bounded() -> (
    None
):
    router = GatewayOwnerControlRouter(
        now=lambda: NOW,
        max_in_flight=2,
        max_transports=1,
    )
    await router.connector_connected(
        identity=IDENTITY,
        connection_id=CONNECTION_1,
        connector_instance_id="66666666-6666-4666-8666-666666666666",
        runtime_generation="runtime-1",
    )
    peer_id = "77777777-7777-4777-8777-777777777777"
    opened = asyncio.create_task(
        router.handle_bridge_request(
            peer_id=peer_id,
            route=ROUTE,
            payload=_request(),
        )
    )
    open_delivery = await router.wait_for_control_request(
        identity=IDENTITY,
        connection_id=CONNECTION_1,
        connector_instance_id="66666666-6666-4666-8666-666666666666",
        runtime_generation="runtime-1",
    )
    assert open_delivery is not None
    assert await router.accept_control_response(
        identity=IDENTITY,
        connection_id=CONNECTION_1,
        connector_instance_id="66666666-6666-4666-8666-666666666666",
        runtime_generation="runtime-1",
        payload=_response(dict(open_delivery)),
    )
    await opened

    overflow = await router.handle_bridge_request(
        peer_id=peer_id,
        route=ROUTE,
        payload=_request(
            request_id="55555555-5555-4555-8555-555555555555",
            control_transport_id=("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
        ),
    )
    assert overflow["error"] == {
        "code": 4215,
        "reason": "relay_overloaded",
    }

    pending = [
        asyncio.create_task(
            router.handle_bridge_request(
                peer_id=peer_id,
                route=ROUTE,
                payload=_request(
                    request_id=f"00000000-0000-4000-8000-{index:012d}",
                    operation="session.control.status",
                ),
            )
        )
        for index in range(1, 4)
    ]
    try:
        while router.snapshot()["tracked_requests"] < 2:
            await asyncio.sleep(0)
        assert router.snapshot() == {
            "live_connectors": 1,
            "live_transports": 1,
            "tracked_requests": 2,
            "queued_requests": 2,
            "max_in_flight": 2,
            "max_transports": 1,
        }
    finally:
        for request in pending:
            request.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
    assert router.snapshot()["tracked_requests"] == 0
