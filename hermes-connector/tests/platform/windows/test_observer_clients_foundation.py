from __future__ import annotations

import os
from uuid import UUID

import pytest
from hermes_agent_plugin.adapters.platform.windows.local_relay import (
    create_local_relay_backend,
)
from hermes_agent_plugin.adapters.platform.windows.runtime_authority import (
    capture_windows_host_authority,
)

from hermes_connector.adapters.platform.windows.observer_client import (
    WindowsObserverClient,
)
from hermes_connector.adapters.platform.windows.process_identity import (
    normalize_process_identity,
)
from hermes_connector.adapters.platform.windows.session_catalog_client import (
    WindowsSessionCatalogClient,
)
from hermes_connector.domain.local_gateway import LocalRuntimeAuthority

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows Named Pipes required")


def _plugin_authority():
    return capture_windows_host_authority(
        profile="default",
        host_bundle_id="com.hermes.windows-observer-client-test",
    ).bind_runtime("runtime-generation-1")


def _runtime_authority(plugin_authority) -> LocalRuntimeAuthority:
    identity = normalize_process_identity(plugin_authority.process_identity)
    assert identity is not None
    return LocalRuntimeAuthority(
        profile=plugin_authority.profile,
        runtime_generation=plugin_authority.runtime_generation,
        instance_id=plugin_authority.instance_id,
        host_bundle_id=plugin_authority.host_bundle_id,
        process_identity=identity,
        required_capabilities=("session.observe",),
        optional_capabilities=("session.catalog.v1",),
    )


def _observer_snapshot(request_id: object) -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {
            "subscription_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
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


def _observer_event(sequence: int) -> dict[str, object]:
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


def _catalog_entry(session_key: str) -> dict[str, object]:
    return {
        "session_key": session_key,
        "surface": "gateway",
        "authority_revision": 3,
        "available_actions": ["prompt.submit"],
    }


def _catalog_page(
    *,
    request_id: object,
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
            "sessions": [] if page_index else [_catalog_entry("durable-session-real")],
            "next_cursor": None if is_last else "opaque-cursor",
        },
    }


def _catalog_event(sequence: int) -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "method": "session.catalog.event",
        "params": {
            "subscription_id": "22222222-2222-4222-8222-222222222222",
            "profile": "default",
            "runtime_generation": "runtime-generation-1",
            "catalog_sequence": sequence,
            "action": "upsert",
            "entry": _catalog_entry(f"durable-session-{sequence}"),
        },
    }


@pytest.mark.asyncio
async def test_windows_high_level_observer_and_catalog_share_authority_and_stream() -> None:
    plugin_authority = _plugin_authority()
    current = _runtime_authority(plugin_authority)
    observed: list[dict] = []
    transports: dict[str, object] = {}
    disconnected: list[object] = []

    async def authority() -> LocalRuntimeAuthority:
        return current

    def dispatch(request: dict, transport: object) -> dict | None:
        observed.append(request)
        method = request.get("method")
        transports[str(method)] = transport
        if method == "session.observe.subscribe":
            return _observer_snapshot(request.get("id"))
        if method == "session.observe.unsubscribe":
            return None
        if method == "session.catalog.subscribe":
            return _catalog_page(
                request_id=request.get("id"),
                page_index=0,
                is_last=False,
            )
        if method == "session.catalog.page":
            assert transport.write(_catalog_event(8)) is True
            return _catalog_page(
                request_id=request.get("id"),
                page_index=1,
                is_last=True,
            )
        if method == "session.catalog.unsubscribe":
            return None
        raise AssertionError(f"unexpected method: {method}")

    backend = create_local_relay_backend()
    registration = backend.start_observer_endpoint(
        authority=plugin_authority,
        dispatch=dispatch,
        remove_observer_subscriptions=disconnected.append,
        observer_contract=1,
    )
    observer_client = WindowsObserverClient(
        authority=authority,
        authority_poll_seconds=0.01,
    )
    catalog_ids = iter(
        (
            UUID("11111111-1111-4111-8111-111111111111"),
            UUID("44444444-4444-4444-8444-444444444444"),
            UUID("55555555-5555-4555-8555-555555555555"),
        )
    )
    catalog_client = WindowsSessionCatalogClient(
        authority=authority,
        authority_poll_seconds=0.01,
        request_id_factory=catalog_ids.__next__,
    )
    observer_subscription = None
    catalog_subscription = None
    try:
        observer_subscription = await observer_client.subscribe(
            profile="default",
            session_key="session-root-1",
        )
        catalog_subscription = await catalog_client.subscribe(
            profile="default",
            runtime_generation="runtime-generation-1",
            page_size=128,
        )

        assert observer_subscription.snapshot.event_sequence == 4
        pages = [page async for page in catalog_subscription.pages()]
        assert [page.page_index for page in pages] == [0, 1]
        assert pages[0].sessions[0].session_key == "durable-session-real"

        observer_transport = transports["session.observe.subscribe"]
        assert observer_transport.write(_observer_event(5)) is True
        observer_events = observer_subscription.events()
        observer_event = await anext(observer_events)
        assert observer_event.event_sequence == 5
        assert observer_event.payload == {"text": "delta-5"}

        catalog_events = catalog_subscription.events()
        first_catalog_event = await anext(catalog_events)
        assert first_catalog_event.catalog_sequence == 8
        catalog_transport = transports["session.catalog.subscribe"]
        assert catalog_transport.write(_catalog_event(9)) is True
        second_catalog_event = await anext(catalog_events)
        assert second_catalog_event.catalog_sequence == 9

        observe_request = next(
            item for item in observed if item.get("method") == "session.observe.subscribe"
        )
        assert observe_request["params"]["relay_local_only"] is True
        assert any(item.get("method") == "session.catalog.page" for item in observed)
    finally:
        if observer_subscription is not None:
            await observer_subscription.close()
        if catalog_subscription is not None:
            await catalog_subscription.close()
        await observer_client.aclose()
        await catalog_client.aclose()
        registration.close()

    assert backend.list_observer_endpoints() == []
    assert len(disconnected) == 2


@pytest.mark.asyncio
async def test_windows_observer_authority_rollover_closes_old_stream() -> None:
    plugin_authority = _plugin_authority()
    current = _runtime_authority(plugin_authority)

    async def authority() -> LocalRuntimeAuthority:
        return current

    def dispatch(request: dict, _transport: object) -> dict | None:
        if request.get("method") == "session.observe.subscribe":
            return _observer_snapshot(request.get("id"))
        return None

    backend = create_local_relay_backend()
    registration = backend.start_observer_endpoint(
        authority=plugin_authority,
        dispatch=dispatch,
        remove_observer_subscriptions=lambda _transport: None,
        observer_contract=1,
    )
    client = WindowsObserverClient(
        authority=authority,
        authority_poll_seconds=0.01,
    )
    subscription = await client.subscribe(
        profile="default",
        session_key="session-root-1",
    )
    current = LocalRuntimeAuthority(
        profile=current.profile,
        runtime_generation="runtime-generation-2",
        instance_id=current.instance_id,
        host_bundle_id=current.host_bundle_id,
        process_identity=current.process_identity,
        required_capabilities=current.required_capabilities,
        optional_capabilities=current.optional_capabilities,
    )
    try:
        with pytest.raises(RuntimeError, match="authority"):
            await anext(subscription.events())
    finally:
        await subscription.close()
        registration.close()
