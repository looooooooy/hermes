from __future__ import annotations

import threading
import uuid
from collections.abc import Callable, Mapping
from typing import Any

from ....ports.local_relay import OwnerActionDispatcherPort
from ...host.owner_actions import (
    DEFAULT_OWNER_ACTION_MAX_QUEUED,
    DEFAULT_OWNER_ACTION_MAX_WORKERS,
    BoundedOwnerActionDispatcher,
)
from ...local_protocol.control_relay import (
    _ALLOWED_METHODS,
    ControlEndpoint,
    _relay_overloaded,
    _rpc_error,
    _sanitize_claims,
)
from ...local_protocol.control_v1 import CONTROL_ERROR_CODES
from ...local_protocol.frame_codec import encode_frame, try_decode_frame
from .framed_pipe import WindowsFramedPipeConnection, WindowsFramedPipeServer
from .named_pipe_security import profile_pipe_name
from .runtime_authority import (
    WindowsRuntimeAuthorityV2,
    require_current_process_authority,
)

_ATTACH_METHOD = "relay.control.attach"
_OWNER_ACTION_MAX_WORKERS = DEFAULT_OWNER_ACTION_MAX_WORKERS
_OWNER_ACTION_MAX_QUEUED = DEFAULT_OWNER_ACTION_MAX_QUEUED
_ENDPOINTS_LOCK = threading.RLock()
_ENDPOINTS: dict[str, ControlEndpoint] = {}


class _ControlPipeTransport:
    connection_role = "control"

    def __init__(
        self,
        connection: WindowsFramedPipeConnection,
        claims: Mapping[str, str],
    ) -> None:
        self.auth_claims = claims
        self.transport_id = uuid.uuid4().hex
        self._connection = connection
        self._closed = False
        self._lock = threading.Lock()

    def write(self, frame: dict) -> None:
        encoded = encode_frame(frame)
        with self._lock:
            if self._closed:
                return
            self._connection.send(encoded)

    def disconnect(self) -> None:
        with self._lock:
            self._closed = True


def _dispatch_request(
    request: dict,
    transport: _ControlPipeTransport,
    dispatcher: Callable[[dict, Any], dict | None],
) -> None:
    try:
        params = request.get("params")
        local_request = dict(request)
        local_request["params"] = {
            **(params if isinstance(params, dict) else {}),
            "relay_local_only": True,
        }
        response = dispatcher(local_request, transport)
    except Exception:
        response = _rpc_error(request.get("id"), -32603, "internal error")
    if response is not None:
        transport.write(response)


def _await_rejected_peer_close(connection: WindowsFramedPipeConnection) -> None:
    """Give a rejected client a bounded window to read the error before disconnect."""

    try:
        connection.recv()
    except (
        EOFError,
        OSError,
        PermissionError,
        TimeoutError,
        UnicodeError,
        ValueError,
    ):
        pass


def _handle_control_connection(
    connection: WindowsFramedPipeConnection,
    *,
    dispatcher: Callable[[dict, Any], dict | None],
    transport_cleanup: Callable[[Any], None] | None,
    owner_action_dispatcher: OwnerActionDispatcherPort,
) -> None:
    transport: _ControlPipeTransport | None = None
    try:
        while True:
            try:
                request = try_decode_frame(connection.recv())
            except (EOFError, OSError, TimeoutError, UnicodeError, ValueError):
                return
            request_id = request.get("id") if isinstance(request, dict) else None
            if request is None:
                connection.send(encode_frame(_rpc_error(None, -32700, "parse error")))
                continue

            if transport is None:
                params = request.get("params")
                claims = _sanitize_claims(
                    params.get("claims") if isinstance(params, dict) else None
                )
                if request.get("method") != _ATTACH_METHOD or claims is None:
                    connection.send(
                        encode_frame(
                            _rpc_error(
                                request_id,
                                CONTROL_ERROR_CODES["control_role_required"],
                                "control_role_required",
                            )
                        )
                    )
                    _await_rejected_peer_close(connection)
                    return
                transport = _ControlPipeTransport(connection, claims)
                transport.write(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": {
                            "attached": True,
                            "connection_role": "control",
                        },
                    }
                )
                continue

            method = request.get("method")
            if method not in _ALLOWED_METHODS:
                transport.write(
                    _rpc_error(
                        request_id,
                        CONTROL_ERROR_CODES["method_not_allowed"],
                        "method_not_allowed",
                    )
                )
                continue
            if (
                owner_action_dispatcher.submit(
                    _dispatch_request,
                    request,
                    transport,
                    dispatcher,
                )
                is None
            ):
                transport.write(_relay_overloaded(request_id))
    finally:
        if transport is not None:
            transport.disconnect()
            if transport_cleanup is not None:
                try:
                    transport_cleanup(transport)
                except Exception:
                    pass


class WindowsControlEndpointRegistration:
    def __init__(
        self,
        *,
        endpoint: ControlEndpoint,
        server: WindowsFramedPipeServer,
        owner_action_dispatcher: OwnerActionDispatcherPort,
        owns_dispatcher: bool,
    ) -> None:
        self.endpoint = endpoint
        self._server = server
        self._owner_action_dispatcher = owner_action_dispatcher
        self._owns_dispatcher = owns_dispatcher
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._server.close()
        with _ENDPOINTS_LOCK:
            current = _ENDPOINTS.get(self.endpoint.profile)
            if current == self.endpoint:
                _ENDPOINTS.pop(self.endpoint.profile, None)
        if self._owns_dispatcher:
            self._owner_action_dispatcher.shutdown(
                wait=False,
                cancel_futures=True,
            )


def start_control_endpoint(
    *,
    authority: WindowsRuntimeAuthorityV2,
    dispatcher: Callable[[dict, Any], dict | None],
    transport_cleanup: Callable[[Any], None] | None = None,
    owner_action_dispatcher: OwnerActionDispatcherPort | None = None,
    **_kwargs: object,
) -> WindowsControlEndpointRegistration:
    require_current_process_authority(authority)
    if not callable(dispatcher):
        raise TypeError("dispatcher must be callable")
    pipe_name = profile_pipe_name("control", authority.profile)
    endpoint = ControlEndpoint(
        pid=authority.pid,
        profile=authority.profile,
        socket_path=pipe_name,
        instance_id=authority.instance_id,
    )
    with _ENDPOINTS_LOCK:
        if authority.profile in _ENDPOINTS:
            raise RuntimeError("Windows control endpoint already registered")
    owns_dispatcher = owner_action_dispatcher is None
    owner_dispatch = owner_action_dispatcher or BoundedOwnerActionDispatcher(
        max_workers=_OWNER_ACTION_MAX_WORKERS,
        max_queued=_OWNER_ACTION_MAX_QUEUED,
        thread_name_prefix="windows-control-owner-action",
    )
    server = WindowsFramedPipeServer(
        pipe_name,
        lambda connection: _handle_control_connection(
            connection,
            dispatcher=dispatcher,
            transport_cleanup=transport_cleanup,
            owner_action_dispatcher=owner_dispatch,
        ),
    )
    try:
        server.start()
        with _ENDPOINTS_LOCK:
            _ENDPOINTS[authority.profile] = endpoint
        return WindowsControlEndpointRegistration(
            endpoint=endpoint,
            server=server,
            owner_action_dispatcher=owner_dispatch,
            owns_dispatcher=owns_dispatcher,
        )
    except BaseException:
        server.close()
        if owns_dispatcher:
            owner_dispatch.shutdown(wait=False, cancel_futures=True)
        raise


def list_control_endpoints() -> list[ControlEndpoint]:
    with _ENDPOINTS_LOCK:
        return list(_ENDPOINTS.values())


__all__ = [
    "WindowsControlEndpointRegistration",
    "list_control_endpoints",
    "start_control_endpoint",
]
