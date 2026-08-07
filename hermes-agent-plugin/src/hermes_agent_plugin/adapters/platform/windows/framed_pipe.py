from __future__ import annotations

import ctypes
import struct
import threading
import time
from collections.abc import Callable
from ctypes import wintypes
from typing import Any

from ...local_protocol.frame_codec import MAX_FRAME_BYTES
from .named_pipe_security import (
    close_handle,
    free_security_descriptor,
    libraries,
    protected_security_attributes,
    same_user_client_after_read,
)

_PIPE_ACCESS_DUPLEX = 0x00000003
_FILE_FLAG_FIRST_PIPE_INSTANCE = 0x00080000
_PIPE_TYPE_BYTE = 0x00000000
_PIPE_READMODE_BYTE = 0x00000000
_PIPE_WAIT = 0x00000000
_PIPE_REJECT_REMOTE_CLIENTS = 0x00000008
_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_OPEN_EXISTING = 3
_ERROR_PIPE_CONNECTED = 535
_ERROR_BROKEN_PIPE = 109
_ERROR_NO_DATA = 232
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
_CLOSE_TIMEOUT_S = 3.0
_DEFAULT_IO_TIMEOUT_S = 3.0
_MAX_WIRE_FRAME_BYTES = MAX_FRAME_BYTES + 4


def _configure_pipe_api(kernel32: Any) -> None:
    dword_pointer = ctypes.POINTER(wintypes.DWORD)
    kernel32.CreateNamedPipeW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
    ]
    kernel32.CreateNamedPipeW.restype = wintypes.HANDLE
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.ConnectNamedPipe.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
    kernel32.ConnectNamedPipe.restype = wintypes.BOOL
    kernel32.DisconnectNamedPipe.argtypes = [wintypes.HANDLE]
    kernel32.DisconnectNamedPipe.restype = wintypes.BOOL
    kernel32.PeekNamedPipe.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        wintypes.DWORD,
        dword_pointer,
        dword_pointer,
        dword_pointer,
    ]
    kernel32.PeekNamedPipe.restype = wintypes.BOOL
    kernel32.ReadFile.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        wintypes.DWORD,
        dword_pointer,
        ctypes.c_void_p,
    ]
    kernel32.ReadFile.restype = wintypes.BOOL
    kernel32.WriteFile.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        wintypes.DWORD,
        dword_pointer,
        ctypes.c_void_p,
    ]
    kernel32.WriteFile.restype = wintypes.BOOL
    kernel32.FlushFileBuffers.argtypes = [wintypes.HANDLE]
    kernel32.FlushFileBuffers.restype = wintypes.BOOL


def _peek_available(kernel32: Any, pipe: int) -> int:
    available = wintypes.DWORD()
    if not kernel32.PeekNamedPipe(
        wintypes.HANDLE(pipe),
        None,
        0,
        None,
        ctypes.byref(available),
        None,
    ):
        error = ctypes.get_last_error()
        if error in {_ERROR_BROKEN_PIPE, _ERROR_NO_DATA}:
            return -1
        raise OSError(error, "PeekNamedPipe failed")
    return int(available.value)


def _read_exact(kernel32: Any, pipe: int, size: int, *, deadline: float) -> bytes:
    if size < 0 or size > MAX_FRAME_BYTES:
        raise ValueError("Windows framed Pipe read size is invalid")
    result = bytearray()
    while len(result) < size:
        if time.monotonic() >= deadline:
            raise TimeoutError("Windows framed Pipe read timed out")
        available = _peek_available(kernel32, pipe)
        if available < 0:
            raise EOFError("Windows framed Pipe ended early")
        if available == 0:
            time.sleep(0.005)
            continue
        chunk_size = min(size - len(result), available, 4096)
        buffer = ctypes.create_string_buffer(chunk_size)
        read = wintypes.DWORD()
        if not kernel32.ReadFile(
            wintypes.HANDLE(pipe),
            buffer,
            chunk_size,
            ctypes.byref(read),
            None,
        ):
            raise OSError(ctypes.get_last_error(), "ReadFile failed")
        if read.value == 0:
            raise EOFError("Windows framed Pipe ended early")
        result.extend(buffer.raw[: read.value])
    return bytes(result)


def _write_all(kernel32: Any, pipe: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        chunk = ctypes.create_string_buffer(payload[offset:])
        written = wintypes.DWORD()
        if not kernel32.WriteFile(
            wintypes.HANDLE(pipe),
            chunk,
            len(payload) - offset,
            ctypes.byref(written),
            None,
        ):
            raise OSError(ctypes.get_last_error(), "WriteFile failed")
        if written.value == 0:
            raise RuntimeError("Windows framed Pipe write made no progress")
        offset += written.value


class WindowsFramedPipeConnection:
    """One authenticated same-user length-prefixed text session."""

    def __init__(
        self,
        pipe: int,
        *,
        io_timeout_seconds: float = _DEFAULT_IO_TIMEOUT_S,
    ) -> None:
        if io_timeout_seconds <= 0:
            raise ValueError("io_timeout_seconds must be positive")
        self._pipe = pipe
        self._io_timeout_seconds = io_timeout_seconds
        self._authenticated = False
        self._closed = False
        self._write_lock = threading.Lock()

    def recv(self) -> str:
        if self._closed:
            raise EOFError("Windows framed Pipe connection is closed")
        kernel32, _ = libraries()
        _configure_pipe_api(kernel32)
        deadline = time.monotonic() + self._io_timeout_seconds
        prefix = _read_exact(kernel32, self._pipe, 4, deadline=deadline)
        size = struct.unpack(">I", prefix)[0]
        if size == 0 or size > MAX_FRAME_BYTES:
            raise ValueError("Windows framed Pipe frame length is invalid")
        body = _read_exact(kernel32, self._pipe, size, deadline=deadline)
        if not self._authenticated:
            if not same_user_client_after_read(self._pipe):
                raise PermissionError("Windows framed Pipe client SID is untrusted")
            self._authenticated = True
        return body.decode("utf-8", errors="strict")

    def send(self, frame: str) -> None:
        if self._closed:
            raise EOFError("Windows framed Pipe connection is closed")
        if not isinstance(frame, str):
            raise TypeError("Windows framed Pipe frame must be text")
        encoded = frame.encode("utf-8", errors="strict")
        if not 1 <= len(encoded) <= MAX_FRAME_BYTES:
            raise ValueError("Windows framed Pipe frame size is invalid")
        payload = struct.pack(">I", len(encoded)) + encoded
        with self._write_lock:
            if self._closed:
                raise EOFError("Windows framed Pipe connection is closed")
            kernel32, _ = libraries()
            _configure_pipe_api(kernel32)
            _write_all(kernel32, self._pipe, payload)
            if not kernel32.FlushFileBuffers(wintypes.HANDLE(self._pipe)):
                raise OSError(ctypes.get_last_error(), "FlushFileBuffers failed")

    def close(self) -> None:
        self._closed = True


class WindowsFramedPipeServer:
    """Single-instance same-user Pipe server with one session at a time."""

    def __init__(
        self,
        pipe_name: str,
        handler: Callable[[WindowsFramedPipeConnection], None],
        *,
        io_timeout_seconds: float = _DEFAULT_IO_TIMEOUT_S,
    ) -> None:
        if not isinstance(pipe_name, str) or not pipe_name.startswith("\\\\.\\pipe\\"):
            raise ValueError("Windows framed Pipe name is invalid")
        if not callable(handler):
            raise TypeError("handler must be callable")
        self._pipe_name = pipe_name
        self._handler = handler
        self._io_timeout_seconds = io_timeout_seconds
        self._pipe: int | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    @property
    def pipe_name(self) -> str:
        return self._pipe_name

    def start(self) -> WindowsFramedPipeServer:
        if self._pipe is not None:
            return self
        kernel32, _ = libraries()
        _configure_pipe_api(kernel32)
        attributes, descriptor = protected_security_attributes()
        try:
            raw = kernel32.CreateNamedPipeW(
                self._pipe_name,
                _PIPE_ACCESS_DUPLEX | _FILE_FLAG_FIRST_PIPE_INSTANCE,
                _PIPE_TYPE_BYTE
                | _PIPE_READMODE_BYTE
                | _PIPE_WAIT
                | _PIPE_REJECT_REMOTE_CLIENTS,
                1,
                _MAX_WIRE_FRAME_BYTES,
                _MAX_WIRE_FRAME_BYTES,
                1_000,
                ctypes.byref(attributes),
            )
        finally:
            free_security_descriptor(descriptor)
        if raw in (None, 0, _INVALID_HANDLE_VALUE):
            raise OSError(ctypes.get_last_error(), "CreateNamedPipeW failed")
        self._pipe = int(raw)
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._serve,
            name="hermes-windows-framed-pipe",
            daemon=True,
        )
        self._thread.start()
        return self

    def close(self) -> None:
        self._stop.set()
        self._wake()
        thread = self._thread
        if thread is not None:
            thread.join(_CLOSE_TIMEOUT_S)
        pipe = self._pipe
        self._pipe = None
        self._thread = None
        close_handle(pipe)
        if thread is not None and thread.is_alive():
            raise RuntimeError("Windows framed Pipe server did not stop")

    def _wake(self) -> None:
        if self._pipe is None:
            return
        kernel32, _ = libraries()
        _configure_pipe_api(kernel32)
        raw = kernel32.CreateFileW(
            self._pipe_name,
            _GENERIC_READ | _GENERIC_WRITE,
            0,
            None,
            _OPEN_EXISTING,
            0,
            None,
        )
        if raw not in (None, 0, _INVALID_HANDLE_VALUE):
            close_handle(int(raw))

    def _serve(self) -> None:
        kernel32, _ = libraries()
        _configure_pipe_api(kernel32)
        while not self._stop.is_set():
            pipe = self._pipe
            if pipe is None:
                return
            connected = kernel32.ConnectNamedPipe(wintypes.HANDLE(pipe), None)
            if not connected and ctypes.get_last_error() != _ERROR_PIPE_CONNECTED:
                if self._stop.is_set():
                    return
                time.sleep(0.01)
                continue
            if self._stop.is_set():
                kernel32.DisconnectNamedPipe(wintypes.HANDLE(pipe))
                return
            connection = WindowsFramedPipeConnection(
                pipe,
                io_timeout_seconds=self._io_timeout_seconds,
            )
            try:
                self._handler(connection)
            except BaseException:
                pass
            finally:
                connection.close()
                kernel32.DisconnectNamedPipe(wintypes.HANDLE(pipe))


__all__ = [
    "WindowsFramedPipeConnection",
    "WindowsFramedPipeServer",
]
