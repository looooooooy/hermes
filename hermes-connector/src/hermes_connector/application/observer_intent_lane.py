from __future__ import annotations

import asyncio
import math
from collections import deque
from collections.abc import AsyncIterator, Awaitable
from dataclasses import dataclass
from typing import Protocol

from hermes_connector.adapters.platform.macos.observer_client import (
    ObserverResnapshotRequired,
)
from hermes_connector.domain.observer import (
    SessionEvent,
    SessionObserveClose,
    SessionObserveOpen,
    SessionSnapshot,
    StreamAck,
    StreamNack,
)
from hermes_connector.domain.storage import ObserverOutboxRecord


class ObserverIntentMismatch(ValueError):
    """A close or recovery command does not match the authorized target."""


class ObserverSubscriptionLimitExceeded(RuntimeError):
    """The bounded local Observer subscription capacity is exhausted."""


class _Subscription(Protocol):
    snapshot: SessionSnapshot

    def events(self) -> AsyncIterator[SessionEvent]: ...

    async def close(self) -> None: ...


class _LocalClient(Protocol):
    async def subscribe(
        self,
        *,
        profile: str,
        session_key: str,
    ) -> _Subscription: ...

    async def aclose(self) -> None: ...


class _Publisher(Protocol):
    async def publish_observer_snapshot(
        self,
        snapshot: SessionSnapshot,
        *,
        force_new_attempt: bool = False,
    ) -> ObserverOutboxRecord: ...

    async def publish_observer_event(self, event: SessionEvent) -> object: ...


@dataclass(slots=True)
class _Active:
    intent: SessionObserveOpen
    subscription: _Subscription | None
    task: asyncio.Task[None] | None = None
    recovery: _Recovery | None = None
    recovered_nacks: set[tuple[str, str, int]] | None = None
    recovered_nack_order: deque[tuple[str, str, int]] | None = None

    def __post_init__(self) -> None:
        if self.recovered_nacks is None:
            self.recovered_nacks = set()
        if self.recovered_nack_order is None:
            self.recovered_nack_order = deque()


@dataclass(frozen=True, slots=True)
class _Recovery:
    trigger_nack_identity: tuple[str, str, int] | None
    snapshot_message_id: str
    snapshot_connector_sequence: int
    snapshot_payload_digest: str
    snapshot_message_type: str


class ObserverIntentLane:
    """Apply only Cloud-authorized Observer targets to the local UDS client."""

    def __init__(
        self,
        *,
        local_client: _LocalClient,
        publisher: _Publisher,
        cleanup_timeout_seconds: float = 1.0,
        max_active_subscriptions: int = 32,
    ) -> None:
        if not math.isfinite(cleanup_timeout_seconds) or cleanup_timeout_seconds <= 0:
            raise ValueError("Observer cleanup timeout must be positive")
        if type(max_active_subscriptions) is not int or not (
            1 <= max_active_subscriptions <= 256
        ):
            raise ValueError(
                "Observer subscription bound must be an integer between 1 and 256"
            )
        self._local_client = local_client
        self._publisher = publisher
        self._active: dict[str, _Active] = {}
        self._targets: dict[tuple[str, str], str] = {}
        self._lock = asyncio.Lock()
        self._failure: BaseException | None = None
        self._cleanup_timeout_seconds = cleanup_timeout_seconds
        self._max_active_subscriptions = max_active_subscriptions

    @property
    def failure(self) -> BaseException | None:
        return self._failure

    def raise_if_failed(self) -> None:
        failure = self._failure
        self._failure = None
        if failure is not None:
            raise failure

    async def open(self, intent: SessionObserveOpen) -> None:
        if intent.target_source != "cloud_authorized_binding":
            raise ObserverIntentMismatch("Observer target source is not authoritative")
        async with self._lock:
            subscription_id = str(intent.subscription_id)
            active = self._active.get(subscription_id)
            if active is not None and (
                active.intent.subscription_id == intent.subscription_id
                and active.intent.profile == intent.profile
                and active.intent.session_key == intent.session_key
                and active.intent.observer_contract == intent.observer_contract
            ):
                subscription = active.subscription
                if subscription is not None:
                    await self._publisher.publish_observer_snapshot(
                        subscription.snapshot
                    )
                return
            if active is not None:
                raise ObserverIntentMismatch(
                    "Observer subscription identity target does not match"
                )
            target = (intent.profile, intent.session_key)
            if target in self._targets:
                raise ObserverIntentMismatch(
                    "Observer target already has an active subscription"
                )
            if len(self._active) >= self._max_active_subscriptions:
                raise ObserverSubscriptionLimitExceeded(
                    "Observer active subscription bound is exhausted"
                )
            await self._open_locked(intent)

    async def close(self, intent: SessionObserveClose) -> None:
        if intent.target_source != "cloud_authorized_binding":
            raise ObserverIntentMismatch("Observer target source is not authoritative")
        async with self._lock:
            subscription_id = str(intent.subscription_id)
            active = self._active.get(subscription_id)
            if active is None:
                return
            if (
                active.intent.subscription_id != intent.subscription_id
                or active.intent.profile != intent.profile
                or active.intent.session_key != intent.session_key
                or active.intent.observer_contract != intent.observer_contract
            ):
                raise ObserverIntentMismatch("Observer close target does not match")
            await self._close_active_locked(subscription_id, active)

    async def recover(self, nack: StreamNack) -> None:
        async with self._lock:
            nack_identity = _receipt_identity(nack)
            subscription_id = self._targets.get((nack.profile, nack.session_key))
            active = (
                self._active.get(subscription_id)
                if subscription_id is not None
                else None
            )
            if active is None:
                raise ObserverIntentMismatch("Observer recovery has no active target")
            if nack.observer_contract != active.intent.observer_contract:
                raise ObserverIntentMismatch("Observer recovery contract does not match")
            recovered_nacks = active.recovered_nacks
            assert recovered_nacks is not None
            if nack_identity in recovered_nacks:
                return
            recovery = active.recovery
            if recovery is not None:
                subscription = active.subscription
                if subscription is None:
                    return
                if (
                    nack_identity == recovery.trigger_nack_identity
                    or not _receipt_matches_recovery(
                        nack,
                        recovery,
                        subscription.snapshot,
                        active.intent,
                    )
                ):
                    return
            self._remember_recovery(active, nack_identity)
            intent = active.intent
            history = (
                set(active.recovered_nacks or ()),
                deque(active.recovered_nack_order or ()),
            )
            assert subscription_id is not None
            await self._close_active_locked(subscription_id, active)
            if nack.recovery == "send_snapshot":
                await self._open_locked(
                    intent,
                    recovery_nack=nack,
                    recovery_history=history,
                )

    async def acknowledge(self, ack: StreamAck) -> None:
        async with self._lock:
            subscription_id = self._targets.get((ack.profile, ack.session_key))
            active = (
                self._active.get(subscription_id)
                if subscription_id is not None
                else None
            )
            recovery = active.recovery if active is not None else None
            if active is None or recovery is None:
                return
            subscription = active.subscription
            if subscription is None:
                return
            snapshot = subscription.snapshot
            if not _receipt_matches_recovery(
                ack,
                recovery,
                snapshot,
                active.intent,
            ):
                return
            active.recovery = None
            self._start_pump(active)

    async def shutdown(self) -> None:
        failure: BaseException | None = None
        async with self._lock:
            for subscription_id, active in tuple(self._active.items()):
                try:
                    await self._close_active_locked(subscription_id, active)
                except BaseException as error:  # noqa: BLE001 - preserve cancellation
                    failure = failure or error
            try:
                await self._bounded_cleanup(self._local_client.aclose())
            except BaseException as error:  # noqa: BLE001 - preserve cancellation
                failure = failure or error
        if failure is not None:
            raise failure

    async def _open_locked(
        self,
        intent: SessionObserveOpen,
        *,
        recovery_nack: StreamNack | None = None,
        recovery_history: tuple[
            set[tuple[str, str, int]],
            deque[tuple[str, str, int]],
        ]
        | None = None,
    ) -> None:
        subscription = await self._local_client.subscribe(
            profile=intent.profile,
            session_key=intent.session_key,
        )
        snapshot = subscription.snapshot
        if (
            snapshot.profile != intent.profile
            or snapshot.session_key != intent.session_key
            or snapshot.observer_contract != intent.observer_contract
        ):
            await self._bounded_cleanup(subscription.close())
            raise ObserverIntentMismatch("Observer snapshot target does not match")
        try:
            record = await self._publisher.publish_observer_snapshot(
                snapshot,
                force_new_attempt=recovery_nack is not None,
            )
        except BaseException:
            await self._bounded_cleanup(subscription.close())
            raise
        recovery = None
        if recovery_nack is not None:
            recovery = _recovery_attempt(
                record,
                trigger_nack_identity=_receipt_identity(recovery_nack),
            )
        active = _Active(
            intent=intent,
            subscription=subscription,
            recovery=recovery,
            recovered_nacks=(recovery_history or (set(), deque()))[0],
            recovered_nack_order=(recovery_history or (set(), deque()))[1],
        )
        subscription_id = str(intent.subscription_id)
        self._active[subscription_id] = active
        self._targets[(intent.profile, intent.session_key)] = subscription_id
        if recovery is None:
            self._start_pump(active)

    def _start_pump(self, active: _Active) -> None:
        if active.task is not None:
            return
        active.task = asyncio.create_task(
            self._pump(active),
            name=f"hermes-connector:observer-intent:{active.intent.subscription_id}",
        )

    async def _close_active_locked(
        self,
        subscription_id: str,
        active: _Active,
    ) -> None:
        if self._active.get(subscription_id) is not active:
            return
        self._active.pop(subscription_id, None)
        target = (active.intent.profile, active.intent.session_key)
        if self._targets.get(target) == subscription_id:
            self._targets.pop(target, None)
        task = active.task
        if task is not None and task is not asyncio.current_task():
            task.cancel()
        failure: BaseException | None = None
        if task is not None and task is not asyncio.current_task():
            try:
                await self._bounded_cleanup(_gather_task(task))
            except BaseException as error:  # noqa: BLE001 - preserve cancellation
                failure = failure or error
        subscription = active.subscription
        active.subscription = None
        if subscription is not None:
            try:
                await self._bounded_cleanup(subscription.close())
            except BaseException as error:  # noqa: BLE001 - preserve cancellation
                failure = failure or error
        if failure is not None:
            raise failure

    async def _pump(self, active: _Active) -> None:
        subscription_id = str(active.intent.subscription_id)
        try:
            while self._active.get(subscription_id) is active:
                subscription = active.subscription
                if subscription is None:
                    return
                try:
                    async for event in subscription.events():
                        if self._active.get(subscription_id) is not active:
                            return
                        await self._publisher.publish_observer_event(event)
                    return
                except ObserverResnapshotRequired:
                    active.subscription = None
                    await self._bounded_cleanup(subscription.close())
                    if self._active.get(subscription_id) is not active:
                        return
                    replacement = await self._local_client.subscribe(
                        profile=active.intent.profile,
                        session_key=active.intent.session_key,
                    )
                    replacement_owned = True
                    try:
                        snapshot = replacement.snapshot
                        if (
                            snapshot.profile != active.intent.profile
                            or snapshot.session_key != active.intent.session_key
                            or snapshot.observer_contract
                            != active.intent.observer_contract
                        ):
                            raise ObserverIntentMismatch(
                                "Observer replacement snapshot target does not match"
                            )
                        if self._active.get(subscription_id) is not active:
                            return
                        record = await self._publisher.publish_observer_snapshot(
                            snapshot,
                            force_new_attempt=True,
                        )
                        if self._active.get(subscription_id) is not active:
                            return
                        active.subscription = replacement
                        active.recovery = _recovery_attempt(
                            record,
                            trigger_nack_identity=None,
                        )
                        active.task = None
                        replacement_owned = False
                        return
                    finally:
                        if replacement_owned:
                            await self._bounded_cleanup(replacement.close())
        except asyncio.CancelledError:
            raise
        except (ConnectionError, OSError, RuntimeError, TypeError, ValueError) as error:
            if self._failure is None:
                self._failure = error
            subscription = active.subscription
            active.subscription = None
            if subscription is not None:
                await self._bounded_cleanup(subscription.close())
            if self._active.get(subscription_id) is active:
                self._active.pop(subscription_id, None)
                target = (active.intent.profile, active.intent.session_key)
                if self._targets.get(target) == subscription_id:
                    self._targets.pop(target, None)

    def _remember_recovery(
        self,
        active: _Active,
        identity: tuple[str, str, int],
    ) -> None:
        recovered_nacks = active.recovered_nacks
        recovered_nack_order = active.recovered_nack_order
        assert recovered_nacks is not None
        assert recovered_nack_order is not None
        if len(recovered_nack_order) >= 256:
            oldest = recovered_nack_order.popleft()
            recovered_nacks.discard(oldest)
        recovered_nack_order.append(identity)
        recovered_nacks.add(identity)

    async def _bounded_cleanup(self, awaitable: Awaitable[None]) -> None:
        task = asyncio.ensure_future(awaitable)
        try:
            async with asyncio.timeout(self._cleanup_timeout_seconds):
                await asyncio.shield(task)
        except TimeoutError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        except asyncio.CancelledError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise


def _receipt_identity(nack: StreamNack) -> tuple[str, str, int]:
    return (
        str(nack.observer_message_id),
        nack.payload_digest,
        nack.connector_sequence,
    )


def _recovery_attempt(
    record: ObserverOutboxRecord,
    *,
    trigger_nack_identity: tuple[str, str, int] | None,
) -> _Recovery:
    return _Recovery(
        trigger_nack_identity=trigger_nack_identity,
        snapshot_message_id=record.message_id,
        snapshot_connector_sequence=record.connector_sequence,
        snapshot_payload_digest=record.payload_digest,
        snapshot_message_type=record.message_type,
    )


def _receipt_matches_recovery(
    receipt: StreamAck | StreamNack,
    recovery: _Recovery,
    snapshot: SessionSnapshot,
    intent: SessionObserveOpen,
) -> bool:
    return (
        str(receipt.observer_message_id) == recovery.snapshot_message_id
        and receipt.connector_sequence == recovery.snapshot_connector_sequence
        and receipt.payload_digest == recovery.snapshot_payload_digest
        and receipt.observer_message_type == recovery.snapshot_message_type
        and receipt.observer_contract == intent.observer_contract
        and receipt.observer_message_type
        == _snapshot_message_type(intent.observer_contract)
        and receipt.profile == snapshot.profile
        and receipt.session_key == snapshot.session_key
        and receipt.runtime_generation == snapshot.runtime_generation
        and receipt.runtime_session_id == snapshot.runtime_session_id
        and receipt.event_sequence == snapshot.event_sequence
    )


def _snapshot_message_type(observer_contract: int) -> str:
    if observer_contract == 1:
        return "session.snapshot"
    if observer_contract == 2:
        return "session.snapshot.v2"
    raise ObserverIntentMismatch("Observer contract is unsupported")


async def _gather_task(task: asyncio.Task[None]) -> None:
    await asyncio.gather(task, return_exceptions=True)


__all__ = [
    "ObserverIntentLane",
    "ObserverIntentMismatch",
    "ObserverSubscriptionLimitExceeded",
]
