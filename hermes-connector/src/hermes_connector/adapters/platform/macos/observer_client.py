from __future__ import annotations

import asyncio
import json
import math
import os
import re
import stat
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from typing import Protocol

from websockets.asyncio.client import unix_connect

from hermes_connector.adapters.cloud.codec import ConnectorProtocolCodec
from hermes_connector.adapters.platform.macos.observer_discovery import ObserverEndpoint
from hermes_connector.adapters.platform.macos.process_identity import (
    ProcessIdentityProvider,
    current_process_identity,
    normalize_process_identity,
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
from hermes_connector.domain.observer import (
    ObserverEvent,
    SessionEvent,
    SessionSnapshot,
)

MAX_OBSERVER_FRAME_BYTES = 262_144
MAX_PRE_SNAPSHOT_EVENTS = 32
_SUBSCRIBE_ID = "connector-observer-subscribe"
_UNSUBSCRIBE_ID = "connector-observer-unsubscribe"
_SUBSCRIBE_V2_ID = 1
_UNSUBSCRIBE_V2_ID = 2
_OUTPUT_PARITY_CAPABILITY = "session.observe.output-parity.v1"
_PROFILE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_SOL_LOCAL = 0
_LOCAL_PEERPID = 2


class ObserverEndpointUnavailable(RuntimeError):
    """Exactly one trusted Observer endpoint was not available."""


class ObserverProtocolError(RuntimeError):
    """The local Observer peer violated its frozen JSON-RPC contract."""


class ObserverResnapshotRequired(RuntimeError):
    """The subscription must close and restart from a new snapshot."""


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


class MacOSObserverClient:
    """Open one explicit, identity-bound Observer UDS subscription."""

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
    ) -> None:
        if (
            not isinstance(rpc_timeout_seconds, int | float)
            or isinstance(rpc_timeout_seconds, bool)
            or not math.isfinite(rpc_timeout_seconds)
            or rpc_timeout_seconds <= 0
        ):
            raise ValueError("Observer RPC timeout must be a finite positive number")
        if not math.isfinite(authority_poll_seconds) or authority_poll_seconds <= 0:
            raise ValueError("Observer authority poll interval must be positive")
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
        self._codec = ConnectorProtocolCodec()

    async def subscribe(
        self,
        *,
        profile: str,
        session_key: str,
    ) -> MacOSObserverSubscription:
        if not isinstance(profile, str) or _PROFILE.fullmatch(profile) is None:
            raise ValueError("Observer profile is invalid")
        if (
            not isinstance(session_key, str)
            or not 1 <= len(session_key) <= 256
            or session_key != session_key.strip()
            or "\x00" in session_key
        ):
            raise ValueError("Observer session_key is invalid")
        deadline = asyncio.get_running_loop().time() + self._rpc_timeout_seconds
        websocket: _WebSocket | None = None
        try:
            async with asyncio.timeout_at(deadline):
                authority = await _require_authority(self._authority)
                endpoints = await self._discovery.discover(profile)
                if len(endpoints) != 1:
                    raise ObserverEndpointUnavailable(
                        "exactly one trusted Observer endpoint is required"
                    )
                endpoint = endpoints[0]
                if not _endpoint_matches_authority(endpoint, authority):
                    raise ObserverResnapshotRequired(
                        "Observer descriptor runtime authority changed"
                    )
                self._require_process_identity(endpoint)
                websocket = await self._connect(
                    str(endpoint.socket_path),
                    uri="ws://localhost/observer",
                    open_timeout=_remaining(deadline),
                    close_timeout=1.0,
                    max_size=MAX_OBSERVER_FRAME_BYTES,
                    max_queue=32,
                )
                if _connected_peer_pid(websocket) != endpoint.pid:
                    raise ObserverEndpointUnavailable(
                        "Observer peer does not match descriptor publisher"
                    )
                self._require_process_identity(endpoint)
                pending = await self._await_ready_and_subscribe(
                    websocket,
                    profile=profile,
                    session_key=session_key,
                    authority=authority,
                    deadline=deadline,
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
                return MacOSObserverSubscription(
                    websocket=websocket,
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
            if websocket is not None:
                await _bounded_websocket_close(
                    websocket,
                    timeout_seconds=min(self._rpc_timeout_seconds, 1.0),
                )
            raise

    def _require_process_identity(self, endpoint: ObserverEndpoint) -> None:
        if not self._socket_identity_provider(endpoint):
            raise ObserverEndpointUnavailable("Observer socket identity changed")
        try:
            observed = self._process_identity_provider(endpoint.pid)
        except BaseException:  # noqa: BLE001 - process evidence boundary
            observed = None
        if normalize_process_identity(observed) != endpoint.process_identity:
            raise ObserverEndpointUnavailable("Observer process identity changed")

    async def aclose(self) -> None:
        await self._discovery.aclose()

    async def _await_ready_and_subscribe(
        self,
        websocket: _WebSocket,
        *,
        profile: str,
        session_key: str,
        authority: LocalRuntimeAuthority,
        deadline: float,
    ) -> _PreparedSubscription:
        runtime_generation = authority.runtime_generation
        observer_contract = _observer_contract(authority)
        async with asyncio.timeout_at(deadline):
            while True:
                frame = _decode_frame(await websocket.recv())
                ready = _ready_payload(
                    frame,
                    observer_contract=observer_contract,
                )
                if ready is not None:
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
            await websocket.send(_encode_frame(request))
            subscribe_id = request["id"]
            pending: list[Mapping[str, object]] = []
            while True:
                frame = _decode_frame(await websocket.recv())
                if frame.get("id") != subscribe_id:
                    if _is_event_notification(frame):
                        if len(pending) >= MAX_PRE_SNAPSHOT_EVENTS:
                            raise ObserverProtocolError(
                                "Observer pre-snapshot event queue exceeded its bound"
                            )
                        pending.append(frame)
                        continue
                    raise ObserverProtocolError(
                        "Observer pre-snapshot frame is invalid"
                    )
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
                generation = result.get("runtime_generation")
                if generation != runtime_generation:
                    raise ObserverResnapshotRequired(
                        "Observer snapshot runtime authority changed"
                    )
                try:
                    subscription_id = str(canonical_uuid(result.get("subscription_id")))
                except (TypeError, ValueError):
                    raise ObserverProtocolError("Observer subscription id is invalid")
                return _PreparedSubscription(
                    result=result,
                    subscription_id=subscription_id,
                    events=tuple(pending),
                )


def _remaining(deadline: float) -> float:
    remaining = deadline - asyncio.get_running_loop().time()
    if remaining <= 0:
        raise TimeoutError
    return remaining


def _observer_contract(authority: LocalRuntimeAuthority) -> int:
    capabilities = {
        *authority.required_capabilities,
        *authority.optional_capabilities,
    }
    return 2 if _OUTPUT_PARITY_CAPABILITY in capabilities else 1


def _subscribe_request(
    *,
    observer_contract: int,
    profile: str,
    session_key: str,
    runtime_generation: str,
) -> dict[str, object]:
    if observer_contract == 2:
        return {
            "jsonrpc": "2.0",
            "id": _SUBSCRIBE_V2_ID,
            "method": "session.observe.subscribe",
            "params": {
                "observer_contract": 2,
                "session_key": session_key,
                "profile": profile,
            },
        }
    return {
        "jsonrpc": "2.0",
        "id": _SUBSCRIBE_ID,
        "method": "session.observe.subscribe",
        "params": {
            "session_key": session_key,
            "profile": profile,
            "runtime_generation": runtime_generation,
            "relay_local_only": True,
        },
    }


def _unsubscribe_request(
    *,
    observer_contract: int,
    subscription_id: str,
) -> dict[str, object]:
    if observer_contract == 2:
        return {
            "jsonrpc": "2.0",
            "id": _UNSUBSCRIBE_V2_ID,
            "method": "session.observe.unsubscribe",
            "params": {
                "observer_contract": 2,
                "subscription_id": subscription_id,
            },
        }
    return {
        "jsonrpc": "2.0",
        "id": _UNSUBSCRIBE_ID,
        "method": "session.observe.unsubscribe",
        "params": {"subscription_id": subscription_id},
    }


def _connected_peer_pid(websocket: _WebSocket) -> int:
    transport = getattr(websocket, "transport", None)
    if transport is None:
        raise ObserverEndpointUnavailable("Observer peer identity is unavailable")
    try:
        connected_socket = transport.get_extra_info("socket")
        peer_pid = connected_socket.getsockopt(_SOL_LOCAL, _LOCAL_PEERPID)
    except (AttributeError, OSError):
        raise ObserverEndpointUnavailable(
            "Observer peer identity is unavailable"
        ) from None
    if type(peer_pid) is not int or peer_pid <= 0:
        raise ObserverEndpointUnavailable("Observer peer identity is invalid")
    return peer_pid


def _same_socket_identity(endpoint: ObserverEndpoint) -> bool:
    try:
        metadata = endpoint.socket_path.lstat()
    except OSError:
        return False
    return (
        stat.S_ISSOCK(metadata.st_mode)
        and stat.S_IMODE(metadata.st_mode) == 0o600
        and metadata.st_uid == os.geteuid()
        and metadata.st_dev == endpoint.socket_device
        and metadata.st_ino == endpoint.socket_inode
    )


class MacOSObserverSubscription:
    def __init__(
        self,
        *,
        websocket: _WebSocket,
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
        self._websocket = websocket
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
                        observer_event = ObserverEvent(
                            type=event.type,
                            session_id=event.session_id,
                            session_key=event.session_key,
                            event_sequence=event.event_sequence,
                            event_sequence_start=event.event_sequence_start,
                            payload=event.payload,
                            observer_contract=event.observer_contract,
                        )
                        accepted = self._guard.accept(observer_event)
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
                    await self._websocket.send(
                        _encode_frame(
                            _unsubscribe_request(
                                observer_contract=self._observer_contract,
                                subscription_id=self._subscription_id,
                            )
                        )
                    )
            except asyncio.CancelledError as error:
                cancelled = error
            except (ConnectionError, OSError, TimeoutError):
                pass
            self._closed = await _bounded_websocket_close(
                self._websocket,
                timeout_seconds=self._close_timeout_seconds,
            )
        if cancelled is not None:
            raise cancelled

    async def _ensure_authority(self) -> None:
        await _require_authority(
            self._authority,
            expected_authority=self._expected_authority,
        )

    async def _receive_frame(self) -> Mapping[str, object]:
        receive = asyncio.create_task(self._websocket.recv())
        try:
            while True:
                done, _pending = await asyncio.wait(
                    {receive},
                    timeout=self._authority_poll_seconds,
                )
                if done:
                    return _decode_frame(receive.result())
                await self._ensure_authority()
        finally:
            if not receive.done():
                receive.cancel()
                await asyncio.gather(receive, return_exceptions=True)

    async def _abort(self) -> None:
        self._terminated = True
        async with self._close_lock:
            if self._closed:
                return
            self._closed = await _bounded_websocket_close(
                self._websocket,
                timeout_seconds=self._close_timeout_seconds,
            )


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


async def _require_authority(
    provider: AuthorityProvider,
    *,
    expected_authority: LocalRuntimeAuthority | None = None,
) -> LocalRuntimeAuthority:
    authority = await provider()
    if authority is None or "session.observe" not in {
        *authority.required_capabilities,
        *authority.optional_capabilities,
    }:
        raise ObserverResnapshotRequired("Observer runtime authority is unavailable")
    if expected_authority is not None and not _same_authority_identity(
        authority,
        expected_authority,
    ):
        raise ObserverResnapshotRequired("Observer runtime authority changed")
    return authority


def _endpoint_matches_authority(
    endpoint: ObserverEndpoint,
    authority: LocalRuntimeAuthority,
) -> bool:
    return (
        endpoint.profile == authority.profile
        and endpoint.runtime_generation == authority.runtime_generation
        and endpoint.instance_id == authority.instance_id
        and endpoint.host_bundle_id == authority.host_bundle_id
        and endpoint.process_identity == authority.process_identity
    )


def _same_authority_identity(
    current: LocalRuntimeAuthority,
    expected: LocalRuntimeAuthority,
) -> bool:
    return (
        current.profile == expected.profile
        and current.runtime_generation == expected.runtime_generation
        and current.instance_id == expected.instance_id
        and current.host_bundle_id == expected.host_bundle_id
        and current.process_identity == expected.process_identity
    )


def _snapshot_from_result(
    codec: ConnectorProtocolCodec,
    result: Mapping[str, object],
    *,
    profile: str,
    runtime_generation: str,
    session_key: str,
    observer_contract: int,
) -> SessionSnapshot:
    if observer_contract == 2:
        allowed = {
            "subscription_id",
            "observer_contract",
            "profile",
            "runtime_generation",
            "session_key",
            "runtime_session_id",
            "running",
            "status",
            "event_sequence",
            "snapshot_event_sequence",
            "messages",
            "inflight",
            "todo_sections",
            "subagents",
            "tools",
            "terminals",
            "replay_events",
            "extensions",
        }
        required = allowed - {"subscription_id", "extensions"}
        if set(result) - allowed or not required <= set(result):
            raise ObserverProtocolError(
                "Observer v2 snapshot does not match the exact result schema"
            )
        payload = {
            key: value for key, value in result.items() if key != "subscription_id"
        }
        try:
            snapshot = codec.decode_session_snapshot_v2_payload(payload)
        except ValueError as error:
            raise ObserverProtocolError("Observer v2 snapshot contract is invalid") from error
        if snapshot.session_key != session_key:
            raise ObserverResnapshotRequired("Observer snapshot session does not match")
        if snapshot.profile != profile:
            raise ObserverResnapshotRequired("Observer snapshot profile does not match")
        if snapshot.runtime_generation != runtime_generation:
            raise ObserverResnapshotRequired(
                "Observer snapshot runtime authority changed"
            )
        return snapshot
    allowed = {
        "subscription_id",
        "profile",
        "runtime_generation",
        "session_key",
        "runtime_session_id",
        "running",
        "status",
        "event_sequence",
        "snapshot_event_sequence",
        "messages",
        "inflight",
        "replay_events",
    }
    if set(result) - allowed:
        raise ObserverProtocolError("Observer snapshot contains an unexpected field")
    required = allowed - {"subscription_id"}
    if not required <= set(result):
        raise ObserverProtocolError("Observer snapshot is missing an explicit field")
    if result.get("session_key") != session_key:
        raise ObserverResnapshotRequired("Observer snapshot session does not match")
    if result["profile"] != profile:
        raise ObserverResnapshotRequired("Observer snapshot profile does not match")
    if result.get("runtime_generation") != runtime_generation:
        raise ObserverResnapshotRequired("Observer snapshot runtime authority changed")
    payload = {key: value for key, value in result.items() if key != "subscription_id"}
    return codec.decode_session_snapshot_payload(payload)


def _session_event_from_frame(
    codec: ConnectorProtocolCodec,
    frame: Mapping[str, object],
    *,
    profile: str,
    runtime_generation: str,
    observer_contract: int = 1,
) -> SessionEvent:
    if not _is_event_notification(frame):
        raise ObserverProtocolError("Observer live frame is invalid")
    params = frame.get("params")
    if not isinstance(params, dict):
        raise ObserverProtocolError("Observer event params are invalid")
    if params.get("profile") != profile:
        raise ObserverResnapshotRequired("Observer event profile does not match")
    if params.get("runtime_generation") != runtime_generation:
        raise ObserverResnapshotRequired(
            "Observer event runtime authority does not match"
        )
    payload = dict(params)
    try:
        if observer_contract == 2:
            return codec.decode_session_event_v2_payload(payload)
        return codec.decode_session_event_payload(payload)
    except ValueError as error:
        raise ObserverProtocolError("Observer live event contract is invalid") from error


def _ready_payload(
    frame: Mapping[str, object],
    *,
    observer_contract: int,
) -> Mapping[str, object] | None:
    if frame.get("method") != "event":
        return None
    if set(frame) != {"jsonrpc", "method", "params"} or frame.get("jsonrpc") != "2.0":
        raise ObserverProtocolError("Observer local envelope is invalid")
    params = frame.get("params")
    if not isinstance(params, dict):
        raise ObserverProtocolError("Observer ready params are invalid")
    if params.get("type") != "gateway.ready":
        return None
    if set(params) != {"type", "payload"}:
        raise ObserverProtocolError("Observer ready params contain an unknown field")
    payload = params.get("payload")
    if observer_contract == 2:
        if (
            not isinstance(payload, dict)
            or set(payload) != {"observer_contract", "connection_role"}
            or payload.get("observer_contract") != 2
            or payload.get("connection_role") != "observer"
        ):
            raise ObserverProtocolError("Observer ready contract is invalid")
        return payload
    if (
        not isinstance(payload, dict)
        or set(payload)
        != {
            "observer_contract",
            "local_gateway_protocol",
            "connection_role",
            "profile",
            "runtime_generation",
            "instance_id",
        }
        or type(payload["observer_contract"]) is not int
        or payload["observer_contract"] != 1
        or type(payload["local_gateway_protocol"]) is not int
        or payload["local_gateway_protocol"] != 1
        or payload["connection_role"] != "observer"
    ):
        raise ObserverProtocolError("Observer ready contract is invalid")
    return payload


def _is_event_notification(frame: Mapping[str, object]) -> bool:
    return (
        set(frame) == {"jsonrpc", "method", "params"}
        and frame.get("jsonrpc") == "2.0"
        and frame.get("method") == "event"
        and isinstance(frame.get("params"), dict)
    )


def _validate_ready_identity(
    payload: Mapping[str, object],
    *,
    profile: str,
    runtime_generation: str,
    instance_id: str,
) -> None:
    ready_profile = payload.get("profile")
    ready_generation = payload.get("runtime_generation")
    ready_instance = payload.get("instance_id")
    if (
        ready_profile != profile
        or ready_generation != runtime_generation
        or ready_instance != instance_id
    ):
        raise ObserverResnapshotRequired(
            "Observer ready identity does not match runtime authority"
        )


def _decode_frame(raw: str | bytes) -> Mapping[str, object]:
    if isinstance(raw, bytes):
        if len(raw) > MAX_OBSERVER_FRAME_BYTES:
            raise ObserverProtocolError("Observer frame exceeds its size limit")
        try:
            raw = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            raise ObserverProtocolError("Observer frame must be UTF-8") from None
    elif not isinstance(raw, str):
        raise ObserverProtocolError("Observer frame must be text or bytes")
    if len(raw.encode("utf-8")) > MAX_OBSERVER_FRAME_BYTES:
        raise ObserverProtocolError("Observer frame exceeds its size limit")
    try:
        value = json.loads(
            raw,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_non_json_number,
        )
    except (TypeError, ValueError, json.JSONDecodeError):
        raise ObserverProtocolError("Observer frame must be strict JSON") from None
    if not isinstance(value, dict):
        raise ObserverProtocolError("Observer frame must be an object")
    return value


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ObserverProtocolError("Observer frame contains a duplicate field")
        value[key] = item
    return value


def _reject_non_json_number(_value: str) -> None:
    raise ObserverProtocolError("Observer frame contains a non-JSON number")


async def _bounded_websocket_close(
    websocket: _WebSocket,
    *,
    timeout_seconds: float,
) -> bool:
    task = asyncio.create_task(websocket.close())
    try:
        async with asyncio.timeout(timeout_seconds):
            await asyncio.shield(task)
        return True
    except TimeoutError:
        if not task.done():
            task.cancel()
        await _bounded_task_drain(task, timeout_seconds=timeout_seconds)
        return False
    except asyncio.CancelledError:
        if not task.done():
            task.cancel()
        await _bounded_task_drain(task, timeout_seconds=timeout_seconds)
        raise


async def _bounded_task_drain(
    task: asyncio.Task[None],
    *,
    timeout_seconds: float,
) -> None:
    await asyncio.wait({task}, timeout=timeout_seconds)


def _encode_frame(frame: Mapping[str, object]) -> str:
    return json.dumps(
        frame,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


__all__ = [
    "MAX_OBSERVER_FRAME_BYTES",
    "MacOSObserverClient",
    "MacOSObserverSubscription",
    "ObserverEndpointUnavailable",
    "ObserverProtocolError",
    "ObserverResnapshotRequired",
]
