"""Private same-user Windows Named Pipe relay for update-safety evidence."""

from __future__ import annotations

import ctypes
import json
import os
import threading
import time
from collections.abc import Callable, Mapping
from ctypes import wintypes
from typing import Any

_MAX_REQUEST_BYTES = 1_024
_MAX_RESPONSE_BYTES = 8_192
_IO_TIMEOUT_S = 1.0
_CLOSE_TIMEOUT_S = 3.0
_REQUEST = {"method": "update-safety.snapshot", "schema_version": 1}
_RESPONSE_FIELDS = frozenset(
    {
        "active_tasks",
        "evidence_complete",
        "pending_approvals",
        "pending_clarifications",
        "profile",
        "runtime_generation",
        "schema_version",
    }
)
_ERROR_RESPONSE = b'{"error":"unavailable","schema_version":1}\n'

_TOKEN_QUERY = 0x0008
_TOKEN_USER = 1
_PIPE_ACCESS_DUPLEX = 0x00000003
_FILE_FLAG_FIRST_PIPE_INSTANCE = 0x00080000
_PIPE_TYPE_BYTE = 0x00000000
_PIPE_READMODE_BYTE = 0x00000000
_PIPE_WAIT = 0x00000000
_PIPE_REJECT_REMOTE_CLIENTS = 0x00000008
_ERROR_PIPE_CONNECTED = 535
_ERROR_BROKEN_PIPE = 109
_ERROR_NO_DATA = 232
_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_OPEN_EXISTING = 3
_SDDL_REVISION_1 = 1
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
_LIBRARIES: tuple[Any, Any] | None = None


class _SidAndAttributes(ctypes.Structure):
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]


class _TokenUser(ctypes.Structure):
    _fields_ = [("User", _SidAndAttributes)]


class _SecurityAttributes(ctypes.Structure):
    _fields_ = [
        ("nLength", wintypes.DWORD),
        ("lpSecurityDescriptor", ctypes.c_void_p),
        ("bInheritHandle", wintypes.BOOL),
    ]


def _require_windows() -> None:
    if os.name != "nt":
        raise RuntimeError("Windows update-safety relay requires Windows")


def _configure_win32(kernel32: Any, advapi32: Any) -> None:
    handle_pointer = ctypes.POINTER(wintypes.HANDLE)
    dword_pointer = ctypes.POINTER(wintypes.DWORD)

    kernel32.GetCurrentProcess.argtypes = []
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.GetCurrentThread.argtypes = []
    kernel32.GetCurrentThread.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    kernel32.CreateNamedPipeW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(_SecurityAttributes),
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
    kernel32.GetNamedPipeClientProcessId.argtypes = [
        wintypes.HANDLE,
        dword_pointer,
    ]
    kernel32.GetNamedPipeClientProcessId.restype = wintypes.BOOL
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

    advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        handle_pointer,
    ]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.OpenThreadToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.BOOL,
        handle_pointer,
    ]
    advapi32.OpenThreadToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        dword_pointer,
    ]
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.ConvertSidToStringSidW.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.LPWSTR),
    ]
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        dword_pointer,
    ]
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    advapi32.ImpersonateNamedPipeClient.argtypes = [wintypes.HANDLE]
    advapi32.ImpersonateNamedPipeClient.restype = wintypes.BOOL
    advapi32.EqualSid.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    advapi32.EqualSid.restype = wintypes.BOOL
    advapi32.RevertToSelf.argtypes = []
    advapi32.RevertToSelf.restype = wintypes.BOOL


def _libraries() -> tuple[Any, Any]:
    global _LIBRARIES  # noqa: PLW0603
    _require_windows()
    if _LIBRARIES is None:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        _configure_win32(kernel32, advapi32)
        _LIBRARIES = kernel32, advapi32
    return _LIBRARIES


def _close_handle(kernel32: Any, handle: int | None) -> None:
    if handle not in (None, 0, _INVALID_HANDLE_VALUE):
        kernel32.CloseHandle(wintypes.HANDLE(handle))


def _token_user_buffer(advapi32: Any, token: int) -> ctypes.Array[ctypes.c_char]:
    required = wintypes.DWORD()
    advapi32.GetTokenInformation(
        wintypes.HANDLE(token),
        _TOKEN_USER,
        None,
        0,
        ctypes.byref(required),
    )
    if required.value == 0 or required.value > 64 * 1024:
        raise RuntimeError("Windows TokenUser size is invalid")
    buffer = ctypes.create_string_buffer(required.value)
    if not advapi32.GetTokenInformation(
        wintypes.HANDLE(token),
        _TOKEN_USER,
        buffer,
        required.value,
        ctypes.byref(required),
    ):
        raise OSError(ctypes.get_last_error(), "GetTokenInformation failed")
    return buffer


def _current_user_sid_string() -> str:
    kernel32, advapi32 = _libraries()
    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(
        kernel32.GetCurrentProcess(),
        _TOKEN_QUERY,
        ctypes.byref(token),
    ):
        raise OSError(ctypes.get_last_error(), "OpenProcessToken failed")
    try:
        buffer = _token_user_buffer(advapi32, token.value)
        user = ctypes.cast(buffer, ctypes.POINTER(_TokenUser)).contents
        if not user.User.Sid:
            raise RuntimeError("Windows TokenUser SID is null")
        string_sid = wintypes.LPWSTR()
        if not advapi32.ConvertSidToStringSidW(
            user.User.Sid,
            ctypes.byref(string_sid),
        ):
            raise OSError(ctypes.get_last_error(), "ConvertSidToStringSidW failed")
        try:
            value = string_sid.value
            if not value or len(value) > 256:
                raise RuntimeError("Windows SID string is invalid")
            return value
        finally:
            kernel32.LocalFree(ctypes.cast(string_sid, ctypes.c_void_p))
    finally:
        _close_handle(kernel32, token.value)


def resolve_update_safety_pipe_name(
    environment: Mapping[str, str] | None = None,
    *,
    user_sid: str | None = None,
) -> str:
    """Resolve the process-independent current-user update-safety pipe name."""

    source = os.environ if environment is None else environment
    configured = source.get("HERMES_UPDATE_SAFETY_PIPE")
    if configured is not None:
        value = configured
    else:
        sid = user_sid if user_sid is not None else _current_user_sid_string()
        value = rf"\\.\pipe\HermesUpdateSafety-{sid}"
    if (
        not value.startswith("\\\\.\\pipe\\")
        or len(value) > 240
        or "\x00" in value
        or value.endswith("\\")
    ):
        raise ValueError("Windows update-safety pipe name is invalid")
    return value


def _security_attributes() -> tuple[_SecurityAttributes, int]:
    kernel32, advapi32 = _libraries()
    sid = _current_user_sid_string()
    descriptor = ctypes.c_void_p()
    descriptor_size = wintypes.DWORD()
    if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        f"D:P(A;;GA;;;{sid})",
        _SDDL_REVISION_1,
        ctypes.byref(descriptor),
        ctypes.byref(descriptor_size),
    ):
        raise OSError(
            ctypes.get_last_error(),
            "ConvertStringSecurityDescriptorToSecurityDescriptorW failed",
        )
    if not descriptor.value or descriptor_size.value == 0:
        if descriptor.value:
            kernel32.LocalFree(descriptor)
        raise RuntimeError("Windows update-safety security descriptor is empty")
    return (
        _SecurityAttributes(
            nLength=ctypes.sizeof(_SecurityAttributes),
            lpSecurityDescriptor=descriptor.value,
            bInheritHandle=False,
        ),
        int(descriptor.value),
    )


def _same_user_client(pipe: int) -> bool:
    kernel32, advapi32 = _libraries()
    if not advapi32.ImpersonateNamedPipeClient(wintypes.HANDLE(pipe)):
        raise OSError(ctypes.get_last_error(), "ImpersonateNamedPipeClient failed")
    try:
        client_token = wintypes.HANDLE()
        if not advapi32.OpenThreadToken(
            kernel32.GetCurrentThread(),
            _TOKEN_QUERY,
            True,
            ctypes.byref(client_token),
        ):
            raise OSError(ctypes.get_last_error(), "OpenThreadToken failed")
        try:
            server_token = wintypes.HANDLE()
            if not advapi32.OpenProcessToken(
                kernel32.GetCurrentProcess(),
                _TOKEN_QUERY,
                ctypes.byref(server_token),
            ):
                raise OSError(ctypes.get_last_error(), "OpenProcessToken failed")
            try:
                client_buffer = _token_user_buffer(advapi32, client_token.value)
                server_buffer = _token_user_buffer(advapi32, server_token.value)
                client_user = ctypes.cast(
                    client_buffer,
                    ctypes.POINTER(_TokenUser),
                ).contents
                server_user = ctypes.cast(
                    server_buffer,
                    ctypes.POINTER(_TokenUser),
                ).contents
                if not client_user.User.Sid or not server_user.User.Sid:
                    raise RuntimeError("Windows Named Pipe TokenUser SID is null")
                return bool(
                    advapi32.EqualSid(client_user.User.Sid, server_user.User.Sid)
                )
            finally:
                _close_handle(kernel32, server_token.value)
        finally:
            _close_handle(kernel32, client_token.value)
    finally:
        if not advapi32.RevertToSelf():
            raise OSError(ctypes.get_last_error(), "RevertToSelf failed")


def _snapshot_payload(value: object) -> dict[str, object]:
    payload_method = getattr(value, "payload", None)
    if callable(payload_method):
        value = payload_method()
    if not isinstance(value, Mapping) or set(value) != _RESPONSE_FIELDS:
        raise RuntimeError("update-safety snapshot is malformed")
    payload = dict(value)
    if payload.get("schema_version") != 1 or payload.get("evidence_complete") is not True:
        raise RuntimeError("update-safety snapshot is incomplete")
    for field_name in (
        "active_tasks",
        "pending_approvals",
        "pending_clarifications",
    ):
        count = payload.get(field_name)
        if type(count) is not int or not 0 <= count <= 1_000_000:
            raise RuntimeError("update-safety snapshot count is invalid")
    for field_name, maximum in (("profile", 128), ("runtime_generation", 256)):
        text = payload.get(field_name)
        if (
            not isinstance(text, str)
            or not text
            or text != text.strip()
            or len(text) > maximum
            or any(ord(character) < 32 or ord(character) == 127 for character in text)
        ):
            raise RuntimeError("update-safety snapshot identity is invalid")
    return payload


def _encode_snapshot(value: object) -> bytes:
    encoded = (
        json.dumps(
            _snapshot_payload(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )
    if len(encoded) > _MAX_RESPONSE_BYTES:
        raise RuntimeError("update-safety response is oversized")
    return encoded


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


def _read_request(kernel32: Any, pipe: int) -> dict[str, Any]:
    deadline = time.monotonic() + _IO_TIMEOUT_S
    body = bytearray()
    while b"\n" not in body:
        if time.monotonic() >= deadline:
            raise TimeoutError("Windows update-safety request timed out")
        available = _peek_available(kernel32, pipe)
        if available < 0:
            raise ValueError("Windows update-safety request is incomplete")
        if available == 0:
            time.sleep(0.01)
            continue
        remaining = _MAX_REQUEST_BYTES + 1 - len(body)
        if remaining <= 0:
            raise ValueError("Windows update-safety request is oversized")
        chunk_size = min(available, remaining, 256)
        chunk = ctypes.create_string_buffer(chunk_size)
        read = wintypes.DWORD()
        if not kernel32.ReadFile(
            wintypes.HANDLE(pipe),
            chunk,
            chunk_size,
            ctypes.byref(read),
            None,
        ):
            raise OSError(ctypes.get_last_error(), "ReadFile failed")
        if read.value == 0:
            raise ValueError("Windows update-safety request is incomplete")
        body.extend(chunk.raw[: read.value])
        if len(body) > _MAX_REQUEST_BYTES:
            raise ValueError("Windows update-safety request is oversized")
    frame, separator, trailing = bytes(body).partition(b"\n")
    if separator != b"\n" or trailing:
        raise ValueError("Windows update-safety request framing is invalid")
    value = json.loads(frame.decode("utf-8"))
    if not isinstance(value, dict) or set(value) != set(_REQUEST):
        raise ValueError("Windows update-safety request schema is invalid")
    return value


def _write_all(kernel32: Any, pipe: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = wintypes.DWORD()
        chunk = ctypes.create_string_buffer(payload[offset:])
        if not kernel32.WriteFile(
            wintypes.HANDLE(pipe),
            chunk,
            len(payload) - offset,
            ctypes.byref(written),
            None,
        ):
            raise OSError(ctypes.get_last_error(), "WriteFile failed")
        if written.value == 0:
            raise RuntimeError("Windows update-safety zero-byte write")
        offset += written.value


class WindowsUpdateSafetyRelay:
    """Serve one aggregate-only update-safety method over a current-user pipe."""

    def __init__(
        self,
        snapshot_provider: Callable[[], object],
        *,
        pipe_name: str | None = None,
    ) -> None:
        if not callable(snapshot_provider):
            raise TypeError("snapshot_provider must be callable")
        self._snapshot_provider = snapshot_provider
        self._pipe_name = pipe_name or resolve_update_safety_pipe_name()
        resolve_update_safety_pipe_name(
            {"HERMES_UPDATE_SAFETY_PIPE": self._pipe_name},
            user_sid="validation-only",
        )
        self._handle: int | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.RLock()

    @property
    def endpoint(self) -> str:
        return self._pipe_name

    def start(self) -> WindowsUpdateSafetyRelay:
        kernel32, _advapi32 = _libraries()
        with self._lock:
            if self._handle is not None:
                return self
            attributes, descriptor = _security_attributes()
            try:
                handle = kernel32.CreateNamedPipeW(
                    self._pipe_name,
                    _PIPE_ACCESS_DUPLEX | _FILE_FLAG_FIRST_PIPE_INSTANCE,
                    _PIPE_TYPE_BYTE
                    | _PIPE_READMODE_BYTE
                    | _PIPE_WAIT
                    | _PIPE_REJECT_REMOTE_CLIENTS,
                    1,
                    _MAX_RESPONSE_BYTES,
                    _MAX_REQUEST_BYTES,
                    1_000,
                    ctypes.byref(attributes),
                )
            finally:
                kernel32.LocalFree(ctypes.c_void_p(descriptor))
            if handle in (None, 0, _INVALID_HANDLE_VALUE):
                raise OSError(ctypes.get_last_error(), "CreateNamedPipeW failed")
            self._handle = int(handle)
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._serve,
                name="hermes-windows-update-safety-relay",
                daemon=True,
            )
            self._thread.start()
            return self

    def close(self) -> None:
        with self._lock:
            handle = self._handle
            thread = self._thread
            if handle is None and thread is None:
                return
            self._stop.set()
        self._wake_listener()
        if thread is not None:
            thread.join(_CLOSE_TIMEOUT_S)
            if thread.is_alive():
                kernel32, _advapi32 = _libraries()
                _close_handle(kernel32, handle)
                thread.join(0.5)
                if thread.is_alive():
                    raise RuntimeError("Windows update-safety relay did not stop")
        with self._lock:
            kernel32, _advapi32 = _libraries()
            _close_handle(kernel32, self._handle)
            self._handle = None
            self._thread = None

    def _wake_listener(self) -> None:
        kernel32, _advapi32 = _libraries()
        handle = kernel32.CreateFileW(
            self._pipe_name,
            _GENERIC_READ | _GENERIC_WRITE,
            0,
            None,
            _OPEN_EXISTING,
            0,
            None,
        )
        if handle not in (None, 0, _INVALID_HANDLE_VALUE):
            _close_handle(kernel32, int(handle))

    def _serve(self) -> None:
        kernel32, _advapi32 = _libraries()
        while not self._stop.is_set():
            with self._lock:
                handle = self._handle
            if handle is None:
                return
            connected = kernel32.ConnectNamedPipe(wintypes.HANDLE(handle), None)
            if not connected:
                error = ctypes.get_last_error()
                if error != _ERROR_PIPE_CONNECTED:
                    if self._stop.is_set():
                        return
                    time.sleep(0.01)
                    continue
            if self._stop.is_set():
                kernel32.DisconnectNamedPipe(wintypes.HANDLE(handle))
                return
            try:
                self._serve_connection(kernel32, handle)
            finally:
                kernel32.DisconnectNamedPipe(wintypes.HANDLE(handle))

    def _serve_connection(self, kernel32: Any, pipe: int) -> None:
        try:
            client_pid = wintypes.DWORD()
            if not kernel32.GetNamedPipeClientProcessId(
                wintypes.HANDLE(pipe),
                ctypes.byref(client_pid),
            ) or client_pid.value == 0:
                raise RuntimeError("Windows update-safety client PID is unavailable")
            if not _same_user_client(pipe):
                return
            request = _read_request(kernel32, pipe)
            if request != _REQUEST:
                raise ValueError("unsupported Windows update-safety request")
            response = _encode_snapshot(self._snapshot_provider())
        except BaseException:  # noqa: BLE001
            response = _ERROR_RESPONSE
        try:
            _write_all(kernel32, pipe, response)
            kernel32.FlushFileBuffers(wintypes.HANDLE(pipe))
        except BaseException:  # noqa: BLE001
            return


def start_update_safety_relay(
    snapshot_provider: Callable[[], object],
    *,
    pipe_name: str | None = None,
) -> WindowsUpdateSafetyRelay:
    return WindowsUpdateSafetyRelay(
        snapshot_provider,
        pipe_name=pipe_name,
    ).start()


__all__ = [
    "WindowsUpdateSafetyRelay",
    "resolve_update_safety_pipe_name",
    "start_update_safety_relay",
]
