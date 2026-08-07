from __future__ import annotations

import ctypes
import json
import os
from ctypes import wintypes

import pytest

from hermes_agent_plugin.adapters.platform.windows.update_safety_relay import (
    WindowsUpdateSafetyRelay,
    resolve_update_safety_pipe_name,
)

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows Named Pipes required")

_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_OPEN_EXISTING = 3
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


def _snapshot() -> dict[str, object]:
    return {
        "schema_version": 1,
        "profile": "default",
        "runtime_generation": "generation-42",
        "active_tasks": 0,
        "pending_approvals": 0,
        "pending_clarifications": 0,
        "evidence_complete": True,
    }


def _pipe_name(label: str) -> str:
    return rf"\\.\pipe\HermesUpdateSafetyTest-{os.getpid()}-{label}"


def _configure_client(kernel32: object) -> None:
    dword_pointer = ctypes.POINTER(wintypes.DWORD)
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
    kernel32.WriteFile.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        wintypes.DWORD,
        dword_pointer,
        ctypes.c_void_p,
    ]
    kernel32.WriteFile.restype = wintypes.BOOL
    kernel32.ReadFile.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        wintypes.DWORD,
        dword_pointer,
        ctypes.c_void_p,
    ]
    kernel32.ReadFile.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL


def _call(pipe_name: str, payload: object) -> dict[str, object]:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _configure_client(kernel32)
    handle = kernel32.CreateFileW(
        pipe_name,
        _GENERIC_READ | _GENERIC_WRITE,
        0,
        None,
        _OPEN_EXISTING,
        0,
        None,
    )
    if handle in (None, 0, _INVALID_HANDLE_VALUE):
        raise OSError(ctypes.get_last_error(), "CreateFileW failed")
    try:
        body = (
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
            + b"\n"
        )
        request = ctypes.create_string_buffer(body)
        written = wintypes.DWORD()
        if not kernel32.WriteFile(
            handle,
            ctypes.cast(request, ctypes.c_void_p),
            len(body),
            ctypes.byref(written),
            None,
        ) or written.value != len(body):
            raise OSError(ctypes.get_last_error(), "WriteFile failed")

        response = bytearray()
        while b"\n" not in response:
            buffer = ctypes.create_string_buffer(512)
            read = wintypes.DWORD()
            if not kernel32.ReadFile(
                handle,
                ctypes.cast(buffer, ctypes.c_void_p),
                len(buffer),
                ctypes.byref(read),
                None,
            ):
                raise OSError(ctypes.get_last_error(), "ReadFile failed")
            if read.value == 0:
                raise RuntimeError("Named Pipe response ended early")
            response.extend(buffer.raw[: read.value])
            if len(response) > 8_192:
                raise RuntimeError("Named Pipe response is oversized")
        frame, separator, trailing = bytes(response).partition(b"\n")
        assert separator == b"\n"
        assert trailing == b""
        return json.loads(frame.decode("utf-8"))
    finally:
        kernel32.CloseHandle(handle)


def test_resolves_sid_bound_default_and_strict_override() -> None:
    assert resolve_update_safety_pipe_name({}, user_sid="S-1-5-21-1") == (
        r"\\.\pipe\HermesUpdateSafety-S-1-5-21-1"
    )
    assert resolve_update_safety_pipe_name(
        {"HERMES_UPDATE_SAFETY_PIPE": r"\\.\pipe\CustomHermesSafety"},
        user_sid="ignored",
    ) == r"\\.\pipe\CustomHermesSafety"

    with pytest.raises(ValueError, match="invalid"):
        resolve_update_safety_pipe_name(
            {"HERMES_UPDATE_SAFETY_PIPE": r"C:\temp\host.pipe"},
            user_sid="ignored",
        )


def test_serves_only_the_aggregate_snapshot_contract() -> None:
    pipe_name = _pipe_name("snapshot")
    relay = WindowsUpdateSafetyRelay(_snapshot, pipe_name=pipe_name).start()
    try:
        assert _call(
            pipe_name,
            {"schema_version": 1, "method": "update-safety.snapshot"},
        ) == _snapshot()
    finally:
        relay.close()


def test_malformed_request_returns_body_free_error_without_provider_call() -> None:
    pipe_name = _pipe_name("malformed")
    called = False

    def provider() -> dict[str, object]:
        nonlocal called
        called = True
        return _snapshot()

    relay = WindowsUpdateSafetyRelay(provider, pipe_name=pipe_name).start()
    try:
        response = _call(
            pipe_name,
            {
                "schema_version": 1,
                "method": "update-safety.snapshot",
                "session_key": "must-not-cross-boundary",
            },
        )
    finally:
        relay.close()

    assert response == {"schema_version": 1, "error": "unavailable"}
    assert called is False


def test_provider_failure_never_leaks_exception_text() -> None:
    pipe_name = _pipe_name("provider-failure")

    def provider() -> object:
        raise RuntimeError("approval body secret")

    relay = WindowsUpdateSafetyRelay(provider, pipe_name=pipe_name).start()
    try:
        response = _call(
            pipe_name,
            {"schema_version": 1, "method": "update-safety.snapshot"},
        )
    finally:
        relay.close()

    assert response == {"schema_version": 1, "error": "unavailable"}
    assert "secret" not in repr(response)


def test_first_pipe_instance_prevents_live_endpoint_takeover() -> None:
    pipe_name = _pipe_name("takeover")
    first = WindowsUpdateSafetyRelay(_snapshot, pipe_name=pipe_name).start()
    try:
        with pytest.raises(OSError, match="CreateNamedPipeW"):
            WindowsUpdateSafetyRelay(_snapshot, pipe_name=pipe_name).start()
        assert _call(
            pipe_name,
            {"schema_version": 1, "method": "update-safety.snapshot"},
        ) == _snapshot()
    finally:
        first.close()
