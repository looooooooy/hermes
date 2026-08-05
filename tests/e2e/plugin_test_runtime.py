"""Test-only Plugin resource harness excluded from production distributions."""

from __future__ import annotations

import sys
import time
import uuid
from collections.abc import Awaitable, Callable, Iterable, Mapping
from typing import Any

from hermes_connector.domain.local_gateway import (
    LocalRuntimeAuthority,
    ProcessIdentityEvidence,
)

from hermes_agent_plugin.adapters.host.lifecycle import control_relay_resource
from hermes_agent_plugin.adapters.local_protocol.handshake_v1 import (
    PLUGIN_LOCAL_CAPABILITIES,
    LocalContractV1Adapter,
)
from hermes_agent_plugin.adapters.platform.capabilities import (
    PlatformLocalGatewayUnavailable,
)
from hermes_agent_plugin.adapters.platform.macos.runtime_descriptor_v2 import (
    MacOSRuntimeAuthorityV2,
    capture_macos_host_authority,
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


def _new_test_runtime_generation() -> str:
    return f"runtime-{uuid.uuid4()}"


def create_test_runtime_authority(
    *,
    profile: str = "default",
    runtime_generation: str | None = None,
) -> MacOSRuntimeAuthorityV2:
    """Capture one verified authority shared by every test-only endpoint role."""

    return capture_macos_host_authority(
        profile=profile,
        host_bundle_id="com.nousresearch.hermes",
    ).bind_runtime(runtime_generation or _new_test_runtime_generation())


def create_connector_authority_provider(
    authority: MacOSRuntimeAuthorityV2,
    *,
    required_capabilities: tuple[str, ...] = ("session.control",),
    optional_capabilities: tuple[str, ...] = (),
) -> Callable[[], Awaitable[LocalRuntimeAuthority]]:
    """Expose one Plugin authority through the Connector production port."""

    connector_authority = LocalRuntimeAuthority(
        profile=authority.profile,
        runtime_generation=authority.runtime_generation,
        instance_id=authority.instance_id,
        host_bundle_id=authority.host_bundle_id,
        process_identity=ProcessIdentityEvidence(
            start_time_ns=authority.process_identity.start_time_ns,
            executable_path=authority.process_identity.executable_path,
            executable_device=authority.process_identity.executable_device,
            executable_inode=authority.process_identity.executable_inode,
        ),
        required_capabilities=required_capabilities,
        optional_capabilities=optional_capabilities,
    )

    async def current_authority() -> LocalRuntimeAuthority:
        return connector_authority

    return current_authority


def _create_platform_local_gateway_test_resource(
    *,
    paths: object,
    authority: MacOSRuntimeAuthorityV2,
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
        authority=authority,
        hello_handler=hello_handler,
        ready=ready,
        clock=clock,
        **dict(options or {}),
    )


def create_control_relay_test_resource(
    *,
    authority: MacOSRuntimeAuthorityV2,
    owner_action: Callable[[dict[str, Any], Any], Mapping[str, Any]],
    leases: ControlLeaseManager | None = None,
    commands: CommandLedger | None = None,
    desktop_controller_present: Callable[[], bool] = lambda: False,
) -> LifecycleResourcePort:
    dispatcher = ControlRequestDispatcher(
        owner_action=owner_action,
        leases=leases,
        commands=commands,
        desktop_controller_present=desktop_controller_present,
    )
    return control_relay_resource(
        authority=authority,
        dispatcher=dispatcher,
    )


class LocalGatewayTestRuntime:
    """Drive Plugin resource lifecycle without claiming a production binding."""

    def __init__(
        self,
        *,
        resources: Iterable[LifecycleResourcePort] = (),
        available_capabilities: frozenset[str] = PLUGIN_LOCAL_CAPABILITIES,
        generation_factory: Callable[[], str] = _new_test_runtime_generation,
        local_contract_factory: Callable[[str], LocalHandshakePort] | None = None,
        clock: Callable[[], float] = time.monotonic,
        default_timeout_s: float = 3.0,
        macos_local_gateway_paths: object | None = None,
        macos_local_gateway_profile: str = "default",
        macos_local_gateway_options: Mapping[str, Any] | None = None,
        runtime_authority: MacOSRuntimeAuthorityV2 | None = None,
    ) -> None:
        if runtime_authority is None and macos_local_gateway_paths is not None:
            runtime_authority = create_test_runtime_authority(
                profile=macos_local_gateway_profile,
                runtime_generation=generation_factory(),
            )
        if runtime_authority is not None:
            if runtime_authority.profile != macos_local_gateway_profile:
                raise ValueError("test runtime authority profile mismatch")

            def authoritative_generation() -> str:
                return runtime_authority.runtime_generation

            generation_factory = authoritative_generation
        adapter_factory = local_contract_factory or (
            lambda generation: LocalContractV1Adapter(
                runtime_generation=generation,
                available_capabilities=available_capabilities,
            )
        )
        lifecycle_resources = list(resources)
        if macos_local_gateway_paths is not None:
            lifecycle_resources.append(
                _create_platform_local_gateway_test_resource(
                    paths=macos_local_gateway_paths,
                    authority=runtime_authority,
                    hello_handler=self.handle_local_hello,
                    ready=lambda: self.ready,
                    clock=clock,
                    options=macos_local_gateway_options,
                )
            )
        self._lifecycle = GatewayLifecycle(
            resources=lifecycle_resources,
            adapter_factory=adapter_factory,
            generation_factory=generation_factory,
            clock=clock,
            default_timeout_s=default_timeout_s,
        )
        self._runtime_authority = runtime_authority

    @property
    def state(self) -> GatewayState:
        return self._lifecycle.state

    @property
    def ready(self) -> bool:
        return self._lifecycle.ready

    @property
    def runtime_generation(self) -> str | None:
        return self._lifecycle.runtime_generation

    @property
    def runtime_authority(self) -> MacOSRuntimeAuthorityV2 | None:
        return self._runtime_authority

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
    "create_connector_authority_provider",
    "create_control_relay_test_resource",
    "create_test_runtime_authority",
]
