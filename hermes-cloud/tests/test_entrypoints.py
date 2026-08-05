from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from hermes_cloud.entrypoints.business_api import create_app as create_business_api
from hermes_cloud.entrypoints.connector_gateway import (
    create_app as create_connector_gateway,
)
from hermes_cloud.entrypoints.file_gateway import create_app as create_file_gateway
from hermes_cloud.entrypoints.worker import create_worker

Send = Callable[[dict[str, Any]], Awaitable[None]]


class _ConfiguredConnectorAuthenticator:
    async def authenticate(self, _bearer_token: str):
        raise AssertionError("health checks must not authenticate")


async def _get(app: Any, path: str) -> tuple[int, dict[str, Any]]:
    messages: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    await app(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 1234),
            "server": ("127.0.0.1", 8000),
        },
        receive,
        send,
    )

    start = next(
        message for message in messages if message["type"] == "http.response.start"
    )
    body = next(
        message for message in messages if message["type"] == "http.response.body"
    )
    return start["status"], json.loads(body["body"])


@pytest.mark.parametrize(
    ("factory", "component"),
    [
        (
            lambda: create_business_api(projection_repository=object()),
            "business-api",
        ),
        (
            lambda: create_connector_gateway(
                authenticator=_ConfiguredConnectorAuthenticator()
            ),
            "connector-gateway",
        ),
        (create_file_gateway, "file-gateway"),
    ],
)
def test_http_entrypoint_exposes_only_safe_health_routes(
    factory: Callable[[], Any],
    component: str,
) -> None:
    async def scenario() -> None:
        app = factory()

        live_status, live = await _get(app, "/live")
        ready_before_status, ready_before = await _get(app, "/ready")
        unknown_status, unknown = await _get(app, "/business-data")

        assert live_status == 200
        assert live == {
            "component": component,
            "error": None,
            "live": True,
            "ready": False,
            "state": "CREATED",
        }
        assert ready_before_status == 503
        assert ready_before["ready"] is False
        assert unknown_status == 404
        assert unknown == {
            "category": "PROTOCOL",
            "code": "ROUTE_NOT_FOUND",
            "retryable": False,
        }

        await app.startup()
        ready_status, ready = await _get(app, "/ready")
        assert ready_status == 200
        assert ready["state"] == "READY"
        assert ready["ready"] is True

        await app.shutdown()
        stopped_status, stopped = await _get(app, "/ready")
        assert stopped_status == 503
        assert stopped["state"] == "STOPPED"

    asyncio.run(scenario())


def test_http_entrypoint_supports_asgi_lifespan() -> None:
    async def scenario() -> None:
        app = create_business_api()
        incoming = asyncio.Queue[dict[str, Any]]()
        outgoing: list[dict[str, Any]] = []
        await incoming.put({"type": "lifespan.startup"})
        await incoming.put({"type": "lifespan.shutdown"})

        async def receive() -> dict[str, Any]:
            return await incoming.get()

        async def send(message: dict[str, Any]) -> None:
            outgoing.append(message)

        await app({"type": "lifespan", "asgi": {"version": "3.0"}}, receive, send)

        assert outgoing == [
            {"type": "lifespan.startup.complete"},
            {"type": "lifespan.shutdown.complete"},
        ]
        assert app.snapshot()["state"] == "STOPPED"

    asyncio.run(scenario())


def test_worker_runner_starts_and_stops_without_external_dependencies() -> None:
    async def scenario() -> None:
        worker = create_worker()
        assert worker.snapshot()["ready"] is False

        await worker.start()
        assert worker.snapshot() == {
            "component": "async-worker",
            "error": None,
            "live": True,
            "ready": True,
            "state": "READY",
        }

        await worker.stop()
        assert worker.snapshot()["state"] == "STOPPED"
        assert worker.snapshot()["ready"] is False

    asyncio.run(scenario())
