"""Private, read-only UDS relay for Runtime Manager update-safety evidence."""

from __future__ import annotations

import json
import os
import socket
import stat
import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

_MAX_UDS_PATH_BYTES = 103
_MAX_REQUEST_BYTES = 1_024
_MAX_RESPONSE_BYTES = 8_192
_ACCEPT_TIMEOUT_S = 0.2
_CONNECTION_TIMEOUT_S = 1.0
_CLOSE_TIMEOUT_S = 3.0
_REQUEST = {"method": "update-safety.snapshot", "schema_version": 1}
_RESPONSE_FIELDS = frozenset(
    {
        "active_tasks",
        "evidence_complete",
        "pending_approvals",
        "pending_clarifications",
        "profile",
        "runtime_generation",
        "schema_version",
    }
)
_ERROR_RESPONSE = b'{"error":"unavailable","schema_version":1}\n'


def resolve_update_safety_socket_path(
    environment: Mapping[str, str] | None = None,
    *,
    effective_uid: int | None = None,
) -> Path:
    """Resolve the one process-independent same-user update-safety endpoint."""

    source = os.environ if environment is None else environment
    configured = source.get("HERMES_UPDATE_SAFETY_SOCKET")
    if configured is not None:
        if not configured or "\x00" in configured:
            raise ValueError("HERMES_UPDATE_SAFETY_SOCKET is invalid")
        endpoint = Path(configured).expanduser()
    else:
        uid = os.getuid() if effective_uid is None else effective_uid
        if type(uid) is not int or uid < 0:
            raise ValueError("effective uid is invalid")
        endpoint = Path("/tmp") / f"hermes-update-safety-{uid}" / "host.sock"
    if not endpoint.is_absolute() or ".." in endpoint.parts:
        raise ValueError("update-safety socket path must be absolute and canonical")
    if len(os.fsencode(endpoint)) > _MAX_UDS_PATH_BYTES:
        raise ValueError("update-safety socket path exceeds the macOS UDS limit")
    return endpoint


def _private_directory(directory: Path) -> tuple[int, int]:
    try:
        os.mkdir(directory, 0o700)
    except FileExistsError:
        pass
    except OSError as error:
        raise RuntimeError("update-safety directory cannot be created") from error
    try:
        metadata = directory.lstat()
    except OSError as error:
        raise RuntimeError("update-safety directory cannot be inspected") from error
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise RuntimeError("update-safety directory is not private")
    return metadata.st_dev, metadata.st_ino


def _remove_stale_owned_socket(endpoint: Path) -> None:
    try:
        metadata = endpoint.lstat()
    except FileNotFoundError:
        return
    except OSError as error:
        raise RuntimeError("update-safety socket cannot be inspected") from error
    if (
        not stat.S_ISSOCK(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.getuid()
    ):
        raise RuntimeError("update-safety endpoint is not an owned socket")

    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(0.1)
    try:
        probe.connect(str(endpoint))
    except ConnectionRefusedError:
        current = endpoint.lstat()
        if (
            current.st_dev != metadata.st_dev
            or current.st_ino != metadata.st_ino
            or not stat.S_ISSOCK(current.st_mode)
            or current.st_uid != os.getuid()
        ):
            raise RuntimeError("update-safety socket changed during recovery")
        endpoint.unlink()
    except FileNotFoundError:
        return
    except OSError as error:
        raise RuntimeError("update-safety endpoint is already active or unavailable") from error
    else:
        raise RuntimeError("update-safety endpoint is already active")
    finally:
        probe.close()


def _snapshot_payload(value: object) -> dict[str, object]:
    payload_method = getattr(value, "payload", None)
    if callable(payload_method):
        value = payload_method()
    if not isinstance(value, Mapping) or set(value) != _RESPONSE_FIELDS:
        raise RuntimeError("update-safety snapshot is malformed")
    payload = dict(value)
    if payload.get("schema_version") != 1 or payload.get("evidence_complete") is not True:
        raise RuntimeError("update-safety snapshot is incomplete")
    for field_name in (
        "active_tasks",
        "pending_approvals",
        "pending_clarifications",
    ):
        count = payload.get(field_name)
        if type(count) is not int or not 0 <= count <= 1_000_000:
            raise RuntimeError("update-safety snapshot count is invalid")
    for field_name, maximum in (("profile", 128), ("runtime_generation", 256)):
        text = payload.get(field_name)
        if (
            not isinstance(text, str)
            or not text
            or text != text.strip()
            or len(text) > maximum
            or any(ord(character) < 32 or ord(character) == 127 for character in text)
        ):
            raise RuntimeError("update-safety snapshot identity is invalid")
    return payload


def _encode_snapshot(value: object) -> bytes:
    encoded = (
        json.dumps(
            _snapshot_payload(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )
    if len(encoded) > _MAX_RESPONSE_BYTES:
        raise RuntimeError("update-safety response is oversized")
    return encoded


class MacOSUpdateSafetyRelay:
    """Serve one body-free, aggregate-only snapshot method over a private UDS."""

    def __init__(
        self,
        snapshot_provider: Callable[[], object],
        *,
        socket_path: Path | None = None,
    ) -> None:
        if not callable(snapshot_provider):
            raise TypeError("snapshot_provider must be callable")
        self._snapshot_provider = snapshot_provider
        self._endpoint = socket_path or resolve_update_safety_socket_path()
        self._listener: socket.socket | None = None
        self._socket_identity: tuple[int, int] | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.RLock()

    @property
    def endpoint(self) -> Path:
        return self._endpoint

    def start(self) -> MacOSUpdateSafetyRelay:
        with self._lock:
            if self._listener is not None:
                return self
            parent_identity = _private_directory(self._endpoint.parent)
            _remove_stale_owned_socket(self._endpoint)
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                listener.bind(str(self._endpoint))
                os.chmod(self._endpoint, 0o600, follow_symlinks=False)
                listener.listen(4)
                listener.settimeout(_ACCEPT_TIMEOUT_S)
                socket_metadata = self._endpoint.lstat()
                parent_metadata = self._endpoint.parent.lstat()
                if (
                    not stat.S_ISSOCK(socket_metadata.st_mode)
                    or socket_metadata.st_uid != os.getuid()
                    or stat.S_IMODE(socket_metadata.st_mode) & 0o077
                    or (parent_metadata.st_dev, parent_metadata.st_ino)
                    != parent_identity
                ):
                    raise RuntimeError("update-safety endpoint trust changed during bind")
            except BaseException:
                listener.close()
                try:
                    self._endpoint.unlink()
                except FileNotFoundError:
                    pass
                raise
            self._listener = listener
            self._socket_identity = (socket_metadata.st_dev, socket_metadata.st_ino)
            self._stop.clear()
            thread = threading.Thread(
                target=self._serve,
                name="hermes-update-safety-relay",
                daemon=True,
            )
            self._thread = thread
            thread.start()
            return self

    def close(self) -> None:
        with self._lock:
            listener = self._listener
            thread = self._thread
            identity = self._socket_identity
            if listener is None and thread is None:
                self._unlink_if_owned(identity)
                return
            self._listener = None
            self._thread = None
            self._socket_identity = None
            self._stop.set()
            if listener is not None:
                listener.close()
        if thread is not None:
            thread.join(_CLOSE_TIMEOUT_S)
            if thread.is_alive():
                raise RuntimeError("update-safety relay did not stop")
        self._unlink_if_owned(identity)

    def _unlink_if_owned(self, identity: tuple[int, int] | None) -> None:
        if identity is None:
            return
        try:
            metadata = self._endpoint.lstat()
        except FileNotFoundError:
            return
        except OSError:
            return
        if (
            stat.S_ISSOCK(metadata.st_mode)
            and metadata.st_uid == os.getuid()
            and (metadata.st_dev, metadata.st_ino) == identity
        ):
            try:
                self._endpoint.unlink()
            except OSError:
                return

    def _serve(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                listener = self._listener
            if listener is None:
                return
            try:
                connection, _address = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                if self._stop.is_set():
                    return
                continue
            with connection:
                connection.settimeout(_CONNECTION_TIMEOUT_S)
                self._serve_connection(connection)

    def _serve_connection(self, connection: socket.socket) -> None:
        getpeereid = getattr(connection, "getpeereid", None)
        if callable(getpeereid):
            try:
                peer_uid, _peer_gid = getpeereid()
            except OSError:
                return
            if peer_uid != os.getuid():
                return
        try:
            request = self._read_request(connection)
            if request != _REQUEST:
                raise ValueError("unsupported update-safety request")
            response = _encode_snapshot(self._snapshot_provider())
        except BaseException:  # noqa: BLE001
            response = _ERROR_RESPONSE
        try:
            connection.sendall(response)
        except OSError:
            return

    @staticmethod
    def _read_request(connection: socket.socket) -> dict[str, Any]:
        body = bytearray()
        while b"\n" not in body:
            chunk = connection.recv(256)
            if not chunk:
                raise ValueError("update-safety request is incomplete")
            body.extend(chunk)
            if len(body) > _MAX_REQUEST_BYTES:
                raise ValueError("update-safety request is oversized")
        frame, separator, trailing = bytes(body).partition(b"\n")
        if separator != b"\n" or trailing:
            raise ValueError("update-safety request framing is invalid")
        value = json.loads(frame.decode("utf-8"))
        if not isinstance(value, dict) or set(value) != set(_REQUEST):
            raise ValueError("update-safety request schema is invalid")
        return value



def start_update_safety_relay(
    snapshot_provider: Callable[[], object],
    *,
    socket_path: Path | None = None,
) -> MacOSUpdateSafetyRelay:
    return MacOSUpdateSafetyRelay(
        snapshot_provider,
        socket_path=socket_path,
    ).start()


__all__ = [
    "MacOSUpdateSafetyRelay",
    "resolve_update_safety_socket_path",
    "start_update_safety_relay",
]
