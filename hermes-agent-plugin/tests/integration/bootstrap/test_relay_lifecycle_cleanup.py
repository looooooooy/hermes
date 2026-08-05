"""Real relay cleanup under the canonical Plugin runtime lifecycle."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

from tests.test_support.local_gateway_runtime import LocalGatewayTestRuntime
from tests.test_support.runtime_descriptor_v2 import runtime_authority_v2

from hermes_agent_plugin.adapters.host.lifecycle import (
    control_relay_resource,
    observer_relay_resource,
)
from hermes_agent_plugin.adapters.platform.macos.local_gateway_paths import (
    MacOSLocalGatewayPaths,
)
from hermes_agent_plugin.adapters.platform.macos.local_relay import (
    MacOSLocalRelayBackend,
)
from hermes_agent_plugin.domain.lifecycle import GatewayState


def test_one_hundred_real_relay_lifecycle_cycles_leave_no_resources(
    tmp_path: Path,
    monkeypatch,
    short_socket_root: Path,
) -> None:
    observer_registry = tmp_path / "observer-registry"
    observer_sockets = short_socket_root / "observer"
    control_registry = tmp_path / "control-registry"
    control_sockets = short_socket_root / "control"
    monkeypatch.setenv("HERMES_OBSERVER_REGISTRY_DIR", str(observer_registry))
    monkeypatch.setenv("HERMES_OBSERVER_SOCKET_DIR", str(observer_sockets))
    monkeypatch.setenv("HERMES_CONTROL_REGISTRY_DIR", str(control_registry))
    monkeypatch.setenv("HERMES_CONTROL_SOCKET_DIR", str(control_sockets))
    backend = MacOSLocalRelayBackend(
        MacOSLocalGatewayPaths(
            local_gateway_registry_directory=tmp_path / "local-registry",
            local_gateway_socket_directory=short_socket_root / "local",
            control_registry_directory=control_registry,
            control_socket_directory=control_sockets,
            observer_registry_directory=observer_registry,
            observer_socket_directory=observer_sockets,
        )
    )
    baseline_threads = {(thread.name, thread.ident) for thread in threading.enumerate()}
    generations: set[str] = set()
    authority = runtime_authority_v2()
    bootstrap = LocalGatewayTestRuntime(
        resources=(
            observer_relay_resource(
                authority=authority,
                dispatch=lambda _request, _transport: None,
                remove_observer_subscriptions=lambda _transport: None,
                backend=backend,
            ),
            control_relay_resource(
                authority=authority,
                dispatcher=lambda _request, _transport: None,
                backend=backend,
            ),
        ),
    )
    bootstrap.install()

    async def run_cycles() -> None:
        baseline_tasks = asyncio.all_tasks()
        for _ in range(100):
            generations.add(bootstrap.start())
            bootstrap.stop()
        assert asyncio.all_tasks() == baseline_tasks

    asyncio.run(run_cycles())

    assert len(generations) == 100
    assert bootstrap.state is GatewayState.STOPPED
    assert {
        (thread.name, thread.ident) for thread in threading.enumerate()
    } == baseline_threads
    for directory in (
        observer_registry,
        observer_sockets,
        control_registry,
        control_sockets,
    ):
        assert list(directory.iterdir()) == []
