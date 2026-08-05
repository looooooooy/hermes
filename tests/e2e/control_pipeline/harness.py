"""Host-only test double behind the production Plugin control relay."""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from types import SimpleNamespace

from hermes_agent_plugin.adapters.host.extension import HermesAgentPluginExtension
from hermes_agent_plugin.adapters.platform.macos.local_gateway_paths import (
    MacOSLocalGatewayPaths,
)
from hermes_agent_plugin.adapters.platform.macos.local_relay import (
    MacOSLocalRelayBackend,
)
from hermes_agent_plugin.adapters.platform.macos.runtime_descriptor_v2 import (
    MacOSRuntimeAuthorityV2,
    capture_macos_host_authority,
)

from tests.test_support.host_spi_v1 import TestOwnerActionResult


class CloseRegistration:
    def __init__(self, close: Callable[[], None] = lambda: None) -> None:
        self._close = close
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._close()


class GatewayExtensionV1ControlTestHost:
    """Exercise production Plugin control composition with only Core doubled."""

    host_api_version = 1

    def __init__(
        self,
        paths: MacOSLocalGatewayPaths,
        *,
        profile: str,
        runtime_generation: str,
        effect_unknown_on_call: Mapping[str, int] | None = None,
    ) -> None:
        self._backend = MacOSLocalRelayBackend(paths)
        self._profile = profile
        self._runtime_generation = runtime_generation
        self._effect_unknown_on_call = dict(effect_unknown_on_call or {})
        self._authority = capture_macos_host_authority(
            profile=profile,
            host_bundle_id="com.nousresearch.hermes",
        ).bind_runtime(runtime_generation)
        self._registration: object | None = None
        self._endpoint_resources: dict[str, object] = {}
        self._lock = threading.RLock()
        self._control_revision = 0
        self._pending_input: dict[str, object] | None = {
            "request_id": "pending-approval",
            "kind": "approval",
            "title": "Approve command",
            "description": "Review the requested operation.",
            "command": "./gradlew test",
            "choices": ["allow_once", "deny"],
            "expires_at_epoch_ms": 4_102_444_800_000,
        }
        self.owner_calls: list[str] = []
        self.audits: list[str] = []
        self.endpoint_events: list[str] = []

    @property
    def authority(self) -> MacOSRuntimeAuthorityV2:
        return self._authority

    @property
    def active_endpoint_roles(self) -> tuple[str, ...]:
        return tuple(sorted(self._endpoint_resources))

    def start(self) -> None:
        if self._registration is not None:
            raise RuntimeError("control Host is already started")
        self._registration = HermesAgentPluginExtension().install(self)

    def close(self) -> None:
        registration = self._registration
        self._registration = None
        if registration is not None:
            registration.close()

    def runtime_descriptor(self) -> object:
        return SimpleNamespace(
            profile=self._profile,
            runtime_generation=self._runtime_generation,
            state="ready",
            capabilities=frozenset(
                {
                    "approval.respond",
                    "clarify.respond",
                    "prompt.submit",
                    "session.control",
                    "session.interrupt",
                    "session.observe",
                    "session.steer",
                }
            ),
        )

    def add_runtime_listener(self, _listener: object) -> CloseRegistration:
        return CloseRegistration()

    def register_local_endpoint(self, endpoint: object) -> CloseRegistration:
        role = endpoint.connection_role
        if role == "local-gateway":
            resource = self._backend.start_local_gateway_endpoint(
                authority=self._authority,
                hello_handler=endpoint.handle_local_hello,
            )
        elif role == "observer":
            resource = CloseRegistration()
        elif role == "control":
            resource = self._backend.start_control_endpoint(
                authority=self._authority,
                dispatcher=endpoint.handle_control_request,
                transport_cleanup=endpoint.transport_disconnected,
            )
        else:
            raise ValueError("unknown Host local endpoint role")
        self._endpoint_resources[role] = resource
        self.endpoint_events.append(f"open:{role}")

        def close_endpoint() -> None:
            try:
                resource.close()
            finally:
                self._endpoint_resources.pop(role, None)
                self.endpoint_events.append(f"close:{role}")

        return CloseRegistration(close_endpoint)

    def prepare_observer(self, _request: object, _sink: object) -> object:
        raise RuntimeError("observer operations are outside Control E2E")

    def control_snapshot(self, _scope: object) -> object:
        with self._lock:
            return SimpleNamespace(
                controller_kind="mobile",
                controller_label="Hermes Mobile",
                control_revision=self._control_revision,
                pending_input=self._pending_input,
            )

    def invoke_owner_action(self, request: object) -> TestOwnerActionResult:
        method = request.method
        payload = dict(request.payload)
        with self._lock:
            self.owner_calls.append(method)
            if self.owner_calls.count(method) == self._effect_unknown_on_call.get(
                method
            ):
                return TestOwnerActionResult(status="effect_unknown")
            response: dict[str, object] = {}
            if method == "prompt.submit":
                response = {
                    "client_turn_id": payload["client_turn_id"],
                    "server_turn_id": "server-turn-1",
                }
            elif method == "approval.respond":
                self._pending_input = {
                    "request_id": "pending-clarify",
                    "kind": "clarify",
                    "question": "Choose the failing target",
                    "choices": [{"id": "choice-1", "label": "app:test"}],
                    "allow_other": False,
                    "expires_at_epoch_ms": 4_102_444_800_000,
                }
                self._control_revision += 1
                response = {
                    "kind": "approval",
                    "request_id": payload["request_id"],
                    "control_revision": self._control_revision,
                }
            elif method == "clarify.respond":
                self._pending_input = None
                self._control_revision += 1
                response = {
                    "kind": "clarify",
                    "request_id": payload["request_id"],
                    "control_revision": self._control_revision,
                }
            return TestOwnerActionResult(status="accepted", payload=response)

    def audit(self, event: object) -> None:
        self.audits.append(str(event.name))


__all__ = ["GatewayExtensionV1ControlTestHost"]
