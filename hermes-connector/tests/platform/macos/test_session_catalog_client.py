from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_connector.adapters.platform.macos.observer_discovery import ObserverEndpoint
from hermes_connector.adapters.platform.macos.process_identity import (
    current_process_identity,
)
from hermes_connector.adapters.platform.macos.session_catalog_client import (
    MacOSSessionCatalogClient as _MacOSSessionCatalogClient,
)
from hermes_connector.adapters.platform.macos.session_catalog_client import (
    SessionCatalogResnapshotRequired,
)

_PROCESS_IDENTITY = current_process_identity(os.getpid())
assert _PROCESS_IDENTITY is not None


class _Discovery:
    async def discover(self, profile: str) -> tuple[ObserverEndpoint, ...]:
        return (_endpoint(),) if profile == "default" else ()

    async def aclose(self) -> None:
        return None


class _PeerSocket:
    def getsockopt(self, _level: int, _option: int) -> int:
        return os.getpid()


class _Transport:
    def get_extra_info(self, name: str):
        return _PeerSocket() if name == "socket" else None


class _Socket:
    def __init__(self, incoming: list[dict[str, object]]) -> None:
        self.incoming: asyncio.Queue[str] = asyncio.Queue()
        for frame in incoming:
            self.incoming.put_nowait(json.dumps(frame))
        self.sent: list[dict[str, object]] = []
        self.closed = False
        self.transport = _Transport()

    async def recv(self) -> str:
        return await self.incoming.get()

    async def send(self, frame: str | bytes) -> None:
        if isinstance(frame, bytes):
            frame = frame.decode()
        self.sent.append(json.loads(frame))

    async def close(self) -> None:
        self.closed = True


def _endpoint() -> ObserverEndpoint:
    path = Path("/tmp/hermes-catalog-test/observer.sock")
    return ObserverEndpoint(
        pid=os.getpid(),
        profile="default",
        runtime_generation="runtime-generation-1",
        socket_path=path,
        instance_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        host_bundle_id="com.nousresearch.hermes",
        process_identity=_PROCESS_IDENTITY,
        socket_device=1,
        socket_inode=2,
        registry_path=path.with_suffix(".json"),
    )


async def _authority():
    endpoint = _endpoint()
    return SimpleNamespace(
        profile=endpoint.profile,
        runtime_generation=endpoint.runtime_generation,
        instance_id=endpoint.instance_id,
        host_bundle_id=endpoint.host_bundle_id,
        process_identity=endpoint.process_identity,
        required_capabilities=("session.observe",),
        optional_capabilities=("session.catalog.v1",),
    )


def _ready() -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "method": "event",
        "params": {
            "type": "gateway.ready",
            "payload": {
                "observer_contract": 1,
                "local_gateway_protocol": 1,
                "connection_role": "observer",
                "profile": "default",
                "runtime_generation": "runtime-generation-1",
                "instance_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            },
        },
    }


def _page(
    *,
    request_id: str,
    page_index: int,
    is_last: bool,
) -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {
            "subscription_id": "22222222-2222-4222-8222-222222222222",
            "snapshot_id": "33333333-3333-4333-8333-333333333333",
            "profile": "default",
            "runtime_generation": "runtime-generation-1",
            "catalog_revision": 7,
            "page_index": page_index,
            "is_last": is_last,
            "sessions": [] if page_index else [_entry("durable-session-real")],
            "next_cursor": None if is_last else "opaque-cursor",
        },
    }


def _entry(session_key: str) -> dict[str, object]:
    return {
        "session_key": session_key,
        "surface": "gateway",
        "authority_revision": 3,
        "available_actions": ["prompt.submit"],
    }


def _event(sequence: int) -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "method": "session.catalog.event",
        "params": {
            "subscription_id": "22222222-2222-4222-8222-222222222222",
            "profile": "default",
            "runtime_generation": "runtime-generation-1",
            "catalog_sequence": sequence,
            "action": "upsert",
            "entry": _entry(f"durable-session-{sequence}"),
        },
    }


def _client(socket: _Socket) -> _MacOSSessionCatalogClient:
    async def connect(_path: str, **_options: object) -> _Socket:
        return socket

    return _MacOSSessionCatalogClient(
        discovery=_Discovery(),
        authority=_authority,
        connect=connect,
        process_identity_provider=lambda _pid: _PROCESS_IDENTITY,
        socket_identity_provider=lambda _endpoint: True,
        request_id_factory=iter(
            (
                "11111111-1111-4111-8111-111111111111",
                "44444444-4444-4444-8444-444444444444",
                "55555555-5555-4555-8555-555555555555",
            )
        ).__next__,
    )


@pytest.mark.asyncio
async def test_catalog_pages_buffer_events_and_then_enforce_contiguous_sequence() -> None:
    socket = _Socket(
        [
            _ready(),
            _page(
                request_id="11111111-1111-4111-8111-111111111111",
                page_index=0,
                is_last=False,
            ),
            _event(8),
            _page(
                request_id="44444444-4444-4444-8444-444444444444",
                page_index=1,
                is_last=True,
            ),
            _event(9),
        ]
    )
    subscription = await _client(socket).subscribe(
        profile="default",
        runtime_generation="runtime-generation-1",
        page_size=128,
    )

    pages = [page async for page in subscription.pages()]
    events = subscription.events()
    first = await anext(events)
    second = await anext(events)

    assert [page.page_index for page in pages] == [0, 1]
    assert str(pages[0].snapshot_id) == "33333333-3333-4333-8333-333333333333"
    assert [first.catalog_sequence, second.catalog_sequence] == [8, 9]
    assert socket.sent[0]["method"] == "session.catalog.subscribe"
    assert socket.sent[1]["method"] == "session.catalog.page"
    await subscription.close()
    assert socket.sent[-1]["method"] == "session.catalog.unsubscribe"


@pytest.mark.asyncio
async def test_catalog_event_gap_requires_a_fresh_snapshot() -> None:
    socket = _Socket(
        [
            _ready(),
            _page(
                request_id="11111111-1111-4111-8111-111111111111",
                page_index=0,
                is_last=True,
            ),
            _event(9),
        ]
    )
    subscription = await _client(socket).subscribe(
        profile="default",
        runtime_generation="runtime-generation-1",
    )
    _ = [page async for page in subscription.pages()]

    with pytest.raises(SessionCatalogResnapshotRequired, match="gap"):
        await anext(subscription.events())


@pytest.mark.asyncio
async def test_runtime_generation_rollover_closes_the_old_subscription() -> None:
    socket = _Socket(
        [
            _ready(),
            _page(
                request_id="11111111-1111-4111-8111-111111111111",
                page_index=0,
                is_last=True,
            ),
        ]
    )
    current = await _authority()

    async def authority():
        return current

    async def connect(_path: str, **_options: object) -> _Socket:
        return socket

    client = _MacOSSessionCatalogClient(
        discovery=_Discovery(),
        authority=authority,
        connect=connect,
        process_identity_provider=lambda _pid: _PROCESS_IDENTITY,
        socket_identity_provider=lambda _endpoint: True,
        authority_poll_seconds=0.001,
        request_id_factory=iter(
            ("11111111-1111-4111-8111-111111111111",)
        ).__next__,
    )
    subscription = await client.subscribe(
        profile="default",
        runtime_generation="runtime-generation-1",
    )
    _ = [page async for page in subscription.pages()]
    current = SimpleNamespace(
        **{
            **vars(current),
            "runtime_generation": "runtime-generation-2",
        }
    )

    with pytest.raises(SessionCatalogResnapshotRequired, match="unavailable"):
        await anext(subscription.events())
    assert socket.closed is True
