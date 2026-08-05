"""Bounded macOS transport and discovery publication for Local Gateway v1."""

from __future__ import annotations

import hashlib
import logging
import math
import os
import socket
import struct
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final

from ....domain.lifecycle import (
    LifecycleDeadlineExceeded,
    LifecycleNotReady,
)
from ...local_protocol.frame_codec import (
    MAX_FRAME_BYTES,
    FrameCodecError,
    decode_frame,
    encode_frame,
)
from ...local_protocol.handshake_v1 import (
    LocalContractError,
    decode_local_hello,
)
from . import local_gateway_paths
from .availability import LOCAL_GATEWAY_AVAILABLE
from .local_trust import (
    ensure_private_directory,
    is_private_socket,
    read_private_registry,
    unlink_private_registry,
    unlink_private_socket,
    validate_profile,
)
from .runtime_descriptor_v2 import (
    RUNTIME_DESCRIPTOR_VERSION,
    MacOSRuntimeAuthorityV2,
    ProcessIdentityProvider,
    current_process_identity,
    publish_runtime_descriptor_v2,
    require_current_process_authority,
)

_log = logging.getLogger(__name__)

LOCAL_DESCRIPTOR_VERSION = RUNTIME_DESCRIPTOR_VERSION
DEFAULT_FIRST_FRAME_TIMEOUT_S = 2.0
DEFAULT_READ_TIMEOUT_S = 3.0
DEFAULT_WRITE_TIMEOUT_S = 3.0
DEFAULT_HANDSHAKE_TIMEOUT_S = 3.0
_MAX_TIMEOUT_S = 30.0
_ACCEPT_POLL_S = 0.05
_LENGTH_PREFIX_BYTES = 4
_LISTEN_BACKLOG = 8
_MAX_UDS_PATH_BYTES = 103

_ERROR_REASONS: Final[Mapping[int, str]] = MappingProxyType(
    {
        4300: "contract_unsupported",
        4301: "invalid_envelope",
        4302: "frame_too_large",
        4303: "invalid_utf8",
        4304: "capability_not_available",
        4305: "overloaded",
        4306: "deadline_exceeded_before_effect",
    }
)


class LocalTransportState(str, Enum):
    NEW = "new"
    STARTING = "starting"
    READY = "ready"
    DRAINING = "draining"
    STOPPING = "stopping"
    STOPPED = "stopped"


# Local transport resource state machine:
#
#   NEW -> STARTING -> READY -> DRAINING -> STOPPING -> STOPPED
#             |          |                    ^            |
#             +----------+--------------------+            |
#                                                        STARTING
#
# The STARTING -> STOPPING edge is rollback. READY -> STOPPING is a
# defensive direct-stop path; GatewayLifecycle normally drains first.
LOCAL_TRANSPORT_TRANSITIONS: Final[
    Mapping[LocalTransportState, frozenset[LocalTransportState]]
] = MappingProxyType(
    {
        LocalTransportState.NEW: frozenset({LocalTransportState.STARTING}),
        LocalTransportState.STARTING: frozenset(
            {
                LocalTransportState.READY,
                LocalTransportState.STOPPING,
            }
        ),
        LocalTransportState.READY: frozenset(
            {
                LocalTransportState.DRAINING,
                LocalTransportState.STOPPING,
            }
        ),
        LocalTransportState.DRAINING: frozenset({LocalTransportState.STOPPING}),
        LocalTransportState.STOPPING: frozenset({LocalTransportState.STOPPED}),
        LocalTransportState.STOPPED: frozenset({LocalTransportState.STARTING}),
    }
)


class LocalTransportTransitionError(RuntimeError):
    """Raised when a transport resource transition is not documented."""


class _ConnectionCancelled(Exception):
    pass


class _WireFailure(Exception):
    def __init__(self, code: int) -> None:
        self.code = code if code in _ERROR_REASONS else 4301
        self.reason = _ERROR_REASONS[self.code]
        super().__init__(self.reason)


def _bounded_timeout(value: object, field_name: str) -> float:
    if isinstance(value, bool):
        raise TypeError(f"{field_name} must be a finite positive number")
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field_name} must be a finite positive number") from None
    if not math.isfinite(seconds) or not 0 < seconds <= _MAX_TIMEOUT_S:
        raise ValueError(f"{field_name} must be a finite positive number")
    return seconds


@dataclass(frozen=True, slots=True)
class _MacOSLocalGatewaySettings:
    """Non-secret macOS endpoint settings.

    All timeout values are seconds. The descriptor contains no credentials and
    is published only inside a caller-owned mode-0700 directory.
    """

    profile: str
    registry_directory: Path
    socket_directory: Path
    first_frame_timeout_s: float = DEFAULT_FIRST_FRAME_TIMEOUT_S
    read_timeout_s: float = DEFAULT_READ_TIMEOUT_S
    write_timeout_s: float = DEFAULT_WRITE_TIMEOUT_S
    handshake_timeout_s: float = DEFAULT_HANDSHAKE_TIMEOUT_S
    pid: int | None = None
    authority: (
        MacOSRuntimeAuthorityV2 | Callable[[], MacOSRuntimeAuthorityV2] | None
    ) = None
    process_identity_provider: ProcessIdentityProvider = current_process_identity

    def __post_init__(self) -> None:
        object.__setattr__(self, "profile", validate_profile(self.profile))
        object.__setattr__(
            self,
            "registry_directory",
            Path(self.registry_directory).expanduser(),
        )
        object.__setattr__(
            self,
            "socket_directory",
            Path(self.socket_directory).expanduser(),
        )
        for field_name in (
            "first_frame_timeout_s",
            "read_timeout_s",
            "write_timeout_s",
            "handshake_timeout_s",
        ):
            object.__setattr__(
                self,
                field_name,
                _bounded_timeout(getattr(self, field_name), field_name),
            )
        endpoint_pid = os.getpid() if self.pid is None else self.pid
        if (
            isinstance(endpoint_pid, bool)
            or not isinstance(endpoint_pid, int)
            or endpoint_pid <= 0
            or endpoint_pid > 2_147_483_647
        ):
            raise ValueError("pid must be a valid POSIX pid")
        object.__setattr__(self, "pid", endpoint_pid)
        if self.authority is None:
            raise ValueError("authority is required")
        if not callable(self.process_identity_provider):
            raise TypeError("process_identity_provider must be callable")


class MacOSLocalGatewayResource:
    r"""Lifecycle-owned macOS Local Gateway endpoint.

    Resource state:

        NEW -> STARTING -> READY -> DRAINING -> STOPPING -> STOPPED
                  |          |                    ^            |
                  +----------+--------------------+            |
                                                             STARTING

    Wire contract:

    - one request and one response per connection;
    - four-byte unsigned big-endian body length;
    - strict UTF-8 JSON body no larger than 262144 bytes;
    - no credentials in the mode-0600 discovery descriptor.

    ``start``, ``drain`` and ``stop`` receive absolute monotonic deadlines.
    A LocalHello negotiation has no business side effect; a timeout is
    therefore returned as 4306 before-effect whenever the socket is writable.
    """

    name = "macos-local-gateway"

    def __init__(
        self,
        *,
        settings: _MacOSLocalGatewaySettings,
        hello_handler: Callable[[Any], str],
        ready: Callable[[], bool] | None = None,
        clock: Callable[[], float] = time.monotonic,
        path_contract: local_gateway_paths.MacOSLocalGatewayPaths | None = None,
    ) -> None:
        self._settings = settings
        self._hello_handler = hello_handler
        self._ready = ready or (lambda: True)
        self._clock = clock
        self._path_contract = path_contract
        self._state = LocalTransportState.NEW
        self._lock = threading.RLock()
        self._stop_requested = threading.Event()
        self._listener: socket.socket | None = None
        self._active_connection: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._instance_id: str | None = None
        self._runtime_authority: MacOSRuntimeAuthorityV2 | None = None
        self._socket_identity: tuple[int, int] | None = None

        endpoint_key = hashlib.sha256(settings.profile.encode("ascii")).hexdigest()[:12]
        self._descriptor_path = (
            settings.registry_directory / f"gateway-{settings.pid}-{endpoint_key}.json"
        )
        self._socket_path = (
            settings.socket_directory / f"g-{settings.pid}-{endpoint_key}.sock"
        )
        encoded_socket_path = os.fsencode(self._socket_path)
        if (
            b"\x00" in encoded_socket_path
            or len(encoded_socket_path) > _MAX_UDS_PATH_BYTES
        ):
            raise ValueError("unix socket path exceeds native limit")

    @property
    def state(self) -> LocalTransportState:
        return self._state

    @property
    def instance_id(self) -> str | None:
        return self._instance_id

    @property
    def descriptor_path(self) -> Path:
        return self._descriptor_path

    @property
    def socket_path(self) -> Path:
        return self._socket_path

    def _transition(self, target: LocalTransportState) -> None:
        with self._lock:
            if target not in LOCAL_TRANSPORT_TRANSITIONS[self._state]:
                raise LocalTransportTransitionError(
                    "local_transport_transition_not_allowed"
                )
            self._state = target

    def _remaining(self, deadline: float) -> float:
        remaining = deadline - self._clock()
        if remaining <= 0:
            raise LifecycleDeadlineExceeded("lifecycle_deadline_exceeded")
        return remaining

    @staticmethod
    def _path_exists(path: Path) -> bool:
        try:
            path.lstat()
            return True
        except FileNotFoundError:
            return False

    def _clean_stale_paths(self) -> None:
        if self._path_exists(self._descriptor_path) and not (
            unlink_private_registry(
                self._descriptor_path,
                directory=self._settings.registry_directory,
            )
        ):
            raise RuntimeError("untrusted stale local gateway descriptor")
        if self._path_exists(self._socket_path) and not (
            unlink_private_socket(
                self._socket_path,
                directory=self._settings.socket_directory,
            )
        ):
            raise RuntimeError("untrusted stale local gateway socket")

    def _publish_descriptor(self) -> None:
        assert self._runtime_authority is not None
        publish_runtime_descriptor_v2(
            authority=self._runtime_authority,
            socket_path=self._socket_path,
            target=self._descriptor_path,
            registry_directory=self._settings.registry_directory,
        )

    def _unpublish_owned_descriptor(self) -> bool:
        if self._instance_id is None:
            return False
        payload = read_private_registry(
            self._descriptor_path,
            directory=self._settings.registry_directory,
        )
        if payload is None or payload.get("instance_id") != self._instance_id:
            return False
        return unlink_private_registry(
            self._descriptor_path,
            directory=self._settings.registry_directory,
        )

    def _unlink_owned_socket(self) -> None:
        identity = self._socket_identity
        if identity is None:
            return
        try:
            metadata = self._socket_path.lstat()
        except OSError:
            return
        if (metadata.st_dev, metadata.st_ino) != identity:
            return
        unlink_private_socket(
            self._socket_path,
            directory=self._settings.socket_directory,
        )

    def _close_listener(self) -> None:
        with self._lock:
            listener = self._listener
            self._listener = None
        if listener is None:
            return
        try:
            listener.close()
        except OSError:
            pass

    def _cancel_active_connection(self) -> None:
        with self._lock:
            connection = self._active_connection
        if connection is None:
            return
        try:
            connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            connection.close()
        except OSError:
            pass

    def _clear_runtime(self) -> None:
        with self._lock:
            self._listener = None
            self._active_connection = None
            self._thread = None
            self._instance_id = None
            self._runtime_authority = None
            self._socket_identity = None

    def start(self, deadline: float) -> None:
        """Bind, validate and atomically publish before ``deadline``.

        The operation is idempotent while READY. It owns no business effect;
        any partial bind or publication is rolled back before failure returns.
        """

        with self._lock:
            if self._state is LocalTransportState.READY:
                return
        self._remaining(deadline)
        authority_source = self._settings.authority
        authority = (
            authority_source() if callable(authority_source) else authority_source
        )
        if authority is None:
            raise RuntimeError("runtime authority is unavailable")
        if (
            authority.profile != self._settings.profile
            or authority.pid != self._settings.pid
        ):
            raise RuntimeError("runtime authority does not match endpoint settings")
        require_current_process_authority(
            authority,
            process_identity_provider=self._settings.process_identity_provider,
        )
        self._transition(LocalTransportState.STARTING)
        try:
            self._runtime_authority = authority
            if self._path_contract is not None:
                local_gateway_paths.ensure_distinct_local_gateway_directories(
                    self._path_contract
                )
            registry = ensure_private_directory(self._settings.registry_directory)
            socket_directory = ensure_private_directory(self._settings.socket_directory)
            if (
                registry != self._settings.registry_directory
                or socket_directory != self._settings.socket_directory
            ):
                raise RuntimeError("local gateway path mismatch")
            self._clean_stale_paths()
            self._remaining(deadline)

            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                listener.bind(str(self._socket_path))
                os.chmod(self._socket_path, 0o600)
                if not is_private_socket(
                    self._socket_path,
                    directory=self._settings.socket_directory,
                ):
                    raise RuntimeError("untrusted local gateway socket")
                metadata = self._socket_path.lstat()
                listener.listen(_LISTEN_BACKLOG)
                listener.settimeout(_ACCEPT_POLL_S)
            except BaseException:
                listener.close()
                raise

            self._stop_requested.clear()
            self._instance_id = authority.instance_id
            self._socket_identity = (metadata.st_dev, metadata.st_ino)
            self._listener = listener
            thread = threading.Thread(
                target=self._serve,
                name=f"local-gateway-{self._settings.pid}",
                daemon=True,
            )
            self._thread = thread
            self._publish_descriptor()
            self._remaining(deadline)
            thread.start()
            self._transition(LocalTransportState.READY)
        except BaseException:
            self._stop_requested.set()
            self._close_listener()
            self._cancel_active_connection()
            thread = self._thread
            if (
                thread is not None
                and thread.is_alive()
                and thread is not threading.current_thread()
            ):
                thread.join(timeout=max(0.0, deadline - self._clock()))
            self._unpublish_owned_descriptor()
            self._unlink_owned_socket()
            self._transition(LocalTransportState.STOPPING)
            if thread is not None and thread.is_alive():
                raise RuntimeError("local gateway start cleanup incomplete") from None
            self._clear_runtime()
            self._transition(LocalTransportState.STOPPED)
            raise

    def drain(self, deadline: float) -> None:
        """Stop discovery and accepting new requests before ``deadline``."""

        with self._lock:
            if self._state is not LocalTransportState.READY:
                return
        self._remaining(deadline)
        self._transition(LocalTransportState.DRAINING)
        self._unpublish_owned_descriptor()
        self._close_listener()
        self._remaining(deadline)

    def stop(self, deadline: float) -> None:
        """Cancel pending local I/O and remove owned entries by ``deadline``."""

        with self._lock:
            state = self._state
        if state in {
            LocalTransportState.NEW,
            LocalTransportState.STOPPED,
        }:
            return
        self._remaining(deadline)
        if state is LocalTransportState.READY:
            self.drain(deadline)
        if state is not LocalTransportState.STOPPING:
            self._transition(LocalTransportState.STOPPING)
        self._stop_requested.set()
        self._unpublish_owned_descriptor()
        self._close_listener()
        self._cancel_active_connection()
        thread = self._thread
        if (
            thread is not None
            and thread.is_alive()
            and thread is not threading.current_thread()
        ):
            thread.join(timeout=self._remaining(deadline))
            if thread.is_alive():
                raise LifecycleDeadlineExceeded("lifecycle_deadline_exceeded")
        self._unlink_owned_socket()
        self._clear_runtime()
        self._transition(LocalTransportState.STOPPED)

    def _serve(self) -> None:
        while not self._stop_requested.is_set():
            with self._lock:
                listener = self._listener
            if listener is None:
                return
            try:
                connection, _address = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                if not self._stop_requested.is_set():
                    _log.warning("local gateway listener stopped unexpectedly")
                return
            with self._lock:
                self._active_connection = connection
            try:
                self._handle_connection(connection)
            finally:
                try:
                    connection.close()
                except OSError:
                    pass
                with self._lock:
                    if self._active_connection is connection:
                        self._active_connection = None

    def _check_connection(
        self,
        deadline: float,
    ) -> float:
        if self._stop_requested.is_set():
            raise _ConnectionCancelled
        remaining = deadline - self._clock()
        if remaining <= 0:
            raise _WireFailure(4306)
        return remaining

    def _read_exact(
        self,
        connection: socket.socket,
        size: int,
        deadline: float,
    ) -> bytes:
        body = bytearray()
        while len(body) < size:
            remaining = self._check_connection(deadline)
            connection.settimeout(min(remaining, _ACCEPT_POLL_S))
            try:
                chunk = connection.recv(size - len(body))
            except TimeoutError:
                continue
            except OSError:
                if self._stop_requested.is_set():
                    raise _ConnectionCancelled from None
                raise _WireFailure(4301) from None
            if not chunk:
                raise _WireFailure(4301)
            body.extend(chunk)
        return bytes(body)

    def _send_payload(
        self,
        connection: socket.socket,
        payload: bytes,
        deadline: float,
    ) -> None:
        if len(payload) > MAX_FRAME_BYTES:
            raise _WireFailure(4302)
        connection.settimeout(self._check_connection(deadline))
        try:
            connection.sendall(struct.pack("!I", len(payload)) + payload)
        except TimeoutError:
            raise _WireFailure(4306) from None
        except OSError:
            if self._stop_requested.is_set():
                raise _ConnectionCancelled from None
            raise _WireFailure(4301) from None

    def _wait_until_ready(self, deadline: float) -> None:
        while not self._ready():
            remaining = self._check_connection(deadline)
            time.sleep(min(0.005, remaining))

    def _response_body(self, value: Any) -> bytes:
        if isinstance(value, str):
            try:
                payload = value.encode("utf-8", errors="strict")
            except UnicodeEncodeError:
                raise _WireFailure(4301) from None
        elif isinstance(value, bytes):
            payload = value
        else:
            raise _WireFailure(4301)
        if len(payload) > MAX_FRAME_BYTES:
            raise _WireFailure(4302)
        try:
            decode_frame(payload)
        except FrameCodecError as error:
            if error.category == "frame_too_large":
                raise _WireFailure(4302) from None
            if error.category == "invalid_utf8":
                raise _WireFailure(4303) from None
            raise _WireFailure(4301) from None
        return payload

    @staticmethod
    def _mapped_failure(error: BaseException) -> _WireFailure:
        if isinstance(error, _WireFailure):
            return error
        if isinstance(error, LocalContractError):
            return _WireFailure(error.code)
        if isinstance(error, LifecycleNotReady):
            return _WireFailure(4305)
        if isinstance(error, FrameCodecError):
            if error.category == "frame_too_large":
                return _WireFailure(4302)
            if error.category == "invalid_utf8":
                return _WireFailure(4303)
        return _WireFailure(4301)

    def _send_error(
        self,
        connection: socket.socket,
        failure: _WireFailure,
        handshake_deadline: float,
    ) -> None:
        payload = encode_frame(
            {
                "error": {
                    "code": failure.code,
                    "reason": failure.reason,
                }
            }
        ).encode("utf-8")
        write_deadline = min(
            handshake_deadline,
            self._clock() + self._settings.write_timeout_s,
        )
        self._send_payload(connection, payload, write_deadline)

    def _handle_connection(self, connection: socket.socket) -> None:
        accepted_at = self._clock()
        handshake_deadline = accepted_at + self._settings.handshake_timeout_s
        try:
            header = self._read_exact(
                connection,
                _LENGTH_PREFIX_BYTES,
                min(
                    handshake_deadline,
                    accepted_at + self._settings.first_frame_timeout_s,
                ),
            )
            body_size = struct.unpack("!I", header)[0]
            if body_size == 0:
                raise _WireFailure(4301)
            if body_size > MAX_FRAME_BYTES:
                raise _WireFailure(4302)
            body = self._read_exact(
                connection,
                body_size,
                min(
                    handshake_deadline,
                    self._clock() + self._settings.read_timeout_s,
                ),
            )
            hello = decode_local_hello(body)
            if hello.profile != self._settings.profile:
                raise _WireFailure(4301)
            self._wait_until_ready(handshake_deadline)
            response = self._response_body(self._hello_handler(body))
            self._check_connection(handshake_deadline)
            self._send_payload(
                connection,
                response,
                min(
                    handshake_deadline,
                    self._clock() + self._settings.write_timeout_s,
                ),
            )
        except _ConnectionCancelled:
            return
        except BaseException as error:  # noqa: BLE001
            failure = self._mapped_failure(error)
            if failure.code == 4301 and not isinstance(
                error,
                (
                    _WireFailure,
                    LocalContractError,
                    LifecycleNotReady,
                    FrameCodecError,
                ),
            ):
                _log.warning("local gateway handshake rejected internal error")
            try:
                self._send_error(
                    connection,
                    failure,
                    handshake_deadline,
                )
            except (
                _ConnectionCancelled,
                _WireFailure,
                OSError,
            ):
                return


def create_local_gateway_resource(
    *,
    paths: local_gateway_paths.MacOSLocalGatewayPaths,
    authority: MacOSRuntimeAuthorityV2 | Callable[[], MacOSRuntimeAuthorityV2],
    hello_handler: Callable[[Any], str],
    ready: Callable[[], bool] | None = None,
    clock: Callable[[], float] = time.monotonic,
    first_frame_timeout_s: float = DEFAULT_FIRST_FRAME_TIMEOUT_S,
    read_timeout_s: float = DEFAULT_READ_TIMEOUT_S,
    write_timeout_s: float = DEFAULT_WRITE_TIMEOUT_S,
    handshake_timeout_s: float = DEFAULT_HANDSHAKE_TIMEOUT_S,
    profile: str | None = None,
    pid: int | None = None,
    process_identity_provider: ProcessIdentityProvider = current_process_identity,
) -> MacOSLocalGatewayResource:
    """Build the verified macOS lifecycle resource."""
    concrete_authority = (
        authority if isinstance(authority, MacOSRuntimeAuthorityV2) else None
    )
    endpoint_profile = (
        concrete_authority.profile
        if profile is None and concrete_authority
        else profile
    )
    endpoint_pid = concrete_authority.pid if pid is None and concrete_authority else pid
    if endpoint_profile is None or endpoint_pid is None:
        raise ValueError("profile and pid are required for a deferred authority")
    return MacOSLocalGatewayResource(
        settings=_MacOSLocalGatewaySettings(
            profile=endpoint_profile,
            registry_directory=paths.local_gateway_registry_directory,
            socket_directory=paths.local_gateway_socket_directory,
            first_frame_timeout_s=first_frame_timeout_s,
            read_timeout_s=read_timeout_s,
            write_timeout_s=write_timeout_s,
            handshake_timeout_s=handshake_timeout_s,
            pid=endpoint_pid,
            authority=authority,
            process_identity_provider=process_identity_provider,
        ),
        hello_handler=hello_handler,
        ready=ready,
        clock=clock,
        path_contract=paths,
    )


__all__ = [
    "DEFAULT_FIRST_FRAME_TIMEOUT_S",
    "DEFAULT_HANDSHAKE_TIMEOUT_S",
    "DEFAULT_READ_TIMEOUT_S",
    "DEFAULT_WRITE_TIMEOUT_S",
    "LOCAL_DESCRIPTOR_VERSION",
    "LOCAL_GATEWAY_AVAILABLE",
    "LOCAL_TRANSPORT_TRANSITIONS",
    "LocalTransportState",
    "LocalTransportTransitionError",
    "MacOSLocalGatewayResource",
    "create_local_gateway_resource",
]
