from __future__ import annotations

import asyncio
from uuid import UUID

import pytest

from hermes_connector.application.session_catalog_sync import SessionCatalogSync
from hermes_connector.domain.session_catalog import (
    LocalSessionCatalogPage,
    SessionCatalogEntry,
    SessionCatalogNack,
)


class _Subscription:
    def __init__(self, pages: tuple[LocalSessionCatalogPage, ...]) -> None:
        self._pages = pages
        self.closed = False

    async def pages(self):
        for page in self._pages:
            yield page

    async def events(self):
        await asyncio.Event().wait()
        if False:
            yield None

    async def close(self) -> None:
        self.closed = True


class _LocalClient:
    def __init__(self, subscriptions: list[_Subscription]) -> None:
        self.subscriptions = subscriptions
        self.calls = 0
        self.closed = False

    async def subscribe(self, **_values: object) -> _Subscription:
        subscription = self.subscriptions[self.calls]
        self.calls += 1
        return subscription

    async def aclose(self) -> None:
        self.closed = True


class _Publisher:
    def __init__(self, *, enabled: bool = True) -> None:
        self.session_catalog_enabled = enabled
        self.pages = []
        self.events = []
        self.retired_pending = 0
        self._capability_generation = 0
        self._capability_retire_pending = False
        self._capability_changed = asyncio.Condition()

    async def publish_session_catalog_snapshot_page(self, page, **_values: object):
        self.pages.append(page)

    async def publish_session_catalog_event(self, event, **_values: object):
        self.events.append(event)

    async def wait_session_catalog_capability_change(self, after_generation: int):
        async with self._capability_changed:
            await self._capability_changed.wait_for(
                lambda: self._capability_generation > after_generation
            )
            return (
                self._capability_generation,
                self.session_catalog_enabled,
                self._capability_retire_pending,
            )

    async def announce_capability(
        self,
        *,
        enabled: bool,
        retire_pending: bool,
    ) -> None:
        async with self._capability_changed:
            self.session_catalog_enabled = enabled
            self._capability_retire_pending = retire_pending
            if retire_pending:
                self.retired_pending += 1
            self._capability_generation += 1
            self._capability_changed.notify_all()


def _page(
    *,
    snapshot_id: str,
    page_index: int,
    is_last: bool,
) -> LocalSessionCatalogPage:
    return LocalSessionCatalogPage(
        subscription_id=UUID("22222222-2222-4222-8222-222222222222"),
        snapshot_id=UUID(snapshot_id),
        profile="default",
        runtime_generation="runtime-generation-1",
        catalog_revision=7,
        page_index=page_index,
        is_last=is_last,
        sessions=(
            SessionCatalogEntry(
                session_key=f"durable-session-{page_index}",
                surface="gateway",
                authority_revision=3,
                available_actions=("prompt.submit",),
            ),
        ),
        next_cursor=None if is_last else "opaque-cursor",
    )


@pytest.mark.asyncio
async def test_sync_replaces_local_snapshot_id_and_nack_starts_new_snapshot() -> None:
    first = _Subscription(
        (
            _page(
                snapshot_id="33333333-3333-4333-8333-333333333333",
                page_index=0,
                is_last=False,
            ),
            _page(
                snapshot_id="33333333-3333-4333-8333-333333333333",
                page_index=1,
                is_last=True,
            ),
        )
    )
    second = _Subscription(
        (
            _page(
                snapshot_id="44444444-4444-4444-8444-444444444444",
                page_index=0,
                is_last=True,
            ),
        )
    )
    local = _LocalClient([first, second])
    publisher = _Publisher()
    ids = iter(
        (
            UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
            UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
        )
    )
    sync = SessionCatalogSync(
        profile="default",
        local_client=local,
        publisher=publisher,
        runtime_authority=lambda: _authority(),
        snapshot_id_factory=ids.__next__,
    )
    await sync.start()
    runner = asyncio.create_task(sync.run())
    await _wait_until(lambda: len(publisher.pages) == 2)

    assert {str(page.snapshot_id) for page in publisher.pages} == {
        "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    }
    assert all(
        str(page.snapshot_id) != "33333333-3333-4333-8333-333333333333"
        for page in publisher.pages
    )
    await sync.recover(
        SessionCatalogNack(
            profile="default",
            runtime_generation="runtime-generation-1",
            rejected_message_id=UUID("99999999-9999-4999-8999-999999999999"),
            rejected_payload_digest="a" * 64,
            rejected_connector_sequence=1,
            reason="page_gap",
            snapshot_id=UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
            expected_page_index=0,
            expected_catalog_sequence=None,
        )
    )
    await _wait_until(lambda: len(publisher.pages) == 3)

    assert str(publisher.pages[-1].snapshot_id) == (
        "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    )
    assert first.closed is True
    await sync.drain()
    await sync.stop()
    await runner
    assert second.closed is True


@pytest.mark.asyncio
async def test_sync_starts_after_catalog_capability_is_enabled_on_reconnect() -> None:
    subscription = _Subscription(
        (
            _page(
                snapshot_id="55555555-5555-4555-8555-555555555555",
                page_index=0,
                is_last=True,
            ),
        )
    )
    local = _LocalClient([subscription])
    publisher = _Publisher(enabled=False)
    sync = SessionCatalogSync(
        profile="default",
        local_client=local,
        publisher=publisher,
        runtime_authority=lambda: _authority(),
    )
    await sync.start()
    runner = asyncio.create_task(sync.run())

    await asyncio.sleep(0)
    assert local.calls == 0
    await publisher.announce_capability(enabled=True, retire_pending=False)
    await _wait_until(lambda: local.calls == 1)

    await sync.drain()
    await sync.stop()
    await asyncio.wait_for(runner, timeout=0.2)


@pytest.mark.asyncio
async def test_capability_lifecycle_closes_retires_and_restarts_exactly_once() -> None:
    first = _Subscription(
        (
            _page(
                snapshot_id="66666666-6666-4666-8666-666666666666",
                page_index=0,
                is_last=True,
            ),
        )
    )
    second = _Subscription(
        (
            _page(
                snapshot_id="77777777-7777-4777-8777-777777777777",
                page_index=0,
                is_last=True,
            ),
        )
    )
    local = _LocalClient([first, second])
    publisher = _Publisher(enabled=True)
    sync = SessionCatalogSync(
        profile="default",
        local_client=local,
        publisher=publisher,
        runtime_authority=lambda: _authority(),
    )
    await sync.start()
    runner = asyncio.create_task(sync.run())
    await _wait_until(lambda: local.calls == 1)

    await publisher.announce_capability(enabled=False, retire_pending=True)
    await _wait_until(lambda: first.closed and publisher.retired_pending == 1)
    await publisher.announce_capability(enabled=True, retire_pending=False)
    await _wait_until(lambda: local.calls == 2)
    await asyncio.sleep(0.01)
    assert local.calls == 2

    await sync.drain()
    await sync.stop()
    await asyncio.wait_for(runner, timeout=0.2)
    assert second.closed is True


@pytest.mark.asyncio
async def test_ready_waits_for_first_published_snapshot_instead_of_failing_fast() -> None:
    subscription = _Subscription(
        (
            _page(
                snapshot_id="55555555-5555-4555-8555-555555555555",
                page_index=0,
                is_last=True,
            ),
        )
    )
    local = _LocalClient([subscription])
    publisher = _Publisher()
    sync = SessionCatalogSync(
        profile="default",
        local_client=local,
        publisher=publisher,
        runtime_authority=lambda: _authority(),
    )
    await sync.start()

    ready_task = asyncio.create_task(sync.ready())
    await asyncio.sleep(0.05)
    assert not ready_task.done(), "ready() must not fail fast while the first snapshot is pending"

    runner = asyncio.create_task(sync.run())
    async with asyncio.timeout(1):
        assert await ready_task is True
    assert len(publisher.pages) == 1

    await sync.drain()
    await sync.stop()
    await runner
    assert subscription.closed is True


async def _authority():
    from types import SimpleNamespace

    return SimpleNamespace(
        profile="default",
        runtime_generation="runtime-generation-1",
        required_capabilities=("session.observe",),
        optional_capabilities=("session.catalog.v1",),
    )


async def _wait_until(predicate) -> None:
    async with asyncio.timeout(1):
        while not predicate():
            await asyncio.sleep(0)
