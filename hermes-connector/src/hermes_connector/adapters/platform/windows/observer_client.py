from __future__ import annotations

import asyncio
import math
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping

from hermes_connector.adapters.cloud.codec import ConnectorProtocolCodec
from hermes_connector.adapters.platform.macos.observer_client import (
    MAX_PRE_SNAPSHOT_EVENTS,
    ObserverProtocolError,
    ObserverResnapshotRequired,
    _is_event_notification,
    _observer_contract,
    _ready_payload,
    _require_authority,
    _same_authority_identity,
    _session_event_from_frame,
    _snapshot_from_result,
    _subscribe_request,
    _unsubscribe_request,
    _validate_ready_identity,
)
from hermes_connector.application.observer_projection_v2 import (
    ObserverProjectionV2,
    ObserverProjectionV2Error,
)
from hermes_connector.application.observer_sequence import (
    ObserverSequenceError,
    ObserverSequenceGuard,
)
from hermes_connector.domain.identifiers import canonical_uuid
from hermes_connector.domain.local_gateway import LocalRuntimeAuthority
from hermes_connector.domain.observer import ObserverEvent, SessionEvent, SessionSnapshot

from .duplex_pipe import (
    WindowsAuthorityBoundDuplexPipe,
    WindowsDuplexPipeClosed,
    WindowsDuplexPipeProtocolError,
)

_PROFILE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
AuthorityProvider = Callable[[], Awaitable[LocalRuntimeAuthority | None]]


class WindowsObserverClient:
    """Open one authority-bound Observer subscription over a same-user Pipe."""

    def __init__(
        self,
        *,
        authority: AuthorityProvider,
        connect_timeout_seconds: float = 1.5,
        rpc_timeout_seconds: float = 5.0,
        authority_poll_seconds: float = 0.25,
    ) -> None:
        if connect_timeout_seconds <= 0:
            raise ValueError("Observer connect timeout must be positive")
        if (
            not isinstance(rpc_timeout_seconds, int | float)
            or isinstance(rpc_timeout_seconds, bool)
            or not math.isfinite(rpc_timeout_seconds)
            or rpc_timeout_seconds <= 0
        ):
            raise ValueError("Observer RPC timeout must be finite and positive")
        if not math.isfinite(authority_poll_seconds) or authority_poll_seconds <= 0:
            raise ValueError("Observer authority poll interval must be positive")
        self._authority = authority
        self._connect_timeout_seconds = connect_timeout_seconds
        self._rpc_timeout_seconds = float(rpc_timeout_seconds)
        self._authority_poll_seconds = authority_poll_seconds
        self._codec = ConnectorProtocolCodec()

    async def subscribe(
        self,
        *,
        profile: str,
        session_key: str,
    ) -> WindowsObserverSubscription:
        if not isinstance(profile, str) or _PROFILE.fullmatch(profile) is None:
            raise ValueError("Observer profile is invalid")
        if (
            not isinstance(session_key, str)
            or not 1 <= len(session_key) <= 256
            or session_key != session_key.strip()
            or "\x00" in session_key
        ):
            raise ValueError("Observer session_key is invalid")
        authority = await _require_authority(self._authority)
        if authority.profile != profile:
            raise ObserverResnapshotRequired("Observer profile authority changed")
        stream: WindowsAuthorityBoundDuplexPipe | None = None
        try:
            async with asyncio.timeout(self._rpc_timeout_seconds):
                stream = await WindowsAuthorityBoundDuplexPipe.connect(
                    role="observer",
                    authority=authority,
                    timeout_seconds=self._connect_timeout_seconds,
                )
                pending = await self._await_ready_and_subscribe(
                    stream,
                    profile=profile,
                    session_key=session_key,
                    authority=authority,
                )
                current = await _require_authority(
                    self._authority,
                    expected_authority=authority,
                )
                snapshot = _snapshot_from_result(
                    self._codec,
                    pending.result,
                    profile=profile,
                    runtime_generation=current.runtime_generation,
                    session_key=session_key,
                    observer_contract=_observer_contract(current),
                )
                if snapshot.observer_contract == 2:
                    guard = ObserverProjectionV2.from_snapshot(
                        snapshot,
                        expected_profile=profile,
                        expected_runtime_generation=current.runtime_generation,
                        expected_session_key=session_key,
                    )
                else:
                    guard = ObserverSequenceGuard.from_snapshot(
                        snapshot,
                        expected_profile=profile,
                        expected_runtime_generation=current.runtime_generation,
                        expected_session_key=session_key,
                    )
                return WindowsObserverSubscription(
                    stream=stream,
                    snapshot=snapshot,
                    subscription_id=pending.subscription_id,
                    pending_frames=pending.events,
                    authority=self._authority,
                    expected_authority=current,
                    codec=self._codec,
                    guard=guard,
                    authority_poll_seconds=self._authority_poll_seconds,
                    close_timeout_seconds=min(self._rpc_timeout_seconds, 1.0),
                    observer_contract=snapshot.observer_contract,
                )
        except BaseException:
            if stream is not None:
                await _close_quietly(stream)
            raise

    async def _await_ready_and_subscribe(
        self,
        stream: WindowsAuthorityBoundDuplexPipe,
        *,
        profile: str,
        session_key: str,
        authority: LocalRuntimeAuthority,
    ) -> _PreparedSubscription:
        runtime_generation = authority.runtime_generation
        observer_contract = _observer_contract(authority)
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
                    runtime_generation=runtime_generation,
                    instance_id=authority.instance_id,
                )
            break
        await _require_authority(
            self._authority,
            expected_authority=authority,
        )
        request = _subscribe_request(
            observer_contract=observer_contract,
            profile=profile,
            session_key=session_key,
            runtime_generation=runtime_generation,
        )
        await stream.send(request)
        subscribe_id = request["id"]
        pending: list[Mapping[str, object]] = []
        while True:
            frame = await stream.recv()
            if frame.get("id") != subscribe_id:
                if _is_event_notification(frame):
                    if len(pending) >= MAX_PRE_SNAPSHOT_EVENTS:
                        raise ObserverProtocolError(
                            "Observer pre-snapshot event queue exceeded its bound"
                        )
                    pending.append(frame)
                    continue
                raise ObserverProtocolError("Observer pre-snapshot frame is invalid")
            if frame.get("jsonrpc") != "2.0" or set(frame) not in (
                {"jsonrpc", "id", "result"},
                {"jsonrpc", "id", "error"},
            ):
                raise ObserverProtocolError(
                    "Observer subscribe response envelope is invalid"
                )
            if "error" in frame:
                raise ObserverProtocolError("Observer subscribe RPC was rejected")
            result = frame.get("result")
            if not isinstance(result, dict):
                raise ObserverProtocolError("Observer subscribe result is invalid")
            if result.get("runtime_generation") != runtime_generation:
                raise ObserverResnapshotRequired(
                    "Observer snapshot runtime authority changed"
                )
            try:
                subscription_id = str(canonical_uuid(result.get("subscription_id")))
            except (TypeError, ValueError):
                raise ObserverProtocolError("Observer subscription id is invalid") from None
            return _PreparedSubscription(
                result=result,
                subscription_id=subscription_id,
                events=tuple(pending),
            )

    async def aclose(self) -> None:
        return None


class WindowsObserverSubscription:
    def __init__(
        self,
        *,
        stream: WindowsAuthorityBoundDuplexPipe,
        snapshot: SessionSnapshot,
        subscription_id: str,
        pending_frames: tuple[Mapping[str, object], ...],
        authority: AuthorityProvider,
        expected_authority: LocalRuntimeAuthority,
        codec: ConnectorProtocolCodec,
        guard: ObserverSequenceGuard | ObserverProjectionV2,
        authority_poll_seconds: float,
        close_timeout_seconds: float,
        observer_contract: int,
    ) -> None:
        self.snapshot = snapshot
        self._stream = stream
        self._subscription_id = subscription_id
        self._pending_frames = list(pending_frames)
        self._authority = authority
        self._expected_authority = expected_authority
        self._codec = codec
        self._guard = guard
        self._authority_poll_seconds = authority_poll_seconds
        self._close_timeout_seconds = close_timeout_seconds
        self._observer_contract = observer_contract
        self._events_started = False
        self._closed = False
        self._terminated = False
        self._close_lock = asyncio.Lock()

    async def events(self) -> AsyncIterator[SessionEvent]:
        if self._events_started:
            raise RuntimeError("Observer subscription allows one event consumer")
        self._events_started = True
        try:
            while not self._terminated:
                await self._ensure_authority()
                if self._pending_frames:
                    frame = self._pending_frames.pop(0)
                else:
                    frame = await self._receive_frame()
                await self._ensure_authority()
                event = _session_event_from_frame(
                    self._codec,
                    frame,
                    profile=self.snapshot.profile,
                    runtime_generation=self.snapshot.runtime_generation,
                    observer_contract=self._observer_contract,
                )
                try:
                    if isinstance(self._guard, ObserverProjectionV2):
                        accepted = self._guard.accept(event)
                    else:
                        accepted = self._guard.accept(
                            ObserverEvent(
                                type=event.type,
                                session_id=event.session_id,
                                session_key=event.session_key,
                                event_sequence=event.event_sequence,
                                event_sequence_start=event.event_sequence_start,
                                payload=event.payload,
                                observer_contract=event.observer_contract,
                            )
                        )
                except (ObserverProjectionV2Error, ObserverSequenceError) as error:
                    raise ObserverResnapshotRequired(str(error)) from None
                if accepted:
                    yield event
        except BaseException:
            await self._abort()
            raise

    async def close(self) -> None:
        self._terminated = True
        cancelled: asyncio.CancelledError | None = None
        async with self._close_lock:
            if self._closed:
                return
            try:
                async with asyncio.timeout(self._close_timeout_seconds):
                    await self._stream.send(
                        _unsubscribe_request(
                            observer_contract=self._observer_contract,
                            subscription_id=self._subscription_id,
                        )
                    )
            except asyncio.CancelledError as error:
                cancelled = error
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
        if cancelled is not None:
            raise cancelled

    async def _ensure_authority(self) -> None:
        await _require_authority(
            self._authority,
            expected_authority=self._expected_authority,
        )

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

    async def _abort(self) -> None:
        self._terminated = True
        async with self._close_lock:
            if self._closed:
                return
            await _close_quietly(self._stream)
            self._closed = True


class _PreparedSubscription:
    def __init__(
        self,
        *,
        result: Mapping[str, object],
        subscription_id: str,
        events: tuple[Mapping[str, object], ...],
    ) -> None:
        self.result = result
        self.subscription_id = subscription_id
        self.events = events


async def _close_quietly(stream: WindowsAuthorityBoundDuplexPipe) -> None:
    try:
        await stream.close()
    except (ConnectionError, OSError, RuntimeError):
        pass


__all__ = [
    "WindowsObserverClient",
    "WindowsObserverSubscription",
]
