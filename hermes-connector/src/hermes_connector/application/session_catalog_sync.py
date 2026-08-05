from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Protocol
from uuid import UUID, uuid4

from hermes_connector.domain.local_gateway import LocalRuntimeAuthority
from hermes_connector.domain.session_catalog import (
    LocalSessionCatalogPage,
    SessionCatalogAck,
    SessionCatalogEvent,
    SessionCatalogNack,
    SessionCatalogResnapshotRequired,
    SessionCatalogSnapshotPage,
)


class _LocalSubscription(Protocol):
    def pages(self) -> AsyncIterator[LocalSessionCatalogPage]: ...

    def events(self) -> AsyncIterator[SessionCatalogEvent]: ...

    async def close(self) -> None: ...


class _LocalClient(Protocol):
    async def subscribe(
        self,
        *,
        profile: str,
        runtime_generation: str,
        page_size: int = 128,
    ) -> _LocalSubscription: ...

    async def aclose(self) -> None: ...


class _Publisher(Protocol):
    @property
    def session_catalog_enabled(self) -> bool: ...

    async def publish_session_catalog_snapshot_page(
        self,
        page: SessionCatalogSnapshotPage,
        *,
        force_new_attempt: bool = False,
    ) -> object: ...

    async def publish_session_catalog_event(
        self,
        event: SessionCatalogEvent,
        *,
        force_new_attempt: bool = False,
    ) -> object: ...

    async def wait_session_catalog_capability_change(
        self,
        after_generation: int,
    ) -> tuple[int, bool, bool]: ...

AuthorityProvider = Callable[[], Awaitable[LocalRuntimeAuthority | None]]


class SessionCatalogSync:
    """Mirror the authoritative Host catalog into the negotiated Cloud lane."""

    name = "session_catalog_sync"

    def __init__(
        self,
        *,
        profile: str,
        local_client: _LocalClient,
        publisher: _Publisher,
        runtime_authority: AuthorityProvider,
        snapshot_id_factory: Callable[[], UUID] = uuid4,
        page_size: int = 128,
    ) -> None:
        if not isinstance(profile, str) or not 1 <= len(profile) <= 128:
            raise ValueError("session catalog profile is invalid")
        if type(page_size) is not int or not 1 <= page_size <= 128:
            raise ValueError("session catalog page size is invalid")
        self._profile = profile
        self._local_client = local_client
        self._publisher = publisher
        self._runtime_authority = runtime_authority
        self._snapshot_id_factory = snapshot_id_factory
        self._page_size = page_size
        self._ready = asyncio.Event()
        self._reset_required = asyncio.Event()
        self._stopping = asyncio.Event()
        self._started = False
        self._active: _LocalSubscription | None = None

    async def start(self) -> None:
        if self._started:
            raise RuntimeError("session catalog sync is already started")
        self._started = True
        if not self._publisher.session_catalog_enabled:
            self._ready.set()

    async def run(self) -> None:
        if not self._started:
            raise RuntimeError("session catalog sync is not started")
        capability_generation = -1
        capability_enabled = False
        while not self._stopping.is_set():
            if not capability_enabled:
                capability = await self._wait_for_capability_change(
                    capability_generation
                )
                if capability is None:
                    return
                (
                    capability_generation,
                    capability_enabled,
                    retire_pending,
                ) = capability
                if not capability_enabled:
                    if retire_pending:
                        self._ready.set()
                    else:
                        self._ready.clear()
                    continue
                self._ready.clear()
            self._reset_required.clear()
            authority = await self._require_authority()
            subscription = await self._local_client.subscribe(
                profile=self._profile,
                runtime_generation=authority.runtime_generation,
                page_size=self._page_size,
            )
            self._active = subscription
            active = asyncio.create_task(
                self._run_subscription(subscription),
                name="hermes-connector:session-catalog-subscription",
            )
            capability_change = asyncio.create_task(
                self._publisher.wait_session_catalog_capability_change(
                    capability_generation
                ),
                name="hermes-connector:session-catalog-capability",
            )
            stopping = asyncio.create_task(
                self._stopping.wait(),
                name="hermes-connector:session-catalog-stop",
            )
            try:
                done, _ = await asyncio.wait(
                    {active, capability_change, stopping},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if stopping in done:
                    return
                if capability_change in done:
                    (
                        capability_generation,
                        capability_enabled,
                        retire_pending,
                    ) = capability_change.result()
                    self._ready.clear()
                else:
                    active.result()
            finally:
                for task in (active, capability_change, stopping):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(
                    active,
                    capability_change,
                    stopping,
                    return_exceptions=True,
                )
                self._active = None
                await subscription.close()
            await asyncio.sleep(0)

    async def _run_subscription(self, subscription: _LocalSubscription) -> None:
        try:
            await self._publish_snapshot(subscription)
            self._ready.set()
            await self._publish_events_until_reset(subscription)
        except SessionCatalogResnapshotRequired:
            self._ready.clear()
        except RuntimeError:
            if self._publisher.session_catalog_enabled:
                raise
            self._ready.clear()

    async def ready(self) -> bool:
        # The first ready signal depends on the negotiated subscription's
        # first snapshot publish, which is asynchronous by design. Wait for it
        # instead of reporting a one-shot False; the supervisor bounds the
        # wait with the shared start deadline.
        await self._ready.wait()
        return not self._stopping.is_set()

    async def drain(self) -> None:
        self._stopping.set()
        self._reset_required.set()

    async def stop(self) -> None:
        self._stopping.set()
        self._reset_required.set()
        active = self._active
        if active is not None:
            await active.close()
        await self._local_client.aclose()

    async def acknowledge(self, _ack: SessionCatalogAck) -> None:
        """The durable outbound lane owns ACK settlement."""

    async def recover(self, _nack: SessionCatalogNack) -> None:
        self._ready.clear()
        self._reset_required.set()

    async def _publish_snapshot(self, subscription: _LocalSubscription) -> None:
        cloud_snapshot_id: UUID | None = None
        local_snapshot_id: UUID | None = None
        page_count = 0
        async for local_page in subscription.pages():
            if cloud_snapshot_id is None:
                local_snapshot_id = local_page.snapshot_id
                cloud_snapshot_id = self._snapshot_id_factory()
                if cloud_snapshot_id == local_snapshot_id:
                    raise RuntimeError(
                        "Cloud catalog snapshot id must differ from local cursor id"
                    )
            elif local_page.snapshot_id != local_snapshot_id:
                raise RuntimeError("local catalog snapshot changed between pages")
            cloud_page = SessionCatalogSnapshotPage(
                profile=local_page.profile,
                runtime_generation=local_page.runtime_generation,
                snapshot_id=cloud_snapshot_id,
                catalog_revision=local_page.catalog_revision,
                page_index=local_page.page_index,
                is_last=local_page.is_last,
                sessions=local_page.sessions,
            )
            await self._publisher.publish_session_catalog_snapshot_page(cloud_page)
            page_count += 1
        if page_count == 0:
            raise RuntimeError("local catalog snapshot contains no pages")

    async def _publish_events_until_reset(
        self,
        subscription: _LocalSubscription,
    ) -> None:
        events = subscription.events()
        while not self._stopping.is_set():
            next_event = asyncio.create_task(anext(events))
            reset = asyncio.create_task(self._reset_required.wait())
            try:
                done, _ = await asyncio.wait(
                    {next_event, reset},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if reset in done:
                    return
                event = next_event.result()
                await self._publisher.publish_session_catalog_event(event)
            except StopAsyncIteration:
                return
            finally:
                for task in (next_event, reset):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(next_event, reset, return_exceptions=True)

    async def _require_authority(self) -> LocalRuntimeAuthority:
        authority = await self._runtime_authority()
        capabilities = (
            {*authority.required_capabilities, *authority.optional_capabilities}
            if authority is not None
            else set()
        )
        if (
            authority is None
            or authority.profile != self._profile
            or "session.catalog.v1" not in capabilities
        ):
            raise RuntimeError("local session catalog authority is unavailable")
        return authority

    async def _wait_for_capability_change(
        self,
        after_generation: int,
    ) -> tuple[int, bool, bool] | None:
        capability = asyncio.create_task(
            self._publisher.wait_session_catalog_capability_change(after_generation),
            name="hermes-connector:session-catalog-capability",
        )
        stopping = asyncio.create_task(
            self._stopping.wait(),
            name="hermes-connector:session-catalog-stop",
        )
        try:
            done, _ = await asyncio.wait(
                {capability, stopping},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if stopping in done:
                return None
            return capability.result()
        finally:
            for task in (capability, stopping):
                if not task.done():
                    task.cancel()
            await asyncio.gather(capability, stopping, return_exceptions=True)


__all__ = ["SessionCatalogSync"]
