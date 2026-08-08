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
_THREAD_TERMINATE = 0x0001
_ERROR_PIPE_CONNECTED = 535
_ERROR_BROKEN_PIPE = 109
_ERROR_NO_DATA = 232
_ERROR_NOT_FOUND = 1168
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
_CLOSE_TIMEOUT_S = 3.0
_DEFAULT_IO_TIMEOUT_S = 3.0
_MAX_SERVER_INSTANCES = 16
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
    kernel32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenThread.restype = wintypes.HANDLE
    kernel32.CancelSynchronousIo.argtypes = [wintypes.HANDLE]
    kernel32.CancelSynchronousIo.restype = wintypes.BOOL


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


def _read_exact(
    kernel32: Any,
    pipe: int,
    size: int,
    *,
    deadline: float | None,
) -> bytes:
    if size < 0 or size > MAX_FRAME_BYTES:
        raise ValueError("Windows framed Pipe read size is invalid")
    result = bytearray()
    while len(result) < size:
        if deadline is not None and time.monotonic() >= deadline:
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


def _cancel_thread_io(kernel32: Any, thread: threading.Thread) -> None:
    native_id = thread.native_id
    if native_id is None:
        return
    raw = kernel32.OpenThread(_THREAD_TERMINATE, False, native_id)
    if raw in (None, 0, _INVALID_HANDLE_VALUE):
        return
    handle = int(raw)
    try:
        if not kernel32.CancelSynchronousIo(wintypes.HANDLE(handle)):
            error = ctypes.get_last_error()
            if error != _ERROR_NOT_FOUND:
                return
    finally:
        close_handle(handle)


class WindowsFramedPipeConnection:
    """One authenticated same-user length-prefixed text session."""

    def __init__(
        self,
        pipe: int,
        *,
        io_timeout_seconds: float | None = _DEFAULT_IO_TIMEOUT_S,
    ) -> None:
        if io_timeout_seconds is not None and io_timeout_seconds <= 0:
            raise ValueError("io_timeout_seconds must be positive when bounded")
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
        deadline = (
            None
            if self._io_timeout_seconds is None
            else time.monotonic() + self._io_timeout_seconds
        )
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

    def close(self) -> None:
        self._closed = True


class WindowsFramedPipeServer:
    """Bounded same-user Pipe server with independent concurrent instances."""

    def __init__(
        self,
        pipe_name: str,
        handler: Callable[[WindowsFramedPipeConnection], None],
        *,
        io_timeout_seconds: float | None = _DEFAULT_IO_TIMEOUT_S,
        max_instances: int = 1,
    ) -> None:
        if not isinstance(pipe_name, str) or not pipe_name.startswith("\\\\.\\pipe\\"):
            raise ValueError("Windows framed Pipe name is invalid")
        if not callable(handler):
            raise TypeError("handler must be callable")
        if type(max_instances) is not int or not 1 <= max_instances <= _MAX_SERVER_INSTANCES:
            raise ValueError("Windows framed Pipe instance bound is invalid")
        if io_timeout_seconds is not None and io_timeout_seconds <= 0:
            raise ValueError("io_timeout_seconds must be positive when bounded")
        self._pipe_name = pipe_name
        self._handler = handler
        self._io_timeout_seconds = io_timeout_seconds
        self._max_instances = max_instances
        self._pipes: tuple[int, ...] = ()
        self._threads: tuple[threading.Thread, ...] = ()
        self._stop = threading.Event()
        self._state_lock = threading.Lock()

    @property
    def pipe_name(self) -> str:
        return self._pipe_name

    @property
    def max_instances(self) -> int:
        return self._max_instances

    def start(self) -> WindowsFramedPipeServer:
        with self._state_lock:
            if self._pipes:
                return self
            kernel32, _ = libraries()
            _configure_pipe_api(kernel32)
            pipes: list[int] = []
            try:
                for index in range(self._max_instances):
                    pipes.append(self._create_pipe(kernel32, first=index == 0))
            except BaseException:
                for pipe in pipes:
                    close_handle(pipe)
                raise
            self._stop.clear()
            threads = tuple(
                threading.Thread(
                    target=self._serve,
                    args=(pipe,),
                    name=f"hermes-windows-framed-pipe-{index + 1}",
                    daemon=True,
                )
                for index, pipe in enumerate(pipes)
            )
            self._pipes = tuple(pipes)
            self._threads = threads
            try:
                for thread in threads:
                    thread.start()
            except BaseException:
                self._stop.set()
                for thread in threads:
                    if thread.ident is not None:
                        _cancel_thread_io(kernel32, thread)
                for pipe in self._pipes:
                    close_handle(pipe)
                for thread in threads:
                    if thread.ident is not None:
                        thread.join(_CLOSE_TIMEOUT_S)
                self._pipes = ()
                self._threads = ()
                raise
        return self

    def close(self) -> None:
        with self._state_lock:
            pipes = self._pipes
            threads = self._threads
            self._pipes = ()
            self._threads = ()
            self._stop.set()
        kernel32, _ = libraries()
        _configure_pipe_api(kernel32)
        for thread in threads:
            _cancel_thread_io(kernel32, thread)
        for pipe in pipes:
            close_handle(pipe)
        deadline = time.monotonic() + _CLOSE_TIMEOUT_S
        for thread in threads:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            thread.join(remaining)
        if any(thread.is_alive() for thread in threads):
            raise RuntimeError("Windows framed Pipe server did not stop")

    def _create_pipe(self, kernel32: Any, *, first: bool) -> int:
        attributes, descriptor = protected_security_attributes()
        try:
            access = _PIPE_ACCESS_DUPLEX
            if first:
                access |= _FILE_FLAG_FIRST_PIPE_INSTANCE
            raw = kernel32.CreateNamedPipeW(
                self._pipe_name,
                access,
                _PIPE_TYPE_BYTE
                | _PIPE_READMODE_BYTE
                | _PIPE_WAIT
                | _PIPE_REJECT_REMOTE_CLIENTS,
                self._max_instances,
                _MAX_WIRE_FRAME_BYTES,
                _MAX_WIRE_FRAME_BYTES,
                1_000,
                ctypes.byref(attributes),
            )
        finally:
            free_security_descriptor(descriptor)
        if raw in (None, 0, _INVALID_HANDLE_VALUE):
            raise OSError(ctypes.get_last_error(), "CreateNamedPipeW failed")
        return int(raw)

    def _serve(self, pipe: int) -> None:
        kernel32, _ = libraries()
        _configure_pipe_api(kernel32)
        while not self._stop.is_set():
            connected = kernel32.ConnectNamedPipe(wintypes.HANDLE(pipe), None)
            if not connected and ctypes.get_last_error() != _ERROR_PIPE_CONNECTED:
                if self._stop.is_set():
                    return
                time.sleep(0.01)
                continue
            if self._stop.is_set():
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
                if self._stop.is_set():
                    return
                kernel32.DisconnectNamedPipe(wintypes.HANDLE(pipe))


__all__ = [
    "WindowsFramedPipeConnection",
    "WindowsFramedPipeServer",
]
