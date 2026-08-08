from __future__ import annotations

import threading
import uuid
from collections.abc import Callable
from typing import Any

from ...local_protocol.frame_codec import encode_frame, try_decode_frame
from ...local_protocol.observer_relay import ObserverEndpoint
from ...local_protocol.session_catalog_v1 import SESSION_CATALOG_METHODS
from .framed_pipe import WindowsFramedPipeConnection, WindowsFramedPipeServer
from .named_pipe_security import profile_pipe_name
from .runtime_authority import (
    WindowsRuntimeAuthorityV2,
    require_current_process_authority,
)

_OBSERVER_METHODS = frozenset(
    {
        "session.observe.subscribe",
        "session.observe.unsubscribe",
    }
)
_ALLOWED_METHODS = _OBSERVER_METHODS | SESSION_CATALOG_METHODS
_V2_SUBSCRIBE_FIELDS = frozenset({"observer_contract", "session_key", "profile"})
_V2_UNSUBSCRIBE_FIELDS = frozenset({"observer_contract", "subscription_id"})
_MAX_CONCURRENT_CONNECTIONS = 8
_ENDPOINTS_LOCK = threading.RLock()
_ENDPOINTS: dict[str, ObserverEndpoint] = {}


def _validated_observer_contract(value: object) -> int:
    if type(value) is not int or value not in {1, 2}:
        raise ValueError("observer_contract must be 1 or 2")
    return value


def _validated_runtime_generation(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or "\x00" in value
        or len(value) > 256
    ):
        raise ValueError("runtime_generation must be canonical text")
    return value


def _rpc_error(request_id: object, code: int, message: str) -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


class _ObserverPipeTransport:
    connection_role = "observer"

    def __init__(self, connection: WindowsFramedPipeConnection) -> None:
        self.transport_id = str(uuid.uuid4())
        self._connection = connection
        self._closed = False
        self._lock = threading.Lock()

    def write(self, frame: dict) -> bool:
        with self._lock:
            if self._closed:
                return False
        try:
            self._connection.send(encode_frame(frame))
            return True
        except (EOFError, OSError, RuntimeError, UnicodeError, ValueError):
            self.disconnect()
            return False

    def disconnect(self) -> None:
        with self._lock:
            self._closed = True

    def close(self) -> None:
        self.disconnect()


def _ready_frame(
    *,
    profile: str,
    runtime_generation: str,
    instance_id: str,
    observer_contract: int,
) -> dict[str, object]:
    if observer_contract == 2:
        payload: dict[str, object] = {
            "observer_contract": 2,
            "connection_role": "observer",
        }
    else:
        payload = {
            "local_gateway_protocol": 1,
            "observer_contract": 1,
            "connection_role": "observer",
            "profile": profile,
            "runtime_generation": runtime_generation,
            "instance_id": instance_id,
        }
    return {
        "jsonrpc": "2.0",
        "method": "event",
        "params": {
            "type": "gateway.ready",
            "payload": payload,
        },
    }


def _prepare_observer_request(
    request: dict,
    *,
    observer_contract: int,
    runtime_generation: str,
) -> tuple[dict | None, dict[str, object] | None]:
    request_id = request.get("id")
    method = request.get("method")
    if method not in _ALLOWED_METHODS:
        return None, _rpc_error(
            request_id,
            4003,
            "observer connection is read-only",
        )
    if observer_contract == 2 and method in _OBSERVER_METHODS:
        params = request.get("params")
        expected_fields = (
            _V2_SUBSCRIBE_FIELDS
            if method == "session.observe.subscribe"
            else _V2_UNSUBSCRIBE_FIELDS
        )
        if (
            set(request) != {"jsonrpc", "id", "method", "params"}
            or request.get("jsonrpc") != "2.0"
            or type(request_id) not in {str, int}
            or not isinstance(params, dict)
            or frozenset(params) != expected_fields
            or params.get("observer_contract") != 2
        ):
            return None, _rpc_error(request_id, -32602, "invalid params")
        prepared = dict(request)
        prepared["params"] = dict(params)
        if method == "session.observe.subscribe":
            prepared["params"]["runtime_generation"] = runtime_generation
        return prepared, None
    if method == "session.observe.subscribe":
        params = request.get("params")
        prepared = dict(request)
        prepared["params"] = {
            **(params if isinstance(params, dict) else {}),
            "relay_local_only": True,
        }
        return prepared, None
    return request, None


def _handle_observer_connection(
    connection: WindowsFramedPipeConnection,
    *,
    dispatch: Callable[[dict[str, Any], Any], dict[str, Any] | None],
    remove_observer_subscriptions: Callable[[Any], None],
    profile: str,
    runtime_generation: str,
    instance_id: str,
    observer_contract: int,
) -> None:
    transport = _ObserverPipeTransport(connection)
    transport.write(
        _ready_frame(
            profile=profile,
            runtime_generation=runtime_generation,
            instance_id=instance_id,
            observer_contract=observer_contract,
        )
    )
    try:
        while True:
            try:
                request = try_decode_frame(connection.recv())
            except (EOFError, OSError, PermissionError, UnicodeError, ValueError):
                return
            if request is None:
                if not transport.write(_rpc_error(None, -32700, "parse error")):
                    return
                continue
            prepared, rejection = _prepare_observer_request(
                request,
                observer_contract=observer_contract,
                runtime_generation=runtime_generation,
            )
            if rejection is not None:
                if not transport.write(rejection):
                    return
                continue
            if prepared is None:
                return
            method = prepared.get("method")
            try:
                response = dispatch(prepared, transport)
            except (TypeError, ValueError):
                if method not in SESSION_CATALOG_METHODS:
                    raise
                response = _rpc_error(prepared.get("id"), -32602, "invalid params")
            if response is not None and not transport.write(response):
                return
    finally:
        try:
            remove_observer_subscriptions(transport)
        finally:
            transport.disconnect()


class WindowsObserverEndpointRegistration:
    def __init__(
        self,
        *,
        endpoint: ObserverEndpoint,
        server: WindowsFramedPipeServer,
    ) -> None:
        self.endpoint = endpoint
        self._server = server
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


def start_observer_endpoint(
    *,
    authority: WindowsRuntimeAuthorityV2,
    dispatch: Callable[[dict[str, Any], Any], dict[str, Any] | None],
    remove_observer_subscriptions: Callable[[Any], None],
    observer_contract: int = 1,
    **_kwargs: object,
) -> WindowsObserverEndpointRegistration:
    require_current_process_authority(authority)
    if not callable(dispatch) or not callable(remove_observer_subscriptions):
        raise TypeError("Observer dispatch and cleanup must be callable")
    observer_contract = _validated_observer_contract(observer_contract)
    runtime_generation = _validated_runtime_generation(authority.runtime_generation)
    pipe_name = profile_pipe_name("observer", authority.profile)
    endpoint = ObserverEndpoint(
        pid=authority.pid,
        profile=authority.profile,
        runtime_generation=runtime_generation,
        socket_path=pipe_name,
        instance_id=authority.instance_id,
    )
    with _ENDPOINTS_LOCK:
        if authority.profile in _ENDPOINTS:
            raise RuntimeError("Windows observer endpoint already registered")
    server = WindowsFramedPipeServer(
        pipe_name,
        lambda connection: _handle_observer_connection(
            connection,
            dispatch=dispatch,
            remove_observer_subscriptions=remove_observer_subscriptions,
            profile=authority.profile,
            runtime_generation=runtime_generation,
            instance_id=authority.instance_id,
            observer_contract=observer_contract,
        ),
        io_timeout_seconds=None,
        max_instances=_MAX_CONCURRENT_CONNECTIONS,
    )
    try:
        server.start()
        with _ENDPOINTS_LOCK:
            _ENDPOINTS[authority.profile] = endpoint
        return WindowsObserverEndpointRegistration(endpoint=endpoint, server=server)
    except BaseException:
        server.close()
        raise


def list_observer_endpoints() -> list[ObserverEndpoint]:
    with _ENDPOINTS_LOCK:
        return list(_ENDPOINTS.values())


__all__ = [
    "WindowsObserverEndpointRegistration",
    "list_observer_endpoints",
    "start_observer_endpoint",
]
