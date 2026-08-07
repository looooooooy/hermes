from __future__ import annotations

import ctypes
import hashlib
import os
import re
import time
from ctypes import wintypes

_TOKEN_QUERY = 0x0008
_TOKEN_USER = 1
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_OPEN_EXISTING = 3
_ERROR_FILE_NOT_FOUND = 2
_ERROR_PIPE_BUSY = 231
_ERROR_BROKEN_PIPE = 109
_ERROR_NO_DATA = 232
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
_PROFILE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_LIBRARIES: tuple[object, object] | None = None


class _SidAndAttributes(ctypes.Structure):
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]


class _TokenUser(ctypes.Structure):
    _fields_ = [("User", _SidAndAttributes)]


def _configure(kernel32: object, advapi32: object) -> None:
    handle_pointer = ctypes.POINTER(wintypes.HANDLE)
    dword_pointer = ctypes.POINTER(wintypes.DWORD)
    kernel32.GetCurrentProcess.argtypes = []
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
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
    kernel32.WaitNamedPipeW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
    kernel32.WaitNamedPipeW.restype = wintypes.BOOL
    kernel32.GetNamedPipeServerProcessId.argtypes = [
        wintypes.HANDLE,
        dword_pointer,
    ]
    kernel32.GetNamedPipeServerProcessId.restype = wintypes.BOOL
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
    advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        handle_pointer,
    ]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        dword_pointer,
    ]
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.EqualSid.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    advapi32.EqualSid.restype = wintypes.BOOL
    advapi32.ConvertSidToStringSidW.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.LPWSTR),
    ]
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p


def libraries() -> tuple[object, object]:
    global _LIBRARIES
    if os.name != "nt":
        raise RuntimeError("Windows Named Pipe client requires Windows")
    if _LIBRARIES is None:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        _configure(kernel32, advapi32)
        _LIBRARIES = kernel32, advapi32
    return _LIBRARIES


def close_handle(handle: int | None) -> None:
    if handle in (None, 0, _INVALID_HANDLE_VALUE):
        return
    kernel32, _ = libraries()
    kernel32.CloseHandle(wintypes.HANDLE(handle))


def _token_user_buffer(advapi32: object, token: int) -> ctypes.Array[ctypes.c_char]:
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


def _process_token(process: int) -> int:
    _kernel32, advapi32 = libraries()
    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(
        wintypes.HANDLE(process),
        _TOKEN_QUERY,
        ctypes.byref(token),
    ):
        raise OSError(ctypes.get_last_error(), "OpenProcessToken failed")
    return int(token.value)


def _same_token_user(left: int, right: int) -> bool:
    _kernel32, advapi32 = libraries()
    left_buffer = _token_user_buffer(advapi32, left)
    right_buffer = _token_user_buffer(advapi32, right)
    left_user = ctypes.cast(left_buffer, ctypes.POINTER(_TokenUser)).contents
    right_user = ctypes.cast(right_buffer, ctypes.POINTER(_TokenUser)).contents
    if not left_user.User.Sid or not right_user.User.Sid:
        raise RuntimeError("Windows TokenUser SID is null")
    return bool(advapi32.EqualSid(left_user.User.Sid, right_user.User.Sid))


def current_user_sid_string() -> str:
    kernel32, advapi32 = libraries()
    token = _process_token(int(kernel32.GetCurrentProcess()))
    try:
        buffer = _token_user_buffer(advapi32, token)
        user = ctypes.cast(buffer, ctypes.POINTER(_TokenUser)).contents
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
        close_handle(token)


def profile_pipe_name(role: str, profile: str, *, user_sid: str | None = None) -> str:
    if role not in {"discovery", "gateway", "observer", "control"}:
        raise ValueError("Windows Local Gateway pipe role is invalid")
    if not isinstance(profile, str) or _PROFILE.fullmatch(profile) is None:
        raise ValueError("profile is invalid")
    sid = user_sid or current_user_sid_string()
    digest = hashlib.sha256(f"{sid}\0{profile}".encode()).hexdigest()[:24]
    return rf"\\.\pipe\HermesLocal-{role}-{digest}"


class WindowsPipeConnection:
    def __init__(self, handle: int, server_pid: int) -> None:
        self._handle = handle
        self.server_pid = server_pid
        self._closed = False

    @property
    def handle(self) -> int:
        if self._closed:
            raise RuntimeError("Windows Named Pipe connection is closed")
        return self._handle

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        close_handle(self._handle)


def connect_same_user_pipe(name: str, *, timeout_seconds: float) -> WindowsPipeConnection:
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    kernel32, _advapi32 = libraries()
    deadline = time.monotonic() + timeout_seconds
    handle: int | None = None
    while time.monotonic() < deadline:
        raw = kernel32.CreateFileW(
            name,
            _GENERIC_READ | _GENERIC_WRITE,
            0,
            None,
            _OPEN_EXISTING,
            0,
            None,
        )
        if raw not in (None, 0, _INVALID_HANDLE_VALUE):
            handle = int(raw)
            break
        error = ctypes.get_last_error()
        if error not in {_ERROR_FILE_NOT_FOUND, _ERROR_PIPE_BUSY}:
            raise OSError(error, "CreateFileW failed")
        remaining_ms = max(1, int((deadline - time.monotonic()) * 1000))
        kernel32.WaitNamedPipeW(name, min(remaining_ms, 100))
        time.sleep(0.005)
    if handle is None:
        raise TimeoutError("Windows Named Pipe connect timed out")
    try:
        server_pid = wintypes.DWORD()
        if not kernel32.GetNamedPipeServerProcessId(
            wintypes.HANDLE(handle),
            ctypes.byref(server_pid),
        ) or server_pid.value <= 0:
            raise RuntimeError("Windows Named Pipe server PID is unavailable")
        server_process = kernel32.OpenProcess(
            _PROCESS_QUERY_LIMITED_INFORMATION,
            False,
            server_pid.value,
        )
        if server_process in (None, 0, _INVALID_HANDLE_VALUE):
            raise RuntimeError("Windows Named Pipe server process is unavailable")
        try:
            server_token = _process_token(int(server_process))
            local_token = _process_token(int(kernel32.GetCurrentProcess()))
            try:
                if not _same_token_user(server_token, local_token):
                    raise PermissionError("Windows Named Pipe server SID is untrusted")
            finally:
                close_handle(local_token)
                close_handle(server_token)
        finally:
            close_handle(int(server_process))
        return WindowsPipeConnection(handle, int(server_pid.value))
    except BaseException:
        close_handle(handle)
        raise


def _peek_available(handle: int) -> int:
    kernel32, _ = libraries()
    available = wintypes.DWORD()
    if not kernel32.PeekNamedPipe(
        wintypes.HANDLE(handle),
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


def read_exact(handle: int, size: int, *, deadline: float) -> bytes:
    kernel32, _ = libraries()
    result = bytearray()
    while len(result) < size:
        if time.monotonic() >= deadline:
            raise TimeoutError("Windows Named Pipe read timed out")
        available = _peek_available(handle)
        if available < 0:
            raise EOFError("Windows Named Pipe ended early")
        if available == 0:
            time.sleep(0.005)
            continue
        chunk_size = min(size - len(result), available, 4096)
        buffer = ctypes.create_string_buffer(chunk_size)
        read = wintypes.DWORD()
        if not kernel32.ReadFile(
            wintypes.HANDLE(handle),
            buffer,
            chunk_size,
            ctypes.byref(read),
            None,
        ):
            raise OSError(ctypes.get_last_error(), "ReadFile failed")
        if read.value == 0:
            raise EOFError("Windows Named Pipe ended early")
        result.extend(buffer.raw[: read.value])
    return bytes(result)


def read_line(handle: int, *, maximum: int, deadline: float) -> bytes:
    result = bytearray()
    while b"\n" not in result:
        if len(result) >= maximum or time.monotonic() >= deadline:
            raise ValueError("Windows Named Pipe line is invalid")
        available = _peek_available(handle)
        if available < 0:
            raise EOFError("Windows Named Pipe ended early")
        if available == 0:
            time.sleep(0.005)
            continue
        result.extend(read_exact(handle, min(available, 256), deadline=deadline))
    frame, separator, trailing = bytes(result).partition(b"\n")
    if separator != b"\n" or trailing:
        raise ValueError("Windows Named Pipe framing is invalid")
    return frame


def write_all(handle: int, payload: bytes) -> None:
    kernel32, _ = libraries()
    offset = 0
    while offset < len(payload):
        buffer = ctypes.create_string_buffer(payload[offset:])
        written = wintypes.DWORD()
        if not kernel32.WriteFile(
            wintypes.HANDLE(handle),
            buffer,
            len(payload) - offset,
            ctypes.byref(written),
            None,
        ):
            raise OSError(ctypes.get_last_error(), "WriteFile failed")
        if written.value == 0:
            raise RuntimeError("Windows Named Pipe write made no progress")
        offset += written.value


__all__ = [
    "WindowsPipeConnection",
    "connect_same_user_pipe",
    "current_user_sid_string",
    "profile_pipe_name",
    "read_exact",
    "read_line",
    "write_all",
]
