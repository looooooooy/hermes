from __future__ import annotations

import asyncio
import json
import struct
import threading
import time
from collections.abc import Mapping
from uuid import UUID

from hermes_connector.domain.local_gateway import LocalRuntimeAuthority

from .named_pipe import (
    WindowsPipeConnection,
    connect_same_user_pipe,
    profile_pipe_name,
    read_exact,
    write_all,
)
from .process_identity import current_process_identity, normalize_process_identity

_MAX_FRAME_BYTES = 1024 * 1024
_ATTACH_METHOD = "relay.control.attach"


class WindowsControlRelayClient:
    """Bind one current-user Control Pipe to one Local Runtime authority and session."""

    def __init__(
        self,
        authority: LocalRuntimeAuthority,
        *,
        user_id: str,
        provider: str,
        client_instance_id: UUID,
        session_key: str,
        connect_timeout_seconds: float = 1.5,
        io_timeout_seconds: float = 3.0,
    ) -> None:
        if not isinstance(client_instance_id, UUID):
            raise TypeError("client_instance_id must be UUID")
        for field_name, value in (
            ("user_id", user_id),
            ("provider", provider),
            ("session_key", session_key),
        ):
            if (
                not isinstance(value, str)
                or not value
                or value != value.strip()
                or "\x00" in value
            ):
                raise ValueError(f"{field_name} is invalid")
        if connect_timeout_seconds <= 0 or io_timeout_seconds <= 0:
            raise ValueError("Windows control relay timeouts must be positive")
        self._authority = authority
        self._user_id = user_id
        self._provider = provider
        self._client_instance_id = client_instance_id
        self._session_key = session_key
        self._connect_timeout_seconds = float(connect_timeout_seconds)
        self._io_timeout_seconds = float(io_timeout_seconds)
        self._connection: WindowsPipeConnection | None = None
        self._request_lock = threading.Lock()
        self._next_id = 0

    @property
    def is_open(self) -> bool:
        return self._connection is not None

    async def open(self) -> WindowsControlRelayClient:
        await asyncio.to_thread(self._open_sync)
        return self

    def _open_sync(self) -> None:
        if self._connection is not None:
            return
        pipe_name = profile_pipe_name("control", self._authority.profile)
        connection = connect_same_user_pipe(
            pipe_name,
            timeout_seconds=self._connect_timeout_seconds,
        )
        try:
            expected_identity = normalize_process_identity(
                self._authority.process_identity
            )
            observed_identity = normalize_process_identity(
                current_process_identity(connection.server_pid)
            )
            if expected_identity is None or observed_identity != expected_identity:
                raise PermissionError("Windows control relay process identity changed")
            self._connection = connection
            response = self._request_sync(
                _ATTACH_METHOD,
                {
                    "claims": {
                        "user_id": self._user_id,
                        "provider": self._provider,
                        "connection_role": "control",
                        "client_instance_id": str(self._client_instance_id),
                        "session_key": self._session_key,
                        "profile": self._authority.profile,
                    }
                },
                request_id="attach-1",
                timeout_seconds=self._io_timeout_seconds,
            )
            result = response.get("result")
            if (
                response.get("id") != "attach-1"
                or not isinstance(result, Mapping)
                or result.get("attached") is not True
                or result.get("connection_role") != "control"
            ):
                raise RuntimeError("Windows control relay attach was rejected")
        except BaseException:
            self._connection = None
            connection.close()
            raise

    async def request(
        self,
        method: str,
        params: Mapping[str, object],
        *,
        timeout_seconds: float | None = None,
    ) -> dict:
        if not isinstance(method, str) or not method or method != method.strip():
            raise ValueError("Windows control relay method is invalid")
        if not isinstance(params, Mapping):
            raise TypeError("Windows control relay params must be a mapping")
        timeout = self._io_timeout_seconds if timeout_seconds is None else timeout_seconds
        if not isinstance(timeout, int | float) or isinstance(timeout, bool) or timeout <= 0:
            raise ValueError("Windows control relay request timeout must be positive")
        return await asyncio.to_thread(
            self._request_sync,
            method,
            dict(params),
            timeout_seconds=float(timeout),
        )

    def _request_sync(
        self,
        method: str,
        params: Mapping[str, object],
        *,
        request_id: str | None = None,
        timeout_seconds: float | None = None,
    ) -> dict:
        connection = self._connection
        if connection is None:
            raise RuntimeError("Windows control relay is not open")
        timeout = self._io_timeout_seconds if timeout_seconds is None else timeout_seconds
        with self._request_lock:
            if request_id is None:
                self._next_id += 1
                request_id = f"control-{self._next_id}"
            self._send_frame(
                connection,
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": method,
                    "params": dict(params),
                },
            )
            response = self._recv_frame(connection, timeout_seconds=float(timeout))
            if response.get("id") != request_id:
                raise RuntimeError("Windows control relay response id is invalid")
            return response

    def _send_frame(self, connection: WindowsPipeConnection, frame: dict) -> None:
        encoded = json.dumps(
            frame,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        if not 1 <= len(encoded) <= _MAX_FRAME_BYTES:
            raise ValueError("Windows control relay request is oversized")
        write_all(connection.handle, struct.pack(">I", len(encoded)) + encoded)

    def _recv_frame(
        self,
        connection: WindowsPipeConnection,
        *,
        timeout_seconds: float,
    ) -> dict:
        deadline = time.monotonic() + timeout_seconds
        prefix = read_exact(connection.handle, 4, deadline=deadline)
        size = struct.unpack(">I", prefix)[0]
        if size == 0 or size > _MAX_FRAME_BYTES:
            raise ValueError("Windows control relay response length is invalid")
        value = json.loads(
            read_exact(connection.handle, size, deadline=deadline).decode("utf-8")
        )
        if not isinstance(value, dict) or value.get("jsonrpc") != "2.0":
            raise ValueError("Windows control relay response is invalid")
        return value

    async def close(self) -> None:
        connection = self._connection
        self._connection = None
        if connection is not None:
            connection.close()


__all__ = ["WindowsControlRelayClient"]
