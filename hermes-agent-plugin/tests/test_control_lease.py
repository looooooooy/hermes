from __future__ import annotations

import threading

import pytest

from hermes_agent_plugin.domain.control_lease import (
    ControlBinding,
    ControlLeaseManager,
    ControllerConflict,
    LeaseExpired,
    LeaseMismatch,
    SessionBindingMismatch,
)


class FakeTime:
    def __init__(self) -> None:
        self.monotonic_seconds = 100.0
        self.epoch_seconds = 1_900_000_000.0

    def monotonic(self) -> float:
        return self.monotonic_seconds

    def epoch(self) -> float:
        return self.epoch_seconds

    def advance(self, seconds: float) -> None:
        self.monotonic_seconds += seconds
        self.epoch_seconds += seconds


def binding(
    *,
    user_id: str = "user-1",
    client_instance_id: str = "11111111-1111-4111-8111-111111111111",
    transport_id: str = "transport-1",
    runtime_session_id: str | None = "runtime-1",
    runtime_generation: str = "runtime-generation-1",
) -> ControlBinding:
    return ControlBinding(
        session_key="durable-root-1",
        profile="default",
        runtime_generation=runtime_generation,
        runtime_session_id=runtime_session_id,
        user_id=user_id,
        provider="basic",
        client_instance_id=client_instance_id,
        transport_id=transport_id,
    )


def manager(fake_time: FakeTime) -> ControlLeaseManager:
    issued = iter(("lease-secret-1", "lease-secret-2", "lease-secret-3"))
    return ControlLeaseManager(
        ttl_seconds=30,
        reconnect_grace_seconds=5,
        monotonic=fake_time.monotonic,
        epoch_time=fake_time.epoch,
        lease_id_factory=lambda: next(issued),
    )


def test_single_explicit_controller_lease_is_bound_and_redacted() -> None:
    fake_time = FakeTime()
    leases = manager(fake_time)

    acquired = leases.acquire(binding())

    assert acquired.lease_id == "lease-secret-1"
    assert acquired.expires_at_epoch_ms == 1_900_000_030_000
    assert acquired.control_revision == 1
    assert "lease-secret-1" not in repr(acquired)
    assert leases.status(
        session_key="durable-root-1",
        profile="default",
        desktop_controller_present=True,
    ) == {
        "controller_kind": "mobile",
        "controller_label": "Hermes Mobile",
        "control_revision": 1,
        "lease_expires_at_epoch_ms": 1_900_000_030_000,
        "pending_input": None,
    }

    with pytest.raises(ControllerConflict) as conflict:
        leases.acquire(binding(user_id="user-2", transport_id="transport-2"))
    assert "lease-secret-1" not in str(conflict.value)


def test_renew_release_and_expiry_require_exact_principal_client_transport_binding() -> (
    None
):
    fake_time = FakeTime()
    leases = manager(fake_time)
    acquired = leases.acquire(binding())

    with pytest.raises(LeaseMismatch):
        leases.renew(
            binding(transport_id="transport-other"),
            lease_id=acquired.lease_id,
        )

    fake_time.advance(10)
    renewed = leases.renew(binding(), lease_id=acquired.lease_id)
    assert renewed.control_revision == 2
    assert renewed.expires_at_epoch_ms == 1_900_000_040_000

    fake_time.advance(31)
    with pytest.raises(LeaseExpired):
        leases.release(binding(), lease_id=renewed.lease_id)

    assert (
        leases.status(
            session_key="durable-root-1",
            profile="default",
            desktop_controller_present=True,
        )["controller_kind"]
        == "desktop"
    )


def test_same_principal_client_may_rebind_only_after_disconnect_and_within_grace() -> (
    None
):
    fake_time = FakeTime()
    leases = manager(fake_time)
    first = leases.acquire(binding())

    with pytest.raises(ControllerConflict):
        leases.acquire(binding(transport_id="transport-2"))

    leases.transport_disconnected("transport-1")
    rebound = leases.acquire(binding(transport_id="transport-2"))

    assert rebound.lease_id == "lease-secret-2"
    assert rebound.lease_id != first.lease_id
    assert rebound.control_revision == 2

    leases.transport_disconnected("transport-2")
    fake_time.advance(6)
    with pytest.raises(ControllerConflict):
        leases.acquire(binding(transport_id="transport-3"))


def test_acquire_is_thread_safe_and_only_one_competing_client_wins() -> None:
    fake_time = FakeTime()
    leases = manager(fake_time)
    outcomes: list[str] = []
    barrier = threading.Barrier(3)

    def contend(candidate: ControlBinding) -> None:
        barrier.wait()
        try:
            leases.acquire(candidate)
            outcomes.append("acquired")
        except ControllerConflict:
            outcomes.append("conflict")

    threads = [
        threading.Thread(
            target=contend, args=(binding(user_id="user-a", transport_id="a"),)
        ),
        threading.Thread(
            target=contend, args=(binding(user_id="user-b", transport_id="b"),)
        ),
    ]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=2)

    assert sorted(outcomes) == ["acquired", "conflict"]


def test_runtime_fence_rejects_every_stale_lease_operation() -> None:
    fake_time = FakeTime()
    leases = manager(fake_time)
    leases.bind_runtime(
        profile="default",
        runtime_generation="runtime-generation-1",
        ready=True,
    )
    old_binding = binding()
    old_lease = leases.acquire(old_binding)
    leases.bind_runtime(
        profile="default",
        runtime_generation="runtime-generation-2",
        ready=True,
    )

    for operation in (
        lambda: leases.acquire(old_binding),
        lambda: leases.renew(old_binding, lease_id=old_lease.lease_id),
        lambda: leases.authorize(old_binding, lease_id=old_lease.lease_id),
        lambda: leases.release(old_binding, lease_id=old_lease.lease_id),
    ):
        with pytest.raises(SessionBindingMismatch):
            operation()

    current = leases.acquire(binding(runtime_generation="runtime-generation-2"))
    assert current.lease_id == "lease-secret-2"
