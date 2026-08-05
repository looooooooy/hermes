"""Bounded ASGI WebSocket transport for the Connector Gateway."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from hermes_cloud.domain.connector_gateway import (
    ConnectorDisconnected,
    ConnectorUnsupportedData,
)

ASGIReceive = Callable[[], Awaitable[dict[str, Any]]]
ASGISend = Callable[[dict[str, Any]], Awaitable[None]]
CONNECTOR_SUBPROTOCOL = "hermes.connector.v1"


def bearer_token_from_headers(
    headers: Iterable[tuple[bytes, bytes]],
) -> str | None:
    values = [value for name, value in headers if name.lower() == b"authorization"]
    if len(values) != 1:
        return None
    try:
        authorization = values[0].decode("ascii")
    except UnicodeDecodeError:
        return None
    scheme, separator, token = authorization.partition(" ")
    if (
        separator != " "
        or scheme.lower() != "bearer"
        or not token
        or token != token.strip()
        or any(character.isspace() for character in token)
    ):
        return None
    return token


class ASGIConnectorConnection:
    """Adapt one ASGI WebSocket event stream to the application port."""

    def __init__(self, receive: ASGIReceive, send: ASGISend) -> None:
        self._receive = receive
        self._send = send
        self._accepted = False
        self._closed = False
        self._peer_disconnected = False

    @property
    def peer_disconnected(self) -> bool:
        return self._peer_disconnected

    async def accept(self, *, timeout_seconds: float) -> None:
        message = await self._receive_with_timeout(timeout_seconds)
        if message.get("type") != "websocket.connect":
            raise ConnectorUnsupportedData(
                "connector websocket did not start with connect"
            )
        await self._send_with_timeout(
            {
                "type": "websocket.accept",
                "subprotocol": CONNECTOR_SUBPROTOCOL,
            },
            timeout_seconds,
        )
        self._accepted = True

    async def receive_text(self, *, timeout_seconds: float) -> str:
        message = await self._receive_with_timeout(timeout_seconds)
        message_type = message.get("type")
        if message_type == "websocket.disconnect":
            self._peer_disconnected = True
            raise ConnectorDisconnected()
        if message_type != "websocket.receive":
            raise ConnectorUnsupportedData("connector websocket event is invalid")
        text = message.get("text")
        if not isinstance(text, str) or message.get("bytes") is not None:
            raise ConnectorUnsupportedData(
                "connector websocket requires one text document per frame"
            )
        return text

    async def send_text(self, text: str, *, timeout_seconds: float) -> None:
        if not self._accepted or self._closed:
            raise ConnectorUnsupportedData("connector websocket is not writable")
        await self._send_with_timeout(
            {"type": "websocket.send", "text": text},
            timeout_seconds,
        )

    async def close(
        self,
        *,
        code: int,
        reason: str,
        timeout_seconds: float,
    ) -> None:
        if self._closed:
            return
        self._closed = True
        if self._peer_disconnected:
            return
        await self._send_with_timeout(
            {
                "type": "websocket.close",
                "code": code,
                "reason": reason,
            },
            timeout_seconds,
        )

    async def _receive_with_timeout(
        self,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        try:
            async with asyncio.timeout(timeout_seconds):
                return await self._receive()
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            raise
        except OSError as error:
            self._peer_disconnected = True
            raise ConnectorDisconnected() from error

    async def _send_with_timeout(
        self,
        message: dict[str, Any],
        timeout_seconds: float,
    ) -> None:
        try:
            async with asyncio.timeout(timeout_seconds):
                await self._send(message)
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            raise
        except OSError as error:
            self._peer_disconnected = True
            raise ConnectorDisconnected() from error


class FailClosedConnectorAuthenticator:
    """Default authenticator and readiness probe that never accepts traffic."""

    name = "connector-authentication"
    critical = True
    deadline_seconds = 0.1

    async def authenticate(self, _bearer_token: str):
        raise PermissionError("connector authentication is not configured")

    async def check(self) -> None:
        raise RuntimeError("connector authentication is not configured")
