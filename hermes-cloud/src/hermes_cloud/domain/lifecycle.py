"""Component lifecycle state and transition invariants."""

from __future__ import annotations

from enum import Enum


class LifecycleState(str, Enum):
    CREATED = "CREATED"
    STARTING = "STARTING"
    READY = "READY"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    FAILED = "FAILED"


class InvalidTransition(ValueError):
    """Raised when a component attempts an undeclared lifecycle transition."""


# State machine:
#   CREATED -> STARTING -> READY -> STOPPING -> STOPPED
#                  |          |          ^
#                  +-> FAILED <-+          |
#                        +-----------------+
#
# Invariants:
#   - Lifecycle tracks component ownership and fatal failure, not dependency health.
#   - External readiness additionally requires every critical dependency to be healthy.
#   - STOPPED is terminal for a component instance.
#   - FAILED components must pass through STOPPING before STOPPED.
_ALLOWED_TRANSITIONS: dict[LifecycleState, frozenset[LifecycleState]] = {
    LifecycleState.CREATED: frozenset({LifecycleState.STARTING}),
    LifecycleState.STARTING: frozenset({LifecycleState.READY, LifecycleState.FAILED}),
    LifecycleState.READY: frozenset({LifecycleState.STOPPING, LifecycleState.FAILED}),
    LifecycleState.FAILED: frozenset({LifecycleState.STOPPING}),
    LifecycleState.STOPPING: frozenset({LifecycleState.STOPPED}),
    LifecycleState.STOPPED: frozenset(),
}


class ComponentLifecycle:
    """Own one component's lifecycle state and enforce valid transitions."""

    def __init__(self, component: str) -> None:
        self._component = component
        self._state = LifecycleState.CREATED

    @property
    def component(self) -> str:
        return self._component

    @property
    def state(self) -> LifecycleState:
        return self._state

    @property
    def is_live(self) -> bool:
        return self._state is not LifecycleState.STOPPED

    @property
    def is_ready(self) -> bool:
        return self._state is LifecycleState.READY

    def transition(self, target: LifecycleState) -> None:
        if target not in _ALLOWED_TRANSITIONS[self._state]:
            raise InvalidTransition(
                f"invalid lifecycle transition: {self._state.value} -> {target.value}"
            )
        self._state = target
