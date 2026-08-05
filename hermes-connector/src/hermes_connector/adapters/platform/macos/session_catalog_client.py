from __future__ import annotations

import asyncio
import math
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from typing import Protocol
from uuid import UUID, uuid4

from websockets.asyncio.client import unix_connect

from hermes_connector.adapters.cloud.codec import ConnectorProtocolCodec
from hermes_connector.adapters.platform.macos.observer_client import (
    MAX_OBSERVER_FRAME_BYTES,
    ObserverEndpointUnavailable,
    ObserverProtocolError,
    _bounded_websocket_close,
    _connected_peer_pid,
    _decode_frame,
    _encode_frame,
    _endpoint_matches_authority,
    _observer_contract,
    _ready_payload,
    _same_authority_identity,
    _same_socket_identity,
    _validate_ready_identity,
)
from hermes_connector.adapters.platform.macos.observer_discovery import ObserverEndpoint
from hermes_connector.adapters.platform.macos.process_identity import (
    ProcessIdentityProvider,
    current_process_identity,
    normalize_process_identity,
)
from hermes_connector.domain.identifiers import canonical_uuid
from hermes_connector.domain.local_gateway import LocalRuntimeAuthority
from hermes_connector.domain.session_catalog import (
    LocalSessionCatalogPage,
    SessionCatalogEvent,
    SessionCatalogResnapshotRequired,
)

MAX_CATALOG_EVENT_BUFFER = 1_024
MAX_CATALOG_PAGE_SIZE = 128
_CATALOG_CAPABILITY = "session.catalog.v1"
_PROFILE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


class SessionCatalogProtocolError(ObserverProtocolError):
    """The local peer violated the frozen catalog RPC contract."""


class _Discovery(Protocol):
    async def discover(self, profile: str) -> tuple[ObserverEndpoint, ...]: ...

    async def aclose(self) -> None: ...


class _WebSocket(Protocol):
    async def recv(self) -> str | bytes: ...

    async def send(self, frame: str | bytes) -> None: ...

    async def close(self) -> None: ...


AuthorityProvider = Callable[[], Awaitable[LocalRuntimeAuthority | None]]
WebSocketConnector = Callable[..., Awaitable[_WebSocket]]
SocketIdentityProvider = Callable[[ObserverEndpoint], bool]
RequestIdFactory = Callable[[], UUID | str]


class MacOSSessionCatalogClient:
    """Open one identity-bound persistent Observer catalog subscription."""

    def __init__(
        self,
        *,
        discovery: _Discovery,
        authority: AuthorityProvider,
        connect: WebSocketConnector = unix_connect,
        rpc_timeout_seconds: float = 5.0,
        authority_poll_seconds: float = 0.25,
        process_identity_provider: ProcessIdentityProvider | None = None,
        socket_identity_provider: SocketIdentityProvider | None = None,
        request_id_factory: RequestIdFactory = uuid4,
    ) -> None:
        if (
            not isinstance(rpc_timeout_seconds, int | float)
            or isinstance(rpc_timeout_seconds, bool)
            or not math.isfinite(rpc_timeout_seconds)
            or rpc_timeout_seconds <= 0
        ):
            raise ValueError("catalog RPC timeout must be a finite positive number")
        if not math.isfinite(authority_poll_seconds) or authority_poll_seconds <= 0:
            raise ValueError("catalog authority poll interval must be positive")
        self._discovery = discovery
        self._authority = authority
        self._connect = connect
        self._rpc_timeout_seconds = rpc_timeout_seconds
        self._authority_poll_seconds = authority_poll_seconds
        self._process_identity_provider = (
            process_identity_provider or current_process_identity
        )
        self._socket_identity_provider = (
            socket_identity_provider or _same_socket_identity
        )
        self._request_id_factory = request_id_factory
        self._codec = ConnectorProtocolCodec()

    async def subscribe(
        self,
        *,
        profile: str,
        runtime_generation: str,
        page_size: int = MAX_CATALOG_PAGE_SIZE,
    ) -> MacOSSessionCatalogSubscription:
        if not isinstance(profile, str) or _PROFILE.fullmatch(profile) is None:
            raise ValueError("catalog profile is invalid")
        if not isinstance(runtime_generation, str) or not 1 <= len(runtime_generation) <= 128:
            raise ValueError("catalog runtime generation is invalid")
        if type(page_size) is not int or not 1 <= page_size <= MAX_CATALOG_PAGE_SIZE:
            raise ValueError("catalog page size is outside contract bounds")
        deadline = asyncio.get_running_loop().time() + self._rpc_timeout_seconds
        websocket: _WebSocket | None = None
        try:
            async with asyncio.timeout_at(deadline):
                authority = await _require_catalog_authority(
                    self._authority,
                    profile=profile,
                    runtime_generation=runtime_generation,
                )
                endpoints = await self._discovery.discover(profile)
                if len(endpoints) != 1:
                    raise ObserverEndpointUnavailable(
                        "exactly one trusted Observer endpoint is required"
                    )
                endpoint = endpoints[0]
                if not _endpoint_matches_authority(endpoint, authority):
                    raise SessionCatalogResnapshotRequired(
                        "catalog descriptor runtime authority changed"
                    )
                self._require_endpoint_identity(endpoint)
                websocket = await self._connect(
                    str(endpoint.socket_path),
                    uri="ws://localhost/observer",
                    open_timeout=max(0.001, deadline - asyncio.get_running_loop().time()),
                    close_timeout=1.0,
                    max_size=MAX_OBSERVER_FRAME_BYTES,
                    max_queue=32,
                )
                if _connected_peer_pid(websocket) != endpoint.pid:
                    raise ObserverEndpointUnavailable(
                        "Observer peer does not match descriptor publisher"
                    )
                self._require_endpoint_identity(endpoint)
                observer_contract = _observer_contract(authority)
                await _await_ready(
                    websocket,
                    profile=profile,
                    authority=authority,
                    observer_contract=observer_contract,
                )
                request_id = _request_id(self._request_id_factory)
                await websocket.send(
                    _encode_frame(
                        {
                            "jsonrpc": "2.0",
                            "id": request_id,
                            "method": "session.catalog.subscribe",
                            "params": {
                                "profile": profile,
                                "runtime_generation": runtime_generation,
                                "page_size": page_size,
                            },
                        }
                    )
                )
                pending: list[Mapping[str, object]] = []
                response = await _await_catalog_response(
                    websocket,
                    request_id=request_id,
                    pending=pending,
                )
                page = _local_page(
                    self._codec,
                    response,
                    profile=profile,
                    runtime_generation=runtime_generation,
                    page_index=0,
                )
                current = await _require_catalog_authority(
                    self._authority,
                    profile=profile,
                    runtime_generation=runtime_generation,
                    expected=authority,
                )
                return MacOSSessionCatalogSubscription(
                    websocket=websocket,
                    first_page=page,
                    pending_frames=pending,
                    authority=self._authority,
                    expected_authority=current,
                    codec=self._codec,
                    request_id_factory=self._request_id_factory,
                    authority_poll_seconds=self._authority_poll_seconds,
                    close_timeout_seconds=min(self._rpc_timeout_seconds, 1.0),
                )
        except BaseException:
            if websocket is not None:
                await _bounded_websocket_close(
                    websocket,
                    timeout_seconds=min(self._rpc_timeout_seconds, 1.0),
                )
            raise

    def _require_endpoint_identity(self, endpoint: ObserverEndpoint) -> None:
        if not self._socket_identity_provider(endpoint):
            raise ObserverEndpointUnavailable("Observer socket identity changed")
        try:
            observed = self._process_identity_provider(endpoint.pid)
        except BaseException:  # noqa: BLE001 - platform evidence boundary
            observed = None
        if normalize_process_identity(observed) != endpoint.process_identity:
            raise ObserverEndpointUnavailable("Observer process identity changed")

    async def aclose(self) -> None:
        await self._discovery.aclose()


class MacOSSessionCatalogSubscription:
    def __init__(
        self,
        *,
        websocket: _WebSocket,
        first_page: LocalSessionCatalogPage,
        pending_frames: list[Mapping[str, object]],
        authority: AuthorityProvider,
        expected_authority: LocalRuntimeAuthority,
        codec: ConnectorProtocolCodec,
        request_id_factory: RequestIdFactory,
        authority_poll_seconds: float,
        close_timeout_seconds: float,
    ) -> None:
        self._websocket = websocket
        self._first_page = first_page
        self._pending_frames = pending_frames
        self._authority = authority
        self._expected_authority = expected_authority
        self._codec = codec
        self._request_id_factory = request_id_factory
        self._authority_poll_seconds = authority_poll_seconds
        self._close_timeout_seconds = close_timeout_seconds
        self._pages_started = False
        self._pages_complete = False
        self._events_started = False
        self._closed = False
        self._terminated = False
        self._close_lock = asyncio.Lock()

    async def pages(self) -> AsyncIterator[LocalSessionCatalogPage]:
        if self._pages_started:
            raise RuntimeError("catalog pages allow one consumer")
        self._pages_started = True
        page = self._first_page
        try:
            while True:
                await self._ensure_authority()
                yield page
                if page.is_last:
                    self._pages_complete = True
                    return
                if page.next_cursor is None:
                    raise SessionCatalogProtocolError(
                        "non-final catalog page is missing its cursor"
                    )
                request_id = _request_id(self._request_id_factory)
                await self._websocket.send(
                    _encode_frame(
                        {
                            "jsonrpc": "2.0",
                            "id": request_id,
                            "method": "session.catalog.page",
                            "params": {
                                "subscription_id": str(page.subscription_id),
                                "snapshot_id": str(page.snapshot_id),
                                "page_index": page.page_index + 1,
                                "cursor": page.next_cursor,
                            },
                        }
                    )
                )
                response = await self._await_response(request_id)
                next_page = _local_page(
                    self._codec,
                    response,
                    profile=page.profile,
                    runtime_generation=page.runtime_generation,
                    page_index=page.page_index + 1,
                )
                if (
                    next_page.subscription_id != page.subscription_id
                    or next_page.snapshot_id != page.snapshot_id
                    or next_page.catalog_revision != page.catalog_revision
                ):
                    raise SessionCatalogResnapshotRequired(
                        "catalog page snapshot identity changed"
                    )
                page = next_page
        except BaseException:
            await self._abort()
            raise

    async def events(self) -> AsyncIterator[SessionCatalogEvent]:
        if not self._pages_complete:
            raise RuntimeError("catalog pages must complete before events")
        if self._events_started:
            raise RuntimeError("catalog events allow one consumer")
        self._events_started = True
        expected_sequence = self._first_page.catalog_revision + 1
        try:
            while not self._terminated:
                await self._ensure_authority()
                if self._pending_frames:
                    frame = self._pending_frames.pop(0)
                else:
                    frame = await self._receive_frame()
                event = _catalog_event(
                    self._codec,
                    frame,
                    subscription_id=self._first_page.subscription_id,
                    profile=self._first_page.profile,
                    runtime_generation=self._first_page.runtime_generation,
                )
                if event.catalog_sequence != expected_sequence:
                    raise SessionCatalogResnapshotRequired(
                        "catalog event sequence gap"
                    )
                expected_sequence += 1
                yield event
        except BaseException:
            await self._abort()
            raise

    async def close(self) -> None:
        self._terminated = True
        async with self._close_lock:
            if self._closed:
                return
            request_id = _request_id(self._request_id_factory)
            try:
                async with asyncio.timeout(self._close_timeout_seconds):
                    await self._websocket.send(
                        _encode_frame(
                            {
                                "jsonrpc": "2.0",
                                "id": request_id,
                                "method": "session.catalog.unsubscribe",
                                "params": {
                                    "subscription_id": str(
                                        self._first_page.subscription_id
                                    )
                                },
                            }
                        )
                    )
            except (ConnectionError, OSError, TimeoutError):
                pass
            self._closed = await _bounded_websocket_close(
                self._websocket,
                timeout_seconds=self._close_timeout_seconds,
            )

    async def _await_response(self, request_id: str) -> Mapping[str, object]:
        while True:
            frame = await self._receive_frame()
            if frame.get("id") == request_id:
                return _response_result(frame)
            _buffer_notification(frame, self._pending_frames)

    async def _receive_frame(self) -> Mapping[str, object]:
        receive = asyncio.create_task(self._websocket.recv())
        try:
            while True:
                done, _ = await asyncio.wait(
                    {receive}, timeout=self._authority_poll_seconds
                )
                if done:
                    return _decode_frame(receive.result())
                await self._ensure_authority()
        finally:
            if not receive.done():
                receive.cancel()
                await asyncio.gather(receive, return_exceptions=True)

    async def _ensure_authority(self) -> None:
        await _require_catalog_authority(
            self._authority,
            profile=self._first_page.profile,
            runtime_generation=self._first_page.runtime_generation,
            expected=self._expected_authority,
        )

    async def _abort(self) -> None:
        self._terminated = True
        async with self._close_lock:
            if self._closed:
                return
            self._closed = await _bounded_websocket_close(
                self._websocket,
                timeout_seconds=self._close_timeout_seconds,
            )


async def _require_catalog_authority(
    provider: AuthorityProvider,
    *,
    profile: str,
    runtime_generation: str,
    expected: LocalRuntimeAuthority | None = None,
) -> LocalRuntimeAuthority:
    authority = await provider()
    capabilities = (
        {*authority.required_capabilities, *authority.optional_capabilities}
        if authority is not None
        else set()
    )
    if (
        authority is None
        or authority.profile != profile
        or authority.runtime_generation != runtime_generation
        or _CATALOG_CAPABILITY not in capabilities
    ):
        raise SessionCatalogResnapshotRequired(
            "catalog runtime authority is unavailable"
        )
    if expected is not None and not _same_authority_identity(authority, expected):
        raise SessionCatalogResnapshotRequired("catalog runtime authority changed")
    return authority


async def _await_ready(
    websocket: _WebSocket,
    *,
    profile: str,
    authority: LocalRuntimeAuthority,
    observer_contract: int,
) -> None:
    while True:
        frame = _decode_frame(await websocket.recv())
        ready = _ready_payload(frame, observer_contract=observer_contract)
        if ready is None:
            continue
        if observer_contract == 1:
            _validate_ready_identity(
                ready,
                profile=profile,
                runtime_generation=authority.runtime_generation,
                instance_id=authority.instance_id,
            )
        return


async def _await_catalog_response(
    websocket: _WebSocket,
    *,
    request_id: str,
    pending: list[Mapping[str, object]],
) -> Mapping[str, object]:
    while True:
        frame = _decode_frame(await websocket.recv())
        if frame.get("id") == request_id:
            return _response_result(frame)
        _buffer_notification(frame, pending)


def _response_result(frame: Mapping[str, object]) -> Mapping[str, object]:
    if frame.get("jsonrpc") != "2.0" or set(frame) not in (
        {"jsonrpc", "id", "result"},
        {"jsonrpc", "id", "error"},
    ):
        raise SessionCatalogProtocolError("catalog response envelope is invalid")
    if "error" in frame:
        error = frame["error"]
        if (
            isinstance(error, dict)
            and set(error) == {"code", "message", "reason"}
            and error.get("code") == 4400
            and error.get("message") == "session catalog reset required"
        ):
            raise SessionCatalogResnapshotRequired("catalog reset required")
        raise SessionCatalogProtocolError("catalog RPC error is invalid")
    result = frame.get("result")
    if not isinstance(result, dict):
        raise SessionCatalogProtocolError("catalog response result is invalid")
    return result


def _buffer_notification(
    frame: Mapping[str, object],
    pending: list[Mapping[str, object]],
) -> None:
    if frame.get("jsonrpc") != "2.0" or set(frame) != {
        "jsonrpc",
        "method",
        "params",
    }:
        raise SessionCatalogProtocolError("catalog notification is invalid")
    if frame.get("method") == "session.catalog.reset_required":
        raise SessionCatalogResnapshotRequired("catalog reset required")
    if frame.get("method") != "session.catalog.event":
        raise SessionCatalogProtocolError("catalog notification method is invalid")
    if len(pending) >= MAX_CATALOG_EVENT_BUFFER:
        raise SessionCatalogResnapshotRequired("catalog event buffer overflow")
    pending.append(frame)


def _local_page(
    codec: ConnectorProtocolCodec,
    result: Mapping[str, object],
    *,
    profile: str,
    runtime_generation: str,
    page_index: int,
) -> LocalSessionCatalogPage:
    required = {
        "subscription_id",
        "snapshot_id",
        "profile",
        "runtime_generation",
        "catalog_revision",
        "page_index",
        "is_last",
        "sessions",
        "next_cursor",
    }
    if set(result) != required:
        raise SessionCatalogProtocolError("catalog page shape is invalid")
    payload = {key: value for key, value in result.items() if key not in {"subscription_id", "next_cursor"}}
    try:
        page = codec.decode_session_catalog_snapshot_page_payload(payload)
        subscription_id = canonical_uuid(result["subscription_id"])
    except (TypeError, ValueError) as error:
        raise SessionCatalogProtocolError("catalog page contract is invalid") from error
    next_cursor = result["next_cursor"]
    if next_cursor is not None and (
        not isinstance(next_cursor, str) or not 1 <= len(next_cursor) <= 512
    ):
        raise SessionCatalogProtocolError("catalog page cursor is invalid")
    if (
        page.profile != profile
        or page.runtime_generation != runtime_generation
        or page.page_index != page_index
        or (page.is_last and next_cursor is not None)
        or (not page.is_last and (not page.sessions or next_cursor is None))
    ):
        raise SessionCatalogResnapshotRequired("catalog page authority changed")
    return LocalSessionCatalogPage(
        subscription_id=subscription_id,
        snapshot_id=page.snapshot_id,
        profile=page.profile,
        runtime_generation=page.runtime_generation,
        catalog_revision=page.catalog_revision,
        page_index=page.page_index,
        is_last=page.is_last,
        sessions=page.sessions,
        next_cursor=next_cursor,
    )


def _catalog_event(
    codec: ConnectorProtocolCodec,
    frame: Mapping[str, object],
    *,
    subscription_id: UUID,
    profile: str,
    runtime_generation: str,
) -> SessionCatalogEvent:
    if frame.get("method") == "session.catalog.reset_required":
        raise SessionCatalogResnapshotRequired("catalog reset required")
    if frame.get("jsonrpc") != "2.0" or frame.get("method") != "session.catalog.event":
        raise SessionCatalogProtocolError("catalog event envelope is invalid")
    params = frame.get("params")
    if not isinstance(params, dict) or set(params) != {
        "subscription_id",
        "profile",
        "runtime_generation",
        "catalog_sequence",
        "action",
        "entry",
    }:
        raise SessionCatalogProtocolError("catalog event shape is invalid")
    try:
        observed_subscription = canonical_uuid(params["subscription_id"])
        event = codec.decode_session_catalog_event_payload(
            {key: value for key, value in params.items() if key != "subscription_id"}
        )
    except (TypeError, ValueError) as error:
        raise SessionCatalogProtocolError("catalog event contract is invalid") from error
    if (
        observed_subscription != subscription_id
        or event.profile != profile
        or event.runtime_generation != runtime_generation
    ):
        raise SessionCatalogResnapshotRequired("catalog event authority changed")
    return event


def _request_id(factory: RequestIdFactory) -> str:
    return str(canonical_uuid(factory()))


__all__ = [
    "MacOSSessionCatalogClient",
    "MacOSSessionCatalogSubscription",
    "SessionCatalogProtocolError",
    "SessionCatalogResnapshotRequired",
]
