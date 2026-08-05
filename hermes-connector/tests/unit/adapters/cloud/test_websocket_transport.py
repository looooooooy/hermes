from __future__ import annotations

import ssl

import pytest
from websockets.exceptions import ConnectionClosedError
from websockets.frames import Close

from hermes_connector.adapters.cloud.websocket_transport import (
    CloudFrameLimitExceeded,
    CloudSubprotocolError,
    CloudTransportSecurityError,
    WebSocketsCloudTransport,
)
from hermes_connector.ports.cloud import CloudConnectionClosed


class _WebSocket:
    def __init__(
        self,
        *,
        subprotocol: str | None = "hermes.connector.v1",
        inbound: str | bytes = "{}",
    ) -> None:
        self.subprotocol = subprotocol
        self.inbound = inbound
        self.sent: list[str | bytes] = []
        self.closed: list[tuple[int, str]] = []
        self.recv_decode: list[bool | None] = []

    async def send(self, frame: str | bytes) -> None:
        self.sent.append(frame)

    async def recv(self, decode: bool | None = None) -> str | bytes:
        self.recv_decode.append(decode)
        return self.inbound

    async def close(self, *, code: int, reason: str) -> None:
        self.closed.append((code, reason))


class _ConnectFactory:
    def __init__(self, websocket: _WebSocket) -> None:
        self.websocket = websocket
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def __call__(self, endpoint: str, **kwargs: object) -> _WebSocket:
        self.calls.append((endpoint, kwargs))
        return self.websocket


@pytest.mark.asyncio
async def test_transport_enforces_websockets_15_security_options() -> None:
    websocket = _WebSocket()
    factory = _ConnectFactory(websocket)
    transport = WebSocketsCloudTransport(connect_factory=factory)

    connection = await transport.connect(
        "wss://cloud.example.test/connector",
        token="opaque-secret",
    )

    endpoint, options = factory.calls[0]
    assert endpoint == "wss://cloud.example.test/connector"
    assert options == {
        "additional_headers": {"Authorization": "Bearer opaque-secret"},
        "close_timeout": 5.0,
        "compression": None,
        "max_queue": 16,
        "max_size": 262_144,
        "open_timeout": 10.0,
        "ping_interval": None,
        "subprotocols": ("hermes.connector.v1",),
    }
    await connection.send(b"{}")
    assert await connection.receive() == b"{}"
    assert websocket.sent == ["{}"]
    assert websocket.recv_decode == [None]


@pytest.mark.asyncio
async def test_remote_plaintext_websocket_is_rejected_before_network_io() -> None:
    factory = _ConnectFactory(_WebSocket())
    transport = WebSocketsCloudTransport(connect_factory=factory)

    with pytest.raises(CloudTransportSecurityError):
        await transport.connect(
            "ws://cloud.example.test/connector",
            token="opaque-secret",
        )

    assert factory.calls == []


@pytest.mark.asyncio
async def test_invalid_token_is_rejected_before_network_io() -> None:
    factory = _ConnectFactory(_WebSocket())
    transport = WebSocketsCloudTransport(connect_factory=factory)

    with pytest.raises(CloudTransportSecurityError):
        await transport.connect(
            "wss://cloud.example.test/connector",
            token=123,  # type: ignore[arg-type]
        )

    assert factory.calls == []


@pytest.mark.asyncio
async def test_loopback_plaintext_is_allowed_for_local_integration_only() -> None:
    factory = _ConnectFactory(_WebSocket())
    transport = WebSocketsCloudTransport(connect_factory=factory)

    await transport.connect(
        "ws://127.0.0.1:8765/connector",
        token="local-test-token",
    )

    assert len(factory.calls) == 1


@pytest.mark.asyncio
async def test_subprotocol_mismatch_fails_closed() -> None:
    websocket = _WebSocket(subprotocol=None)
    transport = WebSocketsCloudTransport(connect_factory=_ConnectFactory(websocket))

    with pytest.raises(CloudSubprotocolError):
        await transport.connect(
            "wss://cloud.example.test/connector",
            token="opaque-secret",
        )

    assert websocket.closed == [(1002, "subprotocol_required")]


@pytest.mark.asyncio
@pytest.mark.parametrize("insecure_mode", ("no_hostname", "no_peer_verification"))
async def test_custom_tls_context_must_verify_hostname_and_peer(
    insecure_mode: str,
) -> None:
    context = ssl.create_default_context()
    if insecure_mode == "no_hostname":
        context.check_hostname = False
    else:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    factory = _ConnectFactory(_WebSocket())
    transport = WebSocketsCloudTransport(
        connect_factory=factory,
        ssl_context=context,
    )

    with pytest.raises(
        CloudTransportSecurityError,
        match="custom TLS context must verify peer and hostname",
    ):
        await transport.connect(
            "wss://cloud.example.test/connector",
            token="opaque-secret",
        )

    assert factory.calls == []


@pytest.mark.asyncio
async def test_adapter_enforces_frame_limit_on_both_directions() -> None:
    websocket = _WebSocket(inbound="界" * 87_382)
    transport = WebSocketsCloudTransport(connect_factory=_ConnectFactory(websocket))
    connection = await transport.connect(
        "wss://cloud.example.test/connector",
        token="opaque-secret",
    )

    with pytest.raises(CloudFrameLimitExceeded):
        await connection.send(("界" * 87_382).encode())
    with pytest.raises(TypeError):
        await connection.send("{}")  # type: ignore[arg-type]
    with pytest.raises(CloudFrameLimitExceeded):
        await connection.receive()


@pytest.mark.asyncio
async def test_adapter_uses_strict_utf8_at_the_websocket_boundary() -> None:
    websocket = _WebSocket(inbound="你好")
    transport = WebSocketsCloudTransport(connect_factory=_ConnectFactory(websocket))
    connection = await transport.connect(
        "wss://cloud.example.test/connector",
        token="opaque-secret",
    )

    await connection.send("再见".encode())

    assert websocket.sent == ["再见"]
    assert await connection.receive() == "你好".encode()


@pytest.mark.asyncio
async def test_adapter_rejects_invalid_utf8_and_binary_inbound_frames() -> None:
    websocket = _WebSocket(inbound=b"{}")
    transport = WebSocketsCloudTransport(connect_factory=_ConnectFactory(websocket))
    connection = await transport.connect(
        "wss://cloud.example.test/connector",
        token="opaque-secret",
    )

    with pytest.raises(UnicodeDecodeError):
        await connection.send(b"\xff")
    with pytest.raises(TypeError, match="text WebSocket frames"):
        await connection.receive()

    websocket.inbound = "\ud800"
    with pytest.raises(UnicodeEncodeError):
        await connection.receive()


@pytest.mark.asyncio
async def test_adapter_preserves_remote_close_code_and_reason() -> None:
    websocket = _WebSocket()
    websocket.inbound = ConnectionClosedError(
        Close(1008, "device_authorization_revoked"),
        Close(1008, "device_authorization_revoked"),
        True,
    )

    async def closed_recv(decode=None):
        raise websocket.inbound

    websocket.recv = closed_recv  # type: ignore[method-assign]
    transport = WebSocketsCloudTransport(connect_factory=_ConnectFactory(websocket))
    connection = await transport.connect(
        "wss://cloud.example.test/connector",
        token="opaque-secret",
    )

    with pytest.raises(CloudConnectionClosed) as raised:
        await connection.receive()

    assert raised.value.code == 1008
    assert raised.value.reason == "device_authorization_revoked"
