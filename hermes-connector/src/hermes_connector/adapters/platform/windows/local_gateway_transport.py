from __future__ import annotations

import asyncio
import struct
import time

from hermes_connector.adapters.contract_codec import MAX_FRAME_BYTES, InvalidEnvelope
from hermes_connector.domain.local_gateway import AgentEndpoint
from hermes_connector.ports.local_gateway import LocalGatewayConnectionPort

from .named_pipe import (
    WindowsPipeConnection,
    connect_same_user_pipe,
    read_exact,
    write_all,
)
from .process_identity import current_process_identity


class WindowsNamedPipeGatewayConnection(LocalGatewayConnectionPort):
    def __init__(
        self,
        connection: WindowsPipeConnection,
        *,
        io_timeout_seconds: float,
    ) -> None:
        self._connection = connection
        self._io_timeout_seconds = io_timeout_seconds
        self._closed = False
        self._exchange_lock = asyncio.Lock()

    @property
    def peer_pid(self) -> int:
        return self._connection.server_pid

    async def exchange(self, frame: bytes) -> bytes:
        if self._closed:
            raise RuntimeError("Windows Local Gateway connection is closed")
        if not isinstance(frame, bytes) or not 1 <= len(frame) <= MAX_FRAME_BYTES:
            raise InvalidEnvelope("local gateway request frame is invalid")
        async with self._exchange_lock:
            return await asyncio.to_thread(self._exchange_sync, frame)

    def _exchange_sync(self, frame: bytes) -> bytes:
        deadline = time.monotonic() + self._io_timeout_seconds
        write_all(self._connection.handle, struct.pack(">I", len(frame)) + frame)
        prefix = read_exact(self._connection.handle, 4, deadline=deadline)
        size = struct.unpack(">I", prefix)[0]
        if size == 0 or size > MAX_FRAME_BYTES:
            raise InvalidEnvelope("local gateway response frame is invalid")
        return read_exact(self._connection.handle, size, deadline=deadline)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._connection.close()


class WindowsLocalGatewayTransport:
    """Connect to a discovered same-user Host Local Gateway Named Pipe."""

    def __init__(
        self,
        *,
        connect_timeout_seconds: float = 1.5,
        io_timeout_seconds: float = 2.0,
    ) -> None:
        if connect_timeout_seconds <= 0 or io_timeout_seconds <= 0:
            raise ValueError("Windows Local Gateway timeouts must be positive")
        self._connect_timeout_seconds = connect_timeout_seconds
        self._io_timeout_seconds = io_timeout_seconds

    async def connect(self, endpoint: AgentEndpoint) -> WindowsNamedPipeGatewayConnection:
        return await asyncio.to_thread(self._connect_sync, endpoint)

    def _connect_sync(self, endpoint: AgentEndpoint) -> WindowsNamedPipeGatewayConnection:
        connection = connect_same_user_pipe(
            str(endpoint.socket_path),
            timeout_seconds=self._connect_timeout_seconds,
        )
        try:
            self._require_connection_identity(connection, endpoint)
            return WindowsNamedPipeGatewayConnection(
                connection,
                io_timeout_seconds=self._io_timeout_seconds,
            )
        except BaseException:
            connection.close()
            raise

    def probe_peer(self, endpoint: AgentEndpoint, *, timeout_seconds: float) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        connection = connect_same_user_pipe(
            str(endpoint.socket_path),
            timeout_seconds=timeout_seconds,
        )
        try:
            self._require_connection_identity(connection, endpoint)
        finally:
            connection.close()

    @staticmethod
    def _require_connection_identity(
        connection: WindowsPipeConnection,
        endpoint: AgentEndpoint,
    ) -> None:
        if connection.server_pid != endpoint.pid:
            raise PermissionError("Windows Local Gateway server PID changed")
        if current_process_identity(endpoint.pid) != endpoint.process_identity:
            raise PermissionError("Windows Local Gateway process identity changed")


__all__ = [
    "WindowsLocalGatewayTransport",
    "WindowsNamedPipeGatewayConnection",
]
