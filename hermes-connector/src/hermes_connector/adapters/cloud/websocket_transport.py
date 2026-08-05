from __future__ import annotations

import asyncio
import ipaddress
import ssl
from collections.abc import Awaitable, Callable
from typing import Protocol
from urllib.parse import urlsplit

from websockets.asyncio.client import connect as websockets_connect
from websockets.exceptions import ConnectionClosed, WebSocketException

from hermes_connector.ports.cloud import CloudConnectionClosed

_DEFAULT_SUBPROTOCOL = "hermes.connector.v1"
_MAX_FRAME_BYTES = 262_144


class CloudTransportSecurityError(ValueError):
    """Raised when an endpoint or credential is unsafe for cloud transport."""


class CloudSubprotocolError(ConnectionError):
    """Raised when the peer doesn't negotiate the required subprotocol."""


class CloudFrameLimitExceeded(ValueError):
    """Raised when an inbound or outbound frame exceeds the protocol limit."""


class CloudTransportError(ConnectionError):
    """Raised when the WebSocket adapter cannot complete bounded I/O."""


class _RawWebSocket(Protocol):
    subprotocol: str | None

    async def send(self, message: str) -> None: ...

    async def recv(self, decode: bool | None = None) -> bytes | str: ...

    async def close(self, code: int = 1000, reason: str = "") -> None: ...


ConnectFactory = Callable[..., Awaitable[_RawWebSocket]]


def _is_loopback(hostname: str) -> bool:
    if hostname.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _validate_endpoint(endpoint: str) -> None:
    parsed = urlsplit(endpoint)
    if (
        parsed.scheme not in {"ws", "wss"}
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise CloudTransportSecurityError("invalid WebSocket endpoint")
    if parsed.scheme == "ws" and not _is_loopback(parsed.hostname):
        raise CloudTransportSecurityError("remote cloud endpoints require TLS")


def _validate_token(token: str) -> None:
    if not isinstance(token, str) or not token or "\r" in token or "\n" in token:
        raise CloudTransportSecurityError("invalid cloud access token")


class _WebSocketConnection:
    def __init__(self, raw: _RawWebSocket, *, max_frame_bytes: int) -> None:
        self._raw = raw
        self._max_frame_bytes = max_frame_bytes

    async def send(self, frame: bytes, *, timeout_seconds: float = 10.0) -> None:
        if not isinstance(frame, bytes):
            raise TypeError("cloud transport requires bytes payloads")
        text_frame = frame.decode("utf-8", errors="strict")
        if len(text_frame.encode("utf-8", errors="strict")) > self._max_frame_bytes:
            raise CloudFrameLimitExceeded("outbound frame exceeds protocol limit")
        try:
            async with asyncio.timeout(timeout_seconds):
                await self._raw.send(text_frame)
        except WebSocketException as error:
            raise CloudTransportError("cloud WebSocket send failed") from error

    async def receive(self, *, timeout_seconds: float = 10.0) -> bytes:
        try:
            async with asyncio.timeout(timeout_seconds):
                frame = await self._raw.recv()
        except ConnectionClosed as error:
            received = error.rcvd
            raise CloudConnectionClosed(
                code=received.code if received is not None else 1006,
                reason=received.reason if received is not None else "",
            ) from error
        except WebSocketException as error:
            raise CloudTransportError("cloud WebSocket receive failed") from error
        if not isinstance(frame, str):
            raise TypeError("cloud transport requires text WebSocket frames")
        encoded_frame = frame.encode("utf-8", errors="strict")
        if len(encoded_frame) > self._max_frame_bytes:
            raise CloudFrameLimitExceeded("inbound frame exceeds protocol limit")
        return encoded_frame

    async def close(
        self,
        *,
        code: int = 1000,
        reason: str = "",
        timeout_seconds: float = 5.0,
    ) -> None:
        try:
            async with asyncio.timeout(timeout_seconds):
                await self._raw.close(code=code, reason=reason)
        except WebSocketException as error:
            raise CloudTransportError("cloud WebSocket close failed") from error


class WebSocketsCloudTransport:
    """Secure WebSocket transport for Connector Protocol v1."""

    def __init__(
        self,
        *,
        connect_factory: ConnectFactory = websockets_connect,
        subprotocol: str = _DEFAULT_SUBPROTOCOL,
        max_frame_bytes: int = _MAX_FRAME_BYTES,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        self._connect_factory = connect_factory
        self._subprotocol = subprotocol
        self._max_frame_bytes = max_frame_bytes
        self._ssl_context = ssl_context

    async def connect(
        self,
        endpoint: str,
        *,
        token: str,
    ) -> _WebSocketConnection:
        _validate_endpoint(endpoint)
        _validate_token(token)
        options: dict[str, object] = {
            "additional_headers": {"Authorization": f"Bearer {token}"},
            "close_timeout": 5,
            "compression": None,
            "max_queue": 16,
            "max_size": self._max_frame_bytes,
            "open_timeout": 10,
            "ping_interval": None,
            "subprotocols": (self._subprotocol,),
        }
        if self._ssl_context is not None:
            if urlsplit(endpoint).scheme != "wss":
                raise CloudTransportSecurityError(
                    "custom TLS context requires a wss endpoint"
                )
            if (
                self._ssl_context.verify_mode != ssl.CERT_REQUIRED
                or not self._ssl_context.check_hostname
            ):
                raise CloudTransportSecurityError(
                    "custom TLS context must verify peer and hostname"
                )
            options["ssl"] = self._ssl_context
        raw = await self._connect_factory(endpoint, **options)
        if raw.subprotocol != self._subprotocol:
            await raw.close(code=1002, reason="subprotocol_required")
            raise CloudSubprotocolError("required cloud subprotocol was not negotiated")
        return _WebSocketConnection(raw, max_frame_bytes=self._max_frame_bytes)
