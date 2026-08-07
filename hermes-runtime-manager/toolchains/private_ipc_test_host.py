#!/usr/bin/env python3
"""Private-Python interoperability host for Runtime Manager local IPC.

The script intentionally uses only the Python standard library. CI launches this file
with the qualified Hermes Private CPython while PATH is poisoned, so a green result
proves the Agent-side/Python-side process can consume the same authenticated local IPC
contract without system Python, uv, Git, Node, or developer tooling.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import socket
import struct
import sys
import time
from pathlib import Path
from typing import Any, Callable

MAX_FRAME_BYTES = 64 * 1024
CONNECT_ATTEMPTS = 100
CONNECT_INTERVAL_SECONDS = 0.1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--transport", choices=("unix", "windows-pipe"), required=True)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--toolchains-root", type=Path, required=True)
    parser.add_argument("--expected-platform", required=True)
    args = parser.parse_args()

    executable = Path(sys.executable).resolve()
    toolchains_root = args.toolchains_root.resolve()
    if toolchains_root not in executable.parents:
        raise SystemExit(
            f"private_ipc_host_error: interpreter escaped Hermes toolchain root: {executable}"
        )

    expected_platform = normalize_platform(args.expected_platform)
    if args.transport == "unix":
        request = lambda request_id, body: unix_request(
            args.endpoint, request_id, body
        )
    else:
        if os.name != "nt":
            raise SystemExit("private_ipc_host_error: windows-pipe requested off Windows")
        request = lambda request_id, body: windows_pipe_request(
            args.endpoint, request_id, body
        )

    status = request("private-python-status-1", {"method": "status"})
    if status.get("type") != "snapshot":
        raise SystemExit(f"private_ipc_host_error: expected snapshot, got {status!r}")
    snapshot = status.get("payload")
    if not isinstance(snapshot, dict):
        raise SystemExit("private_ipc_host_error: snapshot payload is not an object")
    if snapshot.get("schema_version") != 1:
        raise SystemExit("private_ipc_host_error: snapshot schema is not v1")
    if snapshot.get("platform") != expected_platform:
        raise SystemExit(
            "private_ipc_host_error: platform mismatch: "
            f"expected {expected_platform!r}, got {snapshot.get('platform')!r}"
        )

    control = request("private-python-control-1", {"method": "start"})
    if control.get("type") != "error":
        raise SystemExit(
            f"private_ipc_host_error: read-only transport accepted control: {control!r}"
        )
    control_payload = control.get("payload")
    if not isinstance(control_payload, dict) or control_payload.get("code") != "read_only_transport":
        raise SystemExit(
            f"private_ipc_host_error: unexpected control rejection: {control!r}"
        )

    print(
        json.dumps(
            {
                "schema_version": 1,
                "transport": args.transport,
                "endpoint": args.endpoint,
                "private_python": str(executable),
                "private_python_under_toolchain_root": True,
                "status_snapshot_received": True,
                "platform": expected_platform,
                "control_failed_closed": True,
                "verified": True,
            },
            sort_keys=True,
        )
    )
    return 0


def normalize_platform(value: str) -> str:
    lowered = value.strip().lower()
    aliases = {
        "linux": "linux",
        "ubuntu": "linux",
        "macos": "macos",
        "macos-latest": "macos",
        "darwin": "macos",
        "windows": "windows",
        "windows-latest": "windows",
    }
    try:
        return aliases[lowered]
    except KeyError as error:
        raise SystemExit(f"private_ipc_host_error: unsupported expected platform: {value}") from error


def unix_request(endpoint: str, request_id: str, body: dict[str, Any]) -> dict[str, Any]:
    last_error: OSError | None = None
    for _ in range(CONNECT_ATTEMPTS):
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            client.connect(endpoint)
            write_socket_frame(client, request_id, body)
            return read_socket_response(client, request_id)
        except OSError as error:
            last_error = error
            time.sleep(CONNECT_INTERVAL_SECONDS)
        finally:
            client.close()
    raise SystemExit(f"private_ipc_host_error: UDS connect/request failed: {last_error}")


def write_socket_frame(client: socket.socket, request_id: str, body: dict[str, Any]) -> None:
    client.sendall(encode_request(request_id, body))


def read_socket_response(client: socket.socket, request_id: str) -> dict[str, Any]:
    prefix = recv_exact(client.recv, 4)
    length = struct.unpack(">I", prefix)[0]
    if length > MAX_FRAME_BYTES:
        raise SystemExit("private_ipc_host_error: response frame exceeds IPC limit")
    payload = recv_exact(client.recv, length)
    return decode_response(payload, request_id)


def windows_pipe_request(endpoint: str, request_id: str, body: dict[str, Any]) -> dict[str, Any]:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    from ctypes import wintypes

    kernel32.WaitNamedPipeW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
    kernel32.WaitNamedPipeW.restype = wintypes.BOOL
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.ReadFile.argtypes = [
        wintypes.HANDLE,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPVOID,
    ]
    kernel32.ReadFile.restype = wintypes.BOOL
    kernel32.WriteFile.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPVOID,
    ]
    kernel32.WriteFile.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    generic_read = 0x80000000
    generic_write = 0x40000000
    open_existing = 3
    invalid_handle = ctypes.c_void_p(-1).value

    handle: Any | None = None
    last_error = 0
    for _ in range(CONNECT_ATTEMPTS):
        if not kernel32.WaitNamedPipeW(endpoint, 100):
            last_error = ctypes.get_last_error()
            time.sleep(CONNECT_INTERVAL_SECONDS)
            continue
        candidate = kernel32.CreateFileW(
            endpoint,
            generic_read | generic_write,
            0,
            None,
            open_existing,
            0,
            None,
        )
        candidate_value = candidate if isinstance(candidate, int) else candidate.value
        if candidate_value not in (None, invalid_handle):
            handle = candidate
            break
        last_error = ctypes.get_last_error()
        time.sleep(CONNECT_INTERVAL_SECONDS)

    if handle is None:
        raise SystemExit(
            f"private_ipc_host_error: Named Pipe connection failed with Win32 error {last_error}"
        )

    try:
        frame = encode_request(request_id, body)
        write_win32_all(kernel32, handle, frame)
        prefix = read_win32_exact(kernel32, handle, 4)
        length = struct.unpack(">I", prefix)[0]
        if length > MAX_FRAME_BYTES:
            raise SystemExit("private_ipc_host_error: response frame exceeds IPC limit")
        payload = read_win32_exact(kernel32, handle, length)
        return decode_response(payload, request_id)
    finally:
        kernel32.CloseHandle(handle)


def encode_request(request_id: str, body: dict[str, Any]) -> bytes:
    envelope = {
        "schema_version": 1,
        "request_id": request_id,
        "body": body,
    }
    payload = json.dumps(envelope, separators=(",", ":")).encode("utf-8")
    if len(payload) > MAX_FRAME_BYTES:
        raise SystemExit("private_ipc_host_error: request frame exceeds IPC limit")
    return struct.pack(">I", len(payload)) + payload


def decode_response(payload: bytes, request_id: str) -> dict[str, Any]:
    envelope = json.loads(payload.decode("utf-8"))
    if not isinstance(envelope, dict):
        raise SystemExit("private_ipc_host_error: response envelope is not an object")
    if envelope.get("schema_version") != 1:
        raise SystemExit("private_ipc_host_error: response schema is not v1")
    if envelope.get("request_id") != request_id:
        raise SystemExit("private_ipc_host_error: response request id mismatch")
    body = envelope.get("body")
    if not isinstance(body, dict):
        raise SystemExit("private_ipc_host_error: response body is not an object")
    return body


def recv_exact(reader: Callable[[int], bytes], length: int) -> bytes:
    chunks: list[bytes] = []
    remaining = length
    while remaining:
        chunk = reader(remaining)
        if not chunk:
            raise SystemExit("private_ipc_host_error: unexpected IPC EOF")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def write_win32_all(kernel32: Any, handle: Any, payload: bytes) -> None:
    from ctypes import wintypes

    offset = 0
    while offset < len(payload):
        chunk = payload[offset:]
        buffer = ctypes.create_string_buffer(chunk)
        written = wintypes.DWORD(0)
        if not kernel32.WriteFile(
            handle, ctypes.cast(buffer, ctypes.c_void_p), len(chunk), ctypes.byref(written), None
        ):
            raise SystemExit(
                f"private_ipc_host_error: WriteFile failed with Win32 error {ctypes.get_last_error()}"
            )
        if written.value == 0:
            raise SystemExit("private_ipc_host_error: zero-byte Named Pipe write")
        offset += written.value


def read_win32_exact(kernel32: Any, handle: Any, length: int) -> bytes:
    from ctypes import wintypes

    chunks: list[bytes] = []
    remaining = length
    while remaining:
        buffer = ctypes.create_string_buffer(remaining)
        read = wintypes.DWORD(0)
        if not kernel32.ReadFile(handle, buffer, remaining, ctypes.byref(read), None):
            raise SystemExit(
                f"private_ipc_host_error: ReadFile failed with Win32 error {ctypes.get_last_error()}"
            )
        if read.value == 0:
            raise SystemExit("private_ipc_host_error: unexpected Named Pipe EOF")
        chunks.append(buffer.raw[: read.value])
        remaining -= read.value
    return b"".join(chunks)


if __name__ == "__main__":
    raise SystemExit(main())
