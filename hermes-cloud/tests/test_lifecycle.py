from __future__ import annotations

import pytest

from hermes_cloud.domain.lifecycle import (
    ComponentLifecycle,
    InvalidTransition,
    LifecycleState,
)


def _lifecycle_at(state: LifecycleState) -> ComponentLifecycle:
    lifecycle = ComponentLifecycle("test-component")
    paths = {
        LifecycleState.CREATED: (),
        LifecycleState.STARTING: (LifecycleState.STARTING,),
        LifecycleState.READY: (LifecycleState.STARTING, LifecycleState.READY),
        LifecycleState.STOPPING: (
            LifecycleState.STARTING,
            LifecycleState.READY,
            LifecycleState.STOPPING,
        ),
        LifecycleState.STOPPED: (
            LifecycleState.STARTING,
            LifecycleState.READY,
            LifecycleState.STOPPING,
            LifecycleState.STOPPED,
        ),
        LifecycleState.FAILED: (LifecycleState.STARTING, LifecycleState.FAILED),
    }
    for target in paths[state]:
        lifecycle.transition(target)
    return lifecycle


@pytest.mark.parametrize(
    ("source", "target"),
    [
        (LifecycleState.CREATED, LifecycleState.STARTING),
        (LifecycleState.STARTING, LifecycleState.READY),
        (LifecycleState.STARTING, LifecycleState.FAILED),
        (LifecycleState.READY, LifecycleState.STOPPING),
        (LifecycleState.READY, LifecycleState.FAILED),
        (LifecycleState.FAILED, LifecycleState.STOPPING),
        (LifecycleState.STOPPING, LifecycleState.STOPPED),
    ],
)
def test_lifecycle_accepts_declared_transitions(
    source: LifecycleState,
    target: LifecycleState,
) -> None:
    lifecycle = _lifecycle_at(source)

    lifecycle.transition(target)

    assert lifecycle.state is target


@pytest.mark.parametrize(
    ("source", "target"),
    [
        (LifecycleState.CREATED, LifecycleState.READY),
        (LifecycleState.READY, LifecycleState.STARTING),
        (LifecycleState.FAILED, LifecycleState.READY),
        (LifecycleState.STOPPED, LifecycleState.STARTING),
    ],
)
def test_lifecycle_rejects_undeclared_transitions(
    source: LifecycleState,
    target: LifecycleState,
) -> None:
    lifecycle = _lifecycle_at(source)

    with pytest.raises(InvalidTransition):
        lifecycle.transition(target)

    assert lifecycle.state is source


def test_readiness_is_true_only_in_ready_state() -> None:
    lifecycle = ComponentLifecycle("test-component")
    assert lifecycle.is_live is True
    assert lifecycle.is_ready is False

    lifecycle.transition(LifecycleState.STARTING)
    lifecycle.transition(LifecycleState.READY)
    assert lifecycle.is_live is True
    assert lifecycle.is_ready is True

    lifecycle.transition(LifecycleState.STOPPING)
    lifecycle.transition(LifecycleState.STOPPED)
    assert lifecycle.is_live is False
    assert lifecycle.is_ready is False
