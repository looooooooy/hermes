from __future__ import annotations

import asyncio
import json
import os
import socket
import tempfile
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_connector.adapters.platform.macos.observer_client import (
    MacOSObserverClient as _MacOSObserverClient,
)
from hermes_connector.adapters.platform.macos.observer_client import (
    ObserverEndpointUnavailable,
    ObserverProtocolError,
    ObserverResnapshotRequired,
)
from hermes_connector.adapters.platform.macos.observer_discovery import ObserverEndpoint
from hermes_connector.adapters.platform.macos.process_identity import (
    current_process_identity,
)
from hermes_connector.domain.local_gateway import (
    LocalRuntimeAuthority,
)

_SUBSCRIPTION_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
_PROCESS_IDENTITY = current_process_identity(os.getpid())
assert _PROCESS_IDENTITY is not None


def MacOSObserverClient(*args, **kwargs) -> _MacOSObserverClient:
    kwargs.setdefault("socket_identity_provider", lambda _: True)
    return _MacOSObserverClient(*args, **kwargs)


def _endpoint(
    name: str = "observer.sock",
    *,
    runtime_generation: str = "runtime-generation-1",
) -> ObserverEndpoint:
    socket_path = Path("/tmp/hermes-observer-test") / name
    return ObserverEndpoint(
        pid=os.getpid(),
        profile="default",
        runtime_generation=runtime_generation,
        socket_path=socket_path,
        instance_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        host_bundle_id="com.nousresearch.hermes",
        process_identity=_PROCESS_IDENTITY,
        socket_device=51,
        socket_inode=79,
        registry_path=socket_path.with_suffix(".json"),
    )


class _Discovery:
    def __init__(self, endpoints: tuple[ObserverEndpoint, ...]) -> None:
        self.endpoints = endpoints
        self.calls: list[str] = []

    async def discover(self, profile: str) -> tuple[ObserverEndpoint, ...]:
        self.calls.append(profile)
        return self.endpoints if profile == "default" else ()

    async def aclose(self) -> None:
        return None


class _PeerCredentialSocket:
    def __init__(self, peer_pid: int) -> None:
        self._peer_pid = peer_pid

    def getsockopt(self, level: int, option: int) -> int:
        assert (level, option) == (0, 2)
        return self._peer_pid


class _PeerTransport:
    def __init__(self, peer_pid: int) -> None:
        self._socket = _PeerCredentialSocket(peer_pid)

    def get_extra_info(self, name: str):
        return self._socket if name == "socket" else None


class _Socket:
    def __init__(
        self,
        incoming: list[dict[str, object] | str],
        *,
        peer_pid: int = os.getpid(),
    ) -> None:
        self.incoming: asyncio.Queue[str | BaseException] = asyncio.Queue()
        for frame in incoming:
            self.incoming.put_nowait(
                frame if isinstance(frame, str) else json.dumps(frame)
            )
        self.sent: list[dict[str, object]] = []
        self.closed = False
        self.transport = _PeerTransport(peer_pid)

    async def recv(self) -> str:
        value = await self.incoming.get()
        if isinstance(value, BaseException):
            raise value
        return value

    async def send(self, value: str | bytes) -> None:
        if isinstance(value, bytes):
            value = value.decode("utf-8")
        self.sent.append(json.loads(value))

    async def close(self) -> None:
        self.closed = True


class _Connector:
    def __init__(self, socket: _Socket) -> None:
        self.socket = socket
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, path: str, *, uri: str, **_options: object) -> _Socket:
        self.calls.append((path, uri))
        return self.socket


@pytest.mark.asyncio
async def test_observer_endpoint_from_different_local_runtime_is_rejected_before_connect() -> (
    None
):
    endpoint = _endpoint()
    connector = _Connector(_Socket([_ready(), _snapshot_response()]))

    async def authority():
        return SimpleNamespace(
            profile="default",
            runtime_generation=endpoint.runtime_generation,
            instance_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
            host_bundle_id=endpoint.host_bundle_id,
            process_identity=endpoint.process_identity,
            required_capabilities=("session.observe",),
            optional_capabilities=(),
        )

    client = MacOSObserverClient(
        discovery=_Discovery((endpoint,)),
        connect=connector,
        authority=authority,
        process_identity_provider=lambda _: endpoint.process_identity,
    )

    with pytest.raises(ObserverResnapshotRequired, match="authority"):
        await client.subscribe(profile="default", session_key="session-root-1")

    assert connector.calls == []


@pytest.mark.asyncio
async def test_observer_same_uid_replacement_socket_is_rejected_before_connect() -> (
    None
):
    with tempfile.TemporaryDirectory() as directory:
        socket_path = Path(directory) / "observer.sock"
        original = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        original.bind(str(socket_path))
        socket_path.chmod(0o600)
        metadata = socket_path.lstat()
        endpoint = replace(
            _endpoint(),
            socket_path=socket_path,
            registry_path=socket_path.with_suffix(".json"),
            socket_device=metadata.st_dev,
            socket_inode=metadata.st_ino,
        )
        socket_path.unlink()
        replacement = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        replacement.bind(str(socket_path))
        socket_path.chmod(0o600)
        connector = _Connector(_Socket([_ready(), _snapshot_response()]))
        try:
            client = _MacOSObserverClient(
                discovery=_Discovery((endpoint,)),
                connect=connector,
                authority=_authority,
            )
            with pytest.raises(ObserverEndpointUnavailable, match="socket identity"):
                await client.subscribe(
                    profile="default",
                    session_key="session-root-1",
                )
            assert connector.calls == []
        finally:
            replacement.close()
            original.close()


class _BlockingCleanupSocket(_Socket):
    def __init__(self, incoming: list[dict[str, object] | str]) -> None:
        super().__init__(incoming)
        self.block_send = False
        self.close_started = asyncio.Event()

    async def send(self, value: str | bytes) -> None:
        if self.block_send:
            await asyncio.Event().wait()
        await super().send(value)

    async def close(self) -> None:
        self.close_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.closed = True


class _RetryableCloseSocket(_Socket):
    def __init__(self, incoming: list[dict[str, object] | str]) -> None:
        super().__init__(incoming)
        self.close_calls = 0

    async def close(self) -> None:
        self.close_calls += 1
        if self.close_calls == 1:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return
        self.closed = True


def _ready(
    *,
    profile: str | None = "default",
    runtime_generation: str | None = "runtime-generation-1",
    instance_id: str | None = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    connection_role: str | None = "observer",
) -> dict[str, object]:
    payload: dict[str, object] = {
        "observer_contract": 1,
        "local_gateway_protocol": 1,
    }
    if profile is not None:
        payload["profile"] = profile
    if runtime_generation is not None:
        payload["runtime_generation"] = runtime_generation
    if instance_id is not None:
        payload["instance_id"] = instance_id
    if connection_role is not None:
        payload["connection_role"] = connection_role
    return {
        "jsonrpc": "2.0",
        "method": "event",
        "params": {
            "type": "gateway.ready",
            "payload": payload,
        },
    }


def _ready_v2() -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "method": "event",
        "params": {
            "type": "gateway.ready",
            "payload": {
                "observer_contract": 2,
                "connection_role": "observer",
            },
        },
    }


def _snapshot_response(
    *,
    subscription_id: str = _SUBSCRIPTION_ID,
) -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "id": "connector-observer-subscribe",
        "result": {
            "subscription_id": subscription_id,
            "profile": "default",
            "runtime_generation": "runtime-generation-1",
            "session_key": "session-root-1",
            "runtime_session_id": "runtime-session-1",
            "running": True,
            "status": "running",
            "event_sequence": 4,
            "snapshot_event_sequence": 4,
            "messages": [],
            "inflight": {
                "user": None,
                "assistant": None,
                "streaming": False,
                "error": None,
            },
            "replay_events": [],
        },
    }


def _snapshot_response_v2() -> dict[str, object]:
    response = _snapshot_response()
    response["id"] = 1
    result = response["result"]
    assert isinstance(result, dict)
    result.update(
        {
            "observer_contract": 2,
            "todo_sections": [],
            "subagents": [],
            "tools": [],
            "terminals": [],
        }
    )
    return response


def _event(sequence: int) -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "method": "event",
        "params": {
            "type": "message.delta",
            "profile": "default",
            "runtime_generation": "runtime-generation-1",
            "session_id": "runtime-session-1",
            "session_key": "session-root-1",
            "event_sequence": sequence,
            "payload": {"text": f"delta-{sequence}"},
        },
    }


def _tool_event_v2(sequence: int, *, revision: int) -> dict[str, object]:
    event = _event(sequence)
    params = event["params"]
    assert isinstance(params, dict)
    params.update(
        {
            "observer_contract": 2,
            "type": "tool.update",
            "payload": {
                "turn_id": "turn-1",
                "tool_call_id": "tool-1",
                "revision": revision,
                "first_event_sequence": 5,
                "operation": "upsert",
                "status": "running",
                "name": "search",
            },
        }
    )
    return event


def _runtime_authority(
    generation: str = "runtime-generation-1",
    *,
    output_parity: bool = False,
) -> LocalRuntimeAuthority:
    return LocalRuntimeAuthority(
        profile="default",
        runtime_generation=generation,
        instance_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        host_bundle_id="com.nousresearch.hermes",
        process_identity=_PROCESS_IDENTITY,
        required_capabilities=("session.observe",),
        optional_capabilities=(
            ("session.observe.output-parity.v1",) if output_parity else ()
        ),
    )


async def _authority() -> LocalRuntimeAuthority:
    return _runtime_authority()


async def _v2_authority() -> LocalRuntimeAuthority:
    return _runtime_authority(output_parity=True)


@pytest.mark.asyncio
async def test_v2_requires_localwelcome_capability_and_exact_ready_snapshot() -> None:
    socket = _Socket([_ready_v2(), _snapshot_response_v2()])
    subscription = await MacOSObserverClient(
        discovery=_Discovery((_endpoint(),)),
        connect=_Connector(socket),
        authority=_v2_authority,
    ).subscribe(profile="default", session_key="session-root-1")

    assert subscription.snapshot.observer_contract == 2
    assert subscription.snapshot.todo_sections == ()
    assert subscription.snapshot.subagents == ()
    assert subscription.snapshot.tools == ()
    assert subscription.snapshot.terminals == ()
    assert socket.sent == [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "session.observe.subscribe",
            "params": {
                "observer_contract": 2,
                "profile": "default",
                "session_key": "session-root-1",
            },
        }
    ]

    await subscription.close()
    assert socket.sent[-1] == {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "session.observe.unsubscribe",
        "params": {
            "observer_contract": 2,
            "subscription_id": _SUBSCRIPTION_ID,
        },
    }


@pytest.mark.asyncio
async def test_v2_live_events_use_atomic_lifecycle_projection() -> None:
    socket = _Socket(
        [
            _ready_v2(),
            _snapshot_response_v2(),
            _tool_event_v2(5, revision=1),
            _tool_event_v2(6, revision=1),
        ]
    )
    subscription = await MacOSObserverClient(
        discovery=_Discovery((_endpoint(),)),
        connect=_Connector(socket),
        authority=_v2_authority,
    ).subscribe(profile="default", session_key="session-root-1")

    events = subscription.events()
    assert (await anext(events)).event_sequence == 5
    with pytest.raises(ObserverResnapshotRequired, match="revision"):
        await anext(events)

    assert socket.closed is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("authority", "ready"),
    ((_v2_authority, _ready()), (_authority, _ready_v2())),
)
async def test_observer_contract_mismatch_fails_without_v1_fallback(
    authority,
    ready: dict[str, object],
) -> None:
    socket = _Socket([ready, _snapshot_response_v2()])
    client = MacOSObserverClient(
        discovery=_Discovery((_endpoint(),)),
        connect=_Connector(socket),
        authority=authority,
    )

    with pytest.raises(ObserverProtocolError, match="contract"):
        await client.subscribe(profile="default", session_key="session-root-1")

    assert socket.sent == []
    assert socket.closed is True


@pytest.mark.parametrize(
    "rpc_timeout_seconds",
    (True, False, float("nan"), float("inf"), -float("inf"), 0, -1, "1"),
)
def test_rpc_timeout_requires_a_finite_positive_number(
    rpc_timeout_seconds: object,
) -> None:
    with pytest.raises(
        ValueError,
        match="Observer RPC timeout must be a finite positive number",
    ):
        MacOSObserverClient(
            discovery=_Discovery((_endpoint(),)),
            connect=_Connector(_Socket([])),
            authority=_authority,
            rpc_timeout_seconds=rpc_timeout_seconds,
        )


@pytest.mark.asyncio
async def test_ready_accepts_exact_observer_role_from_real_plugin() -> None:
    socket = _Socket([_ready(), _snapshot_response()])
    subscription = await MacOSObserverClient(
        discovery=_Discovery((_endpoint(),)),
        connect=_Connector(socket),
        authority=_authority,
    ).subscribe(profile="default", session_key="session-root-1")

    await subscription.close()

    assert socket.closed is True


@pytest.mark.asyncio
async def test_observer_connected_peer_pid_must_match_descriptor_publisher() -> None:
    socket = _Socket(
        [_ready(), _snapshot_response()],
        peer_pid=999,
    )
    client = MacOSObserverClient(
        discovery=_Discovery((_endpoint(),)),
        connect=_Connector(socket),
        authority=_authority,
    )

    with pytest.raises(
        ObserverEndpointUnavailable,
        match="descriptor publisher",
    ):
        await client.subscribe(profile="default", session_key="session-root-1")

    assert socket.closed is True


@pytest.mark.asyncio
async def test_subscribe_is_explicit_snapshot_first_and_live_after_response() -> None:
    pending = _event(5)
    socket = _Socket([_ready(), pending, _snapshot_response(), _event(6)])
    connector = _Connector(socket)
    client = MacOSObserverClient(
        discovery=_Discovery((_endpoint(),)),
        connect=connector,
        authority=_authority,
    )

    subscription = await client.subscribe(
        profile="default",
        session_key="session-root-1",
    )

    assert subscription.snapshot.profile == "default"
    assert subscription.snapshot.runtime_generation == "runtime-generation-1"
    assert subscription.snapshot.inflight == {
        "user": None,
        "assistant": None,
        "streaming": False,
        "error": None,
    }
    assert socket.sent[0] == {
        "jsonrpc": "2.0",
        "id": "connector-observer-subscribe",
        "method": "session.observe.subscribe",
        "params": {
            "session_key": "session-root-1",
            "profile": "default",
            "runtime_generation": "runtime-generation-1",
            "relay_local_only": True,
        },
    }

    events: AsyncIterator[object] = subscription.events()
    first = await anext(events)
    second = await anext(events)
    assert first.event_sequence == 5
    assert second.event_sequence == 6

    await subscription.close()
    assert socket.sent[-1] == {
        "jsonrpc": "2.0",
        "id": "connector-observer-unsubscribe",
        "method": "session.observe.unsubscribe",
        "params": {"subscription_id": _SUBSCRIPTION_ID},
    }
    assert socket.closed is True


@pytest.mark.asyncio
async def test_zero_or_multiple_endpoints_fail_closed_without_selecting_first() -> None:
    for endpoints in ((), (_endpoint("a.sock"), _endpoint("b.sock"))):
        socket = _Socket([])
        connector = _Connector(socket)
        client = MacOSObserverClient(
            discovery=_Discovery(endpoints),
            connect=connector,
            authority=_authority,
        )

        with pytest.raises(ObserverEndpointUnavailable):
            await client.subscribe(profile="default", session_key="session-root-1")

        assert connector.calls == []


@pytest.mark.asyncio
async def test_authority_discovery_and_handshake_share_one_total_deadline() -> None:
    class SlowDiscovery(_Discovery):
        async def discover(self, profile: str) -> tuple[ObserverEndpoint, ...]:
            await asyncio.sleep(0.02)
            return await super().discover(profile)

    async def slow_authority() -> LocalRuntimeAuthority:
        await asyncio.sleep(0.02)
        return await _authority()

    socket = _Socket([_ready(), _snapshot_response()])
    connector = _Connector(socket)
    client = MacOSObserverClient(
        discovery=SlowDiscovery((_endpoint(),)),
        connect=connector,
        authority=slow_authority,
        rpc_timeout_seconds=0.03,
    )

    with pytest.raises(TimeoutError):
        await client.subscribe(profile="default", session_key="session-root-1")

    assert connector.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("profile", "session_key"),
    (
        ("", "session-root-1"),
        (" default", "session-root-1"),
        ("default/other", "session-root-1"),
        ("x" * 129, "session-root-1"),
        ("default", ""),
        ("default", " session-root-1"),
        ("default", "session-root-1 "),
        ("default", "session\x00root"),
        ("default", "x" * 257),
    ),
)
async def test_invalid_subscription_identity_fails_before_discovery_or_connect(
    profile: str,
    session_key: str,
) -> None:
    socket = _Socket([])
    connector = _Connector(socket)
    discovery = _Discovery((_endpoint(),))
    client = MacOSObserverClient(
        discovery=discovery,
        connect=connector,
        authority=_authority,
    )

    with pytest.raises(ValueError, match="profile|session_key"):
        await client.subscribe(profile=profile, session_key=session_key)

    assert discovery.calls == []
    assert connector.calls == []


@pytest.mark.asyncio
async def test_stale_observer_descriptor_generation_fails_before_connect() -> None:
    socket = _Socket([])
    connector = _Connector(socket)
    client = MacOSObserverClient(
        discovery=_Discovery(
            (_endpoint(runtime_generation="runtime-generation-stale"),)
        ),
        connect=connector,
        authority=_authority,
    )

    with pytest.raises(ObserverResnapshotRequired, match="descriptor"):
        await client.subscribe(profile="default", session_key="session-root-1")

    assert connector.calls == []


@pytest.mark.asyncio
async def test_live_gap_closes_subscription_and_requires_new_snapshot() -> None:
    socket = _Socket([_ready(), _snapshot_response(), _event(6)])
    subscription = await MacOSObserverClient(
        discovery=_Discovery((_endpoint(),)),
        connect=_Connector(socket),
        authority=_authority,
    ).subscribe(profile="default", session_key="session-root-1")

    with pytest.raises(ObserverResnapshotRequired, match="gap"):
        await anext(subscription.events())

    assert socket.closed is True


@pytest.mark.asyncio
async def test_generation_change_closes_subscription_before_forwarding_live() -> None:
    calls = 0

    async def changing_authority() -> LocalRuntimeAuthority:
        nonlocal calls
        calls += 1
        generation = "runtime-generation-1" if calls <= 4 else "runtime-generation-2"
        return _runtime_authority(generation)

    socket = _Socket([_ready(), _snapshot_response(), _event(5)])
    subscription = await MacOSObserverClient(
        discovery=_Discovery((_endpoint(),)),
        connect=_Connector(socket),
        authority=changing_authority,
    ).subscribe(profile="default", session_key="session-root-1")

    with pytest.raises(ObserverResnapshotRequired, match="authority"):
        await anext(subscription.events())

    assert socket.closed is True


@pytest.mark.asyncio
async def test_authority_change_after_ready_fails_before_sending_subscribe() -> None:
    calls = 0

    async def changing_authority() -> LocalRuntimeAuthority:
        nonlocal calls
        calls += 1
        generation = "runtime-generation-1" if calls == 1 else "runtime-generation-2"
        return _runtime_authority(generation)

    socket = _Socket([_ready()])
    client = MacOSObserverClient(
        discovery=_Discovery((_endpoint(),)),
        connect=_Connector(socket),
        authority=changing_authority,
    )

    with pytest.raises(ObserverResnapshotRequired, match="authority"):
        await client.subscribe(profile="default", session_key="session-root-1")

    assert socket.sent == []
    assert socket.closed is True


@pytest.mark.asyncio
async def test_ready_identity_must_match_verified_authority_when_present() -> None:
    socket = _Socket(
        [
            _ready(
                profile="default",
                runtime_generation="runtime-generation-stale",
            )
        ]
    )
    client = MacOSObserverClient(
        discovery=_Discovery((_endpoint(),)),
        connect=_Connector(socket),
        authority=_authority,
    )

    with pytest.raises(ObserverResnapshotRequired, match="ready identity"):
        await client.subscribe(profile="default", session_key="session-root-1")

    assert socket.sent == []
    assert socket.closed is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "missing",
    ("profile", "runtime_generation", "instance_id", "connection_role"),
)
async def test_ready_requires_complete_discovered_runtime_identity(
    missing: str,
) -> None:
    ready = _ready()
    params = ready["params"]
    assert isinstance(params, dict)
    payload = params["payload"]
    assert isinstance(payload, dict)
    payload.pop(missing)
    socket = _Socket([ready])
    client = MacOSObserverClient(
        discovery=_Discovery((_endpoint(),)),
        connect=_Connector(socket),
        authority=_authority,
    )

    with pytest.raises(ObserverProtocolError):
        await client.subscribe(profile="default", session_key="session-root-1")

    assert socket.sent == []
    assert socket.closed is True


@pytest.mark.asyncio
async def test_snapshot_cannot_omit_or_replace_requested_runtime_generation() -> None:
    for replacement in (None, "runtime-generation-stale"):
        response = _snapshot_response()
        result = response["result"]
        assert isinstance(result, dict)
        if replacement is None:
            result.pop("runtime_generation")
        else:
            result["runtime_generation"] = replacement
        socket = _Socket([_ready(), response])
        client = MacOSObserverClient(
            discovery=_Discovery((_endpoint(),)),
            connect=_Connector(socket),
            authority=_authority,
        )

        with pytest.raises(ObserverResnapshotRequired, match="snapshot runtime"):
            await client.subscribe(profile="default", session_key="session-root-1")

        assert socket.closed is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "subscription_id",
    (
        "subscription-1",
        "bbbbbbbbbbbb4bbb8bbbbbbbbbbbbbbb",
        "BBBBBBBB-BBBB-4BBB-8BBB-BBBBBBBBBBBB",
        "00000000-0000-0000-0000-000000000000",
    ),
)
async def test_snapshot_requires_canonical_subscription_uuid(
    subscription_id: str,
) -> None:
    socket = _Socket([_ready(), _snapshot_response(subscription_id=subscription_id)])
    client = MacOSObserverClient(
        discovery=_Discovery((_endpoint(),)),
        connect=_Connector(socket),
        authority=_authority,
    )

    with pytest.raises(ObserverProtocolError, match="subscription id"):
        await client.subscribe(profile="default", session_key="session-root-1")

    assert socket.closed is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation",
    (
        {"observer_contract": 2},
        {"observer_contract": True},
        {"local_gateway_protocol": 2},
        {"local_gateway_protocol": True},
        {"local_gateway_protocol": None},
        {"connection_role": "control"},
        {"unexpected": True},
    ),
)
async def test_ready_requires_exact_local_contract_and_no_unknown_fields(
    mutation: dict[str, object],
) -> None:
    ready = _ready()
    params = ready["params"]
    assert isinstance(params, dict)
    payload = params["payload"]
    assert isinstance(payload, dict)
    if mutation.get("local_gateway_protocol", object()) is None:
        payload.pop("local_gateway_protocol")
    else:
        payload.update(mutation)
    socket = _Socket([ready])
    client = MacOSObserverClient(
        discovery=_Discovery((_endpoint(),)),
        connect=_Connector(socket),
        authority=_authority,
    )

    with pytest.raises(ObserverProtocolError):
        await client.subscribe(profile="default", session_key="session-root-1")

    assert socket.closed is True


@pytest.mark.asyncio
async def test_local_frames_reject_nan_and_extra_envelope_fields() -> None:
    invalid_frames = (
        (
            '{"jsonrpc":"2.0","method":"event",'
            '"params":{"type":"gateway.ready","payload":'
            '{"observer_contract":1,"local_gateway_protocol":NaN}}}'
        ),
        json.dumps({**_ready(), "unexpected": True}),
    )
    for raw in invalid_frames:
        socket = _Socket([raw])
        client = MacOSObserverClient(
            discovery=_Discovery((_endpoint(),)),
            connect=_Connector(socket),
            authority=_authority,
        )

        with pytest.raises(ObserverProtocolError):
            await client.subscribe(profile="default", session_key="session-root-1")

        assert socket.closed is True


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ("profile", "runtime_generation", "inflight"))
async def test_snapshot_requires_explicit_exact_identity_and_inflight(
    missing: str,
) -> None:
    response = _snapshot_response()
    result = response["result"]
    assert isinstance(result, dict)
    result.pop(missing)
    socket = _Socket([_ready(), response])
    client = MacOSObserverClient(
        discovery=_Discovery((_endpoint(),)),
        connect=_Connector(socket),
        authority=_authority,
    )

    with pytest.raises((ObserverProtocolError, ObserverResnapshotRequired)):
        await client.subscribe(profile="default", session_key="session-root-1")

    assert socket.closed is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "replacement"),
    (
        ("profile", None),
        ("runtime_generation", None),
        ("profile", "other"),
        ("runtime_generation", "runtime-generation-stale"),
        ("session_key", "another-session"),
        ("session_id", "another-runtime-session"),
    ),
)
async def test_live_event_identity_is_required_and_never_overwritten(
    field: str,
    replacement: str | None,
) -> None:
    event = _event(5)
    params = event["params"]
    assert isinstance(params, dict)
    if replacement is None:
        params.pop(field)
    else:
        params[field] = replacement
    socket = _Socket([_ready(), _snapshot_response(), event])
    subscription = await MacOSObserverClient(
        discovery=_Discovery((_endpoint(),)),
        connect=_Connector(socket),
        authority=_authority,
    ).subscribe(profile="default", session_key="session-root-1")

    with pytest.raises((ObserverProtocolError, ObserverResnapshotRequired)):
        await anext(subscription.events())

    assert socket.closed is True


@pytest.mark.asyncio
async def test_cancelled_subscribe_still_closes_connected_socket() -> None:
    socket = _Socket([_ready()])
    client = MacOSObserverClient(
        discovery=_Discovery((_endpoint(),)),
        connect=_Connector(socket),
        authority=_authority,
    )
    task = asyncio.create_task(
        client.subscribe(profile="default", session_key="session-root-1")
    )
    for _ in range(100):
        if socket.sent:
            break
        await asyncio.sleep(0)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert socket.closed is True


@pytest.mark.asyncio
async def test_unsubscribe_send_and_websocket_close_are_bounded() -> None:
    socket = _BlockingCleanupSocket([_ready(), _snapshot_response()])
    subscription = await MacOSObserverClient(
        discovery=_Discovery((_endpoint(),)),
        connect=_Connector(socket),
        authority=_authority,
        rpc_timeout_seconds=0.001,
    ).subscribe(profile="default", session_key="session-root-1")
    socket.block_send = True

    await asyncio.wait_for(subscription.close(), timeout=0.1)

    assert socket.close_started.is_set()
    assert socket.closed is True


@pytest.mark.asyncio
async def test_cancelled_unsubscribe_still_completes_bounded_socket_cleanup() -> None:
    socket = _BlockingCleanupSocket([_ready(), _snapshot_response()])
    subscription = await MacOSObserverClient(
        discovery=_Discovery((_endpoint(),)),
        connect=_Connector(socket),
        authority=_authority,
        rpc_timeout_seconds=0.01,
    ).subscribe(profile="default", session_key="session-root-1")
    socket.block_send = True
    task = asyncio.create_task(subscription.close())
    await asyncio.sleep(0)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert socket.close_started.is_set()
    assert socket.closed is True


@pytest.mark.asyncio
async def test_cancellation_during_socket_close_propagates_after_cleanup() -> None:
    socket = _BlockingCleanupSocket([_ready(), _snapshot_response()])
    subscription = await MacOSObserverClient(
        discovery=_Discovery((_endpoint(),)),
        connect=_Connector(socket),
        authority=_authority,
        rpc_timeout_seconds=1,
    ).subscribe(profile="default", session_key="session-root-1")
    task = asyncio.create_task(subscription.close())
    await asyncio.wait_for(socket.close_started.wait(), timeout=0.1)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert socket.closed is True


@pytest.mark.asyncio
async def test_timed_out_socket_close_can_be_retried() -> None:
    socket = _RetryableCloseSocket([_ready(), _snapshot_response()])
    subscription = await MacOSObserverClient(
        discovery=_Discovery((_endpoint(),)),
        connect=_Connector(socket),
        authority=_authority,
        rpc_timeout_seconds=0.001,
    ).subscribe(profile="default", session_key="session-root-1")

    await subscription.close()
    assert socket.closed is False
    await subscription.close()

    assert socket.close_calls == 2
    assert socket.closed is True


@pytest.mark.asyncio
async def test_generation_change_closes_idle_subscription_without_waiting_for_event() -> (
    None
):
    generation = "runtime-generation-1"

    async def mutable_authority() -> LocalRuntimeAuthority:
        return _runtime_authority(generation)

    socket = _Socket([_ready(), _snapshot_response()])
    subscription = await MacOSObserverClient(
        discovery=_Discovery((_endpoint(),)),
        connect=_Connector(socket),
        authority=mutable_authority,
        authority_poll_seconds=0.001,
    ).subscribe(profile="default", session_key="session-root-1")
    generation = "runtime-generation-2"

    with pytest.raises(ObserverResnapshotRequired, match="authority"):
        await asyncio.wait_for(anext(subscription.events()), timeout=0.1)

    assert socket.closed is True
