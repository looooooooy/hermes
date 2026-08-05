# ruff: noqa: BLE001, S110
"""macOS UDS relay backend for read-only live-session observers.

Each participating Hermes host process publishes a private, loopback-only
WebSocket endpoint in a per-user runtime registry.  When an external observer
process cannot resolve a session through its bounded host facade, it may
subscribe to another registered owner using the versioned
``session.observe.subscribe`` contract and forward already-sanitized frames.

The relay never resumes or activates a session and never rebinds its owner
transport.  The upstream connection's first successful RPC is the observer
subscribe itself, so the authoritative server marks that connection read-only.
"""

from __future__ import annotations

import logging
import os
import socket as socket_module
import threading
import uuid
from collections.abc import Callable
from functools import partial
from math import isfinite
from pathlib import Path
from queue import Full, Queue
from time import monotonic
from typing import Any

from ...local_protocol.frame_codec import MAX_FRAME_BYTES, encode_frame
from ...local_protocol.frame_codec import (
    try_decode_frame as _decode_frame,
)
from ...local_protocol.observer_relay import (
    ObserverEndpoint,
    ObserverRelayOwnershipError,
    ObserverRelayRpcError,
)
from ...local_protocol.observer_sequence import ObserverSequenceGuard
from ...local_protocol.session_catalog_v1 import SESSION_CATALOG_METHODS
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
except ImportError:  # pragma: no cover - websockets is a required install path
    unix_connect = None  # type: ignore[assignment]
    unix_serve = None  # type: ignore[assignment]

_log = logging.getLogger(__name__)
_REGISTRY_VERSION = RUNTIME_DESCRIPTOR_VERSION
_CONNECT_TIMEOUT_S = 2.0
_RPC_TIMEOUT_S = 3.0
_ACTIVATION_TIMEOUT_S = 3.0
_OBSERVER_SEND_TIMEOUT_S = 1.0
_OBSERVER_SEND_ABORT_GRACE_S = 0.1
_OBSERVER_SEND_WORKER_LIMIT = 8
_OBSERVER_SEND_QUEUE_LIMIT = 8
_OBSERVER_CLOSE_WORKER_LIMIT = 2
_OBSERVER_CLOSE_QUEUE_LIMIT = 16
_MAX_PENDING_FRAMES = 32
_MAX_POST_RESPONSE_QUEUE = 32
_SOL_LOCAL = 0
_LOCAL_PEERPID = 2
_READY_PAYLOAD_FIELDS = frozenset(
    {
        "local_gateway_protocol",
        "observer_contract",
        "connection_role",
        "profile",
        "runtime_generation",
        "instance_id",
    }
)
_V2_SUBSCRIBE_FIELDS = frozenset({"observer_contract", "session_key", "profile"})
_V2_UNSUBSCRIBE_FIELDS = frozenset({"observer_contract", "subscription_id"})


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


def _validated_observer_contract(value: object) -> int:
    if type(value) is not int or value not in {1, 2}:
        raise ValueError("observer_contract must be 1 or 2")
    return value


_RELAY_SUBSCRIBE_ID = "relay-subscribe"


class ObserverEndpointRegistration:
    def __init__(
        self,
        path: Path,
        socket_path: Path,
        instance_id: str,
        server: Any,
        thread: threading.Thread,
    ) -> None:
        self._path = path
        self._socket_path = socket_path
        self._instance_id = instance_id
        self._server = server
        self._thread = thread
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        stopped, failure = shutdown_server_and_join(
            self._server,
            self._thread,
            attempts=1,
        )
        if not stopped:
            raise failure or RuntimeError("observer relay thread did not stop")
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
                "observer endpoint unregister failed error_type=%s",
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
    return paths.observer_registry_directory, paths.observer_socket_directory


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
        raise ValueError("observer peer identity is unavailable") from None
    if type(peer_pid) is not int or peer_pid <= 0:
        raise ValueError("observer peer identity is invalid")
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


class _BoundedCall:
    def __init__(self, action: Callable[[], None]) -> None:
        self._action = action
        self._lock = threading.Lock()
        self._started = False
        self._cancelled = False
        self._succeeded = False
        self.done = threading.Event()
        self.finished = threading.Event()

    @property
    def succeeded(self) -> bool:
        with self._lock:
            return self._succeeded

    def cancel(self) -> None:
        with self._lock:
            self._cancelled = True
            if not self._started:
                self.finished.set()
            self.done.set()

    def run(self) -> None:
        with self._lock:
            if self._cancelled:
                self.done.set()
                self.finished.set()
                return
            self._started = True
        succeeded = False
        try:
            self._action()
            succeeded = True
        except BaseException:
            succeeded = False
        finally:
            with self._lock:
                self._succeeded = succeeded and not self._cancelled
            self.done.set()
            self.finished.set()


class _BoundedCallExecutor:
    """Global-capable bounded daemon executor for potentially blocking I/O calls."""

    def __init__(
        self,
        *,
        worker_limit: int,
        queue_limit: int,
        thread_name_prefix: str,
    ) -> None:
        if worker_limit < 1 or queue_limit < 1:
            raise ValueError("bounded call executor limits must be positive")
        self._worker_limit = worker_limit
        self._queue: Queue[_BoundedCall] = Queue(maxsize=queue_limit)
        self._thread_name_prefix = thread_name_prefix
        self._lock = threading.Lock()
        self._workers: list[threading.Thread] = []
        self._active_workers = 0

    @property
    def worker_count(self) -> int:
        with self._lock:
            return len(self._workers)

    def submit(self, action: Callable[[], None]) -> _BoundedCall | None:
        task = _BoundedCall(action)
        with self._lock:
            try:
                self._queue.put_nowait(task)
            except Full:
                return None
            idle_workers = len(self._workers) - self._active_workers
            if (
                self._queue.qsize() > idle_workers
                and len(self._workers) < self._worker_limit
            ):
                worker = threading.Thread(
                    target=self._work,
                    name=f"{self._thread_name_prefix}-{len(self._workers) + 1}",
                    daemon=True,
                )
                self._workers.append(worker)
                worker.start()
        return task

    def _work(self) -> None:
        while True:
            task = self._queue.get()
            with self._lock:
                self._active_workers += 1
            try:
                task.run()
            finally:
                with self._lock:
                    self._active_workers -= 1
                self._queue.task_done()


_OBSERVER_SEND_EXECUTOR = _BoundedCallExecutor(
    worker_limit=_OBSERVER_SEND_WORKER_LIMIT,
    queue_limit=_OBSERVER_SEND_QUEUE_LIMIT,
    thread_name_prefix="hermes-observer-send",
)
_OBSERVER_CLOSE_EXECUTOR = _BoundedCallExecutor(
    worker_limit=_OBSERVER_CLOSE_WORKER_LIMIT,
    queue_limit=_OBSERVER_CLOSE_QUEUE_LIMIT,
    thread_name_prefix="hermes-observer-close",
)


class _ObserverSocketTransport:
    def __init__(
        self,
        websocket: Any,
        *,
        send_timeout_s: float = _OBSERVER_SEND_TIMEOUT_S,
        send_abort_grace_s: float = _OBSERVER_SEND_ABORT_GRACE_S,
        send_executor: _BoundedCallExecutor = _OBSERVER_SEND_EXECUTOR,
    ) -> None:
        if (
            not isinstance(send_timeout_s, (int, float))
            or isinstance(send_timeout_s, bool)
            or send_timeout_s <= 0
            or not isfinite(send_timeout_s)
            or not isinstance(send_abort_grace_s, (int, float))
            or isinstance(send_abort_grace_s, bool)
            or send_abort_grace_s < 0
            or not isfinite(send_abort_grace_s)
        ):
            raise ValueError("observer send deadlines must be finite and positive")
        self._websocket = websocket
        self._send_timeout_s = float(send_timeout_s)
        self._send_abort_grace_s = float(send_abort_grace_s)
        self._send_executor = send_executor
        self._state_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._closed = False
        self._abort_started = False
        self._in_flight: _BoundedCall | None = None

    def write(self, frame: dict) -> bool:
        try:
            payload = encode_frame(frame)
        except Exception:
            self._abort(None)
            return False
        with self._write_lock:
            with self._state_lock:
                if self._closed:
                    return False
            task = self._send_executor.submit(lambda: self._websocket.send(payload))
            if task is None:
                self._abort(None)
                return False
            with self._state_lock:
                if self._closed:
                    task.cancel()
                    return False
                self._in_flight = task
            if not task.done.wait(timeout=self._send_timeout_s):
                self._abort(task)
                task.finished.wait(timeout=self._send_abort_grace_s)
                return False
            with self._state_lock:
                if self._in_flight is task:
                    self._in_flight = None
                succeeded = task.succeeded and not self._closed
            if succeeded:
                return True
            self._abort(task)
            return False

    def disconnect(self) -> None:
        self._abort(None)

    def close(self) -> None:
        self.disconnect()

    def _abort(self, task: _BoundedCall | None) -> None:
        with self._state_lock:
            current = self._in_flight
            if task is not None:
                task.cancel()
            if current is not None:
                current.cancel()
            self._in_flight = None
            self._closed = True
            if self._abort_started:
                return
            self._abort_started = True
        self._close_raw_socket()
        close_task = _OBSERVER_CLOSE_EXECUTOR.submit(self._close_websocket)
        if close_task is not None:
            close_task.finished.wait(timeout=self._send_abort_grace_s)

    def _close_raw_socket(self) -> None:
        connected_socket = getattr(self._websocket, "socket", None)
        if connected_socket is None:
            transport = getattr(self._websocket, "transport", None)
            connected_socket = getattr(transport, "socket", None)
        if connected_socket is None:
            return
        try:
            connected_socket.shutdown(socket_module.SHUT_RDWR)
        except BaseException:
            pass
        try:
            connected_socket.close()
        except BaseException:
            pass

    def _close_websocket(self) -> None:
        try:
            self._websocket.close()
        except BaseException:
            pass


def _handle_observer_connection(
    websocket: Any,
    *,
    dispatch: Callable[[dict[str, Any], Any], dict[str, Any] | None],
    remove_observer_subscriptions: Callable[[Any], None],
    profile: str,
    runtime_generation: str,
    instance_id: str,
    observer_contract: int = 1,
) -> None:
    observer_contract = _validated_observer_contract(observer_contract)
    transport = _ObserverSocketTransport(websocket)
    if observer_contract == 2:
        ready_payload = {
            "observer_contract": 2,
            "connection_role": "observer",
        }
    else:
        ready_payload = {
            "local_gateway_protocol": 1,
            "observer_contract": 1,
            "connection_role": "observer",
            "profile": profile,
            "runtime_generation": runtime_generation,
            "instance_id": instance_id,
        }
    transport.write(
        {
            "jsonrpc": "2.0",
            "method": "event",
            "params": {
                "type": "gateway.ready",
                "payload": ready_payload,
            },
        }
    )
    try:
        for raw in websocket:
            request = _decode_frame(raw)
            request_id = request.get("id") if isinstance(request, dict) else None
            method = request.get("method") if isinstance(request, dict) else None
            if request is None:
                transport.write(
                    {
                        "jsonrpc": "2.0",
                        "id": None,
                        "error": {"code": -32700, "message": "parse error"},
                    }
                )
                continue
            if (
                method
                not in {
                    "session.observe.subscribe",
                    "session.observe.unsubscribe",
                }
                | SESSION_CATALOG_METHODS
            ):
                transport.write(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "error": {
                            "code": 4003,
                            "message": "observer connection is read-only",
                        },
                    }
                )
                continue
            if observer_contract == 2 and method not in SESSION_CATALOG_METHODS:
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
                    transport.write(
                        {
                            "jsonrpc": "2.0",
                            "id": request_id,
                            "error": {"code": -32602, "message": "invalid params"},
                        }
                    )
                    continue
                request = dict(request)
                request["params"] = dict(params)
                if method == "session.observe.subscribe":
                    request["params"]["runtime_generation"] = runtime_generation
            elif method == "session.observe.subscribe":
                params = request.get("params")
                request = dict(request)
                request["params"] = {
                    **(params if isinstance(params, dict) else {}),
                    "relay_local_only": True,
                }
            try:
                response = dispatch(request, transport)
            except (TypeError, ValueError):
                if method not in SESSION_CATALOG_METHODS:
                    raise
                transport.write(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "error": {"code": -32602, "message": "invalid params"},
                    }
                )
                continue
            if response is not None and not transport.write(response):
                break
    except Exception as error:
        _log.debug(
            "observer socket connection closed error_type=%s",
            type(error).__name__,
        )
    finally:
        remove_observer_subscriptions(transport)
        transport.disconnect()


def start_observer_endpoint(
    *,
    authority: MacOSRuntimeAuthorityV2,
    dispatch: Callable[[dict[str, Any], Any], dict[str, Any] | None],
    remove_observer_subscriptions: Callable[[Any], None],
    paths: local_gateway_paths.MacOSLocalGatewayPaths | None = None,
    process_identity_provider: ProcessIdentityProvider = current_process_identity,
    observer_contract: int = 1,
) -> ObserverEndpointRegistration:
    """Start a mode-0600 Unix observer socket and publish its non-secret path."""
    if unix_serve is None:
        raise RuntimeError("websockets Unix socket support is unavailable")

    observer_contract = _validated_observer_contract(observer_contract)
    require_current_process_authority(
        authority,
        process_identity_provider=process_identity_provider,
    )
    endpoint_profile = validate_profile(authority.profile)
    endpoint_generation = _validated_runtime_generation(authority.runtime_generation)
    registry_directory, socket_directory = _directories(paths)
    registry = ensure_private_directory(registry_directory)
    socket_dir = ensure_private_directory(socket_directory)

    endpoint_pid = authority.pid
    instance_id = authority.instance_id
    target = registry / f"gateway-{endpoint_pid}-{instance_id}.json"
    socket_path = socket_dir / f"o-{endpoint_pid}-{instance_id[:8]}.sock"
    relay_server = None
    relay_thread = None
    try:
        relay_server = unix_serve(
            partial(
                _handle_observer_connection,
                dispatch=dispatch,
                remove_observer_subscriptions=remove_observer_subscriptions,
                profile=endpoint_profile,
                runtime_generation=endpoint_generation,
                instance_id=instance_id,
                observer_contract=observer_contract,
            ),
            path=str(socket_path),
            max_size=MAX_FRAME_BYTES,
        )
        os.chmod(socket_path, 0o600)
        if not is_private_socket(socket_path, directory=socket_dir):
            raise RuntimeError("untrusted local relay socket")
        relay_thread = threading.Thread(
            target=relay_server.serve_forever,
            name=f"observer-socket-{endpoint_pid}",
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
                    _log.debug("observer endpoint shutdown after start failure failed")
            else:
                stopped, shutdown_error = shutdown_server_and_join(
                    relay_server,
                    relay_thread,
                    attempts=2,
                )
                if shutdown_error is not None or not stopped:
                    _log.debug(
                        "observer endpoint thread cleanup after start failure failed"
                    )
        unlink_owned_socket(socket_path, directory=socket_dir)
        try:
            payload = read_private_registry(target, directory=registry)
            if payload is not None and payload.get("instance_id") == instance_id:
                unlink_private_registry(target, directory=registry)
        except BaseException:
            _log.debug("observer endpoint registry cleanup after start failure failed")
        raise
    return ObserverEndpointRegistration(
        target, socket_path, instance_id, relay_server, relay_thread
    )


def list_observer_endpoints(
    *,
    paths: local_gateway_paths.MacOSLocalGatewayPaths | None = None,
    process_identity_provider: ProcessIdentityProvider = current_process_identity,
) -> list[ObserverEndpoint | RuntimeEndpointV2]:
    registry, socket_dir = _directories(paths)
    if not is_private_directory(registry):
        return []

    endpoints: list[ObserverEndpoint] = []
    for path in sorted(registry.glob("gateway-*.json")):
        try:
            payload = read_private_registry(path, directory=registry)
            if payload is None:
                continue
            if "ws_url" in payload:
                unlink_private_registry(path, directory=registry)
                continue
            endpoint = decode_runtime_descriptor_v2(
                payload,
                registry_path=path,
                socket_directory=socket_dir,
                process_identity_provider=process_identity_provider,
            )
            endpoints.append(endpoint)
        except Exception:
            _log.debug("ignoring invalid observer endpoint registration")
    return endpoints


class _RelaySubscription:
    def __init__(
        self,
        *,
        local_id: str,
        transport: Any,
        websocket: Any,
        pending_frames: list[dict],
        sequence_guard: ObserverSequenceGuard,
        hub: ObserverRelayHub,
    ) -> None:
        self.local_id = local_id
        self.transport = transport
        self.websocket = websocket
        self.pending_frames = pending_frames
        self.sequence_guard = sequence_guard
        self.hub = hub
        self.stop_requested = threading.Event()
        self._started = False
        self.lifecycle_state = "prepared"
        self.activation_deadline_monotonic = monotonic() + _ACTIVATION_TIMEOUT_S
        self.activation_timer: Any | None = None
        self.thread = threading.Thread(
            target=self._read,
            name=f"observer-relay-{local_id[:8]}",
            daemon=True,
        )

    def arm_activation_expiry(self) -> None:
        remaining = max(0.0, self.activation_deadline_monotonic - monotonic())
        timer = threading.Timer(
            remaining,
            lambda: self.hub._expire_prepared(self),
        )
        timer.daemon = True
        self.activation_timer = timer
        timer.start()

    def cancel_activation_expiry(self) -> Any | None:
        timer = self.activation_timer
        self.activation_timer = None
        if timer is None:
            return None
        timer.cancel()
        return timer

    def wait_activation_expiry(self, timer: Any | None) -> None:
        if timer is None:
            return
        is_alive = getattr(timer, "is_alive", None)
        join = getattr(timer, "join", None)
        if (
            callable(is_alive)
            and is_alive()
            and callable(join)
            and threading.current_thread() is not timer
        ):
            join(timeout=1.0)
            if is_alive():
                raise RuntimeError("observer activation timer did not stop")

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        try:
            self.thread.start()
        except BaseException:
            self._started = False
            raise

    def close(self) -> None:
        self.stop_requested.set()
        try:
            self.websocket.close()
        except Exception:
            pass
        if (
            self._started
            and self.thread.is_alive()
            and threading.current_thread() is not self.thread
        ):
            self.thread.join(timeout=1.0)
            if self.thread.is_alive():
                raise RuntimeError("observer relay reader did not stop")

    def _forward(self, frame: dict) -> bool:
        if frame.get("method") != "event":
            return True
        if not self.sequence_guard.accept(frame):
            return True
        try:
            return bool(self.transport.write(frame))
        except Exception:
            return False

    def _read(self) -> None:
        unexpected = False
        try:
            for frame in self.pending_frames:
                if self.stop_requested.is_set() or not self._forward(frame):
                    return
            while not self.stop_requested.is_set():
                raw = self.websocket.recv()
                frame = _decode_frame(raw)
                if frame is not None and not self._forward(frame):
                    return
        except Exception:
            unexpected = not self.stop_requested.is_set()
        finally:
            try:
                self.websocket.close()
            except Exception:
                pass
            self.hub._reader_finished(self, unexpected=unexpected)


class ObserverRelayHub:
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
        self._subscriptions: dict[str, _RelaySubscription] = {}
        self._subscriptions_by_transport: dict[Any, set[str]] = {}

    def subscribe(
        self,
        session_key: str,
        profile: str,
        transport: Any,
        *,
        runtime_generation: str,
    ) -> dict | None:
        if self._connect_transport is None:
            return None
        requested_profile = validate_profile(profile)
        requested_generation = _validated_runtime_generation(runtime_generation)
        endpoints = self._discover_endpoints()
        for endpoint in endpoints:
            trusted_socket = (
                _is_private_socket_path(endpoint.socket_path)
                if self._paths is None
                else _is_private_socket_path(endpoint.socket_path, self._paths)
            )
            if (
                endpoint.pid == self._current_pid
                or endpoint.profile != requested_profile
                or endpoint.runtime_generation != requested_generation
                or not self._endpoint_evidence_valid(endpoint)
                or not trusted_socket
            ):
                continue
            websocket = None
            try:
                websocket = self._connect_transport(
                    str(endpoint.socket_path),
                    uri="ws://localhost/observer",
                    open_timeout=_CONNECT_TIMEOUT_S,
                    close_timeout=1.0,
                    max_size=MAX_FRAME_BYTES,
                    max_queue=_MAX_POST_RESPONSE_QUEUE,
                )
                if isinstance(endpoint, RuntimeEndpointV2):
                    if self._peer_pid_provider(websocket) != endpoint.pid:
                        raise RuntimeError("observer relay peer identity mismatch")
                    if not self._endpoint_evidence_valid(endpoint):
                        raise RuntimeError("observer relay endpoint evidence changed")
                result, pending_frames = _subscribe_upstream(
                    websocket,
                    session_key=session_key,
                    profile=requested_profile,
                    runtime_generation=requested_generation,
                    instance_id=endpoint.instance_id,
                )
                if result is None:
                    websocket.close()
                    continue

                sequence_guard = ObserverSequenceGuard.from_snapshot(
                    result,
                    requested_session_key=session_key,
                    requested_profile=requested_profile,
                    requested_runtime_generation=requested_generation,
                )
                local_id = str(uuid.uuid4())
                payload = dict(result)
                payload["subscription_id"] = local_id
                subscription = _RelaySubscription(
                    local_id=local_id,
                    transport=transport,
                    websocket=websocket,
                    pending_frames=pending_frames,
                    sequence_guard=sequence_guard,
                    hub=self,
                )
                activation_timer_error: BaseException | None = None
                with self._lock:
                    self._subscriptions[local_id] = subscription
                    self._subscriptions_by_transport.setdefault(transport, set()).add(
                        local_id
                    )
                    try:
                        subscription.arm_activation_expiry()
                    except BaseException as error:
                        subscription.lifecycle_state = "closing"
                        activation_timer_error = error
                if activation_timer_error is not None:
                    try:
                        self._finish_close(subscription)
                    except BaseException as close_error:
                        activation_timer_error.add_note(
                            "observer prepare cleanup failed: "
                            f"{type(close_error).__name__}"
                        )
                    raise activation_timer_error
                return payload
            except ObserverRelayRpcError:
                if websocket is not None:
                    try:
                        websocket.close()
                    except Exception:
                        pass
                raise
            except Exception as exc:
                if websocket is not None:
                    try:
                        websocket.close()
                    except Exception:
                        pass
                _log.debug(
                    "observer relay candidate failed pid=%s error_type=%s",
                    endpoint.pid,
                    type(exc).__name__,
                )
        return None

    def _discover_endpoints(self) -> list[ObserverEndpoint | RuntimeEndpointV2]:
        try:
            if self._paths is None:
                return list_observer_endpoints(
                    process_identity_provider=self._process_identity_provider
                )
            return list_observer_endpoints(
                paths=self._paths,
                process_identity_provider=self._process_identity_provider,
            )
        except TypeError:
            return (
                list_observer_endpoints()
                if self._paths is None
                else list_observer_endpoints(paths=self._paths)
            )

    def _endpoint_evidence_valid(self, endpoint: ObserverEndpoint) -> bool:
        if not isinstance(endpoint, RuntimeEndpointV2):
            return True
        try:
            observed = self._process_identity_provider(endpoint.pid)
        except BaseException:
            return False
        return self._socket_identity_provider(endpoint) and (
            normalize_process_identity(observed) == endpoint.process_identity
        )

    def activate(self, subscription_id: str, transport: Any) -> bool | None:
        failure: BaseException | None = None
        expired = False
        activated = False
        activation_timer: Any | None = None
        with self._lock:
            subscription = self._subscriptions.get(subscription_id)
            if subscription is None:
                return None
            if subscription.transport is not transport:
                raise ObserverRelayOwnershipError(
                    "observer subscription belongs to another connection"
                )
            if subscription.lifecycle_state != "prepared":
                return None
            activation_timer = subscription.cancel_activation_expiry()
            if monotonic() >= subscription.activation_deadline_monotonic:
                subscription.lifecycle_state = "closing"
                expired = True
            else:
                subscription.lifecycle_state = "activating"
                try:
                    subscription.start()
                except BaseException as exc:
                    subscription.lifecycle_state = "closing"
                    failure = exc
                else:
                    subscription.lifecycle_state = "active"
                    activated = True

        subscription.wait_activation_expiry(activation_timer)
        if activated:
            return True

        if expired:
            self._finish_close(subscription)
            return None
        if failure is not None:
            try:
                self._finish_close(subscription)
            except BaseException as close_error:
                failure.add_note(
                    f"observer activation cleanup failed: {type(close_error).__name__}"
                )
            raise failure
        return None

    def unsubscribe(self, subscription_id: str, transport: Any) -> bool | None:
        with self._lock:
            subscription = self._subscriptions.get(subscription_id)
            if subscription is None:
                return None
            if subscription.transport is not transport:
                raise ObserverRelayOwnershipError(
                    "observer subscription belongs to another connection"
                )
        return True if self._close_subscription(subscription) else None

    def close_transport(self, transport: Any) -> int:
        with self._lock:
            ids = list(self._subscriptions_by_transport.get(transport, ()))
            subscriptions = [
                self._subscriptions[subscription_id]
                for subscription_id in ids
                if subscription_id in self._subscriptions
            ]
        closed = 0
        first_error: BaseException | None = None
        for subscription in subscriptions:
            try:
                if self._close_subscription(subscription):
                    closed += 1
            except BaseException as error:
                first_error = first_error or error
        if first_error is not None:
            raise first_error
        return closed

    def _close_subscription(self, subscription: _RelaySubscription) -> bool:
        activation_timer: Any | None = None
        with self._lock:
            current = self._subscriptions.get(subscription.local_id)
            if current is not subscription or subscription.lifecycle_state == "closing":
                return False
            subscription.lifecycle_state = "closing"
            activation_timer = subscription.cancel_activation_expiry()
        subscription.wait_activation_expiry(activation_timer)
        self._finish_close(subscription)
        return True

    def _finish_close(self, subscription: _RelaySubscription) -> None:
        try:
            subscription.close()
        except BaseException:
            with self._lock:
                current = self._subscriptions.get(subscription.local_id)
                if current is subscription:
                    subscription.lifecycle_state = "close_failed"
            raise
        with self._lock:
            current = self._subscriptions.get(subscription.local_id)
            if current is subscription:
                subscription.lifecycle_state = "closed"
                self._remove_locked(subscription)

    def _expire_prepared(self, subscription: _RelaySubscription) -> None:
        activation_timer: Any | None = None
        with self._lock:
            current = self._subscriptions.get(subscription.local_id)
            if (
                current is not subscription
                or subscription.lifecycle_state != "prepared"
            ):
                return
            subscription.lifecycle_state = "closing"
            activation_timer = subscription.cancel_activation_expiry()
        subscription.wait_activation_expiry(activation_timer)
        try:
            self._finish_close(subscription)
        except BaseException as error:
            _log.debug(
                "observer prepared subscription cleanup failed error_type=%s",
                type(error).__name__,
            )

    def _remove_locked(self, subscription: _RelaySubscription) -> None:
        subscription.cancel_activation_expiry()
        self._subscriptions.pop(subscription.local_id, None)
        ids = self._subscriptions_by_transport.get(subscription.transport)
        if ids is not None:
            ids.discard(subscription.local_id)
            if not ids:
                self._subscriptions_by_transport.pop(subscription.transport, None)

    def _reader_finished(
        self, subscription: _RelaySubscription, *, unexpected: bool
    ) -> None:
        with self._lock:
            current = self._subscriptions.get(subscription.local_id)
            if current is not subscription:
                return
            if subscription.lifecycle_state == "closing":
                return
            subscription.lifecycle_state = "closed"
            self._remove_locked(subscription)
        if unexpected:
            disconnect = getattr(subscription.transport, "disconnect", None)
            if callable(disconnect):
                try:
                    disconnect()
                except Exception:
                    pass


def _subscribe_upstream(
    websocket: Any,
    *,
    session_key: str,
    profile: str,
    runtime_generation: str,
    instance_id: str,
) -> tuple[dict | None, list[dict]]:
    deadline = monotonic() + _RPC_TIMEOUT_S

    def receive_before_deadline() -> Any:
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise TimeoutError("observer relay RPC deadline exceeded")
        return websocket.recv(timeout=remaining)

    ready_seen = False
    pending_frames: list[dict] = []
    while not ready_seen:
        frame = _decode_frame(receive_before_deadline())
        if frame is None:
            continue
        event_params = frame.get("params") if frame.get("method") == "event" else None
        if (
            isinstance(event_params, dict)
            and event_params.get("type") == "gateway.ready"
        ):
            payload = event_params.get("payload") or {}
            if (
                set(frame) != {"jsonrpc", "method", "params"}
                or frame.get("jsonrpc") != "2.0"
                or set(event_params) != {"type", "payload"}
                or not isinstance(payload, dict)
                or set(payload) != _READY_PAYLOAD_FIELDS
                or type(payload.get("local_gateway_protocol")) is not int
                or payload.get("local_gateway_protocol") != 1
                or type(payload.get("observer_contract")) is not int
                or payload.get("observer_contract") != 1
                or payload.get("connection_role") != "observer"
                or payload.get("profile") != profile
                or payload.get("runtime_generation") != runtime_generation
                or payload.get("instance_id") != instance_id
            ):
                return None, []
            ready_seen = True

    params: dict[str, Any] = {
        "session_key": session_key,
        "profile": profile,
        "runtime_generation": runtime_generation,
        "relay_local_only": True,
    }
    websocket.send(
        encode_frame(
            {
                "jsonrpc": "2.0",
                "id": _RELAY_SUBSCRIBE_ID,
                "method": "session.observe.subscribe",
                "params": params,
            }
        )
    )

    while True:
        frame = _decode_frame(receive_before_deadline())
        if frame is None:
            continue
        if frame.get("id") != _RELAY_SUBSCRIBE_ID:
            if frame.get("method") == "event":
                if len(pending_frames) >= _MAX_PENDING_FRAMES:
                    raise RuntimeError("observer relay pending frame limit exceeded")
                pending_frames.append(frame)
            continue
        result = frame.get("result")
        if isinstance(result, dict):
            return result, pending_frames
        error = frame.get("error")
        if isinstance(error, dict):
            code = int(error.get("code") or -32000)
            message = str(error.get("message") or "observer relay failed")
            if code == 4001:
                return None, []
            raise ObserverRelayRpcError(code, message)
        return None, []


observer_relay_hub = ObserverRelayHub()
