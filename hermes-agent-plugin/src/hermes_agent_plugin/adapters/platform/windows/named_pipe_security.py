from __future__ import annotations

import ctypes
import hashlib
import os
import re
from ctypes import wintypes
from typing import Any

_TOKEN_QUERY = 0x0008
_TOKEN_USER = 1
_SDDL_REVISION_1 = 1
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
_PROFILE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_LIBRARIES: tuple[Any, Any] | None = None


class SidAndAttributes(ctypes.Structure):
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]


class TokenUser(ctypes.Structure):
    _fields_ = [("User", SidAndAttributes)]


class SecurityAttributes(ctypes.Structure):
    _fields_ = [
        ("nLength", wintypes.DWORD),
        ("lpSecurityDescriptor", ctypes.c_void_p),
        ("bInheritHandle", wintypes.BOOL),
    ]


def _configure(kernel32: Any, advapi32: Any) -> None:
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


def libraries() -> tuple[Any, Any]:
    global _LIBRARIES  # noqa: PLW0603
    if os.name != "nt":
        raise RuntimeError("Windows Named Pipe security requires Windows")
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


def current_user_sid_string() -> str:
    kernel32, advapi32 = libraries()
    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(
        kernel32.GetCurrentProcess(),
        _TOKEN_QUERY,
        ctypes.byref(token),
    ):
        raise OSError(ctypes.get_last_error(), "OpenProcessToken failed")
    try:
        buffer = _token_user_buffer(advapi32, token.value)
        user = ctypes.cast(buffer, ctypes.POINTER(TokenUser)).contents
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
        close_handle(token.value)


def protected_security_attributes() -> tuple[SecurityAttributes, int]:
    kernel32, advapi32 = libraries()
    descriptor = ctypes.c_void_p()
    descriptor_size = wintypes.DWORD()
    sddl = f"D:P(A;;GA;;;{current_user_sid_string()})"
    if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        sddl,
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
        raise RuntimeError("Windows Named Pipe security descriptor is empty")
    return (
        SecurityAttributes(
            nLength=ctypes.sizeof(SecurityAttributes),
            lpSecurityDescriptor=descriptor.value,
            bInheritHandle=False,
        ),
        int(descriptor.value),
    )


def free_security_descriptor(descriptor: int) -> None:
    kernel32, _ = libraries()
    kernel32.LocalFree(ctypes.c_void_p(descriptor))


def same_user_client_after_read(pipe: int) -> bool:
    """Compare client and server TokenUser after at least one request read."""

    kernel32, advapi32 = libraries()
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
                client_user = ctypes.cast(client_buffer, ctypes.POINTER(TokenUser)).contents
                server_user = ctypes.cast(server_buffer, ctypes.POINTER(TokenUser)).contents
                if not client_user.User.Sid or not server_user.User.Sid:
                    raise RuntimeError("Windows Named Pipe TokenUser SID is null")
                return bool(advapi32.EqualSid(client_user.User.Sid, server_user.User.Sid))
            finally:
                close_handle(server_token.value)
        finally:
            close_handle(client_token.value)
    finally:
        if not advapi32.RevertToSelf():
            raise OSError(ctypes.get_last_error(), "RevertToSelf failed")


def profile_pipe_name(role: str, profile: str, *, user_sid: str | None = None) -> str:
    if role not in {"discovery", "gateway", "observer", "control"}:
        raise ValueError("Windows Local Gateway pipe role is invalid")
    if not isinstance(profile, str) or _PROFILE.fullmatch(profile) is None:
        raise ValueError("profile is invalid")
    sid = user_sid or current_user_sid_string()
    digest = hashlib.sha256(f"{sid}\0{profile}".encode("utf-8")).hexdigest()[:24]
    return rf"\\.\pipe\HermesLocal-{role}-{digest}"


__all__ = [
    "SecurityAttributes",
    "close_handle",
    "current_user_sid_string",
    "free_security_descriptor",
    "libraries",
    "profile_pipe_name",
    "protected_security_attributes",
    "same_user_client_after_read",
]
