from __future__ import annotations

import asyncio
import math
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from uuid import UUID, uuid4

from hermes_connector.adapters.cloud.codec import ConnectorProtocolCodec
from hermes_connector.adapters.platform.macos.observer_client import (
    _observer_contract,
    _ready_payload,
    _same_authority_identity,
    _validate_ready_identity,
)
from hermes_connector.adapters.platform.macos.session_catalog_client import (
    MAX_CATALOG_PAGE_SIZE,
    SessionCatalogProtocolError,
    _buffer_notification,
    _catalog_event,
    _local_page,
    _request_id,
    _require_catalog_authority,
    _response_result,
)
from hermes_connector.domain.local_gateway import LocalRuntimeAuthority
from hermes_connector.domain.session_catalog import (
    LocalSessionCatalogPage,
    SessionCatalogEvent,
    SessionCatalogResnapshotRequired,
)

from .duplex_pipe import (
    WindowsAuthorityBoundDuplexPipe,
    WindowsDuplexPipeClosed,
    WindowsDuplexPipeProtocolError,
)

_PROFILE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
AuthorityProvider = Callable[[], Awaitable[LocalRuntimeAuthority | None]]
RequestIdFactory = Callable[[], UUID | str]


class WindowsSessionCatalogClient:
    """Open an authority-bound persistent Session Catalog subscription."""

    def __init__(
        self,
        *,
        authority: AuthorityProvider,
        connect_timeout_seconds: float = 1.5,
        rpc_timeout_seconds: float = 5.0,
        authority_poll_seconds: float = 0.25,
        request_id_factory: RequestIdFactory = uuid4,
    ) -> None:
        if connect_timeout_seconds <= 0:
            raise ValueError("catalog connect timeout must be positive")
        if (
            not isinstance(rpc_timeout_seconds, int | float)
            or isinstance(rpc_timeout_seconds, bool)
            or not math.isfinite(rpc_timeout_seconds)
            or rpc_timeout_seconds <= 0
        ):
            raise ValueError("catalog RPC timeout must be finite and positive")
        if not math.isfinite(authority_poll_seconds) or authority_poll_seconds <= 0:
            raise ValueError("catalog authority poll interval must be positive")
        self._authority = authority
        self._connect_timeout_seconds = connect_timeout_seconds
        self._rpc_timeout_seconds = float(rpc_timeout_seconds)
        self._authority_poll_seconds = authority_poll_seconds
        self._request_id_factory = request_id_factory
        self._codec = ConnectorProtocolCodec()

    async def subscribe(
        self,
        *,
        profile: str,
        runtime_generation: str,
        page_size: int = MAX_CATALOG_PAGE_SIZE,
    ) -> WindowsSessionCatalogSubscription:
        if not isinstance(profile, str) or _PROFILE.fullmatch(profile) is None:
            raise ValueError("catalog profile is invalid")
        if (
            not isinstance(runtime_generation, str)
            or not 1 <= len(runtime_generation) <= 128
            or runtime_generation != runtime_generation.strip()
        ):
            raise ValueError("catalog runtime generation is invalid")
        if type(page_size) is not int or not 1 <= page_size <= MAX_CATALOG_PAGE_SIZE:
            raise ValueError("catalog page size is outside contract bounds")
        authority = await _require_catalog_authority(
            self._authority,
            profile=profile,
            runtime_generation=runtime_generation,
        )
        stream: WindowsAuthorityBoundDuplexPipe | None = None
        try:
            async with asyncio.timeout(self._rpc_timeout_seconds):
                stream = await WindowsAuthorityBoundDuplexPipe.connect(
                    role="observer",
                    authority=authority,
                    timeout_seconds=self._connect_timeout_seconds,
                )
                observer_contract = _observer_contract(authority)
                await _await_ready(
                    stream,
                    profile=profile,
                    authority=authority,
                    observer_contract=observer_contract,
                )
                request_id = _request_id(self._request_id_factory)
                await stream.send(
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
                pending: list[Mapping[str, object]] = []
                response = await _await_catalog_response(
                    stream,
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
                return WindowsSessionCatalogSubscription(
                    stream=stream,
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
            if stream is not None:
                await _close_quietly(stream)
            raise

    async def aclose(self) -> None:
        return None


class WindowsSessionCatalogSubscription:
    def __init__(
        self,
        *,
        stream: WindowsAuthorityBoundDuplexPipe,
        first_page: LocalSessionCatalogPage,
        pending_frames: list[Mapping[str, object]],
        authority: AuthorityProvider,
        expected_authority: LocalRuntimeAuthority,
        codec: ConnectorProtocolCodec,
        request_id_factory: RequestIdFactory,
        authority_poll_seconds: float,
        close_timeout_seconds: float,
    ) -> None:
        self._stream = stream
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
                await self._stream.send(
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
                    await self._stream.send(
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
            except (
                ConnectionError,
                OSError,
                TimeoutError,
                WindowsDuplexPipeClosed,
                WindowsDuplexPipeProtocolError,
            ):
                pass
            await self._stream.close()
            self._closed = True

    async def _await_response(self, request_id: str) -> Mapping[str, object]:
        while True:
            frame = await self._receive_frame()
            if frame.get("id") == request_id:
                return _response_result(frame)
            _buffer_notification(frame, self._pending_frames)

    async def _receive_frame(self) -> Mapping[str, object]:
        receive = asyncio.create_task(self._stream.recv())
        try:
            while True:
                done, _pending = await asyncio.wait(
                    {receive},
                    timeout=self._authority_poll_seconds,
                )
                if done:
                    return receive.result()
                await self._ensure_authority()
        except BaseException:
            if not receive.done():
                await _close_quietly(self._stream)
            raise
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
            await _close_quietly(self._stream)
            self._closed = True


async def _await_ready(
    stream: WindowsAuthorityBoundDuplexPipe,
    *,
    profile: str,
    authority: LocalRuntimeAuthority,
    observer_contract: int,
) -> None:
    while True:
        ready = _ready_payload(
            await stream.recv(),
            observer_contract=observer_contract,
        )
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
    stream: WindowsAuthorityBoundDuplexPipe,
    *,
    request_id: str,
    pending: list[Mapping[str, object]],
) -> Mapping[str, object]:
    while True:
        frame = await stream.recv()
        if frame.get("id") == request_id:
            return _response_result(frame)
        _buffer_notification(frame, pending)


async def _close_quietly(stream: WindowsAuthorityBoundDuplexPipe) -> None:
    try:
        await stream.close()
    except (ConnectionError, OSError, RuntimeError):
        pass


__all__ = [
    "WindowsSessionCatalogClient",
    "WindowsSessionCatalogSubscription",
]
