"""Test-only local gateway lifecycle harness.

This module lives outside ``src`` and is never packaged.  Production code must
bind through the running Hermes Agent ``gateway-extension/1`` PluginManager.
"""

from __future__ import annotations

import os
import sys
import time
import uuid
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from hermes_agent_plugin.adapters.host.lifecycle import control_relay_resource
from hermes_agent_plugin.adapters.local_protocol.handshake_v1 import (
    PLUGIN_LOCAL_CAPABILITIES,
    LocalContractV1Adapter,
)
from hermes_agent_plugin.adapters.platform.capabilities import (
    PlatformLocalGatewayUnavailable,
)
from hermes_agent_plugin.application.control_commands import CommandLedger
from hermes_agent_plugin.application.control_dispatcher import (
    ControlRequestDispatcher,
)
from hermes_agent_plugin.application.lifecycle import GatewayLifecycle
from hermes_agent_plugin.domain.control_lease import ControlLeaseManager
from hermes_agent_plugin.domain.lifecycle import GatewayState
from hermes_agent_plugin.ports.lifecycle import (
    LifecycleResourcePort,
    LocalHandshakePort,
)
from tests.test_support.runtime_descriptor_v2 import runtime_authority_v2


def new_test_runtime_generation() -> str:
    return f"runtime-{uuid.uuid4()}"


def create_platform_local_gateway_test_resource(
    *,
    paths: object,
    profile: str,
    hello_handler: Callable[[Any], str],
    ready: Callable[[], bool],
    clock: Callable[[], float],
    options: Mapping[str, Any] | None = None,
) -> LifecycleResourcePort:
    if sys.platform == "darwin":
        from hermes_agent_plugin.adapters.platform.macos import (
            create_local_gateway_resource,
        )
    elif sys.platform.startswith("linux"):
        from hermes_agent_plugin.adapters.platform.linux import (
            create_local_gateway_resource,
        )
    elif sys.platform == "win32":
        from hermes_agent_plugin.adapters.platform.windows import (
            create_local_gateway_resource,
        )
    else:
        raise PlatformLocalGatewayUnavailable("unsupported_local_gateway_platform")
    return create_local_gateway_resource(
        paths=paths,
        profile=profile,
        hello_handler=hello_handler,
        ready=ready,
        clock=clock,
        **dict(options or {}),
    )


def create_control_relay_test_resource(
    *,
    profile: str,
    owner_action: Callable[[dict[str, Any], Any], Mapping[str, Any]],
    leases: ControlLeaseManager | None = None,
    commands: CommandLedger | None = None,
    desktop_controller_present: Callable[[], bool] = lambda: False,
    pid: int | None = None,
) -> LifecycleResourcePort:
    dispatcher = ControlRequestDispatcher(
        owner_action=owner_action,
        leases=leases,
        commands=commands,
        desktop_controller_present=desktop_controller_present,
    )
    return control_relay_resource(
        authority=runtime_authority_v2(profile=profile),
        dispatcher=dispatcher,
    )


class LocalGatewayTestRuntime:
    r"""Exercise resource lifecycle without claiming a production Host binding.

    NEW -> INSTALLED -> STARTING -> READY -> DRAINING -> STOPPING -> STOPPED
                               \---------------> STOPPING
                    \--------------------------> STOPPING
                                                              |
                                STARTING <---------------------+

    Allowed transitions:
        NEW -> INSTALLED
        INSTALLED -> STARTING | STOPPING
        STARTING -> READY | STOPPING
        READY -> DRAINING
        DRAINING -> STOPPING
        STOPPING -> STOPPED
        STOPPED -> STARTING
    """

    def __init__(
        self,
        *,
        resources: Iterable[LifecycleResourcePort] = (),
        available_capabilities: frozenset[str] = PLUGIN_LOCAL_CAPABILITIES,
        generation_factory: Callable[[], str] = new_test_runtime_generation,
        local_contract_factory: Callable[[str], LocalHandshakePort] | None = None,
        clock: Callable[[], float] = time.monotonic,
        default_timeout_s: float = 3.0,
        macos_local_gateway_paths: object | None = None,
        macos_local_gateway_profile: str = "default",
        macos_local_gateway_options: Mapping[str, Any] | None = None,
    ) -> None:
        adapter_factory = local_contract_factory or (
            lambda generation: LocalContractV1Adapter(
                runtime_generation=generation,
                available_capabilities=available_capabilities,
            )
        )
        lifecycle_resources = list(resources)
        if macos_local_gateway_paths is not None:
            local_options = dict(macos_local_gateway_options or {})

            def authority_provider():
                generation = self._lifecycle.runtime_generation
                if generation is None:
                    raise RuntimeError("test runtime generation is unavailable")
                return runtime_authority_v2(
                    profile=macos_local_gateway_profile,
                    runtime_generation=generation,
                )

            local_options.setdefault("authority", authority_provider)
            local_options.setdefault("pid", os.getpid())
            lifecycle_resources.append(
                create_platform_local_gateway_test_resource(
                    paths=macos_local_gateway_paths,
                    profile=macos_local_gateway_profile,
                    hello_handler=self.handle_local_hello,
                    ready=lambda: self.ready,
                    clock=clock,
                    options=local_options,
                )
            )
        self._lifecycle = GatewayLifecycle(
            resources=lifecycle_resources,
            adapter_factory=adapter_factory,
            generation_factory=generation_factory,
            clock=clock,
            default_timeout_s=default_timeout_s,
        )

    @property
    def state(self) -> GatewayState:
        return self._lifecycle.state

    @property
    def ready(self) -> bool:
        return self._lifecycle.ready

    @property
    def runtime_generation(self) -> str | None:
        return self._lifecycle.runtime_generation

    def install(self) -> None:
        self._lifecycle.install()

    def start(
        self,
        *,
        timeout_s: float | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> str:
        return self._lifecycle.start(
            timeout_s=timeout_s,
            cancelled=cancelled,
        )

    def drain(self, *, timeout_s: float | None = None) -> None:
        self._lifecycle.drain(timeout_s=timeout_s)

    def stop(self, *, timeout_s: float | None = None) -> None:
        self._lifecycle.stop(timeout_s=timeout_s)

    def handle_local_hello(self, raw: Any) -> str:
        return self._lifecycle.handle_local_hello(raw)


__all__ = [
    "LocalGatewayTestRuntime",
    "create_control_relay_test_resource",
    "create_platform_local_gateway_test_resource",
    "new_test_runtime_generation",
]
