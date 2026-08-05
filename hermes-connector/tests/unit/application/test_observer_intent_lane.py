from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from types import MappingProxyType, SimpleNamespace
from uuid import UUID

import pytest

from hermes_connector.adapters.platform.macos.observer_client import (
    ObserverResnapshotRequired,
)
from hermes_connector.application.observer_intent_lane import (
    ObserverIntentLane,
    ObserverSubscriptionLimitExceeded,
)
from hermes_connector.domain.observer import (
    SessionEvent,
    SessionObserveClose,
    SessionObserveOpen,
    SessionSnapshot,
    StreamAck,
    StreamNack,
)

_DEFAULT_SUBSCRIPTION_ID = UUID("82000000-0000-4000-8000-000000000001")


def _snapshot(
    generation: str = "runtime-generation-1",
    *,
    profile: str = "default",
    session_key: str = "session-root-1",
    runtime_session_id: str = "runtime-session-1",
) -> SessionSnapshot:
    return SessionSnapshot(
        profile=profile,
        runtime_generation=generation,
        session_key=session_key,
        runtime_session_id=runtime_session_id,
        running=True,
        status="running",
        event_sequence=4,
        snapshot_event_sequence=4,
        messages=(),
        inflight=MappingProxyType(
            {
                "user": None,
                "assistant": None,
                "streaming": False,
                "error": None,
            }
        ),
        replay_events=(),
    )


def _event(
    *,
    profile: str = "default",
    session_key: str = "session-root-1",
    runtime_session_id: str = "runtime-session-1",
) -> SessionEvent:
    return SessionEvent(
        profile=profile,
        runtime_generation="runtime-generation-1",
        session_key=session_key,
        session_id=runtime_session_id,
        type="message.delta",
        event_sequence=5,
        payload=MappingProxyType({"text": "live"}),
    )


def _open(
    *,
    subscription_id: UUID = _DEFAULT_SUBSCRIPTION_ID,
    profile: str = "default",
    session_key: str = "session-root-1",
) -> SessionObserveOpen:
    return SessionObserveOpen(
        request_id=UUID("81000000-0000-4000-8000-000000000001"),
        subscription_id=subscription_id,
        profile=profile,
        session_key=session_key,
        target_source="cloud_authorized_binding",
        requested_at=datetime(2026, 7, 31, 9, 0, tzinfo=UTC),
    )


class _Subscription:
    def __init__(self, snapshot: SessionSnapshot) -> None:
        self.snapshot = snapshot
        self.queue: asyncio.Queue[SessionEvent | BaseException | None] = asyncio.Queue()
        self.closed = False
        self.close_calls = 0

    async def events(self):
        while True:
            item = await self.queue.get()
            if item is None:
                return
            if isinstance(item, BaseException):
                raise item
            yield item

    async def close(self) -> None:
        self.close_calls += 1
        self.closed = True


class _LocalClient:
    def __init__(self, subscriptions: list[_Subscription]) -> None:
        self.subscriptions = subscriptions
        self.calls: list[tuple[str, str]] = []

    async def subscribe(self, *, profile: str, session_key: str) -> _Subscription:
        self.calls.append((profile, session_key))
        return self.subscriptions.pop(0)

    async def aclose(self) -> None:
        return None


class _BlockingSubscription(_Subscription):
    async def close(self) -> None:
        try:
            await asyncio.Event().wait()
        finally:
            self.closed = True


class _BlockingLocalClient(_LocalClient):
    def __init__(self, subscriptions: list[_Subscription]) -> None:
        super().__init__(subscriptions)
        self.closed = False

    async def aclose(self) -> None:
        try:
            await asyncio.Event().wait()
        finally:
            self.closed = True


class _Publisher:
    def __init__(self) -> None:
        self.snapshots: list[SessionSnapshot] = []
        self.events: list[SessionEvent] = []
        self.event_published = asyncio.Event()
        self.snapshot_attempts: list[bool] = []
        self.snapshot_records: list[SimpleNamespace] = []

    async def publish_observer_snapshot(
        self,
        snapshot: SessionSnapshot,
        *,
        force_new_attempt: bool = False,
    ) -> object:
        self.snapshots.append(snapshot)
        self.snapshot_attempts.append(force_new_attempt)
        record = SimpleNamespace(
            message_id=(f"83000000-0000-4000-8000-{len(self.snapshots):012d}"),
            connector_sequence=40 + len(self.snapshots),
            payload_digest="c" * 64,
            message_type=(
                "session.snapshot.v2"
                if snapshot.observer_contract == 2
                else "session.snapshot"
            ),
        )
        self.snapshot_records.append(record)
        return record

    async def publish_observer_event(self, event: SessionEvent) -> object:
        self.events.append(event)
        self.event_published.set()
        return object()


class _FailingReplacementPublisher(_Publisher):
    async def publish_observer_snapshot(
        self,
        snapshot: SessionSnapshot,
        *,
        force_new_attempt: bool = False,
    ) -> object:
        record = await super().publish_observer_snapshot(
            snapshot,
            force_new_attempt=force_new_attempt,
        )
        if len(self.snapshots) == 2:
            raise RuntimeError("replacement snapshot publish failed")
        return record


class _BlockingReplacementPublisher(_Publisher):
    def __init__(self) -> None:
        super().__init__()
        self.replacement_publish_started = asyncio.Event()

    async def publish_observer_snapshot(
        self,
        snapshot: SessionSnapshot,
        *,
        force_new_attempt: bool = False,
    ) -> object:
        record = await super().publish_observer_snapshot(
            snapshot,
            force_new_attempt=force_new_attempt,
        )
        if len(self.snapshots) == 2:
            self.replacement_publish_started.set()
            await asyncio.Event().wait()
        return record


@pytest.mark.asyncio
async def test_open_uses_only_explicit_cloud_target_then_publishes_snapshot_and_live() -> (
    None
):
    subscription = _Subscription(_snapshot())
    local = _LocalClient([subscription])
    publisher = _Publisher()
    lane = ObserverIntentLane(local_client=local, publisher=publisher)

    await lane.open(_open())
    subscription.queue.put_nowait(_event())
    await asyncio.wait_for(publisher.event_published.wait(), timeout=1)

    assert local.calls == [("default", "session-root-1")]
    assert publisher.snapshots == [_snapshot()]
    assert publisher.events == [_event()]
    await lane.shutdown()


@pytest.mark.asyncio
async def test_duplicate_open_is_idempotent_and_matching_close_stops_subscription() -> (
    None
):
    subscription = _Subscription(_snapshot())
    local = _LocalClient([subscription])
    publisher = _Publisher()
    lane = ObserverIntentLane(local_client=local, publisher=publisher)
    intent = _open()
    await lane.open(intent)
    await lane.open(intent)

    await lane.close(
        SessionObserveClose(
            request_id=UUID("81000000-0000-4000-8000-000000000002"),
            subscription_id=intent.subscription_id,
            profile=intent.profile,
            session_key=intent.session_key,
            target_source="cloud_authorized_binding",
            reason="client_unsubscribe",
            closed_at=datetime(2026, 7, 31, 9, 5, tzinfo=UTC),
        )
    )

    assert local.calls == [("default", "session-root-1")]
    assert publisher.snapshots == [_snapshot(), _snapshot()]
    assert subscription.closed is True


@pytest.mark.asyncio
async def test_gap_or_generation_change_closes_and_resnapshots_same_explicit_target() -> (
    None
):
    first = _Subscription(_snapshot())
    second = _Subscription(_snapshot("runtime-generation-2"))
    local = _LocalClient([first, second])
    publisher = _Publisher()
    lane = ObserverIntentLane(local_client=local, publisher=publisher)
    await lane.open(_open())

    first.queue.put_nowait(ObserverResnapshotRequired("authority changed"))
    for _ in range(100):
        if len(publisher.snapshots) == 2:
            break
        await asyncio.sleep(0)

    assert first.closed is True
    assert local.calls == [
        ("default", "session-root-1"),
        ("default", "session-root-1"),
    ]
    assert publisher.snapshots == [_snapshot(), _snapshot("runtime-generation-2")]
    assert first.close_calls == 1
    await lane.shutdown()
    assert first.close_calls == 1
    assert second.close_calls == 1


@pytest.mark.asyncio
async def test_local_resnapshot_forces_attempt_and_waits_for_exact_snapshot_ack() -> (
    None
):
    snapshot = replace(_snapshot(), observer_contract=2)
    first = _Subscription(snapshot)
    replacement = _Subscription(snapshot)
    publisher = _Publisher()
    lane = ObserverIntentLane(
        local_client=_LocalClient([first, replacement]),
        publisher=publisher,
    )
    await lane.open(replace(_open(), observer_contract=2))

    first.queue.put_nowait(ObserverResnapshotRequired("event gap"))
    replacement.queue.put_nowait(replace(_event(), observer_contract=2))
    for _ in range(100):
        if len(publisher.snapshot_records) == 2:
            break
        await asyncio.sleep(0)

    assert publisher.snapshot_attempts == [False, True]
    assert publisher.events == []
    original = publisher.snapshot_records[0]
    recovery = publisher.snapshot_records[1]
    assert recovery.message_id != original.message_id
    assert recovery.connector_sequence != original.connector_sequence
    assert recovery.payload_digest == original.payload_digest
    old_attempt_ack = StreamAck(
        observer_message_id=UUID(original.message_id),
        payload_digest=original.payload_digest,
        connector_sequence=original.connector_sequence,
        observer_message_type="session.snapshot.v2",
        profile="default",
        session_key="session-root-1",
        runtime_generation="runtime-generation-1",
        runtime_session_id="runtime-session-1",
        event_sequence=4,
        committed_at=datetime(2026, 7, 31, 9, 0, 2, tzinfo=UTC),
        observer_contract=2,
    )
    await lane.acknowledge(old_attempt_ack)
    await asyncio.sleep(0)
    assert publisher.events == []
    wrong_ack = StreamAck(
        observer_message_id=UUID(recovery.message_id),
        payload_digest="d" * 64,
        connector_sequence=recovery.connector_sequence,
        observer_message_type="session.snapshot.v2",
        profile="default",
        session_key="session-root-1",
        runtime_generation="runtime-generation-1",
        runtime_session_id="runtime-session-1",
        event_sequence=4,
        committed_at=datetime(2026, 7, 31, 9, 0, 3, tzinfo=UTC),
        observer_contract=2,
    )
    await lane.acknowledge(wrong_ack)
    await lane.recover(
        StreamNack(
            observer_message_id=UUID(recovery.message_id),
            payload_digest="d" * 64,
            connector_sequence=recovery.connector_sequence,
            observer_message_type="session.snapshot.v2",
            profile="default",
            session_key="session-root-1",
            runtime_generation="runtime-generation-1",
            runtime_session_id="runtime-session-1",
            event_sequence=4,
            reason="event_gap",
            expected_event_sequence=5,
            recovery="send_snapshot",
            rejected_at=datetime(2026, 7, 31, 9, 0, 4, tzinfo=UTC),
            observer_contract=2,
        )
    )
    await asyncio.sleep(0)
    assert len(publisher.snapshots) == 2
    assert publisher.events == []

    await lane.acknowledge(replace(wrong_ack, payload_digest=recovery.payload_digest))
    await asyncio.wait_for(publisher.event_published.wait(), timeout=1)
    assert publisher.events == [replace(_event(), observer_contract=2)]
    await lane.shutdown()


@pytest.mark.asyncio
async def test_failed_replacement_snapshot_publish_closes_each_subscription_once() -> (
    None
):
    first = _Subscription(_snapshot())
    replacement = _Subscription(_snapshot("runtime-generation-2"))
    lane = ObserverIntentLane(
        local_client=_LocalClient([first, replacement]),
        publisher=_FailingReplacementPublisher(),
    )
    await lane.open(_open())

    first.queue.put_nowait(ObserverResnapshotRequired("authority changed"))
    for _ in range(100):
        if lane.failure is not None:
            break
        await asyncio.sleep(0)

    with pytest.raises(RuntimeError, match="replacement snapshot publish failed"):
        lane.raise_if_failed()
    assert first.close_calls == 1
    assert replacement.close_calls == 1
    await lane.shutdown()


@pytest.mark.asyncio
async def test_cancelled_replacement_publish_closes_each_subscription_once() -> None:
    first = _Subscription(_snapshot())
    replacement = _Subscription(_snapshot("runtime-generation-2"))
    publisher = _BlockingReplacementPublisher()
    lane = ObserverIntentLane(
        local_client=_LocalClient([first, replacement]),
        publisher=publisher,
    )
    intent = _open()
    await lane.open(intent)
    first.queue.put_nowait(ObserverResnapshotRequired("authority changed"))
    await asyncio.wait_for(publisher.replacement_publish_started.wait(), timeout=1)

    await lane.close(
        SessionObserveClose(
            request_id=UUID("81000000-0000-4000-8000-000000000002"),
            subscription_id=intent.subscription_id,
            profile=intent.profile,
            session_key=intent.session_key,
            target_source="cloud_authorized_binding",
            reason="client_unsubscribe",
            closed_at=datetime(2026, 7, 31, 9, 5, tzinfo=UTC),
        )
    )

    assert first.close_calls == 1
    assert replacement.close_calls == 1
    await lane.shutdown()


@pytest.mark.asyncio
async def test_nack_send_snapshot_reuses_authorized_target_and_never_guesses() -> None:
    first = _Subscription(_snapshot())
    second = _Subscription(_snapshot())
    local = _LocalClient([first, second])
    publisher = _Publisher()
    lane = ObserverIntentLane(local_client=local, publisher=publisher)
    await lane.open(_open())
    nack = StreamNack(
        observer_message_id=UUID("83000000-0000-4000-8000-000000000001"),
        payload_digest="a" * 64,
        connector_sequence=41,
        observer_message_type="session.event",
        profile="default",
        session_key="session-root-1",
        runtime_generation="runtime-generation-1",
        runtime_session_id="runtime-session-1",
        event_sequence=5,
        reason="event_gap",
        expected_event_sequence=4,
        recovery="send_snapshot",
        rejected_at=datetime(2026, 7, 31, 9, 0, 2, tzinfo=UTC),
    )

    await lane.recover(nack)

    assert first.closed is True
    assert local.calls == [
        ("default", "session-root-1"),
        ("default", "session-root-1"),
    ]
    assert len(publisher.snapshots) == 2
    assert publisher.snapshot_attempts == [False, True]
    await lane.shutdown()


@pytest.mark.asyncio
async def test_recovery_epoch_ignores_old_nack_and_pauses_live_until_snapshot_ack() -> (
    None
):
    first = _Subscription(_snapshot())
    second = _Subscription(_snapshot())
    local = _LocalClient([first, second])
    publisher = _Publisher()
    lane = ObserverIntentLane(local_client=local, publisher=publisher)
    await lane.open(_open())
    nack = StreamNack(
        observer_message_id=UUID("83000000-0000-4000-8000-000000000001"),
        payload_digest="a" * 64,
        connector_sequence=40,
        observer_message_type="session.event",
        profile="default",
        session_key="session-root-1",
        runtime_generation="runtime-generation-1",
        runtime_session_id="runtime-session-1",
        event_sequence=5,
        reason="event_gap",
        expected_event_sequence=4,
        recovery="send_snapshot",
        rejected_at=datetime(2026, 7, 31, 9, 0, 2, tzinfo=UTC),
    )

    await lane.recover(nack)
    await lane.recover(nack)
    second.queue.put_nowait(_event())
    await asyncio.sleep(0)

    assert local.calls == [
        ("default", "session-root-1"),
        ("default", "session-root-1"),
    ]
    assert publisher.events == []

    await lane.acknowledge(
        StreamAck(
            observer_message_id=UUID("83000000-0000-4000-8000-000000000099"),
            payload_digest="b" * 64,
            connector_sequence=42,
            observer_message_type="session.snapshot",
            profile="default",
            session_key="session-root-1",
            runtime_generation="runtime-generation-1",
            runtime_session_id="runtime-session-1",
            event_sequence=4,
            committed_at=datetime(2026, 7, 31, 9, 0, 3, tzinfo=UTC),
        )
    )
    await asyncio.sleep(0)
    assert publisher.events == []

    await lane.acknowledge(
        StreamAck(
            observer_message_id=UUID("83000000-0000-4000-8000-000000000002"),
            payload_digest="c" * 64,
            connector_sequence=42,
            observer_message_type="session.snapshot",
            profile="default",
            session_key="session-root-1",
            runtime_generation="runtime-generation-1",
            runtime_session_id="runtime-session-1",
            event_sequence=4,
            committed_at=datetime(2026, 7, 31, 9, 0, 4, tzinfo=UTC),
        )
    )
    await asyncio.wait_for(publisher.event_published.wait(), timeout=1)

    assert publisher.events == [_event()]
    await lane.recover(nack)
    assert local.calls == [
        ("default", "session-root-1"),
        ("default", "session-root-1"),
    ]
    await lane.shutdown()


@pytest.mark.asyncio
async def test_intent_shutdown_bounds_subscription_and_discovery_cleanup() -> None:
    subscription = _BlockingSubscription(_snapshot())
    local = _BlockingLocalClient([subscription])
    lane = ObserverIntentLane(
        local_client=local,
        publisher=_Publisher(),
        cleanup_timeout_seconds=0.001,
    )
    await lane.open(_open())

    await asyncio.wait_for(lane.shutdown(), timeout=0.1)

    assert subscription.closed is True
    assert local.closed is True


@pytest.mark.asyncio
async def test_cancelled_intent_shutdown_still_cleans_subscription_and_discovery() -> (
    None
):
    subscription = _BlockingSubscription(_snapshot())
    local = _BlockingLocalClient([subscription])
    lane = ObserverIntentLane(
        local_client=local,
        publisher=_Publisher(),
        cleanup_timeout_seconds=0.01,
    )
    await lane.open(_open())
    task = asyncio.create_task(lane.shutdown())
    await asyncio.sleep(0)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert subscription.closed is True
    assert local.closed is True


@pytest.mark.asyncio
async def test_two_exact_targets_pump_concurrently_and_close_independently() -> None:
    first = _Subscription(_snapshot())
    second_snapshot = _snapshot(
        profile="work",
        session_key="session-root-2",
        runtime_session_id="runtime-session-2",
    )
    second = _Subscription(second_snapshot)
    local = _LocalClient([first, second])
    publisher = _Publisher()
    lane = ObserverIntentLane(local_client=local, publisher=publisher)
    first_intent = _open()
    second_intent = _open(
        subscription_id=UUID("82000000-0000-4000-8000-000000000002"),
        profile="work",
        session_key="session-root-2",
    )

    await lane.open(first_intent)
    await lane.open(second_intent)
    first.queue.put_nowait(_event())
    second_event = _event(
        profile="work",
        session_key="session-root-2",
        runtime_session_id="runtime-session-2",
    )
    second.queue.put_nowait(second_event)
    for _ in range(100):
        if len(publisher.events) == 2:
            break
        await asyncio.sleep(0)

    await lane.close(
        SessionObserveClose(
            request_id=UUID("81000000-0000-4000-8000-000000000002"),
            subscription_id=first_intent.subscription_id,
            profile=first_intent.profile,
            session_key=first_intent.session_key,
            target_source="cloud_authorized_binding",
            reason="client_unsubscribe",
            closed_at=datetime(2026, 7, 31, 9, 5, tzinfo=UTC),
        )
    )
    second.queue.put_nowait(second_event)
    for _ in range(100):
        if len(publisher.events) == 3:
            break
        await asyncio.sleep(0)

    assert publisher.events == [_event(), second_event, second_event]
    assert first.closed is True
    assert second.closed is False
    await lane.shutdown()


@pytest.mark.asyncio
async def test_one_target_nack_does_not_restart_or_pause_another_target() -> None:
    first = _Subscription(_snapshot())
    first_replacement = _Subscription(_snapshot())
    second_snapshot = _snapshot(
        profile="work",
        session_key="session-root-2",
        runtime_session_id="runtime-session-2",
    )
    second = _Subscription(second_snapshot)
    local = _LocalClient([first, second, first_replacement])
    publisher = _Publisher()
    lane = ObserverIntentLane(local_client=local, publisher=publisher)
    await lane.open(_open())
    await lane.open(
        _open(
            subscription_id=UUID("82000000-0000-4000-8000-000000000002"),
            profile="work",
            session_key="session-root-2",
        )
    )

    await lane.recover(
        StreamNack(
            observer_message_id=UUID("83000000-0000-4000-8000-000000000001"),
            payload_digest="a" * 64,
            connector_sequence=41,
            observer_message_type="session.event",
            profile="default",
            session_key="session-root-1",
            runtime_generation="runtime-generation-1",
            runtime_session_id="runtime-session-1",
            event_sequence=5,
            reason="event_gap",
            expected_event_sequence=4,
            recovery="send_snapshot",
            rejected_at=datetime(2026, 7, 31, 9, 0, 2, tzinfo=UTC),
        )
    )
    second_event = _event(
        profile="work",
        session_key="session-root-2",
        runtime_session_id="runtime-session-2",
    )
    second.queue.put_nowait(second_event)
    await asyncio.wait_for(publisher.event_published.wait(), timeout=1)

    assert first.closed is True
    assert second.closed is False
    assert publisher.events == [second_event]
    assert local.calls == [
        ("default", "session-root-1"),
        ("work", "session-root-2"),
        ("default", "session-root-1"),
    ]
    await lane.shutdown()


@pytest.mark.asyncio
async def test_opening_another_target_does_not_consume_prior_pump_failure() -> None:
    failed = _Subscription(_snapshot())
    healthy = _Subscription(
        _snapshot(
            profile="work",
            session_key="session-root-2",
            runtime_session_id="runtime-session-2",
        )
    )
    lane = ObserverIntentLane(
        local_client=_LocalClient([failed, healthy]),
        publisher=_Publisher(),
    )
    await lane.open(_open())
    failed.queue.put_nowait(RuntimeError("target A pump failed"))
    for _ in range(100):
        if lane.failure is not None:
            break
        await asyncio.sleep(0)

    await lane.open(
        _open(
            subscription_id=UUID("82000000-0000-4000-8000-000000000002"),
            profile="work",
            session_key="session-root-2",
        )
    )
    try:
        with pytest.raises(RuntimeError, match="target A pump failed"):
            lane.raise_if_failed()
        assert lane.failure is None
    finally:
        await lane.shutdown()


@pytest.mark.asyncio
async def test_active_subscription_bound_applies_backpressure_before_local_subscribe() -> (
    None
):
    first = _Subscription(_snapshot())
    local = _LocalClient([first])
    lane = ObserverIntentLane(
        local_client=local,
        publisher=_Publisher(),
        max_active_subscriptions=1,
    )
    await lane.open(_open())

    with pytest.raises(ObserverSubscriptionLimitExceeded):
        await lane.open(
            _open(
                subscription_id=UUID("82000000-0000-4000-8000-000000000002"),
                profile="work",
                session_key="session-root-2",
            )
        )

    assert local.calls == [("default", "session-root-1")]
    await lane.shutdown()


@pytest.mark.parametrize(
    "value",
    (True, False, 1.0, "32", 0, -1, 257),
)
def test_active_subscription_bound_requires_exact_in_range_integer(
    value: object,
) -> None:
    with pytest.raises(
        ValueError,
        match="Observer subscription bound must be an integer between 1 and 256",
    ):
        ObserverIntentLane(
            local_client=_LocalClient([]),
            publisher=_Publisher(),
            max_active_subscriptions=value,
        )


@pytest.mark.asyncio
async def test_one_hundred_open_close_cycles_leave_no_subscription_pumps() -> None:
    subscriptions = [_Subscription(_snapshot()) for _ in range(100)]
    local = _LocalClient(subscriptions)
    lane = ObserverIntentLane(local_client=local, publisher=_Publisher())
    baseline = {
        task
        for task in asyncio.all_tasks()
        if task is not asyncio.current_task() and not task.done()
    }

    for index in range(100):
        intent = _open(
            subscription_id=UUID(f"82000000-0000-4000-8000-{index + 1:012d}")
        )
        await lane.open(intent)
        await lane.close(
            SessionObserveClose(
                request_id=UUID(f"81000000-0000-4000-8000-{index + 1:012d}"),
                subscription_id=intent.subscription_id,
                profile=intent.profile,
                session_key=intent.session_key,
                target_source="cloud_authorized_binding",
                reason="client_unsubscribe",
                closed_at=datetime(2026, 7, 31, 9, 5, tzinfo=UTC),
            )
        )

    assert all(subscription.closed for subscription in subscriptions)
    assert {
        task
        for task in asyncio.all_tasks()
        if task is not asyncio.current_task() and not task.done()
    } == baseline
    await lane.shutdown()
