"""Canonical Plugin runtime lifecycle acceptance tests."""

from __future__ import annotations

import json
from uuid import UUID

import pytest
from runtime_lifecycle_support import (
    _hello,
    _InjectedHandshakeAdapter,
    _RecordingResource,
)
from tests.test_support.local_gateway_runtime import (
    LocalGatewayTestRuntime,
    new_test_runtime_generation,
)

from hermes_agent_plugin.domain.lifecycle import (
    ALLOWED_LIFECYCLE_TRANSITIONS,
    GatewayState,
    LifecycleNotReady,
)


def test_default_runtime_generation_uses_canonical_uuid_suffix() -> None:
    runtime_generation = new_test_runtime_generation()

    uuid_suffix = runtime_generation.removeprefix("runtime-")
    assert runtime_generation == f"runtime-{UUID(uuid_suffix)}"


def test_bootstrap_runs_documented_lifecycle_and_wires_local_handshake() -> None:
    events: list[str] = []
    generations = iter(("runtime-cycle-1", "runtime-cycle-2"))
    bootstrap = LocalGatewayTestRuntime(
        resources=(
            _RecordingResource("observer", events),
            _RecordingResource("control", events),
        ),
        generation_factory=lambda: next(generations),
    )
    assert bootstrap.state is GatewayState.NEW
    assert bootstrap.ready is False
    with pytest.raises(LifecycleNotReady):
        bootstrap.handle_local_hello(_hello())

    bootstrap.install()
    first_generation = bootstrap.start()

    assert first_generation == "runtime-cycle-1"
    assert bootstrap.state is GatewayState.READY
    assert bootstrap.ready is True
    assert bootstrap.runtime_generation == "runtime-cycle-1"
    welcome = json.loads(bootstrap.handle_local_hello(_hello()))
    assert welcome == {
        "contract_version": 1,
        "message_type": "local.welcome",
        "runtime_generation": "runtime-cycle-1",
        "profile": "default",
        "accepted_capabilities": ["session.control", "session.observe"],
        "unavailable_optional_capabilities": ["view.card"],
    }

    bootstrap.drain()
    assert bootstrap.state is GatewayState.DRAINING
    assert bootstrap.ready is False
    with pytest.raises(LifecycleNotReady):
        bootstrap.handle_local_hello(_hello())

    bootstrap.stop()
    assert bootstrap.state is GatewayState.STOPPED
    assert bootstrap.runtime_generation is None

    second_generation = bootstrap.start()
    bootstrap.stop()

    assert second_generation == "runtime-cycle-2"
    assert events == [
        "start:observer",
        "start:control",
        "drain:control",
        "drain:observer",
        "stop:control",
        "stop:observer",
        "start:observer",
        "start:control",
        "drain:control",
        "drain:observer",
        "stop:control",
        "stop:observer",
    ]


def test_bootstrap_exposes_local_contract_adapter_injection_point() -> None:
    created: list[_InjectedHandshakeAdapter] = []

    def create_adapter(generation: str) -> _InjectedHandshakeAdapter:
        adapter = _InjectedHandshakeAdapter(generation)
        created.append(adapter)
        return adapter

    bootstrap = LocalGatewayTestRuntime(
        generation_factory=lambda: "runtime-injected",
        local_contract_factory=create_adapter,
    )
    bootstrap.install()
    bootstrap.start()

    assert bootstrap.handle_local_hello("hello") == "runtime-injected:hello"
    assert [adapter.generation for adapter in created] == ["runtime-injected"]

    bootstrap.stop()


def test_lifecycle_docstring_and_transition_table_are_complete() -> None:
    assert ALLOWED_LIFECYCLE_TRANSITIONS == {
        GatewayState.NEW: frozenset({GatewayState.INSTALLED}),
        GatewayState.INSTALLED: frozenset(
            {GatewayState.STARTING, GatewayState.STOPPING}
        ),
        GatewayState.STARTING: frozenset({GatewayState.READY, GatewayState.STOPPING}),
        GatewayState.READY: frozenset({GatewayState.DRAINING}),
        GatewayState.DRAINING: frozenset({GatewayState.STOPPING}),
        GatewayState.STOPPING: frozenset({GatewayState.STOPPED}),
        GatewayState.STOPPED: frozenset({GatewayState.STARTING}),
    }
    assert "NEW -> INSTALLED -> STARTING -> READY" in (
        LocalGatewayTestRuntime.__doc__ or ""
    )
    assert "Allowed transitions:" in (LocalGatewayTestRuntime.__doc__ or "")


def test_repeated_start_and_stop_are_idempotent() -> None:
    events: list[str] = []
    generation_calls = 0

    def generation() -> str:
        nonlocal generation_calls
        generation_calls += 1
        return f"runtime-{generation_calls}"

    bootstrap = LocalGatewayTestRuntime(
        resources=(_RecordingResource("relay", events),),
        generation_factory=generation,
    )
    bootstrap.install()

    assert bootstrap.start() == "runtime-1"
    assert bootstrap.start() == "runtime-1"
    bootstrap.stop()
    bootstrap.stop()

    assert generation_calls == 1
    assert events == ["start:relay", "drain:relay", "stop:relay"]
    assert bootstrap.state is GatewayState.STOPPED


def test_stop_before_install_is_a_safe_idempotent_noop() -> None:
    events: list[str] = []
    bootstrap = LocalGatewayTestRuntime(
        resources=(_RecordingResource("relay", events),),
    )

    bootstrap.stop()
    bootstrap.stop()

    assert bootstrap.state is GatewayState.NEW
    assert bootstrap.runtime_generation is None
    assert events == []
