from __future__ import annotations

import asyncio
import os
import socket
import stat
import struct
from contextlib import suppress
from typing import Final

from hermes_connector.adapters.contract_codec import (
    FrameTooLarge,
    InvalidEnvelope,
)
from hermes_connector.adapters.platform.macos.process_identity import (
    ProcessIdentityProvider,
    current_process_identity,
    normalize_process_identity,
)
from hermes_connector.domain.local_gateway import AgentEndpoint
from hermes_connector.ports.local_gateway import LocalGatewayConnectionPort

MAX_LOCAL_BODY_BYTES: Final = 262_144
_PREFIX_BYTES: Final = 4
_SOL_LOCAL: Final = 0
_LOCAL_PEERPID: Final = 2


class MacOSLocalGatewayTransport:
    """Open one verified macOS UDS stream per Local Gateway exchange."""

    def __init__(
        self,
        *,
        process_identity_provider: ProcessIdentityProvider | None = None,
    ) -> None:
        self._process_identity_provider = (
            process_identity_provider or current_process_identity
        )

    async def connect(
        self,
        endpoint: AgentEndpoint,
    ) -> LocalGatewayConnectionPort:
        self.verify_endpoint(endpoint)
        reader, writer = await asyncio.open_unix_connection(
            path=str(endpoint.socket_path)
        )
        connection = MacOSLocalGatewayConnection(reader, writer)
        try:
            if _connected_peer_pid(writer) != endpoint.pid:
                raise InvalidEnvelope(
                    "local gateway peer does not match descriptor publisher"
                )
            self.verify_endpoint(endpoint)
        except BaseException:
            await connection.close()
            raise
        return connection

    def verify_endpoint(self, endpoint: AgentEndpoint) -> None:
        """Revalidate immutable descriptor evidence without sending a frame."""

        _validate_socket(endpoint)
        try:
            observed = self._process_identity_provider(endpoint.pid)
        except BaseException:  # noqa: BLE001 - process evidence boundary
            observed = None
        if normalize_process_identity(observed) != endpoint.process_identity:
            raise InvalidEnvelope("local gateway process identity changed")

    def probe_peer(self, endpoint: AgentEndpoint, *, timeout_seconds: float) -> None:
        """Connect only for kernel peer proof; never send a protocol frame."""

        self.verify_endpoint(endpoint)
        peer = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            peer.settimeout(timeout_seconds)
            peer.connect(str(endpoint.socket_path))
            try:
                peer_pid = peer.getsockopt(_SOL_LOCAL, _LOCAL_PEERPID)
            except OSError:
                raise InvalidEnvelope(
                    "local gateway peer identity is unavailable"
                ) from None
            if peer_pid != endpoint.pid:
                raise InvalidEnvelope(
                    "local gateway peer does not match descriptor publisher"
                )
            self.verify_endpoint(endpoint)
        finally:
            peer.close()


class MacOSLocalGatewayConnection:
    """Implement the frozen single-request length-prefixed transport.

    Connection state:

        OPEN -> EXCHANGING -> EXCHANGED -> CLOSED
          |         |                       ^
          +---------+-- error/cancel -------+

    A successful exchange is not reusable; the application closes it after the
    validated LocalWelcome has been consumed.
    """

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._exchange_started = False
        self._closed = False

    async def exchange(self, frame: bytes) -> bytes:
        if self._closed or self._exchange_started:
            raise InvalidEnvelope(
                "local gateway connection allows exactly one exchange"
            )
        self._exchange_started = True
        _validate_outbound_body(frame)

        try:
            self._writer.write(struct.pack(">I", len(frame)))
            self._writer.write(frame)
            await self._writer.drain()
            prefix = await _read_exactly(self._reader, _PREFIX_BYTES)
            body_length = struct.unpack(">I", prefix)[0]
            if body_length == 0:
                raise InvalidEnvelope("local gateway frame body cannot be empty")
            if body_length > MAX_LOCAL_BODY_BYTES:
                raise FrameTooLarge()
            return await _read_exactly(self._reader, body_length)
        except BaseException:
            await self.close()
            raise

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._writer.close()
        with suppress(
            ConnectionError,
            BrokenPipeError,
            OSError,
            asyncio.CancelledError,
        ):
            await self._writer.wait_closed()


async def _read_exactly(
    reader: asyncio.StreamReader,
    count: int,
) -> bytes:
    try:
        return await reader.readexactly(count)
    except asyncio.IncompleteReadError:
        raise InvalidEnvelope("local gateway frame is truncated") from None


def _validate_outbound_body(frame: bytes) -> None:
    if not isinstance(frame, bytes):
        raise InvalidEnvelope("local gateway frame body must be bytes")
    if not frame:
        raise InvalidEnvelope("local gateway frame body cannot be empty")
    if len(frame) > MAX_LOCAL_BODY_BYTES:
        raise FrameTooLarge()


def _validate_socket(endpoint: AgentEndpoint) -> None:
    path = endpoint.socket_path
    if not path.is_absolute() or "\x00" in str(path):
        raise InvalidEnvelope("local gateway socket path is invalid")
    try:
        metadata = path.lstat()
    except OSError:
        raise InvalidEnvelope("local gateway socket is unavailable") from None
    if (
        not stat.S_ISSOCK(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_uid != os.geteuid()
        or metadata.st_dev != endpoint.socket_device
        or metadata.st_ino != endpoint.socket_inode
    ):
        raise InvalidEnvelope("local gateway socket is not trusted")


def _connected_peer_pid(writer: asyncio.StreamWriter) -> int:
    connected_socket = writer.get_extra_info("socket")
    if connected_socket is None:
        raise InvalidEnvelope("local gateway peer identity is unavailable")
    try:
        peer_pid = connected_socket.getsockopt(_SOL_LOCAL, _LOCAL_PEERPID)
    except (AttributeError, OSError):
        raise InvalidEnvelope("local gateway peer identity is unavailable") from None
    if type(peer_pid) is not int or peer_pid <= 0:
        raise InvalidEnvelope("local gateway peer identity is invalid")
    return peer_pid
