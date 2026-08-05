"""Private length-prefixed Unix socket bridge for owner-control RPC."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import stat
import struct
from collections.abc import Callable, Mapping
from contextlib import suppress
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID, uuid4

from hermes_cloud.domain.connector_gateway import ConnectorIdentity
from hermes_cloud.modules.control.broker import OwnerControlBroker
from hermes_cloud.modules.control.domain import (
    ControlConnectorRoute,
    ControlRequestContext,
)
from hermes_cloud.modules.control.ports import ControlRouteResolverPort

_HEADER_BYTES = 4


class OwnerControlBridgeProtocolError(RuntimeError):
    pass


class OwnerControlBridgeUnavailable(ConnectionError):
    pass


class OwnerControlBridgeBeforeEffect(OwnerControlBridgeUnavailable):
    pass


class OwnerControlBridgeHandler(Protocol):
    async def handle_bridge_request(
        self,
        *,
        peer_id: str,
        route: ControlConnectorRoute,
        payload: Mapping[str, object],
    ) -> Mapping[str, object]: ...

    async def bridge_disconnected(self, *, peer_id: str) -> None: ...


class OwnerControlBridgeServer:
    """Serve a same-UID private UDS without public HTTP exposure."""

    def __init__(
        self,
        *,
        socket_path: Path,
        handler: OwnerControlBridgeHandler,
        max_frame_bytes: int = 262_144,
        max_in_flight: int = 64,
        peer_id_factory: Callable[[], UUID] = uuid4,
        uid_provider: Callable[[], int] = os.geteuid,
    ) -> None:
        _validate_limits(max_frame_bytes, max_in_flight)
        self._socket_path = socket_path
        self._handler = handler
        self._max_frame_bytes = max_frame_bytes
        self._max_in_flight = max_in_flight
        self._parallel = asyncio.Semaphore(max_in_flight)
        self._peer_id_factory = peer_id_factory
        self._uid_provider = uid_provider
        self._server: asyncio.AbstractServer | None = None
        self._socket_identity: tuple[int, int] | None = None
        self._connections: set[asyncio.Task[None]] = set()
        self._request_tasks: set[asyncio.Task[None]] = set()

    async def start(self) -> None:
        if self._server is not None:
            return
        _validate_private_directory(
            self._socket_path.parent,
            expected_uid=self._uid_provider(),
        )
        _prepare_socket_path(
            self._socket_path,
            expected_uid=self._uid_provider(),
        )
        self._server = await asyncio.start_unix_server(
            self._accept,
            path=self._socket_path,
        )
        os.chmod(self._socket_path, 0o600)
        metadata = self._socket_path.lstat()
        self._socket_identity = (metadata.st_dev, metadata.st_ino)

    async def stop(self) -> None:
        server = self._server
        self._server = None
        if server is not None:
            server.close()
        tasks = tuple(self._connections)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._connections.clear()
        self._request_tasks.clear()
        if server is not None:
            await server.wait_closed()
        _unlink_exact_socket(
            self._socket_path,
            expected_identity=self._socket_identity,
        )
        self._socket_identity = None

    async def _accept(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        task = asyncio.current_task()
        assert task is not None
        self._connections.add(task)
        peer_id = str(self._peer_id_factory())
        requests: set[asyncio.Task[None]] = set()
        write_lock = asyncio.Lock()
        try:
            if _peer_uid(writer) != self._uid_provider():
                return
            while True:
                await self._parallel.acquire()
                try:
                    frame = await _read_frame(reader, self._max_frame_bytes)
                    request = _bridge_request(frame)
                except BaseException:
                    self._parallel.release()
                    raise
                request_task = asyncio.create_task(
                    self._dispatch(
                        request,
                        peer_id=peer_id,
                        writer=writer,
                        write_lock=write_lock,
                    )
                )
                requests.add(request_task)
                self._request_tasks.add(request_task)

                def release_request_slot(
                    completed: asyncio.Task[None],
                ) -> None:
                    requests.discard(completed)
                    self._request_tasks.discard(completed)
                    self._parallel.release()

                request_task.add_done_callback(release_request_slot)
        except asyncio.CancelledError:
            raise
        except (
            asyncio.IncompleteReadError,
            ConnectionError,
            OSError,
            OwnerControlBridgeProtocolError,
        ):
            pass
        finally:
            for request_task in tuple(requests):
                request_task.cancel()
            if requests:
                await asyncio.gather(*requests, return_exceptions=True)
            with suppress(Exception):
                await self._handler.bridge_disconnected(peer_id=peer_id)
            writer.close()
            with suppress(ConnectionError, OSError):
                await writer.wait_closed()
            self._connections.discard(task)

    async def _dispatch(
        self,
        request: dict[str, object],
        *,
        peer_id: str,
        writer: asyncio.StreamWriter,
        write_lock: asyncio.Lock,
    ) -> None:
        route_value = request["route"]
        assert isinstance(route_value, dict)
        route = ControlConnectorRoute(
            tenant_id=str(route_value["tenant_id"]),
            device_id=str(route_value["device_id"]),
        )
        payload = request["payload"]
        assert isinstance(payload, dict)
        response = await self._handler.handle_bridge_request(
            peer_id=peer_id,
            route=route,
            payload=payload,
        )
        frame = _encoded_frame(
            {
                "bridge_version": 1,
                "kind": "control.response",
                "payload": dict(response),
            },
            self._max_frame_bytes,
        )
        async with write_lock:
            writer.write(frame)
            await writer.drain()

    def snapshot(self) -> dict[str, int]:
        """Return bounded counts only; never expose request payloads."""

        return {
            "active_connections": len(self._connections),
            "active_request_tasks": len(self._request_tasks),
            "max_in_flight": self._max_in_flight,
        }


class OwnerControlBridgeClient:
    """Multiplex owner-control calls over one reconnectable private UDS."""

    def __init__(
        self,
        *,
        socket_path: Path,
        max_frame_bytes: int = 262_144,
        max_in_flight: int = 64,
        request_timeout_seconds: float = 3.0,
        uid_provider: Callable[[], int] = os.geteuid,
    ) -> None:
        _validate_limits(max_frame_bytes, max_in_flight)
        if request_timeout_seconds <= 0:
            raise ValueError("bridge request timeout must be positive")
        self._socket_path = socket_path
        self._max_frame_bytes = max_frame_bytes
        self._request_timeout_seconds = request_timeout_seconds
        self._uid_provider = uid_provider
        self._parallel = asyncio.Semaphore(max_in_flight)
        self._connect_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._pending: dict[str, asyncio.Future[dict[str, object]]] = {}

    async def exchange(
        self,
        *,
        route: ControlConnectorRoute,
        payload: Mapping[str, object],
    ) -> Mapping[str, object]:
        request_id = payload.get("request_id")
        if not isinstance(request_id, str):
            raise OwnerControlBridgeProtocolError("owner-control request id is invalid")
        frame = _encoded_frame(
            {
                "bridge_version": 1,
                "kind": "control.request",
                "route": {
                    "tenant_id": route.tenant_id,
                    "device_id": route.device_id,
                },
                "payload": dict(payload),
            },
            self._max_frame_bytes,
        )
        async with self._parallel:
            await self._ensure_connection()
            if request_id in self._pending:
                raise OwnerControlBridgeProtocolError(
                    "duplicate in-flight bridge request"
                )
            future = asyncio.get_running_loop().create_future()
            self._pending[request_id] = future
            try:
                writer = self._writer
                if writer is None:
                    raise OwnerControlBridgeUnavailable(
                        "owner-control bridge is unavailable"
                    )
                async with self._write_lock:
                    writer.write(frame)
                    await writer.drain()
                return await asyncio.wait_for(
                    asyncio.shield(future),
                    timeout=self._request_timeout_seconds,
                )
            except TimeoutError:
                raise
            except asyncio.CancelledError:
                raise
            except (ConnectionError, OSError) as error:
                raise OwnerControlBridgeUnavailable(
                    "owner-control bridge is unavailable"
                ) from error
            finally:
                self._pending.pop(request_id, None)

    async def close(self) -> None:
        task = self._reader_task
        self._reader_task = None
        writer = self._writer
        self._reader = None
        self._writer = None
        if writer is not None:
            writer.close()
            with suppress(ConnectionError, OSError):
                await writer.wait_closed()
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            with suppress(asyncio.CancelledError, ConnectionError, OSError):
                await task
        self._fail_pending()

    async def _ensure_connection(self) -> None:
        if self._writer is not None and not self._writer.is_closing():
            return
        async with self._connect_lock:
            if self._writer is not None and not self._writer.is_closing():
                return
            _validate_private_socket(
                self._socket_path,
                expected_uid=self._uid_provider(),
            )
            try:
                reader, writer = await asyncio.open_unix_connection(self._socket_path)
            except (ConnectionError, OSError) as error:
                raise OwnerControlBridgeBeforeEffect(
                    "owner-control bridge is unavailable"
                ) from error
            self._reader = reader
            self._writer = writer
            self._reader_task = asyncio.create_task(self._read_responses())

    async def _read_responses(self) -> None:
        try:
            reader = self._reader
            assert reader is not None
            while True:
                response = _bridge_response(
                    await _read_frame(reader, self._max_frame_bytes)
                )
                payload = response["payload"]
                assert isinstance(payload, dict)
                request_id = payload.get("request_id")
                future = (
                    self._pending.get(request_id)
                    if isinstance(request_id, str)
                    else None
                )
                if future is None or future.done():
                    raise OwnerControlBridgeProtocolError(
                        "unmatched owner-control bridge response"
                    )
                future.set_result(payload)
        except asyncio.CancelledError:
            raise
        except (
            asyncio.IncompleteReadError,
            ConnectionError,
            OSError,
            OwnerControlBridgeProtocolError,
        ):
            self._fail_pending()
            writer = self._writer
            self._reader = None
            self._writer = None
            if writer is not None:
                writer.close()

    def _fail_pending(self) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(
                    OwnerControlBridgeUnavailable("owner-control bridge disconnected")
                )


class BridgeControlRequestSender:
    """Adapt one route on the private bridge to the process-local broker."""

    def __init__(
        self,
        *,
        client: OwnerControlBridgeClient,
        broker: OwnerControlBroker,
        route: ControlConnectorRoute,
        broker_connection_id: str,
    ) -> None:
        self._client = client
        self._broker = broker
        self._route = route
        self._broker_connection_id = broker_connection_id

    async def send_control_request(
        self,
        request: Mapping[str, object],
    ) -> bool:
        try:
            response = await self._client.exchange(
                route=self._route,
                payload=request,
            )
        except OwnerControlBridgeBeforeEffect:
            return False
        accepted = await self._broker.accept_control_response(
            identity=ConnectorIdentity(
                tenant_id=self._route.tenant_id,
                device_id=self._route.device_id,
            ),
            connector_connection_id=self._broker_connection_id,
            response=response,
        )
        if not accepted:
            raise OwnerControlBridgeProtocolError(
                "bridge response did not match the local broker"
            )
        return True


class BridgeRegisteringRouteResolver:
    """Register the private bridge only after an authorized route resolves."""

    def __init__(
        self,
        *,
        delegate: ControlRouteResolverPort,
        broker: OwnerControlBroker,
        client: OwnerControlBridgeClient,
        broker_connection_id: str,
    ) -> None:
        self._delegate = delegate
        self._broker = broker
        self._client = client
        self._broker_connection_id = broker_connection_id

    async def resolve(
        self,
        context: ControlRequestContext,
    ) -> ControlConnectorRoute:
        route = await self._delegate.resolve(context)
        await self._broker.connector_connected(
            identity=ConnectorIdentity(route.tenant_id, route.device_id),
            connector_connection_id=self._broker_connection_id,
            sender=BridgeControlRequestSender(
                client=self._client,
                broker=self._broker,
                route=route,
                broker_connection_id=self._broker_connection_id,
            ),
        )
        return route


def _validate_limits(max_frame_bytes: int, max_in_flight: int) -> None:
    if type(max_frame_bytes) is not int or not 256 <= max_frame_bytes <= 262_144:
        raise ValueError("bridge frame limit is invalid")
    if type(max_in_flight) is not int or not 1 <= max_in_flight <= 256:
        raise ValueError("bridge concurrency limit is invalid")


def _validate_private_directory(path: Path, *, expected_uid: int) -> None:
    if not path.is_absolute():
        raise ValueError("bridge runtime directory must be absolute")
    try:
        metadata = path.lstat()
    except OSError:
        raise ValueError("bridge runtime directory is unavailable") from None
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != expected_uid
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise ValueError("bridge runtime directory permissions are unsafe")


def _prepare_socket_path(path: Path, *, expected_uid: int) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    except OSError:
        raise ValueError("bridge socket path is unavailable") from None
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISSOCK(metadata.st_mode)
        or metadata.st_uid != expected_uid
    ):
        raise ValueError("bridge socket path is unsafe")
    path.unlink()


def _validate_private_socket(path: Path, *, expected_uid: int) -> None:
    _validate_private_directory(path.parent, expected_uid=expected_uid)
    try:
        metadata = path.lstat()
    except OSError:
        raise OwnerControlBridgeBeforeEffect(
            "owner-control bridge is unavailable"
        ) from None
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISSOCK(metadata.st_mode)
        or metadata.st_uid != expected_uid
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise OwnerControlBridgeBeforeEffect("owner-control bridge is unavailable")


def _unlink_exact_socket(
    path: Path,
    *,
    expected_identity: tuple[int, int] | None,
) -> None:
    if expected_identity is None:
        return
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    except OSError:
        return
    if (
        stat.S_ISSOCK(metadata.st_mode)
        and (metadata.st_dev, metadata.st_ino) == expected_identity
    ):
        path.unlink()


def _peer_uid(writer: asyncio.StreamWriter) -> int:
    peer_socket = writer.get_extra_info("socket")
    if peer_socket is None:
        raise OwnerControlBridgeProtocolError("bridge peer is unavailable")
    if hasattr(socket, "SO_PEERCRED"):
        credentials = peer_socket.getsockopt(
            socket.SOL_SOCKET,
            socket.SO_PEERCRED,
            struct.calcsize("3i"),
        )
        _pid, uid, _gid = struct.unpack("3i", credentials)
        return int(uid)
    if hasattr(socket, "LOCAL_PEERCRED"):
        credentials = peer_socket.getsockopt(
            getattr(socket, "SOL_LOCAL", 0),
            socket.LOCAL_PEERCRED,
            12,
        )
        _version, uid = struct.unpack_from("II", credentials)
        return int(uid)
    getpeereid = getattr(peer_socket, "getpeereid", None)
    if callable(getpeereid):
        uid, _gid = getpeereid()
        return int(uid)
    raise OwnerControlBridgeProtocolError("bridge peer credentials are unavailable")


async def _read_frame(
    reader: asyncio.StreamReader,
    max_frame_bytes: int,
) -> dict[str, object]:
    header = await reader.readexactly(_HEADER_BYTES)
    length = int.from_bytes(header, "big")
    if not 1 <= length <= max_frame_bytes:
        raise OwnerControlBridgeProtocolError("bridge frame size is invalid")
    return _json_object(await reader.readexactly(length))


def _encoded_frame(value: Mapping[str, object], max_frame_bytes: int) -> bytes:
    try:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise OwnerControlBridgeProtocolError("bridge frame is invalid") from error
    if not 1 <= len(payload) <= max_frame_bytes:
        raise OwnerControlBridgeProtocolError("bridge frame size is invalid")
    return len(payload).to_bytes(_HEADER_BYTES, "big") + payload


def _json_object(payload: bytes) -> dict[str, object]:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise OwnerControlBridgeProtocolError(
                    "bridge frame contains duplicate keys"
                )
            value[key] = item
        return value

    def reject_constant(_value: str) -> None:
        raise OwnerControlBridgeProtocolError("bridge frame is invalid")

    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=unique,
            parse_constant=reject_constant,
        )
    except OwnerControlBridgeProtocolError:
        raise
    except (UnicodeError, ValueError, json.JSONDecodeError) as error:
        raise OwnerControlBridgeProtocolError("bridge frame is invalid") from error
    if not isinstance(value, dict):
        raise OwnerControlBridgeProtocolError("bridge frame is invalid")
    return value


def _bridge_request(value: dict[str, object]) -> dict[str, object]:
    if set(value) != {"bridge_version", "kind", "route", "payload"}:
        raise OwnerControlBridgeProtocolError("bridge request is invalid")
    route = value["route"]
    payload = value["payload"]
    if (
        value["bridge_version"] != 1
        or value["kind"] != "control.request"
        or not isinstance(route, dict)
        or set(route) != {"tenant_id", "device_id"}
        or not all(isinstance(member, str) and member for member in route.values())
        or not isinstance(payload, dict)
    ):
        raise OwnerControlBridgeProtocolError("bridge request is invalid")
    return value


def _bridge_response(value: dict[str, object]) -> dict[str, object]:
    if (
        set(value) != {"bridge_version", "kind", "payload"}
        or value["bridge_version"] != 1
        or value["kind"] != "control.response"
        or not isinstance(value["payload"], dict)
    ):
        raise OwnerControlBridgeProtocolError("bridge response is invalid")
    return value
