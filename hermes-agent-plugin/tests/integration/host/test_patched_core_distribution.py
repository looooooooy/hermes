"""Contract test against wheels built from the real patched Hermes Core."""

from __future__ import annotations

import os
from importlib import import_module
from importlib.metadata import distribution, entry_points, version
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar, Mapping

import pytest


def _installed_distribution_path(module: object) -> Path:
    module_file = getattr(module, "__file__", None)
    assert isinstance(module_file, str)
    path = Path(module_file).resolve(strict=True)
    assert any(part in {"site-packages", "dist-packages"} for part in path.parts)
    return path


def test_real_patched_core_wheel_accepts_plugin_owner_action_contract() -> None:
    if os.environ.get("HERMES_PATCHED_CORE_DISTRIBUTION") != "1":
        pytest.skip("requires the isolated patched Core distribution environment")

    core_spi = import_module("hermes_cli.extension_host_v1")
    plugin = import_module("hermes_agent_plugin")
    connector = import_module("hermes_connector")
    owner_control = import_module(
        "hermes_connector.application.owner_control_lane"
    )

    assert version("hermes-agent") == "0.19.0"
    assert version("hermes-agent-plugin") == "0.1.0"
    assert version("hermes-connector") == "0.1.0"
    _installed_distribution_path(core_spi)
    _installed_distribution_path(plugin)
    _installed_distribution_path(connector)
    assert owner_control.OwnerControlLane is not None

    plugin_entry_points = list(
        entry_points().select(
            group="hermes_agent.plugins",
            name="hermes-agent-plugin",
        )
    )
    assert len(plugin_entry_points) == 1
    assert plugin_entry_points[0].load() is plugin

    class Registration:
        def __init__(self, close=lambda: None) -> None:
            self._close = close
            self._closed = False

        def close(self) -> None:
            if self._closed:
                return
            self._closed = True
            self._close()

    class PreparedObserver:
        snapshot = SimpleNamespace(event_sequence=0)
        activation_deadline_monotonic = 100.0

        def activate(self) -> Registration:
            return Registration()

        def close(self) -> None:
            return None

    class Host:
        host_api_version = 1

        def __init__(self) -> None:
            self.endpoints: dict[str, object] = {}
            self.owner_requests: list[object] = []
            self.audit_events: list[object] = []

        def runtime_descriptor(self) -> object:
            return core_spi.RuntimeDescriptor(
                profile="default",
                runtime_generation="generation-1",
                host_bundle_id="com.nousresearch.hermes",
                state="ready",
                capabilities={
                    "prompt.submit": 1,
                    "session.control": 1,
                    "session.observe": 1,
                },
            )

        def add_runtime_listener(self, listener: object) -> Registration:
            self.listener = listener
            return Registration()

        def register_local_endpoint(self, endpoint: object) -> Registration:
            role = endpoint.connection_role
            self.endpoints[role] = endpoint
            return Registration(lambda: self.endpoints.pop(role, None))

        def prepare_observer(
            self,
            request: object,
            _sink: object,
        ) -> PreparedObserver:
            assert type(request) is core_spi.ObserverRequest
            return PreparedObserver()

        def control_snapshot(self, scope: object) -> object:
            assert type(scope) is core_spi.ControlScope
            return SimpleNamespace(control_revision=0)

        def invoke_owner_action(self, request: object) -> object:
            assert type(request) is core_spi.OwnerActionRequest
            self.owner_requests.append(request)
            return core_spi.OwnerActionResult(
                status=core_spi.OwnerActionStatus.ACCEPTED,
                payload={
                    "client_turn_id": request.payload["client_turn_id"],
                    "server_turn_id": "server-turn-1",
                },
            )

        def audit(self, event: object) -> None:
            assert type(event) is core_spi.SafeAuditEvent
            self.audit_events.append(event)

    class Transport:
        connection_role = "control"
        transport_id = "transport-1"
        auth_claims: ClassVar[Mapping[str, str]] = {
            "user_id": "owner-1",
            "provider": "hermes-cloud",
            "client_instance_id": "11111111-1111-4111-8111-111111111111",
            "session_key": "session-1",
            "profile": "default",
        }

    host = Host()
    registration = plugin.HermesAgentPluginExtension().install(host)
    try:
        assert set(host.endpoints) == {"local-gateway", "observer", "control"}
        control = host.endpoints["control"]
        assert "prompt.submit" in control.available_methods

        acquired = control.handle_control_request(
            {
                "jsonrpc": "2.0",
                "id": "acquire-1",
                "method": "session.control.acquire",
                "params": {
                    "session_key": "session-1",
                    "runtime_session_id": "runtime-session-1",
                    "runtime_generation": "generation-1",
                },
            },
            Transport(),
        )
        lease_id = acquired["result"]["lease_id"]
        result = control.handle_control_request(
            {
                "jsonrpc": "2.0",
                "id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                "method": "prompt.submit",
                "params": {
                    "session_key": "session-1",
                    "runtime_session_id": "runtime-session-1",
                    "runtime_generation": "generation-1",
                    "lease_id": lease_id,
                    "client_request_id": "client-request-1",
                    "client_turn_id": "client-turn-1",
                    "text": "Continue the current task.",
                },
            },
            Transport(),
        )

        assert result["result"] == {
            "status": "accepted",
            "client_request_id": "client-request-1",
            "client_turn_id": "client-turn-1",
            "server_turn_id": "server-turn-1",
        }
        assert len(host.owner_requests) == 1
        request = host.owner_requests[0]
        assert request.profile == "default"
        assert request.durable_session_key == "session-1"
        assert request.runtime_generation == "generation-1"
        assert request.command_id == "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        assert request.method == "prompt.submit"
        assert dict(request.payload) == {
            "runtime_session_id": "runtime-session-1",
            "client_turn_id": "client-turn-1",
            "text": "Continue the current task.",
        }
    finally:
        registration.close()

    assert host.endpoints == {}
    assert [event.attributes["action"] for event in host.audit_events] == [
        "started",
        "closed",
    ]


def test_real_patched_core_wheel_contains_runtime_state_module_family() -> None:
    if os.environ.get("HERMES_PATCHED_CORE_DISTRIBUTION") != "1":
        pytest.skip("requires the isolated patched Core distribution environment")

    installed = distribution("hermes-agent").files
    assert installed is not None
    paths = {str(path) for path in installed}
    assert {
        "hermes_state.py",
        "hermes_state_common.py",
        "hermes_state_portability.py",
        "hermes_state_schema.py",
        "hermes_state_search.py",
    } <= paths
