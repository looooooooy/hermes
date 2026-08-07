from __future__ import annotations

import ctypes
import json
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
    profile_pipe_name,
    protected_security_attributes,
    same_user_client_after_read,
)
from .runtime_authority import (
    WindowsRuntimeAuthorityV2,
    require_current_process_authority,
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
_MAX_DISCOVERY_REQUEST_BYTES = 1_024
_MAX_DISCOVERY_RESPONSE_BYTES = 16_384
_IO_TIMEOUT_S = 2.0
_CLOSE_TIMEOUT_S = 3.0

_DISCOVERY_REQUEST_FIELDS = frozenset({"schema_version", "method", "profile"})
_DESCRIPTOR_FIELDS = frozenset(
    {
        "version",
        "pid",
        "profile",
        "runtime_generation",
        "socket_path",
        "instance_id",
        "process_start_time_ns",
        "process_executable",
        "process_executable_device",
        "process_executable_inode",
        "host_bundle_id",
    }
)


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


def _read_exact(
    kernel32: Any,
    pipe: int,
    size: int,
    *,
    deadline: float,
) -> bytes:
    if size < 0 or size > MAX_FRAME_BYTES:
        raise ValueError("Windows Local Gateway frame size is invalid")
    result = bytearray()
    while len(result) < size:
        if time.monotonic() >= deadline:
            raise TimeoutError("Windows Local Gateway read timed out")
        available = _peek_available(kernel32, pipe)
        if available < 0:
            raise ValueError("Windows Local Gateway request ended early")
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
            raise ValueError("Windows Local Gateway request ended early")
        result.extend(buffer.raw[: read.value])
    return bytes(result)


def _read_line(
    kernel32: Any,
    pipe: int,
    *,
    maximum: int,
    deadline: float,
) -> bytes:
    result = bytearray()
    while b"\n" not in result:
        if len(result) >= maximum or time.monotonic() >= deadline:
            raise ValueError("Windows Local Gateway discovery request is invalid")
        available = _peek_available(kernel32, pipe)
        if available < 0:
            raise ValueError("Windows Local Gateway discovery request ended early")
        if available == 0:
            time.sleep(0.005)
            continue
        chunk_size = min(available, maximum + 1 - len(result), 256)
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
            raise ValueError("Windows Local Gateway discovery request ended early")
        result.extend(buffer.raw[: read.value])
    frame, separator, trailing = bytes(result).partition(b"\n")
    if separator != b"\n" or trailing:
        raise ValueError("Windows Local Gateway discovery framing is invalid")
    return frame


def _write_all(kernel32: Any, pipe: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        buffer = ctypes.create_string_buffer(payload[offset:])
        written = wintypes.DWORD()
        if not kernel32.WriteFile(
            wintypes.HANDLE(pipe),
            buffer,
            len(payload) - offset,
            ctypes.byref(written),
            None,
        ):
            raise OSError(ctypes.get_last_error(), "WriteFile failed")
        if written.value == 0:
            raise RuntimeError("Windows Local Gateway write made no progress")
        offset += written.value


def _create_pipe(name: str, *, inbound: int, outbound: int) -> int:
    kernel32, _ = libraries()
    _configure_pipe_api(kernel32)
    attributes, descriptor = protected_security_attributes()
    try:
        handle = kernel32.CreateNamedPipeW(
            name,
            _PIPE_ACCESS_DUPLEX | _FILE_FLAG_FIRST_PIPE_INSTANCE,
            _PIPE_TYPE_BYTE
            | _PIPE_READMODE_BYTE
            | _PIPE_WAIT
            | _PIPE_REJECT_REMOTE_CLIENTS,
            1,
            outbound,
            inbound,
            1_000,
            ctypes.byref(attributes),
        )
    finally:
        free_security_descriptor(descriptor)
    if handle in (None, 0, _INVALID_HANDLE_VALUE):
        raise OSError(ctypes.get_last_error(), "CreateNamedPipeW failed")
    return int(handle)


class _PipeServer:
    def __init__(
        self,
        name: str,
        handler: Callable[[Any, int], None],
        *,
        inbound: int,
        outbound: int,
    ) -> None:
        self._name = name
        self._handler = handler
        self._inbound = inbound
        self._outbound = outbound
        self._handle: int | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        if self._handle is not None:
            return
        self._handle = _create_pipe(
            self._name,
            inbound=self._inbound,
            outbound=self._outbound,
        )
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._serve,
            name=f"hermes-pipe-{self._name.rsplit('-', 1)[-1]}",
            daemon=True,
        )
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        self._wake()
        thread = self._thread
        if thread is not None:
            thread.join(_CLOSE_TIMEOUT_S)
        handle = self._handle
        self._handle = None
        self._thread = None
        close_handle(handle)
        if thread is not None and thread.is_alive():
            raise RuntimeError("Windows Local Gateway pipe thread did not stop")

    def _wake(self) -> None:
        kernel32, _ = libraries()
        _configure_pipe_api(kernel32)
        handle = kernel32.CreateFileW(
            self._name,
            _GENERIC_READ | _GENERIC_WRITE,
            0,
            None,
            _OPEN_EXISTING,
            0,
            None,
        )
        if handle not in (None, 0, _INVALID_HANDLE_VALUE):
            close_handle(int(handle))

    def _serve(self) -> None:
        kernel32, _ = libraries()
        _configure_pipe_api(kernel32)
        while not self._stop.is_set():
            handle = self._handle
            if handle is None:
                return
            connected = kernel32.ConnectNamedPipe(wintypes.HANDLE(handle), None)
            if not connected and ctypes.get_last_error() != _ERROR_PIPE_CONNECTED:
                if self._stop.is_set():
                    return
                time.sleep(0.01)
                continue
            if self._stop.is_set():
                kernel32.DisconnectNamedPipe(wintypes.HANDLE(handle))
                return
            try:
                self._handler(kernel32, handle)
            except BaseException:
                pass
            finally:
                kernel32.DisconnectNamedPipe(wintypes.HANDLE(handle))


class WindowsLocalGatewayResource:
    """Current-user discovery + LocalHello/Welcome Named Pipe pair."""

    def __init__(
        self,
        *,
        authority: WindowsRuntimeAuthorityV2,
        hello_handler: Callable[[object], str],
    ) -> None:
        require_current_process_authority(authority)
        if not callable(hello_handler):
            raise TypeError("hello_handler must be callable")
        self._authority = authority
        self._hello_handler = hello_handler
        self._gateway_name = profile_pipe_name("gateway", authority.profile)
        self._discovery_name = profile_pipe_name("discovery", authority.profile)
        self._gateway = _PipeServer(
            self._gateway_name,
            self._handle_gateway,
            inbound=MAX_FRAME_BYTES + 4,
            outbound=MAX_FRAME_BYTES + 4,
        )
        self._discovery = _PipeServer(
            self._discovery_name,
            self._handle_discovery,
            inbound=_MAX_DISCOVERY_REQUEST_BYTES,
            outbound=_MAX_DISCOVERY_RESPONSE_BYTES,
        )
        self._started = False

    @property
    def gateway_pipe_name(self) -> str:
        return self._gateway_name

    @property
    def discovery_pipe_name(self) -> str:
        return self._discovery_name

    def start(self, deadline: float) -> None:
        if self._started:
            return
        if deadline <= time.monotonic():
            raise TimeoutError("Windows Local Gateway start deadline exceeded")
        require_current_process_authority(self._authority)
        try:
            self._gateway.start()
            self._discovery.start()
        except BaseException:
            try:
                self._discovery.close()
            finally:
                self._gateway.close()
            raise
        self._started = True

    def stop(self, deadline: float) -> None:
        if not self._started:
            return
        if deadline <= time.monotonic():
            raise TimeoutError("Windows Local Gateway stop deadline exceeded")
        failure: BaseException | None = None
        for server in (self._discovery, self._gateway):
            try:
                server.close()
            except BaseException as error:
                failure = failure or error
        self._started = False
        if failure is not None:
            raise failure

    def _handle_discovery(self, kernel32: Any, pipe: int) -> None:
        request_raw = _read_line(
            kernel32,
            pipe,
            maximum=_MAX_DISCOVERY_REQUEST_BYTES,
            deadline=time.monotonic() + _IO_TIMEOUT_S,
        )
        if not same_user_client_after_read(pipe):
            return
        value = json.loads(request_raw.decode("utf-8"))
        if (
            not isinstance(value, dict)
            or set(value) != _DISCOVERY_REQUEST_FIELDS
            or value.get("schema_version") != 1
            or value.get("method") != "local-gateway.discover"
            or value.get("profile") != self._authority.profile
        ):
            return
        identity = self._authority.process_identity
        response = {
            "version": 2,
            "pid": self._authority.pid,
            "profile": self._authority.profile,
            "runtime_generation": self._authority.runtime_generation,
            "socket_path": self._gateway_name,
            "instance_id": self._authority.instance_id,
            "process_start_time_ns": identity.start_time_ns,
            "process_executable": str(identity.executable_path),
            "process_executable_device": identity.executable_device,
            "process_executable_inode": identity.executable_inode,
            "host_bundle_id": self._authority.host_bundle_id,
        }
        if set(response) != _DESCRIPTOR_FIELDS:
            raise RuntimeError("Windows Local Gateway descriptor is incomplete")
        encoded = (
            json.dumps(response, sort_keys=True, separators=(",", ":")).encode("utf-8")
            + b"\n"
        )
        if len(encoded) > _MAX_DISCOVERY_RESPONSE_BYTES:
            raise RuntimeError("Windows Local Gateway descriptor is oversized")
        _write_all(kernel32, pipe, encoded)
        kernel32.FlushFileBuffers(wintypes.HANDLE(pipe))

    def _handle_gateway(self, kernel32: Any, pipe: int) -> None:
        deadline = time.monotonic() + _IO_TIMEOUT_S
        prefix = _read_exact(kernel32, pipe, 4, deadline=deadline)
        size = struct.unpack(">I", prefix)[0]
        if size == 0 or size > MAX_FRAME_BYTES:
            return
        body = _read_exact(kernel32, pipe, size, deadline=deadline)
        if not same_user_client_after_read(pipe):
            return
        try:
            request = body.decode("utf-8")
            response = self._hello_handler(request)
            if not isinstance(response, str):
                raise TypeError("Local Gateway hello_handler returned non-text response")
            encoded = response.encode("utf-8")
        except (UnicodeError, TypeError, ValueError):
            return
        if not 1 <= len(encoded) <= MAX_FRAME_BYTES:
            return
        _write_all(kernel32, pipe, struct.pack(">I", len(encoded)) + encoded)
        kernel32.FlushFileBuffers(wintypes.HANDLE(pipe))


def create_local_gateway_resource(
    *,
    authority: WindowsRuntimeAuthorityV2,
    hello_handler: Callable[[object], str],
    **_kwargs: object,
) -> WindowsLocalGatewayResource:
    return WindowsLocalGatewayResource(
        authority=authority,
        hello_handler=hello_handler,
    )


__all__ = [
    "WindowsLocalGatewayResource",
    "create_local_gateway_resource",
]
