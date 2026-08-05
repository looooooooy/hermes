"""Canonical Plugin runtime failure and cleanup acceptance tests."""

from __future__ import annotations

import pytest
from runtime_lifecycle_support import (
    _FakeClock,
    _RecordingResource,
)
from tests.test_support.local_gateway_runtime import LocalGatewayTestRuntime

from hermes_agent_plugin.domain.lifecycle import (
    GatewayState,
    LifecycleCancelled,
    LifecycleDeadlineExceeded,
)


def test_partial_start_failure_rolls_back_started_resources_in_reverse() -> None:
    events: list[str] = []

    def fail_start(_deadline: float) -> None:
        raise RuntimeError("synthetic_start_failure")

    bootstrap = LocalGatewayTestRuntime(
        resources=(
            _RecordingResource("observer", events),
            _RecordingResource("control", events, on_start=fail_start),
        ),
        generation_factory=lambda: "runtime-failed",
    )
    bootstrap.install()

    with pytest.raises(RuntimeError, match="synthetic_start_failure"):
        bootstrap.start()

    assert events == [
        "start:observer",
        "start:control",
        "stop:control",
        "stop:observer",
    ]
    assert bootstrap.state is GatewayState.STOPPED
    assert bootstrap.ready is False
    assert bootstrap.runtime_generation is None


def test_start_cancellation_rolls_back_without_starting_later_resources() -> None:
    events: list[str] = []
    cancelled = False

    def request_cancel(_deadline: float) -> None:
        nonlocal cancelled
        cancelled = True

    bootstrap = LocalGatewayTestRuntime(
        resources=(
            _RecordingResource("observer", events, on_start=request_cancel),
            _RecordingResource("control", events),
        ),
        generation_factory=lambda: "runtime-cancelled",
    )
    bootstrap.install()

    with pytest.raises(LifecycleCancelled, match="lifecycle_cancelled"):
        bootstrap.start(cancelled=lambda: cancelled)

    assert events == ["start:observer", "stop:observer"]
    assert bootstrap.state is GatewayState.STOPPED
    assert bootstrap.runtime_generation is None


def test_start_deadline_is_capped_and_expiry_rolls_back() -> None:
    events: list[str] = []
    clock = _FakeClock()
    received_deadlines: list[float] = []

    def exceed_deadline(deadline: float) -> None:
        received_deadlines.append(deadline)
        clock.advance(31.0)

    bootstrap = LocalGatewayTestRuntime(
        resources=(_RecordingResource("observer", events, on_start=exceed_deadline),),
        generation_factory=lambda: "runtime-timeout",
        clock=clock,
    )
    bootstrap.install()

    with pytest.raises(
        LifecycleDeadlineExceeded,
        match="lifecycle_deadline_exceeded",
    ):
        bootstrap.start(timeout_s=300.0)

    assert received_deadlines == [130.0]
    assert events == ["start:observer", "stop:observer"]
    assert bootstrap.state is GatewayState.STOPPED


@pytest.mark.parametrize(
    "timeout_s",
    (0, -1, float("nan"), float("inf"), "not-a-deadline", True),
)
def test_invalid_deadline_fails_before_state_or_resource_changes(
    timeout_s,
) -> None:
    events: list[str] = []
    bootstrap = LocalGatewayTestRuntime(
        resources=(_RecordingResource("observer", events),),
        generation_factory=lambda: "runtime-invalid-deadline",
    )
    bootstrap.install()

    with pytest.raises(
        LifecycleDeadlineExceeded,
        match="lifecycle_deadline_invalid",
    ):
        bootstrap.start(timeout_s=timeout_s)

    assert events == []
    assert bootstrap.state is GatewayState.INSTALLED
    assert bootstrap.runtime_generation is None


def test_stop_attempts_every_resource_and_finishes_stopped_after_failure() -> None:
    events: list[str] = []

    def fail_stop(_deadline: float) -> None:
        raise RuntimeError("synthetic_stop_failure")

    bootstrap = LocalGatewayTestRuntime(
        resources=(
            _RecordingResource("observer", events),
            _RecordingResource("control", events, on_stop=fail_stop),
        ),
        generation_factory=lambda: "runtime-stop-failure",
    )
    bootstrap.install()
    bootstrap.start()

    with pytest.raises(RuntimeError, match="synthetic_stop_failure"):
        bootstrap.stop()

    assert events == [
        "start:observer",
        "start:control",
        "drain:control",
        "drain:observer",
        "stop:control",
        "stop:observer",
    ]
    assert bootstrap.state is GatewayState.STOPPED
    assert bootstrap.runtime_generation is None


def test_stop_cleans_every_resource_when_drain_partially_fails() -> None:
    events: list[str] = []

    def fail_drain(_deadline: float) -> None:
        raise RuntimeError("synthetic_drain_failure")

    bootstrap = LocalGatewayTestRuntime(
        resources=(
            _RecordingResource("observer", events),
            _RecordingResource("control", events, on_drain=fail_drain),
        ),
        generation_factory=lambda: "runtime-drain-failure",
    )
    bootstrap.install()
    bootstrap.start()

    with pytest.raises(RuntimeError, match="synthetic_drain_failure"):
        bootstrap.stop()

    assert events == [
        "start:observer",
        "start:control",
        "drain:control",
        "drain:observer",
        "stop:control",
        "stop:observer",
    ]
    assert bootstrap.state is GatewayState.STOPPED
    assert bootstrap.runtime_generation is None
