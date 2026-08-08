from __future__ import annotations

import asyncio
import json
import queue
import struct
import threading
import time
from collections.abc import Mapping
from typing import Final

from hermes_connector.domain.local_gateway import LocalRuntimeAuthority

from .named_pipe import (
    WindowsPipeConnection,
    connect_same_user_pipe,
    profile_pipe_name,
    read_exact,
    write_all,
)
from .process_identity import current_process_identity, normalize_process_identity

_MAX_FRAME_BYTES: Final = 262_144
_MAX_PENDING_FRAMES: Final = 64
_READER_JOIN_SECONDS: Final = 2.0
_STOP = object()


class WindowsDuplexPipeClosed(ConnectionError):
    pass


class WindowsDuplexPipeProtocolError(ValueError):
    pass


class WindowsAuthorityBoundDuplexPipe:
    """One SID/PID/process-bound duplex Pipe with exactly one reader thread."""

    def __init__(
        self,
        *,
        connection: WindowsPipeConnection,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        self._connection = connection
        self._loop = loop
        self._frames: asyncio.Queue[object] = asyncio.Queue(
            maxsize=_MAX_PENDING_FRAMES
        )
        self._closed = False
        self._state_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._reader_error: BaseException | None = None
        self._reader = threading.Thread(
            target=self._read_forever,
            name="hermes-windows-duplex-reader",
            daemon=True,
        )
        self._reader.start()

    @classmethod
    async def connect(
        cls,
        *,
        role: str,
        authority: LocalRuntimeAuthority,
        timeout_seconds: float,
    ) -> WindowsAuthorityBoundDuplexPipe:
        if role not in {"observer", "control"}:
            raise ValueError("Windows duplex Pipe role is invalid")
        if timeout_seconds <= 0:
            raise ValueError("Windows duplex Pipe timeout must be positive")
        name = profile_pipe_name(role, authority.profile)
        connection = await asyncio.to_thread(
            connect_same_user_pipe,
            name,
            timeout_seconds=timeout_seconds,
        )
        try:
            expected_identity = normalize_process_identity(authority.process_identity)
            observed_identity = normalize_process_identity(
                current_process_identity(connection.server_pid)
            )
            if expected_identity is None or observed_identity != expected_identity:
                raise PermissionError("Windows duplex Pipe process identity changed")
            return cls(
                connection=connection,
                loop=asyncio.get_running_loop(),
            )
        except BaseException:
            connection.close()
            raise

    @property
    def server_pid(self) -> int:
        return self._connection.server_pid

    @property
    def is_closed(self) -> bool:
        with self._state_lock:
            return self._closed

    async def send(self, frame: Mapping[str, object]) -> None:
        payload = _encode_frame(frame)
        try:
            await asyncio.to_thread(self._write_payload, payload)
        except asyncio.CancelledError:
            self._close_connection()
            raise
        except BaseException:
            self._close_connection()
            raise

    async def recv(self) -> Mapping[str, object]:
        item = await self._frames.get()
        if item is _STOP:
            error = self._reader_error
            if error is None:
                raise WindowsDuplexPipeClosed("Windows duplex Pipe closed")
            if isinstance(error, (ConnectionError, EOFError, OSError)):
                raise WindowsDuplexPipeClosed("Windows duplex Pipe closed") from error
            raise error
        if not isinstance(item, dict):
            raise WindowsDuplexPipeProtocolError("Windows duplex frame is invalid")
        return item

    async def close(self) -> None:
        self._close_connection()
        if self._reader.is_alive():
            await asyncio.to_thread(self._reader.join, _READER_JOIN_SECONDS)
        if self._reader.is_alive():
            raise RuntimeError("Windows duplex Pipe reader did not stop")

    def _write_payload(self, payload: bytes) -> None:
        with self._write_lock:
            if self.is_closed:
                raise WindowsDuplexPipeClosed("Windows duplex Pipe is closed")
            write_all(self._connection.handle, payload)

    def _read_forever(self) -> None:
        try:
            while not self.is_closed:
                frame = self._read_frame()
                self._loop.call_soon_threadsafe(self._deliver, frame)
        except BaseException as error:  # noqa: BLE001 - OS reader boundary
            self._reader_error = error
        finally:
            self._loop.call_soon_threadsafe(self._deliver_stop)

    def _read_frame(self) -> dict[str, object]:
        handle = self._connection.handle
        prefix = read_exact(handle, 4, deadline=float("inf"))
        size = struct.unpack(">I", prefix)[0]
        if not 1 <= size <= _MAX_FRAME_BYTES:
            raise WindowsDuplexPipeProtocolError(
                "Windows duplex frame length is invalid"
            )
        body = read_exact(handle, size, deadline=float("inf"))
        try:
            value = json.loads(
                body.decode("utf-8", errors="strict"),
                object_pairs_hook=_unique_object,
                parse_constant=_reject_non_json_number,
            )
        except (UnicodeError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise WindowsDuplexPipeProtocolError(
                "Windows duplex frame must be strict JSON"
            ) from error
        if not isinstance(value, dict):
            raise WindowsDuplexPipeProtocolError(
                "Windows duplex frame must be an object"
            )
        return value

    def _deliver(self, frame: Mapping[str, object]) -> None:
        if self.is_closed:
            return
        try:
            self._frames.put_nowait(dict(frame))
        except asyncio.QueueFull:
            self._reader_error = WindowsDuplexPipeProtocolError(
                "Windows duplex pending frame bound exceeded"
            )
            self._close_connection()

    def _deliver_stop(self) -> None:
        if self._frames.full():
            try:
                self._frames.get_nowait()
            except asyncio.QueueEmpty:
                pass
        try:
            self._frames.put_nowait(_STOP)
        except asyncio.QueueFull:
            pass

    def _close_connection(self) -> None:
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
        self._connection.close()


def _encode_frame(frame: Mapping[str, object]) -> bytes:
    try:
        encoded = json.dumps(
            dict(frame),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8", errors="strict")
    except (TypeError, ValueError, UnicodeError) as error:
        raise WindowsDuplexPipeProtocolError(
            "Windows duplex frame is not canonical JSON"
        ) from error
    if not 1 <= len(encoded) <= _MAX_FRAME_BYTES:
        raise WindowsDuplexPipeProtocolError("Windows duplex frame size is invalid")
    return struct.pack(">I", len(encoded)) + encoded


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise WindowsDuplexPipeProtocolError(
                "Windows duplex frame contains duplicate fields"
            )
        value[key] = item
    return value


def _reject_non_json_number(_value: str) -> None:
    raise WindowsDuplexPipeProtocolError(
        "Windows duplex frame contains a non-JSON number"
    )


__all__ = [
    "WindowsAuthorityBoundDuplexPipe",
    "WindowsDuplexPipeClosed",
    "WindowsDuplexPipeProtocolError",
]
