"""Production entry point must use the canonical public Core Host SPI DTOs."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path
from types import MappingProxyType, ModuleType, SimpleNamespace
from typing import ClassVar, Mapping

from tests.test_support.core_host_spi_contract import (
    install_core_host_spi_contract,
    materialize_core_host_spi_contract,
)

import hermes_agent_plugin

PLUGIN_ROOT = Path(__file__).resolve().parents[3]


class _Registration:
    def close(self) -> None:
        return None


class _PreparedObserver:
    snapshot = SimpleNamespace(event_sequence=0)
    activation_deadline_monotonic = 100.0

    def activate(self) -> _Registration:
        return _Registration()

    def close(self) -> None:
        return None


class _CanonicalHost:
    host_api_version = 1

    def __init__(self, public_contract: ModuleType) -> None:
        self._public_contract = public_contract
        self.endpoints: dict[str, object] = {}
        self.audit_events: list[object] = []
        self.observer_requests: list[object] = []
        self.control_scopes: list[object] = []
        self.owner_requests: list[object] = []

    def runtime_descriptor(self) -> object:
        return SimpleNamespace(
            profile="default",
            runtime_generation="generation-1",
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

    def add_runtime_listener(self, listener: object) -> _Registration:
        self.listener = listener
        return _Registration()

    def register_local_endpoint(self, endpoint: object) -> _Registration:
        self.endpoints[endpoint.connection_role] = endpoint
        return _Registration()

    def prepare_observer(
        self,
        request: object,
        sink: object,
    ) -> _PreparedObserver:
        assert type(request) is self._public_contract.ObserverRequest
        self.observer_requests.append(request)
        return _PreparedObserver()

    def control_snapshot(self, scope: object) -> object:
        assert type(scope) is self._public_contract.ControlScope
        self.control_scopes.append(scope)
        return SimpleNamespace(control_revision=0)

    def invoke_owner_action(self, request: object) -> object:
        assert type(request) is self._public_contract.OwnerActionRequest
        self.owner_requests.append(request)
        return SimpleNamespace(status="accepted", payload={})

    def audit(self, event: object) -> None:
        assert type(event) is self._public_contract.SafeAuditEvent
        assert event.name == "runtime.lifecycle"
        assert dict(event.attributes) in (
            {"action": "started", "state": "ready"},
            {"action": "failed", "state": "unavailable"},
            {"action": "closed", "state": "closed"},
        )
        self.audit_events.append(event)


class _Context:
    gateway_extension_spi_version = 1
    gateway_extension_capabilities = frozenset(
        {
            "audit.safe.v1",
            "extension.lifecycle.v1",
            "runtime.descriptor.v1",
            "session.observe.v1",
            "session.owner-actions.v1",
        }
    )

    def __init__(self, host: _CanonicalHost) -> None:
        self._host = host
        self.registration: _Registration | None = None

    def register_gateway_extension(
        self,
        extension: object,
        *,
        spi_version: int,
    ) -> None:
        assert spi_version == 1
        self.registration = extension.install(self._host)


class _ControlTransport:
    connection_role = "control"
    transport_id = "transport-1"
    auth_claims: ClassVar[Mapping[str, str]] = MappingProxyType(
        {
            "user_id": "owner-1",
            "provider": "hermes-cloud",
            "client_instance_id": "11111111-1111-4111-8111-111111111111",
            "session_key": "session-1",
            "profile": "default",
        }
    )


def _public_contract_module(monkeypatch) -> ModuleType:
    module = install_core_host_spi_contract()
    monkeypatch.setitem(sys.modules, module.__name__, module)
    return module


def test_entrypoint_uses_exact_public_core_dtos_for_every_host_call(
    monkeypatch,
) -> None:
    module = _public_contract_module(monkeypatch)
    host = _CanonicalHost(module)
    context = _Context(host)

    hermes_agent_plugin.register(context)

    observer = host.endpoints["observer"]
    observer.prepare_observer(
        {
            "profile": "default",
            "session_key": "session-1",
            "runtime_generation": "generation-1",
        },
        object(),
    ).close()
    control = host.endpoints["control"]
    control.read_control_snapshot(
        {
            "profile": "default",
            "session_key": "session-1",
            "runtime_generation": "generation-1",
        }
    )
    transport = _ControlTransport()
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
        transport,
    )
    control.handle_control_request(
        {
            "jsonrpc": "2.0",
            "id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            "method": "prompt.submit",
            "params": {
                "session_key": "session-1",
                "runtime_session_id": "runtime-session-1",
                "runtime_generation": "generation-1",
                "lease_id": acquired["result"]["lease_id"],
                "client_request_id": "request-1",
                "client_turn_id": "turn-1",
                "text": "continue",
            },
        },
        transport,
    )
    assert context.registration is not None
    context.registration.close()

    assert len(host.observer_requests) == 1
    assert len(host.control_scopes) == 1
    assert len(host.owner_requests) == 1
    assert [dict(event.attributes) for event in host.audit_events] == [
        {"action": "started", "state": "ready"},
        {"action": "closed", "state": "closed"},
    ]


def test_public_register_and_direct_install_use_verified_stage2_core_dtos(
    tmp_path: Path,
) -> None:
    verified_contract_root = materialize_core_host_spi_contract(
        tmp_path / "pinned-core-contract"
    )
    isolated_home = tmp_path / "hermes-home"
    script = textwrap.dedent(
        """
        from types import SimpleNamespace

        from hermes_cli.extension_host_v1 import (
            ControlScope,
            ObserverRequest,
            OwnerActionRequest,
            OwnerActionResult,
            OwnerActionStatus,
            RuntimeDescriptor,
            SafeAuditEvent,
        )
        from hermes_agent_plugin import HermesAgentPluginExtension, register

        CONTEXT_CAPABILITIES = frozenset(
            {
                "audit.safe.v1",
                "extension.lifecycle.v1",
                "runtime.descriptor.v1",
                "session.observe.v1",
                "session.owner-actions.v1",
            }
        )

        class Registration:
            def close(self):
                return None

        class PreparedObserver:
            snapshot = SimpleNamespace(event_sequence=0)
            activation_deadline_monotonic = 100.0

            def activate(self):
                return Registration()

            def close(self):
                return None

        class Host:
            host_api_version = 1

            def __init__(self):
                self.endpoints = {}
                self.audit_events = []

            def runtime_descriptor(self):
                return RuntimeDescriptor(
                    profile="default",
                    runtime_generation="generation-1",
                    host_bundle_id="bundle-1",
                    state="ready",
                    capabilities={
                        "prompt.submit": 1,
                        "session.control": 1,
                        "session.observe": 1,
                    },
                )

            def add_runtime_listener(self, listener):
                self.listener = listener
                return Registration()

            def register_local_endpoint(self, endpoint):
                self.endpoints[endpoint.connection_role] = endpoint
                return Registration()

            def prepare_observer(self, request, sink):
                assert type(request) is ObserverRequest
                return PreparedObserver()

            def control_snapshot(self, scope):
                assert type(scope) is ControlScope
                return SimpleNamespace(control_revision=0)

            def invoke_owner_action(self, request):
                assert type(request) is OwnerActionRequest
                return OwnerActionResult(
                    status=OwnerActionStatus.ACCEPTED,
                    payload={},
                )

            def audit(self, event):
                assert type(event) is SafeAuditEvent
                self.audit_events.append(event)

        class Context:
            gateway_extension_spi_version = 1
            gateway_extension_capabilities = CONTEXT_CAPABILITIES

            def __init__(self, host):
                self.host = host

            def register_gateway_extension(self, extension, *, spi_version):
                assert spi_version == 1
                self.registration = extension.install(self.host)

        class Transport:
            connection_role = "control"
            transport_id = "transport-1"
            auth_claims = {
                "user_id": "owner-1",
                "provider": "hermes-cloud",
                "client_instance_id": "11111111-1111-4111-8111-111111111111",
                "session_key": "session-1",
                "profile": "default",
            }

        def exercise(host, registration):
            host.endpoints["observer"].prepare_observer(
                {
                    "profile": "default",
                    "session_key": "session-1",
                    "runtime_generation": "generation-1",
                },
                object(),
            ).close()
            host.endpoints["control"].read_control_snapshot(
                {
                    "profile": "default",
                    "session_key": "session-1",
                    "runtime_generation": "generation-1",
                }
            )
            transport = Transport()
            acquired = host.endpoints["control"].handle_control_request(
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
                transport,
            )
            result = host.endpoints["control"].handle_control_request(
                {
                    "jsonrpc": "2.0",
                    "id": "command-1",
                    "method": "prompt.submit",
                    "params": {
                        "session_key": "session-1",
                        "runtime_session_id": "runtime-session-1",
                        "runtime_generation": "generation-1",
                        "lease_id": acquired["result"]["lease_id"],
                        "client_request_id": "request-1",
                        "client_turn_id": "turn-1",
                        "text": "continue",
                    },
                },
                transport,
            )
            assert result["result"]["status"] == "accepted"
            registration.close()
            assert len(host.audit_events) == 2

        direct_host = Host()
        exercise(direct_host, HermesAgentPluginExtension().install(direct_host))

        registered_host = Host()
        context = Context(registered_host)
        register(context)
        exercise(registered_host, context.registration)
        """
    )
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["HERMES_HOME"] = str(isolated_home)
    environment["PYTHONPATH"] = os.pathsep.join(
        (str(verified_contract_root), str(PLUGIN_ROOT / "src"))
    )

    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert isolated_home.exists() is False
