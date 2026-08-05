from __future__ import annotations

import asyncio
import gc
import json
from contextlib import suppress
from uuid import UUID

import pytest

from hermes_cloud.modules.cloud_api.adapters import realtime
from hermes_cloud.modules.cloud_api.domain import Principal

TENANT_ID = UUID("11111111-1111-4111-8111-111111111111")
USER_ID = UUID("22222222-2222-4222-8222-222222222222")
AGENT_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
SESSION_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


class _WebSocket:
    def __init__(self) -> None:
        self.sent: list[dict[str, object]] = []

    async def send_text(self, value: str) -> None:
        self.sent.append(json.loads(value))


class _EventSource:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def events(self, **_kwargs: object):
        self.calls.append(dict(_kwargs))
        yield {
            "type": "message.delta",
            "session_id": "runtime-session-1",
            "session_key": "session-root-1",
            "event_sequence_start": 1,
            "event_sequence": 2,
            "payload": {"text": "coalesced"},
        }


class _V2EventSource:
    def __init__(self, *, unsafe: bool = False) -> None:
        self.unsafe = unsafe

    async def events(self, **_kwargs: object):
        payload: dict[str, object] = {
            "turn_id": "turn-1",
            "tool_call_id": "tool-1",
            "revision": 2,
            "first_event_sequence": 1,
            "operation": "upsert",
            "status": "completed",
            "name": "Contract tests",
        }
        if self.unsafe:
            extensions = {
                "vendor.private": {
                    "nested": [{"deeper": {"api_token": "must-not-cross"}}]
                }
            }
        else:
            extensions = None
        event = {
            "observer_contract": 2,
            "profile": "default",
            "runtime_generation": "generation-v2",
            "type": "tool.update",
            "session_id": SESSION_ID,
            "event_sequence": 1,
            "payload": payload,
        }
        if extensions is not None:
            event["extensions"] = extensions
        yield event


class _V2WebSocket(_WebSocket):
    def __init__(self) -> None:
        super().__init__()
        self.close_codes: list[int] = []

    async def close(self, *, code: int) -> None:
        self.close_codes.append(code)


class _BlockedSendWebSocket:
    def __init__(self) -> None:
        self.cancelled = False

    async def send_text(self, _value: str) -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise


class _BlockedReceiveWebSocket:
    def __init__(self) -> None:
        self.receive_cancelled = False

    async def receive(self) -> dict[str, object]:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.receive_cancelled = True
            raise
        raise AssertionError("unreachable")


class _FailedSendWebSocket:
    async def send_text(self, _value: str) -> None:
        raise OSError("peer disconnected")


class _ClosingWebSocket:
    def __init__(self) -> None:
        self.close_codes: list[int] = []

    async def close(self, *, code: int) -> None:
        self.close_codes.append(code)


def _principal() -> Principal:
    return Principal(
        tenant_id=TENANT_ID,
        user_id=USER_ID,
        provider="basic",
        refresh_session_id=UUID("33333333-3333-4333-8333-333333333333"),
    )


def test_subscribe_parsers_preserve_optional_canonical_agent_scope() -> None:
    v1 = realtime._parse_subscribe(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "session.observe.subscribe",
            "params": {
                "session_key": "session-root-1",
                "profile": "default",
                "agent_id": AGENT_ID,
            },
        }
    )
    v2 = realtime._parse_subscribe_v2(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "session.observe.subscribe",
            "params": {
                "observer_contract": 2,
                "session_id": SESSION_ID,
                "profile": "default",
                "agent_id": AGENT_ID,
            },
        }
    )

    assert v1 == (1, "session-root-1", "default", UUID(AGENT_ID))
    assert v2 == (2, SESSION_ID, "default", UUID(AGENT_ID))


def test_v2_subscribe_parser_validates_the_complete_frame_against_root_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "session.observe.subscribe",
        "params": {
            "observer_contract": 2,
            "session_id": SESSION_ID,
            "profile": "default",
            "agent_id": AGENT_ID,
        },
    }
    validated: list[object] = []

    def capture(_name: str, value: object) -> None:
        validated.append(value)

    monkeypatch.setattr(realtime, "require_cloud_frame", capture)

    assert realtime._parse_subscribe_v2(request) == (
        2,
        SESSION_ID,
        "default",
        UUID(AGENT_ID),
    )
    assert validated == [request]


@pytest.mark.parametrize("version", (1, 2))
@pytest.mark.parametrize(
    "agent_id",
    (
        "00000000-0000-0000-0000-000000000000",
        "66666666-6666-6666-8666-666666666666",
        "77777777-7777-7777-8777-777777777777",
        "88888888-8888-8888-8888-888888888888",
        AGENT_ID.upper(),
        "aaaaaaaa-aaaa-4aaa-0aaa-aaaaaaaaaaaa",
    ),
)
def test_subscribe_parsers_reject_noncanonical_agent_scope(
    version: int,
    agent_id: str,
) -> None:
    params: dict[str, object] = {"profile": "default", "agent_id": agent_id}
    if version == 2:
        params["observer_contract"] = 2
        params["session_id"] = SESSION_ID
    else:
        params["session_key"] = "session-root-1"
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "session.observe.subscribe",
        "params": params,
    }

    parser = realtime._parse_subscribe_v2 if version == 2 else realtime._parse_subscribe
    assert parser(request) is None


@pytest.mark.parametrize(
    "raw",
    [
        (
            '{"jsonrpc":"2.0","id":1,'
            '"method":"forbidden","method":"session.observe.unsubscribe",'
            '"params":{"subscription_id":"sub-1"}}'
        ),
        (
            '{"jsonrpc":"2.0","id":1,"method":"session.observe.unsubscribe",'
            '"params":{"subscription_id":"sub-1","subscription_id":"sub-2"}}'
        ),
        (
            '{"jsonrpc":"2.0","id":NaN,"method":"session.observe.unsubscribe",'
            '"params":{"subscription_id":"sub-1"}}'
        ),
        (
            '{"jsonrpc":"2.0","id":Infinity,'
            '"method":"session.observe.unsubscribe",'
            '"params":{"subscription_id":"sub-1"}}'
        ),
    ],
)
def test_json_loader_rejects_duplicate_keys_and_non_finite_numbers(raw: str) -> None:
    loader = getattr(realtime, "_load_json_document", None)

    assert loader is not None
    with pytest.raises(json.JSONDecodeError):
        loader(raw)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_json_limits_reject_non_finite_numbers(value: float) -> None:
    assert not realtime._within_json_limits(value)


@pytest.mark.asyncio
async def test_forward_events_accepts_contiguous_mergeable_sequence_range() -> None:
    websocket = _WebSocket()
    source = _EventSource()

    await realtime._forward_events(
        websocket=websocket,  # type: ignore[arg-type]
        source=source,  # type: ignore[arg-type]
        principal=_principal(),
        session_key="session-root-1",
        profile="default",
        runtime_session_id="runtime-session-1",
        after_sequence=0,
        lock=asyncio.Lock(),
    )

    assert websocket.sent == [
        {
            "jsonrpc": "2.0",
            "method": "event",
            "params": {
                "type": "message.delta",
                "session_id": "runtime-session-1",
                "session_key": "session-root-1",
                "event_sequence_start": 1,
                "event_sequence": 2,
                "payload": {"text": "coalesced"},
            },
        }
    ]
    assert source.calls[0]["profile"] == "default"


@pytest.mark.asyncio
async def test_forward_events_v2_emits_only_generated_exact_frames() -> None:
    websocket = _V2WebSocket()

    await realtime._forward_events(
        websocket=websocket,  # type: ignore[arg-type]
        source=_V2EventSource(),  # type: ignore[arg-type]
        principal=_principal(),
        session_key="session-root-1",
        profile="default",
        runtime_session_id=SESSION_ID,
        runtime_generation="generation-v2",
        after_sequence=0,
        observer_contract=2,
        lock=asyncio.Lock(),
    )

    assert websocket.close_codes == []
    assert websocket.sent[0]["params"]["observer_contract"] == 2
    assert websocket.sent[0]["params"]["type"] == "tool.update"


@pytest.mark.asyncio
async def test_forward_events_v2_fails_closed_on_unsafe_projection() -> None:
    websocket = _V2WebSocket()

    await realtime._forward_events(
        websocket=websocket,  # type: ignore[arg-type]
        source=_V2EventSource(unsafe=True),  # type: ignore[arg-type]
        principal=_principal(),
        session_key="session-root-1",
        profile="default",
        runtime_session_id="runtime-session-1",
        runtime_generation="generation-v2",
        after_sequence=0,
        observer_contract=2,
        lock=asyncio.Lock(),
    )

    assert websocket.sent == []
    assert websocket.close_codes == [1002]


@pytest.mark.asyncio
async def test_send_timeout_is_bounded_and_cancels_peer_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    websocket = _BlockedSendWebSocket()
    monkeypatch.setattr(realtime, "_SEND_TIMEOUT_SECONDS", 0.01, raising=False)

    with pytest.raises(RuntimeError, match="peer send"):
        await asyncio.wait_for(
            realtime._send_json(
                websocket,  # type: ignore[arg-type]
                {"jsonrpc": "2.0"},
                asyncio.Lock(),
            ),
            timeout=0.1,
        )

    assert websocket.cancelled


@pytest.mark.asyncio
async def test_forward_failure_cancels_the_supervised_receive() -> None:
    websocket = _BlockedReceiveWebSocket()
    supervisor = getattr(realtime, "_receive_with_forward_supervision", None)

    assert supervisor is not None

    async def failed_forward() -> None:
        await asyncio.sleep(0)
        raise RuntimeError("peer send failed")

    forward_task = asyncio.create_task(failed_forward())
    with pytest.raises(RuntimeError, match="peer send"):
        await supervisor(
            websocket,  # type: ignore[arg-type]
            forward_task,
        )

    assert websocket.receive_cancelled
    assert forward_task.done()
    assert forward_task.exception() is not None


@pytest.mark.asyncio
async def test_forward_events_normalizes_peer_send_failure() -> None:
    with pytest.raises(RuntimeError, match="peer send"):
        await realtime._forward_events(
            websocket=_FailedSendWebSocket(),  # type: ignore[arg-type]
            source=_EventSource(),  # type: ignore[arg-type]
            principal=_principal(),
            session_key="session-root-1",
            profile="default",
            runtime_session_id="runtime-session-1",
            after_sequence=0,
            lock=asyncio.Lock(),
        )


@pytest.mark.asyncio
async def test_noncooperative_forward_cancel_is_bounded_closes_and_observes_late_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        realtime,
        "_FORWARD_CANCEL_TIMEOUT_SECONDS",
        0.01,
        raising=False,
    )
    websocket = _ClosingWebSocket()
    release = asyncio.Event()
    started = asyncio.Event()
    loop_errors: list[dict[str, object]] = []
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: loop_errors.append(context))

    async def ignores_cancel_then_fails() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await release.wait()
            raise RuntimeError("late forward failure")

    forward_task: asyncio.Task[None] | None = asyncio.create_task(
        ignores_cancel_then_fails()
    )
    await started.wait()
    cleanup_completed = False
    try:
        stopped = await asyncio.wait_for(
            realtime._cancel_forward_task(
                forward_task,
                websocket,  # type: ignore[arg-type]
            ),
            timeout=0.1,
        )
        cleanup_completed = True

        assert stopped is False
        assert websocket.close_codes == [1011]
        assert not forward_task.done()

        release.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert forward_task.done()
        forward_task = None
        gc.collect()
        await asyncio.sleep(0)
        assert loop_errors == []
    finally:
        loop.set_exception_handler(previous_handler)
        release.set()
        if not cleanup_completed and forward_task is not None:
            forward_task.cancel()
            with suppress(asyncio.CancelledError, RuntimeError):
                await forward_task


@pytest.mark.asyncio
async def test_cooperative_forward_cancel_finishes_without_closing_peer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        realtime,
        "_FORWARD_CANCEL_TIMEOUT_SECONDS",
        0.01,
        raising=False,
    )
    websocket = _ClosingWebSocket()
    started = asyncio.Event()

    async def cooperative_forward() -> None:
        started.set()
        await asyncio.Event().wait()

    forward_task = asyncio.create_task(cooperative_forward())
    await started.wait()

    assert await realtime._cancel_forward_task(
        forward_task,
        websocket,  # type: ignore[arg-type]
    )
    assert forward_task.cancelled()
    assert websocket.close_codes == []
