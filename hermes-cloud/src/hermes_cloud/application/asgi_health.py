"""Minimal standard ASGI health application with fail-closed routing."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from hermes_cloud.application.runtime import ComponentRuntime
from hermes_cloud.errors import ClassifiedError, ErrorCategory
from hermes_cloud.ports.dependency_probe import DependencyProbe

Receive = Callable[[], Awaitable[dict[str, Any]]]
Send = Callable[[dict[str, Any]], Awaitable[None]]


class HealthApplication:
    """Expose only lifecycle and readiness health over ASGI."""

    def __init__(
        self,
        component: str,
        dependency_probes: Iterable[DependencyProbe] = (),
    ) -> None:
        self._runtime = ComponentRuntime(component, dependency_probes)

    async def startup(self) -> None:
        await self._runtime.startup()

    async def shutdown(self) -> None:
        await self._runtime.shutdown()

    def snapshot(self) -> dict[str, object]:
        return self._runtime.snapshot()

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Receive,
        send: Send,
    ) -> None:
        scope_type = scope.get("type")
        if scope_type == "lifespan":
            await self._lifespan(receive, send)
            return
        if scope_type != "http":
            if scope_type == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            return

        method = scope.get("method")
        path = scope.get("path")
        if method != "GET" or path not in {"/live", "/ready"}:
            error = ClassifiedError(
                ErrorCategory.PROTOCOL,
                "ROUTE_NOT_FOUND",
                False,
            )
            await self._respond(send, 404, error.as_dict())
            return

        snapshot = self.snapshot()
        healthy = snapshot["live"] if path == "/live" else snapshot["ready"]
        await self._respond(send, 200 if healthy else 503, snapshot)

    async def _lifespan(self, receive: Receive, send: Send) -> None:
        while True:
            message = await receive()
            message_type = message.get("type")
            if message_type == "lifespan.startup":
                try:
                    await self.startup()
                except asyncio.CancelledError:
                    raise
                # The ASGI lifespan boundary must convert arbitrary component
                # startup failures into the protocol-level failure event.
                except Exception as error:  # noqa: BLE001
                    self._runtime.mark_failed(error)
                    await send(
                        {
                            "type": "lifespan.startup.failed",
                            "message": "component startup failed",
                        }
                    )
                    return
                await send({"type": "lifespan.startup.complete"})
                continue
            if message_type == "lifespan.shutdown":
                try:
                    await self.shutdown()
                except asyncio.CancelledError:
                    raise
                # Shutdown hooks are injected component boundaries and may
                # raise implementation-specific exceptions.
                except Exception:  # noqa: BLE001
                    await send(
                        {
                            "type": "lifespan.shutdown.failed",
                            "message": "component shutdown failed",
                        }
                    )
                    return
                await send({"type": "lifespan.shutdown.complete"})
                return

    @staticmethod
    async def _respond(
        send: Send,
        status: int,
        payload: dict[str, object],
    ) -> None:
        body = json.dumps(
            payload,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": body,
                "more_body": False,
            }
        )
