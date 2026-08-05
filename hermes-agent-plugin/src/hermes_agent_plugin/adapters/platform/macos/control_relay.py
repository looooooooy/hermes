# ruff: noqa: BLE001, S110
"""macOS UDS relay backend for explicit-control RPCs."""

from __future__ import annotations

import logging
import os
import queue
import threading
import uuid
from collections.abc import Callable, Mapping
from functools import partial
from pathlib import Path
from types import MappingProxyType
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
from ...local_protocol.frame_codec import MAX_FRAME_BYTES, encode_frame
from ...local_protocol.frame_codec import (
    try_decode_frame as _decode_frame,
)
from . import local_gateway_paths
from .local_trust import (
    ensure_private_directory,
    is_private_directory,
    is_private_socket,
    read_private_registry,
    unlink_owned_socket,
    unlink_private_registry,
    unlink_private_socket,
    validate_profile,
)
from .relay_server_lifecycle import (
    shutdown_server,
    shutdown_server_and_join,
)
from .runtime_descriptor_v2 import (
    RUNTIME_DESCRIPTOR_VERSION,
    MacOSRuntimeAuthorityV2,
    ProcessIdentityProvider,
    RuntimeEndpointV2,
    current_process_identity,
    decode_runtime_descriptor_v2,
    normalize_process_identity,
    publish_runtime_descriptor_v2,
    require_current_process_authority,
    same_runtime_socket_identity,
)

try:
    from websockets.sync.client import unix_connect
    from websockets.sync.server import unix_serve
except ImportError:  # pragma: no cover - required installation path
    unix_connect = None  # type: ignore[assignment]
    unix_serve = None  # type: ignore[assignment]

_log = logging.getLogger(__name__)
_REGISTRY_VERSION = RUNTIME_DESCRIPTOR_VERSION
_CONNECT_TIMEOUT_S = 2.0
_RPC_TIMEOUT_S = 3.0
_OWNER_ACTION_MAX_WORKERS = DEFAULT_OWNER_ACTION_MAX_WORKERS
_OWNER_ACTION_MAX_QUEUED = DEFAULT_OWNER_ACTION_MAX_QUEUED
_MAX_PENDING_RPCS = 64
_ATTACH_METHOD = "relay.control.attach"
_ATTACH_ID = "relay-control-attach"
_SOL_LOCAL = 0
_LOCAL_PEERPID = 2


class ControlEndpointRegistration:
    def __init__(
        self,
        path: Path,
        socket_path: Path,
        instance_id: str,
        server: Any,
        thread: threading.Thread,
        owned_owner_action_dispatcher: OwnerActionDispatcherPort | None = None,
    ) -> None:
        self._path = path
        self._socket_path = socket_path
        self._instance_id = instance_id
        self._server = server
        self._thread = thread
        self._owned_owner_action_dispatcher = owned_owner_action_dispatcher
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        stopped, failure = shutdown_server_and_join(
            self._server,
            self._thread,
            attempts=1,
        )
        try:
            if self._owned_owner_action_dispatcher is not None:
                self._owned_owner_action_dispatcher.shutdown(
                    wait=False,
                    cancel_futures=True,
                )
        except BaseException as error:
            failure = failure or error
        if not stopped:
            raise failure or RuntimeError("control relay thread did not stop")
        try:
            payload = read_private_registry(
                self._path,
                directory=self._path.parent,
            )
            if payload is not None and payload.get("instance_id") == self._instance_id:
                unlink_private_registry(
                    self._path,
                    directory=self._path.parent,
                )
        except Exception as error:
            _log.debug(
                "control endpoint unregister failed error_type=%s",
                type(error).__name__,
            )
        unlink_private_socket(
            self._socket_path,
            directory=self._socket_path.parent,
        )
        self._closed = True
        if failure is not None:
            raise failure


def _directories(
    paths: local_gateway_paths.MacOSLocalGatewayPaths | None = None,
) -> tuple[Path, Path]:
    paths = paths or local_gateway_paths.load_local_gateway_paths()
    return paths.control_registry_directory, paths.control_socket_directory


def _registry_dir(
    paths: local_gateway_paths.MacOSLocalGatewayPaths | None = None,
) -> Path:
    return _directories(paths)[0]


def _socket_dir(
    paths: local_gateway_paths.MacOSLocalGatewayPaths | None = None,
) -> Path:
    return _directories(paths)[1]


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _connected_peer_pid(websocket: Any) -> int:
    connected_socket = getattr(websocket, "socket", None)
    if connected_socket is None:
        transport = getattr(websocket, "transport", None)
        if transport is not None:
            connected_socket = transport.get_extra_info("socket")
    try:
        peer_pid = connected_socket.getsockopt(_SOL_LOCAL, _LOCAL_PEERPID)
    except (AttributeError, OSError):
        raise ValueError("control peer identity is unavailable") from None
    if type(peer_pid) is not int or peer_pid <= 0:
        raise ValueError("control peer identity is invalid")
    return peer_pid


def _is_private_socket_path(
    value: Path,
    paths: local_gateway_paths.MacOSLocalGatewayPaths | None = None,
) -> bool:
    try:
        path = Path(value)
        socket_dir = _socket_dir(paths).resolve()
        return (
            path.is_absolute()
            and path.parent.resolve() == socket_dir
            and path.suffix == ".sock"
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return False


_BoundedExecutor = BoundedOwnerActionDispatcher


class _ControlSocketTransport:
    def __init__(self, websocket: Any, auth_claims: Mapping[str, str]) -> None:
        self._websocket = websocket
        self._lock = threading.Lock()
        self._closed = False
        self._auth_claims = MappingProxyType(dict(auth_claims))
        self._transport_id = str(uuid.uuid4())

    @property
    def auth_claims(self) -> Mapping[str, str]:
        return self._auth_claims

    @property
    def connection_role(self) -> str:
        return "control"

    @property
    def transport_id(self) -> str:
        return self._transport_id

    def write(self, frame: dict) -> bool:
        with self._lock:
            if self._closed:
                return False
            try:
                self._websocket.send(encode_frame(frame))
                return True
            except Exception:
                self._closed = True
                return False

    def disconnect(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                self._websocket.close()
            except Exception:
                pass

    def close(self) -> None:
        self.disconnect()


def _dispatch_request(
    request: dict,
    transport: _ControlSocketTransport,
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


def _handle_control_connection(
    websocket: Any,
    *,
    dispatcher: Callable[[dict, Any], dict | None],
    owner_action_dispatcher: OwnerActionDispatcherPort,
    transport_cleanup: Callable[[Any], None] | None = None,
) -> None:
    transport: _ControlSocketTransport | None = None
    try:
        for raw in websocket:
            request = _decode_frame(raw)
            request_id = request.get("id") if request is not None else None
            if request is None:
                websocket.send(encode_frame(_rpc_error(None, -32700, "parse error")))
                continue

            if transport is None:
                params = request.get("params")
                claims = _sanitize_claims(
                    params.get("claims") if isinstance(params, dict) else None
                )
                if request.get("method") != _ATTACH_METHOD or claims is None:
                    websocket.send(
                        encode_frame(
                            _rpc_error(
                                request_id,
                                CONTROL_ERROR_CODES["control_role_required"],
                                "control_role_required",
                            )
                        )
                    )
                    break
                transport = _ControlSocketTransport(websocket, claims)
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
    except Exception as error:
        _log.debug(
            "control socket connection closed error_type=%s",
            type(error).__name__,
        )
    finally:
        if transport is not None:
            transport.disconnect()
            if transport_cleanup is not None:
                try:
                    transport_cleanup(transport)
                except Exception as error:
                    _log.debug(
                        "control transport cleanup failed error_type=%s",
                        type(error).__name__,
                    )
        else:
            try:
                websocket.close()
            except Exception:
                pass


def start_control_endpoint(
    *,
    authority: MacOSRuntimeAuthorityV2,
    dispatcher: Callable[[dict, Any], dict | None],
    transport_cleanup: Callable[[Any], None] | None = None,
    owner_action_dispatcher: OwnerActionDispatcherPort | None = None,
    paths: local_gateway_paths.MacOSLocalGatewayPaths | None = None,
    process_identity_provider: ProcessIdentityProvider = current_process_identity,
) -> ControlEndpointRegistration:
    """Start a mode-0600 control socket and publish only non-secret metadata."""
    if unix_serve is None:
        raise RuntimeError("websockets Unix socket support is unavailable")

    require_current_process_authority(
        authority,
        process_identity_provider=process_identity_provider,
    )
    validate_profile(authority.profile)
    registry_directory, socket_directory = _directories(paths)
    registry = ensure_private_directory(registry_directory)
    socket_dir = ensure_private_directory(socket_directory)

    endpoint_pid = authority.pid
    instance_id = authority.instance_id
    target = registry / f"gateway-{endpoint_pid}-{instance_id}.json"
    socket_path = socket_dir / f"c-{endpoint_pid}-{instance_id[:8]}.sock"
    owned_owner_action_dispatcher = None
    relay_server = None
    relay_thread = None
    try:
        if owner_action_dispatcher is None:
            owned_owner_action_dispatcher = BoundedOwnerActionDispatcher(
                max_workers=_OWNER_ACTION_MAX_WORKERS,
                max_queued=_OWNER_ACTION_MAX_QUEUED,
                thread_name_prefix="control-owner-action",
            )
            owner_action_dispatcher = owned_owner_action_dispatcher
        relay_server = unix_serve(
            partial(
                _handle_control_connection,
                dispatcher=dispatcher,
                owner_action_dispatcher=owner_action_dispatcher,
                transport_cleanup=transport_cleanup,
            ),
            path=str(socket_path),
            max_size=MAX_FRAME_BYTES,
        )
        os.chmod(socket_path, 0o600)
        if not is_private_socket(socket_path, directory=socket_dir):
            raise RuntimeError("untrusted local relay socket")
        relay_thread = threading.Thread(
            target=relay_server.serve_forever,
            name=f"control-socket-{endpoint_pid}",
            daemon=True,
        )
        publish_runtime_descriptor_v2(
            authority=authority,
            socket_path=socket_path,
            target=target,
            registry_directory=registry,
        )
        relay_thread.start()
    except BaseException:
        if relay_server is not None:
            if relay_thread is None:
                shutdown_error = shutdown_server(relay_server)
                if shutdown_error is not None:
                    _log.debug("control endpoint shutdown after start failure failed")
            else:
                stopped, shutdown_error = shutdown_server_and_join(
                    relay_server,
                    relay_thread,
                    attempts=2,
                )
                if shutdown_error is not None or not stopped:
                    _log.debug(
                        "control endpoint thread cleanup after start failure failed"
                    )
        unlink_owned_socket(socket_path, directory=socket_dir)
        try:
            payload = read_private_registry(target, directory=registry)
            if payload is not None and payload.get("instance_id") == instance_id:
                unlink_private_registry(target, directory=registry)
        except BaseException:
            _log.debug("control endpoint registry cleanup after start failure failed")
        if owned_owner_action_dispatcher is not None:
            try:
                owned_owner_action_dispatcher.shutdown(
                    wait=False,
                    cancel_futures=True,
                )
            except BaseException:
                _log.debug("control owner dispatcher start failure cleanup failed")
        raise
    return ControlEndpointRegistration(
        target,
        socket_path,
        instance_id,
        relay_server,
        relay_thread,
        owned_owner_action_dispatcher,
    )


def list_control_endpoints(
    *,
    paths: local_gateway_paths.MacOSLocalGatewayPaths | None = None,
    process_identity_provider: ProcessIdentityProvider = current_process_identity,
) -> list[ControlEndpoint | RuntimeEndpointV2]:
    registry, socket_dir = _directories(paths)
    if not is_private_directory(registry):
        return []
    endpoints: list[ControlEndpoint] = []
    for path in sorted(registry.glob("gateway-*.json")):
        try:
            payload = read_private_registry(path, directory=registry)
            if payload is None:
                continue
            endpoint = decode_runtime_descriptor_v2(
                payload,
                registry_path=path,
                socket_directory=socket_dir,
                process_identity_provider=process_identity_provider,
            )
            endpoints.append(endpoint)
        except Exception:
            _log.debug("ignoring invalid control endpoint registration")
    return endpoints


class _RelayConnection:
    def __init__(
        self,
        *,
        websocket: Any,
        downstream: Any,
        claims: Mapping[str, str],
        endpoint_instance_id: str,
        on_finished: Callable[[_RelayConnection, bool], None],
        max_pending_rpcs: int = _MAX_PENDING_RPCS,
    ) -> None:
        self.websocket = websocket
        self.downstream = downstream
        self.claims = dict(claims)
        self.endpoint_instance_id = endpoint_instance_id
        self._on_finished = on_finished
        self._send_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._pending: dict[str, queue.Queue[dict | None]] = {}
        self._max_pending_rpcs = max_pending_rpcs
        self._closed = threading.Event()
        self._intentional_close = False
        self._attach()
        self._reader = threading.Thread(
            target=self._read,
            name="control-relay-reader",
            daemon=True,
        )
        self._reader.start()

    def _attach(self) -> None:
        self.websocket.send(
            encode_frame(
                {
                    "jsonrpc": "2.0",
                    "id": _ATTACH_ID,
                    "method": _ATTACH_METHOD,
                    "params": {"claims": self.claims},
                }
            )
        )
        response = _decode_frame(self.websocket.recv(timeout=_RPC_TIMEOUT_S))
        result = response.get("result") if response is not None else None
        if not isinstance(result, dict) or result.get("connection_role") != "control":
            raise RuntimeError("control relay attach rejected")

    def call(self, request: dict) -> dict | None:
        if self._closed.is_set():
            return None
        request_id = request.get("id")
        response_queue: queue.Queue[dict | None] = queue.Queue(maxsize=1)
        with self._pending_lock:
            if self._closed.is_set():
                return None
            if len(self._pending) >= self._max_pending_rpcs:
                return _relay_overloaded(request_id)
            internal_id = str(uuid.uuid4())
            self._pending[internal_id] = response_queue
        forwarded = dict(request)
        downstream_id = request_id
        forwarded["id"] = internal_id
        try:
            with self._send_lock:
                self.websocket.send(encode_frame(forwarded))
            response = response_queue.get(timeout=_RPC_TIMEOUT_S)
        except Exception:
            response = None
        finally:
            with self._pending_lock:
                self._pending.pop(internal_id, None)
        if response is None:
            return None
        mapped = dict(response)
        mapped["id"] = downstream_id
        return mapped

    def close(self) -> None:
        self._intentional_close = True
        self._closed.set()
        self._fail_pending_requests()
        try:
            self.websocket.close()
        except Exception:
            pass
        if self._reader.is_alive() and threading.current_thread() is not self._reader:
            self._reader.join(timeout=1.0)

    def _read(self) -> None:
        unexpected = False
        try:
            while not self._closed.is_set():
                frame = _decode_frame(self.websocket.recv())
                if frame is None:
                    continue
                if frame.get("method") == "event":
                    try:
                        if not self.downstream.write(frame):
                            unexpected = not self._intentional_close
                            return
                    except Exception:
                        unexpected = not self._intentional_close
                        return
                    continue
                response_id = frame.get("id")
                with self._pending_lock:
                    target = (
                        self._pending.get(response_id)
                        if isinstance(response_id, str)
                        else None
                    )
                if target is not None:
                    try:
                        target.put_nowait(frame)
                    except queue.Full:
                        pass
        except Exception:
            unexpected = not self._intentional_close
        finally:
            self._closed.set()
            try:
                self.websocket.close()
            except Exception:
                pass
            self._fail_pending_requests()
            self._on_finished(self, unexpected)

    def _fail_pending_requests(self) -> None:
        with self._pending_lock:
            pending = list(self._pending.values())
            self._pending.clear()
        for target in pending:
            try:
                target.put_nowait(None)
            except queue.Full:
                pass


class ControlRelayHub:
    def __init__(
        self,
        *,
        current_pid: int | None = None,
        paths: local_gateway_paths.MacOSLocalGatewayPaths | None = None,
        connect: Callable[..., Any] | None = None,
        peer_pid_provider: Callable[[Any], int] | None = None,
        process_identity_provider: ProcessIdentityProvider = current_process_identity,
        socket_identity_provider: Callable[[RuntimeEndpointV2], bool] | None = None,
    ) -> None:
        self._current_pid = int(current_pid if current_pid is not None else os.getpid())
        self._paths = paths
        self._connect_transport = connect or unix_connect
        self._peer_pid_provider = peer_pid_provider or _connected_peer_pid
        self._process_identity_provider = process_identity_provider
        self._socket_identity_provider = (
            socket_identity_provider or same_runtime_socket_identity
        )
        self._lock = threading.RLock()
        self._connections: dict[Any, _RelayConnection] = {}

    def call(
        self,
        request: dict,
        *,
        transport: Any,
        auth_claims: Mapping[str, Any],
        profile: str | None,
    ) -> dict | None:
        request_id = request.get("id")
        if request.get("method") not in _ALLOWED_METHODS:
            return _rpc_error(
                request_id,
                CONTROL_ERROR_CODES["method_not_allowed"],
                "method_not_allowed",
            )
        claims = _sanitize_claims(auth_claims)
        if claims is None:
            return _rpc_error(
                request_id,
                CONTROL_ERROR_CODES["control_role_required"],
                "control_role_required",
            )
        requested_profile = claims["profile"]
        if profile is not None and profile != requested_profile:
            return _rpc_error(
                request_id,
                CONTROL_ERROR_CODES["session_binding_mismatch"],
                "session_binding_mismatch",
            )

        attempted_endpoints: set[str] = set()
        candidate_endpoints: tuple[ControlEndpoint, ...] | None = None
        while True:
            with self._lock:
                connection = self._connections.get(transport)
                if connection is None:
                    if candidate_endpoints is None:
                        candidate_endpoints = tuple(self._discover_endpoints())
                    connection = self._connect(
                        transport=transport,
                        claims=claims,
                        profile=requested_profile,
                        endpoints=candidate_endpoints,
                        excluded_instance_ids=attempted_endpoints,
                    )
                    if connection is None:
                        return None
                    self._connections[transport] = connection
            attempted_endpoints.add(connection.endpoint_instance_id)
            response = connection.call(request)
            error = response.get("error") if isinstance(response, dict) else None
            if (
                not isinstance(error, Mapping)
                or error.get("code") != CONTROL_ERROR_CODES["live_runtime_unavailable"]
            ):
                return response
            with self._lock:
                if self._connections.get(transport) is connection:
                    self._connections.pop(transport, None)
            connection.close()

    def _connect(
        self,
        *,
        transport: Any,
        claims: Mapping[str, str],
        profile: str,
        endpoints: tuple[ControlEndpoint, ...],
        excluded_instance_ids: set[str],
    ) -> _RelayConnection | None:
        if self._connect_transport is None:
            return None
        for endpoint in endpoints:
            if (
                endpoint.pid == self._current_pid
                or endpoint.profile != profile
                or endpoint.instance_id in excluded_instance_ids
                or not self._endpoint_evidence_valid(endpoint)
                or not (
                    _is_private_socket_path(endpoint.socket_path)
                    if self._paths is None
                    else _is_private_socket_path(endpoint.socket_path, self._paths)
                )
            ):
                continue
            websocket = None
            try:
                websocket = self._connect_transport(
                    str(endpoint.socket_path),
                    uri="ws://localhost/control",
                    open_timeout=_CONNECT_TIMEOUT_S,
                    close_timeout=1.0,
                    max_size=MAX_FRAME_BYTES,
                )
                if isinstance(endpoint, RuntimeEndpointV2):
                    if self._peer_pid_provider(websocket) != endpoint.pid:
                        raise RuntimeError("control relay peer identity mismatch")
                    if not self._endpoint_evidence_valid(endpoint):
                        raise RuntimeError("control relay endpoint evidence changed")
                return _RelayConnection(
                    websocket=websocket,
                    downstream=transport,
                    claims=claims,
                    endpoint_instance_id=endpoint.instance_id,
                    on_finished=self._reader_finished,
                )
            except Exception as exc:
                if websocket is not None:
                    try:
                        websocket.close()
                    except Exception:
                        pass
                _log.debug(
                    "control relay candidate failed pid=%s error_type=%s",
                    endpoint.pid,
                    type(exc).__name__,
                )
        return None

    def _discover_endpoints(self) -> list[ControlEndpoint | RuntimeEndpointV2]:
        try:
            if self._paths is None:
                return list_control_endpoints(
                    process_identity_provider=self._process_identity_provider
                )
            return list_control_endpoints(
                paths=self._paths,
                process_identity_provider=self._process_identity_provider,
            )
        except TypeError:
            return (
                list_control_endpoints()
                if self._paths is None
                else list_control_endpoints(paths=self._paths)
            )

    def _endpoint_evidence_valid(self, endpoint: ControlEndpoint) -> bool:
        if not isinstance(endpoint, RuntimeEndpointV2):
            return True
        try:
            observed = self._process_identity_provider(endpoint.pid)
        except BaseException:
            return False
        return self._socket_identity_provider(endpoint) and (
            normalize_process_identity(observed) == endpoint.process_identity
        )

    def close_transport(self, transport: Any) -> int:
        with self._lock:
            connection = self._connections.pop(transport, None)
        if connection is None:
            return 0
        connection.close()
        return 1

    def _reader_finished(self, connection: _RelayConnection, unexpected: bool) -> None:
        with self._lock:
            current = self._connections.get(connection.downstream)
            if current is connection:
                self._connections.pop(connection.downstream, None)
        if unexpected:
            disconnect = getattr(connection.downstream, "disconnect", None)
            if callable(disconnect):
                try:
                    disconnect()
                except Exception:
                    pass
