"""Unit tests for the frozen Hermes Gateway Extension Host SPI v1."""

from __future__ import annotations

import inspect
import threading
from dataclasses import replace
from functools import partial
from types import SimpleNamespace
from typing import ClassVar

import pytest
from tests.test_support.host_spi_v1 import TEST_HOST_SPI_FACTORIES

from hermes_agent_plugin.adapters.host import extension as extension_module
from hermes_agent_plugin.adapters.host.extension import (
    HermesAgentPluginExtension as _HermesAgentPluginExtension,
)
from hermes_agent_plugin.adapters.local_protocol.control_v1 import (
    CONTROL_AVAILABLE_METHODS,
)
from hermes_agent_plugin.adapters.local_protocol.handshake_v1 import (
    LocalHello,
    decode_local_welcome,
    encode_local_hello,
)
from hermes_agent_plugin.adapters.platform.macos import observer_relay
from hermes_agent_plugin.domain.control_lease import SessionBindingMismatch

HermesAgentPluginExtension = partial(
    _HermesAgentPluginExtension,
    host_spi_factories=TEST_HOST_SPI_FACTORIES,
)


class _Registration:
    def __init__(
        self,
        label: str,
        events: list[str],
        *,
        close_error: BaseException | None = None,
    ) -> None:
        self._label = label
        self._events = events
        self._close_error = close_error
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1
        self._events.append(f"close:{self._label}")
        if self._close_error is not None:
            raise self._close_error


class _PreparedObserver:
    def __init__(self, events: list[str]) -> None:
        self._events = events
        self.close_calls = 0
        self.close_error: BaseException | None = None
        self.subscription: _Registration | None = None
        self.snapshot = SimpleNamespace(
            durable_session_key="durable-root-1",
            event_sequence=7,
        )
        self.activation_deadline_monotonic = 123.0

    def activate(self) -> _Registration:
        self._events.append("activate_observer")
        self.subscription = _Registration("observer-subscription", self._events)
        return self.subscription

    def close(self) -> None:
        self.close_calls += 1
        self._events.append("close_prepared_observer")
        if self.close_error is not None:
            raise self.close_error


class _GatewayHostV1:
    host_api_version = 1

    def __init__(
        self,
        *,
        fail_endpoint_role: str | None = None,
        owner_status: str = "accepted",
        owner_payload: dict[str, object] | None = None,
        owner_error: BaseException | None = None,
        failing_registration_role: str | None = None,
        runtime_capabilities: object | None = None,
        none_registration_role: str | None = None,
    ) -> None:
        self.events: list[str] = []
        self.endpoints: dict[str, object] = {}
        self.registrations: dict[str, _Registration] = {}
        self.owner_requests: list[object] = []
        self.observer_requests: list[object] = []
        self.observer_preparations: list[_PreparedObserver] = []
        self.observer_sinks: list[object] = []
        self.snapshot_scopes: list[object] = []
        self.audit_events: list[object] = []
        self.fail_endpoint_role = fail_endpoint_role
        self.owner_status = owner_status
        self.owner_payload = owner_payload or {"server_turn_id": "server-turn-1"}
        self.owner_error = owner_error
        self.failing_registration_role = failing_registration_role
        self.none_registration_role = none_registration_role
        self.runtime_capabilities = (
            runtime_capabilities
            if runtime_capabilities is not None
            else frozenset(
                {
                    "session.observe",
                    "session.control",
                    "prompt.submit",
                    "session.interrupt",
                    "session.steer",
                    "approval.respond",
                    "clarify.respond",
                }
            )
        )

    def runtime_descriptor(self) -> object:
        self.events.append("runtime_descriptor")
        return SimpleNamespace(
            agent_id="agent-1",
            agent_version="0.21.0",
            profile="default",
            runtime_generation="runtime-generation-1",
            state="ready",
            capabilities=self.runtime_capabilities,
        )

    def add_runtime_listener(self, listener: object) -> _Registration:
        self.events.append("add_runtime_listener")
        self.runtime_listener = listener
        registration = _Registration("runtime-listener", self.events)
        self.registrations["runtime-listener"] = registration
        return registration

    def register_local_endpoint(self, endpoint: object) -> _Registration:
        role = endpoint.connection_role
        self.events.append(f"register_local_endpoint:{role}")
        if role == self.fail_endpoint_role:
            raise RuntimeError(f"cannot register {role}")
        self.endpoints[role] = endpoint
        if role == self.none_registration_role:
            return None
        registration = _Registration(
            role,
            self.events,
            close_error=(
                RuntimeError(f"cannot close {role}")
                if role == self.failing_registration_role
                else None
            ),
        )
        self.registrations[role] = registration
        return registration

    def prepare_observer(self, request: object, sink: object) -> _PreparedObserver:
        self.events.append("prepare_observer")
        self.observer_requests.append(request)
        self.observer_sink = sink
        self.observer_sinks.append(sink)
        prepared = _PreparedObserver(self.events)
        self.observer_preparations.append(prepared)
        return prepared

    def control_snapshot(self, scope: object) -> object:
        self.events.append("control_snapshot")
        self.snapshot_scopes.append(scope)
        return SimpleNamespace(
            controller_kind="none",
            control_revision=7,
            pending_input=None,
        )

    def invoke_owner_action(self, request: object) -> object:
        self.events.append("invoke_owner_action")
        self.owner_requests.append(request)
        if self.owner_error is not None:
            raise self.owner_error
        return SimpleNamespace(
            status=self.owner_status,
            payload=dict(self.owner_payload),
        )

    def audit(self, event: object) -> None:
        self.audit_events.append(event)
        self.events.append(f"audit:{event.name}")


class _ControlTransport:
    connection_role = "control"
    transport_id = "transport-1"
    auth_claims: ClassVar[dict[str, str]] = {
        "user_id": "user-1",
        "provider": "test",
        "client_instance_id": "11111111-1111-4111-8111-111111111111",
        "session_key": "durable-root-1",
        "profile": "default",
    }


@pytest.mark.parametrize("host_api_version", (True, 1.0))
def test_install_rejects_non_integer_host_api_version_before_host_calls(
    host_api_version: object,
) -> None:
    host = _GatewayHostV1()
    host.host_api_version = host_api_version

    with pytest.raises(RuntimeError, match="Host API v1 is required"):
        HermesAgentPluginExtension().install(host)

    assert host.events == []
    assert host.endpoints == {}


def _rpc(
    *,
    request_id: object,
    method: str,
    params: dict[str, object],
) -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": method,
        "params": params,
    }


def _acquire_lease(endpoint: object, transport: object) -> str:
    response = endpoint.handle_control_request(
        _rpc(
            request_id="acquire-1",
            method="session.control.acquire",
            params={
                "session_key": "durable-root-1",
                "runtime_session_id": "runtime-session-1",
                "runtime_generation": "runtime-generation-1",
            },
        ),
        transport,
    )
    return response["result"]["lease_id"]


def _mutation_params(lease_id: str) -> dict[str, object]:
    return {
        "session_key": "durable-root-1",
        "runtime_session_id": "runtime-session-1",
        "runtime_generation": "runtime-generation-1",
        "lease_id": lease_id,
        "client_request_id": "mobile-request-1",
        "client_turn_id": "client-turn-1",
        "text": "Continue the current task.",
    }


def test_install_uses_only_frozen_host_facade_and_returns_idempotent_registration() -> (
    None
):
    host = _GatewayHostV1()

    registration = HermesAgentPluginExtension().install(host)

    assert host.events == [
        "runtime_descriptor",
        "add_runtime_listener",
        "register_local_endpoint:local-gateway",
        "register_local_endpoint:observer",
        "register_local_endpoint:control",
        "audit:runtime.lifecycle",
    ]
    assert set(host.endpoints) == {"local-gateway", "observer", "control"}
    assert host.endpoints["control"].available_methods == CONTROL_AVAILABLE_METHODS
    assert not hasattr(host, "register_connection_role")
    welcome = decode_local_welcome(
        host.endpoints["local-gateway"].handle_local_hello(
            encode_local_hello(
                LocalHello(
                    contract_version=1,
                    message_type="local.hello",
                    client_instance_id="11111111-1111-4111-8111-111111111111",
                    profile="default",
                    required_capabilities=("session.observe",),
                    optional_capabilities=("session.control",),
                    extensions={},
                )
            )
        )
    )
    assert welcome.runtime_generation == "runtime-generation-1"
    assert welcome.accepted_capabilities == (
        "session.control",
        "session.observe",
    )

    registration.close()
    registration.close()

    assert host.events[-5:] == [
        "close:control",
        "close:observer",
        "close:local-gateway",
        "close:runtime-listener",
        "audit:runtime.lifecycle",
    ]
    assert all(child.close_calls == 1 for child in host.registrations.values())


def test_registered_endpoints_open_through_the_canonical_runtime_descriptor() -> None:
    opened: list[tuple[str, object]] = []
    endpoint_registrations: list[_Registration] = []

    def open_endpoint(endpoint: object, runtime: object) -> _Registration:
        opened.append((endpoint.connection_role, runtime))
        registration = _Registration(
            f"opened-{endpoint.connection_role}",
            host.events,
        )
        endpoint_registrations.append(registration)
        return registration

    class CanonicalOpeningHost(_GatewayHostV1):
        def register_local_endpoint(self, endpoint: object) -> _Registration:
            parameters = tuple(
                inspect.signature(endpoint.open_local_endpoint).parameters.values()
            )
            assert len(parameters) == 1
            assert parameters[0].name == "runtime"
            runtime = self.runtime_descriptor()
            registration = endpoint.open_local_endpoint(runtime)
            self.events.append(f"register_local_endpoint:{endpoint.connection_role}")
            self.endpoints[endpoint.connection_role] = endpoint
            self.registrations[endpoint.connection_role] = registration
            return registration

    host = CanonicalOpeningHost()
    registration = HermesAgentPluginExtension(
        endpoint_opener=open_endpoint,
    ).install(host)

    assert [role for role, _runtime in opened] == [
        "local-gateway",
        "observer",
        "control",
    ]
    assert all(
        runtime.runtime_generation == "runtime-generation-1" for _, runtime in opened
    )

    registration.close()

    assert [child.close_calls for child in endpoint_registrations] == [1, 1, 1]


def test_observer_snapshot_write_failure_closes_prepared_without_activation() -> None:
    class Prepared(_PreparedObserver):
        def __init__(self, events: list[str]) -> None:
            super().__init__(events)
            self.snapshot = {
                "profile": "default",
                "runtime_generation": "runtime-generation-1",
                "session_key": "durable-root-1",
                "event_sequence": 0,
            }

    class Host(_GatewayHostV1):
        def prepare_observer(self, request: object, sink: object) -> Prepared:
            self.events.append("prepare_observer")
            prepared = Prepared(self.events)
            self.observer_preparations.append(prepared)
            return prepared

    class FailingTransport:
        def __init__(self) -> None:
            self.write_calls = 0
            self.disconnect_calls = 0

        def write(self, _frame: dict[str, object]) -> bool:
            self.write_calls += 1
            return False

        def disconnect(self) -> None:
            self.disconnect_calls += 1

    host = Host()
    registration = HermesAgentPluginExtension().install(host)
    prepared: Prepared | None = None
    try:
        transport = FailingTransport()
        endpoint = host.endpoints["observer"]

        result = endpoint.handle_observer_request(
            _rpc(
                request_id="subscribe-1",
                method="session.observe.subscribe",
                params={
                    "profile": "default",
                    "session_key": "durable-root-1",
                    "runtime_generation": "runtime-generation-1",
                },
            ),
            transport,
        )

        prepared = host.observer_preparations[-1]
        assert result is None
        assert transport.write_calls == 1
        assert transport.disconnect_calls == 1
        assert "activate_observer" not in host.events
        assert prepared.close_calls == 1
    finally:
        registration.close()

    assert prepared is not None
    assert prepared.close_calls == 1


def test_observer_snapshot_write_success_precedes_activation_and_disconnect_close() -> (
    None
):
    class Prepared(_PreparedObserver):
        def __init__(self, events: list[str]) -> None:
            super().__init__(events)
            self.snapshot = {
                "profile": "default",
                "runtime_generation": "runtime-generation-1",
                "session_key": "durable-root-1",
                "event_sequence": 0,
            }

    class Host(_GatewayHostV1):
        def prepare_observer(self, request: object, sink: object) -> Prepared:
            self.events.append("prepare_observer")
            prepared = Prepared(self.events)
            self.observer_preparations.append(prepared)
            return prepared

    class Transport:
        def __init__(self, events: list[str]) -> None:
            self.events = events
            self.frames: list[dict[str, object]] = []

        def write(self, frame: dict[str, object]) -> bool:
            self.events.append("snapshot:write")
            self.frames.append(frame)
            return True

        def disconnect(self) -> None:
            self.events.append("transport:disconnect")

    host = Host()
    registration = HermesAgentPluginExtension().install(host)
    try:
        transport = Transport(host.events)
        endpoint = host.endpoints["observer"]

        result = endpoint.handle_observer_request(
            _rpc(
                request_id="subscribe-1",
                method="session.observe.subscribe",
                params={
                    "profile": "default",
                    "session_key": "durable-root-1",
                    "runtime_generation": "runtime-generation-1",
                },
            ),
            transport,
        )

        prepared = host.observer_preparations[-1]
        assert result is None
        assert host.events.index("snapshot:write") < host.events.index(
            "activate_observer"
        )
        assert prepared.subscription is not None
        assert prepared.subscription.close_calls == 0

        endpoint.transport_disconnected(transport)
        endpoint.transport_disconnected(transport)

        assert prepared.subscription.close_calls == 1
    finally:
        registration.close()


def test_catalog_subscribe_pulls_the_initial_host_page_through_the_observer_wire() -> (
    None
):
    class Host(_GatewayHostV1):
        def __init__(self) -> None:
            super().__init__()
            self.runtime_capabilities = self.runtime_capabilities | {
                "session.catalog.v1"
            }
            self.catalog_requests: list[object] = []

        def add_session_catalog_listener(self, listener: object) -> _Registration:
            self.events.append("add_session_catalog_listener")
            self.catalog_listener = listener
            return _Registration("session-catalog-listener", self.events)

        def session_catalog(self, request: object) -> object:
            self.events.append("session_catalog")
            self.catalog_requests.append(request)
            return SimpleNamespace(
                profile="default",
                runtime_generation="runtime-generation-1",
                catalog_revision=7,
                sessions=(
                    SimpleNamespace(
                        profile="default",
                        durable_session_key="durable-root-1",
                        runtime_generation="runtime-generation-1",
                        surface="gateway",
                        authority_revision=3,
                        available_actions=frozenset(
                            {"prompt.submit", "session.interrupt"}
                        ),
                    ),
                ),
                next_cursor=None,
            )

    class Transport:
        def __init__(self) -> None:
            self.frames: list[dict[str, object]] = []

        def write(self, frame: dict[str, object]) -> bool:
            self.frames.append(frame)
            return True

    host = Host()
    registration = HermesAgentPluginExtension().install(host)
    try:
        transport = Transport()
        endpoint = host.endpoints["observer"]

        result = endpoint.handle_observer_request(
            _rpc(
                request_id="11111111-1111-4111-8111-111111111111",
                method="session.catalog.subscribe",
                params={
                    "profile": "default",
                    "runtime_generation": "runtime-generation-1",
                    "page_size": 128,
                },
            ),
            transport,
        )

        assert result is None
        assert host.events.index("add_session_catalog_listener") < host.events.index(
            "session_catalog"
        )
        assert len(host.catalog_requests) == 1
        request = host.catalog_requests[0]
        assert request.profile == "default"
        assert request.runtime_generation == "runtime-generation-1"
        assert request.page_size == 128
        assert request.cursor is None
        frame = transport.frames[0]
        assert frame["jsonrpc"] == "2.0"
        assert frame["id"] == "11111111-1111-4111-8111-111111111111"
        assert set(frame["result"]) == {
            "subscription_id",
            "snapshot_id",
            "profile",
            "runtime_generation",
            "catalog_revision",
            "page_index",
            "is_last",
            "sessions",
            "next_cursor",
        }
        assert frame["result"]["page_index"] == 0
        assert frame["result"]["is_last"] is True
        assert frame["result"]["sessions"] == [
            {
                "session_key": "durable-root-1",
                "surface": "gateway",
                "authority_revision": 3,
                "available_actions": [
                    "prompt.submit",
                    "session.interrupt",
                ],
            }
        ]
        assert endpoint.available_methods.issuperset(
            {
                "session.catalog.subscribe",
                "session.catalog.page",
                "session.catalog.unsubscribe",
            }
        )
        assert "session.catalog.v1" in endpoint.available_capabilities
    finally:
        registration.close()


def test_pre_catalog_host_loads_observer_and_control_without_a_catalog_fallback() -> (
    None
):
    host = _GatewayHostV1()
    legacy_factories = replace(
        TEST_HOST_SPI_FACTORIES,
        session_catalog_request=None,
    )
    registration = _HermesAgentPluginExtension(
        host_spi_factories=legacy_factories,
    ).install(host)
    try:
        endpoint = host.endpoints["observer"]

        assert endpoint.available_methods.isdisjoint(
            {
                "session.catalog.subscribe",
                "session.catalog.page",
                "session.catalog.unsubscribe",
            }
        )
        assert "session.catalog.v1" not in endpoint.available_capabilities
        with pytest.raises(ValueError, match="observer method is unavailable"):
            endpoint.handle_observer_request(
                _rpc(
                    request_id="11111111-1111-4111-8111-111111111111",
                    method="session.catalog.subscribe",
                    params={
                        "profile": "default",
                        "runtime_generation": "runtime-generation-1",
                        "page_size": 128,
                    },
                ),
                object(),
            )
        assert not hasattr(host, "catalog_listener")
        assert "add_session_catalog_listener" not in host.events
        assert "session_catalog" not in host.events
    finally:
        registration.close()


def test_declared_catalog_capability_requires_the_public_catalog_dto() -> None:
    class Host(_GatewayHostV1):
        def add_session_catalog_listener(self, listener: object) -> _Registration:
            self.catalog_listener = listener
            return _Registration("session-catalog-listener", self.events)

        def session_catalog(self, request: object) -> object:
            raise AssertionError("catalog must not be called during installation")

    host = Host()
    host.runtime_capabilities = host.runtime_capabilities | {"session.catalog.v1"}
    legacy_factories = replace(
        TEST_HOST_SPI_FACTORIES,
        session_catalog_request=None,
    )

    with pytest.raises(RuntimeError, match="session catalog Host SPI is unavailable"):
        _HermesAgentPluginExtension(
            host_spi_factories=legacy_factories,
        ).install(host)

    assert host.endpoints == {}
    assert host.events == ["runtime_descriptor"]


def test_declared_catalog_capability_requires_the_host_catalog_methods() -> None:
    host = _GatewayHostV1()
    host.runtime_capabilities = host.runtime_capabilities | {"session.catalog.v1"}

    with pytest.raises(RuntimeError, match="session catalog Host SPI is unavailable"):
        HermesAgentPluginExtension().install(host)

    assert host.endpoints == {}
    assert host.events == ["runtime_descriptor"]


def test_runtime_capability_can_enable_catalog_when_dto_and_host_methods_exist() -> (
    None
):
    class Host(_GatewayHostV1):
        def add_session_catalog_listener(self, listener: object) -> _Registration:
            self.catalog_listener = listener
            return _Registration("session-catalog-listener", self.events)

        def session_catalog(self, request: object) -> object:
            return SimpleNamespace(
                profile=request.profile,
                runtime_generation=request.runtime_generation,
                catalog_revision=1,
                sessions=(),
                next_cursor=None,
            )

    class Transport:
        def __init__(self) -> None:
            self.frames: list[dict[str, object]] = []

        def write(self, frame: dict[str, object]) -> bool:
            self.frames.append(frame)
            return True

    host = Host()
    registration = HermesAgentPluginExtension().install(host)
    try:
        endpoint = host.endpoints["observer"]
        assert "session.catalog.v1" not in endpoint.available_capabilities

        host.runtime_listener(
            SimpleNamespace(
                profile="default",
                runtime_generation="runtime-generation-2",
                state="ready",
                capabilities=host.runtime_capabilities | {"session.catalog.v1"},
            )
        )

        assert "session.catalog.v1" in endpoint.available_capabilities
        transport = Transport()
        endpoint.handle_observer_request(
            _rpc(
                request_id="11111111-1111-4111-8111-111111111111",
                method="session.catalog.subscribe",
                params={
                    "profile": "default",
                    "runtime_generation": "runtime-generation-2",
                    "page_size": 128,
                },
            ),
            transport,
        )
        assert transport.frames[0]["result"]["runtime_generation"] == (
            "runtime-generation-2"
        )
    finally:
        registration.close()


def test_runtime_capability_does_not_publish_catalog_without_dto_or_host_methods() -> (
    None
):
    host = _GatewayHostV1()
    legacy_factories = replace(
        TEST_HOST_SPI_FACTORIES,
        session_catalog_request=None,
    )
    registration = _HermesAgentPluginExtension(
        host_spi_factories=legacy_factories,
    ).install(host)
    endpoint = host.endpoints["observer"]
    events_before_transition = tuple(host.events)
    try:
        with pytest.raises(
            RuntimeError,
            match="session catalog Host SPI is unavailable",
        ):
            host.runtime_listener(
                SimpleNamespace(
                    profile="default",
                    runtime_generation="runtime-generation-2",
                    state="ready",
                    capabilities=host.runtime_capabilities | {"session.catalog.v1"},
                )
            )

        assert endpoint.runtime_generation == "runtime-generation-1"
        assert "session.catalog.v1" not in endpoint.available_capabilities
        assert tuple(host.events) == events_before_transition
    finally:
        registration.close()


def test_runtime_rollover_resets_catalog_before_old_listener_can_deliver() -> None:
    class Host(_GatewayHostV1):
        def __init__(self) -> None:
            super().__init__()
            self.runtime_capabilities = self.runtime_capabilities | {
                "session.catalog.v1"
            }
            self.catalog_registration = _Registration("catalog-listener", self.events)

        def add_session_catalog_listener(self, listener: object) -> _Registration:
            self.catalog_listener = listener
            return self.catalog_registration

        def session_catalog(self, request: object) -> object:
            return SimpleNamespace(
                profile=request.profile,
                runtime_generation=request.runtime_generation,
                catalog_revision=7,
                sessions=(
                    SimpleNamespace(
                        profile=request.profile,
                        durable_session_key="durable-root-1",
                        runtime_generation=request.runtime_generation,
                        surface="gateway",
                        authority_revision=1,
                        available_actions=frozenset({"prompt.submit"}),
                    ),
                ),
                next_cursor="cursor-1",
            )

    class Transport:
        def __init__(self) -> None:
            self.frames: list[dict[str, object]] = []

        def write(self, frame: dict[str, object]) -> bool:
            self.frames.append(frame)
            return True

    host = Host()
    registration = HermesAgentPluginExtension().install(host)
    try:
        transport = Transport()
        endpoint = host.endpoints["observer"]
        endpoint.handle_observer_request(
            _rpc(
                request_id="11111111-1111-4111-8111-111111111111",
                method="session.catalog.subscribe",
                params={
                    "profile": "default",
                    "runtime_generation": "runtime-generation-1",
                    "page_size": 128,
                },
            ),
            transport,
        )
        stale_listener = host.catalog_listener

        host.runtime_listener(
            SimpleNamespace(
                profile="default",
                runtime_generation="runtime-generation-2",
                state="ready",
                capabilities=host.runtime_capabilities,
            )
        )
        stale_listener(
            SimpleNamespace(
                profile="default",
                runtime_generation="runtime-generation-1",
                sequence=8,
                action="upsert",
                entry=SimpleNamespace(),
            )
        )

        assert transport.frames[-1]["method"] == "session.catalog.reset_required"
        assert transport.frames[-1]["params"]["reason"] == "runtime_generation_changed"
        assert len(transport.frames) == 2
        assert host.catalog_registration.close_calls == 1
    finally:
        registration.close()


def test_runtime_rollover_releases_observer_wire_capacity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        extension_module,
        "MAX_OBSERVER_SUBSCRIPTIONS_PER_TRANSPORT",
        1,
        raising=False,
    )
    monkeypatch.setattr(
        extension_module,
        "MAX_OBSERVER_SUBSCRIPTIONS_TOTAL",
        1,
        raising=False,
    )

    class Transport:
        def __init__(self) -> None:
            self.frames: list[dict[str, object]] = []

        def write(self, frame: dict[str, object]) -> bool:
            self.frames.append(frame)
            return True

        def disconnect(self) -> None:
            return None

    class Host(_GatewayHostV1):
        def prepare_observer(self, request: object, sink: object) -> _PreparedObserver:
            prepared = super().prepare_observer(request, sink)
            prepared.snapshot = {
                "profile": "default",
                "runtime_generation": "runtime-generation-1",
                "session_key": "durable-root-1",
                "event_sequence": 0,
            }
            return prepared

    host = Host()
    registration = HermesAgentPluginExtension().install(host)
    try:
        endpoint = host.endpoints["observer"]
        first = Transport()
        endpoint.handle_observer_request(
            _rpc(
                request_id="subscribe-1",
                method="session.observe.subscribe",
                params={
                    "profile": "default",
                    "session_key": "durable-root-1",
                    "runtime_generation": "runtime-generation-1",
                },
            ),
            first,
        )

        host.runtime_listener(
            SimpleNamespace(
                profile="default",
                runtime_generation="runtime-generation-2",
                state="ready",
                capabilities=host.runtime_capabilities,
            )
        )

        assert not endpoint._wire_controller._transport_states
        assert endpoint._wire_controller._reserved_subscriptions == 0
        second = Transport()
        endpoint.handle_observer_request(
            _rpc(
                request_id="subscribe-2",
                method="session.observe.subscribe",
                params={
                    "profile": "default",
                    "session_key": "durable-root-1",
                    "runtime_generation": "runtime-generation-2",
                },
            ),
            second,
        )
    finally:
        registration.close()


def test_runtime_generation_is_not_published_while_old_catalog_event_can_commit() -> (
    None
):
    class Host(_GatewayHostV1):
        def __init__(self) -> None:
            super().__init__()
            self.runtime_capabilities = self.runtime_capabilities | {
                "session.catalog.v1"
            }
            self.catalog_registration = _Registration("catalog-listener", self.events)

        def add_session_catalog_listener(self, listener: object) -> _Registration:
            self.catalog_listener = listener
            return self.catalog_registration

        def session_catalog(self, request: object) -> object:
            return SimpleNamespace(
                profile=request.profile,
                runtime_generation=request.runtime_generation,
                catalog_revision=7,
                sessions=(),
                next_cursor=None,
            )

    class BlockingTransport:
        def __init__(self) -> None:
            self.frames: list[dict[str, object]] = []
            self.event_write_started = threading.Event()
            self.release_event_write = threading.Event()
            self.new_generation_visible = threading.Event()
            self.old_event_committed_after_new_visible = False

        def write(self, frame: dict[str, object]) -> bool:
            if frame.get("method") == "session.catalog.event":
                self.event_write_started.set()
                assert self.release_event_write.wait(timeout=1)
                self.old_event_committed_after_new_visible = (
                    self.new_generation_visible.is_set()
                )
            self.frames.append(frame)
            return True

    host = Host()
    registration = HermesAgentPluginExtension().install(host)
    try:
        transport = BlockingTransport()
        endpoint = host.endpoints["observer"]
        endpoint.handle_observer_request(
            _rpc(
                request_id="11111111-1111-4111-8111-111111111111",
                method="session.catalog.subscribe",
                params={
                    "profile": "default",
                    "runtime_generation": "runtime-generation-1",
                    "page_size": 128,
                },
            ),
            transport,
        )
        event_errors: list[BaseException] = []
        rollover_errors: list[BaseException] = []

        def emit_old_event() -> None:
            try:
                host.catalog_listener(
                    SimpleNamespace(
                        profile="default",
                        runtime_generation="runtime-generation-1",
                        sequence=8,
                        action="upsert",
                        entry=SimpleNamespace(
                            profile="default",
                            durable_session_key="durable-root-1",
                            runtime_generation="runtime-generation-1",
                            surface="gateway",
                            authority_revision=1,
                            available_actions=frozenset({"prompt.submit"}),
                        ),
                    )
                )
            except BaseException as error:  # noqa: BLE001
                event_errors.append(error)

        def rollover() -> None:
            try:
                host.runtime_listener(
                    SimpleNamespace(
                        profile="default",
                        runtime_generation="runtime-generation-2",
                        state="ready",
                        capabilities=host.runtime_capabilities,
                    )
                )
                transport.new_generation_visible.set()
            except BaseException as error:  # noqa: BLE001
                rollover_errors.append(error)

        event_worker = threading.Thread(target=emit_old_event)
        event_worker.start()
        assert transport.event_write_started.wait(timeout=1)
        rollover_worker = threading.Thread(target=rollover)
        rollover_worker.start()
        visible_while_old_write_blocked = transport.new_generation_visible.wait(
            timeout=0.2
        )
        transport.release_event_write.set()
        event_worker.join(timeout=1)
        rollover_worker.join(timeout=1)

        assert event_errors == []
        assert rollover_errors == []
        assert not event_worker.is_alive()
        assert not rollover_worker.is_alive()
        assert visible_while_old_write_blocked is False
        assert transport.old_event_committed_after_new_visible is False
        assert endpoint.runtime_generation == "runtime-generation-2"
    finally:
        registration.close()


def test_production_transport_timeout_unblocks_rollover_and_fails_closed() -> None:
    class Host(_GatewayHostV1):
        def __init__(self) -> None:
            super().__init__()
            self.runtime_capabilities = self.runtime_capabilities | {
                "session.catalog.v1"
            }
            self.catalog_registration = _Registration("catalog-listener", self.events)

        def add_session_catalog_listener(self, listener: object) -> _Registration:
            self.catalog_listener = listener
            return self.catalog_registration

        def session_catalog(self, request: object) -> object:
            return SimpleNamespace(
                profile=request.profile,
                runtime_generation=request.runtime_generation,
                catalog_revision=7,
                sessions=(),
                next_cursor=None,
            )

    class NeverReturningWebSocket:
        def __init__(self) -> None:
            self.send_calls = 0
            self.completed_sends = 0
            self.blocked_send_started = threading.Event()
            self.closed = threading.Event()
            self.never_release = threading.Event()

        def send(self, _value: str) -> None:
            self.send_calls += 1
            if self.send_calls == 1:
                self.completed_sends += 1
                return
            self.blocked_send_started.set()
            self.never_release.wait()
            self.completed_sends += 1

        def close(self) -> None:
            self.closed.set()

    host = Host()
    registration = HermesAgentPluginExtension().install(host)
    websocket = NeverReturningWebSocket()
    transport = observer_relay._ObserverSocketTransport(
        websocket,
        send_timeout_s=0.05,
        send_abort_grace_s=0.01,
        send_executor=observer_relay._BoundedCallExecutor(
            worker_limit=1,
            queue_limit=1,
            thread_name_prefix="hermes-test-never-send",
        ),
    )
    try:
        endpoint = host.endpoints["observer"]
        endpoint.handle_observer_request(
            _rpc(
                request_id="11111111-1111-4111-8111-111111111111",
                method="session.catalog.subscribe",
                params={
                    "profile": "default",
                    "runtime_generation": "runtime-generation-1",
                    "page_size": 128,
                },
            ),
            transport,
        )
        event_errors: list[BaseException] = []
        rollover_errors: list[BaseException] = []
        rollover_done = threading.Event()

        def emit_old_event() -> None:
            try:
                host.catalog_listener(
                    SimpleNamespace(
                        profile="default",
                        runtime_generation="runtime-generation-1",
                        sequence=8,
                        action="upsert",
                        entry=SimpleNamespace(
                            profile="default",
                            durable_session_key="durable-root-1",
                            runtime_generation="runtime-generation-1",
                            surface="gateway",
                            authority_revision=1,
                            available_actions=frozenset({"prompt.submit"}),
                        ),
                    )
                )
            except BaseException as error:  # noqa: BLE001
                event_errors.append(error)

        def rollover() -> None:
            try:
                host.runtime_listener(
                    SimpleNamespace(
                        profile="default",
                        runtime_generation="runtime-generation-2",
                        state="ready",
                        capabilities=host.runtime_capabilities,
                    )
                )
            except BaseException as error:  # noqa: BLE001
                rollover_errors.append(error)
            finally:
                rollover_done.set()

        event_worker = threading.Thread(target=emit_old_event)
        event_worker.start()
        assert websocket.blocked_send_started.wait(timeout=1)
        rollover_worker = threading.Thread(target=rollover)
        rollover_worker.start()

        assert rollover_done.wait(timeout=0.5)
        event_worker.join(timeout=0.5)
        rollover_worker.join(timeout=0.5)
        assert event_errors == []
        assert rollover_errors == []
        assert not event_worker.is_alive()
        assert not rollover_worker.is_alive()
        assert endpoint.runtime_generation == "runtime-generation-2"
        assert websocket.completed_sends == 1
        assert websocket.closed.wait(timeout=0.5)
        assert host.catalog_registration.close_calls == 1
    finally:
        registration.close()


def test_runtime_rollover_closes_all_old_roles_once_before_opening_new_roles() -> None:
    class RuntimeOpeningHost(_GatewayHostV1):
        def __init__(self) -> None:
            super().__init__()
            self.current_runtime = SimpleNamespace(
                profile="default",
                runtime_generation="runtime-generation-1",
                host_bundle_id="ai.hermes.agent",
                state="ready",
                capabilities=self.runtime_capabilities,
            )

        def runtime_descriptor(self) -> object:
            self.events.append("runtime_descriptor")
            return self.current_runtime

        def register_local_endpoint(self, endpoint: object) -> _Registration:
            return endpoint.open_local_endpoint(self.runtime_descriptor())

    host = RuntimeOpeningHost()

    def open_endpoint(endpoint: object, runtime: object) -> _Registration:
        generation = runtime.runtime_generation
        host.events.append(f"open:{endpoint.connection_role}:{generation}")
        return _Registration(
            f"opened-{endpoint.connection_role}-{generation}",
            host.events,
        )

    registration = HermesAgentPluginExtension(
        endpoint_opener=open_endpoint,
    ).install(host)
    host.current_runtime = SimpleNamespace(
        profile="default",
        runtime_generation="runtime-generation-2",
        host_bundle_id="ai.hermes.agent",
        state="ready",
        capabilities=host.runtime_capabilities,
    )

    host.runtime_listener(host.current_runtime)

    rollover_events = host.events[
        host.events.index("close:opened-local-gateway-runtime-generation-1") :
    ]
    assert rollover_events[:9] == [
        "close:opened-local-gateway-runtime-generation-1",
        "close:opened-observer-runtime-generation-1",
        "close:opened-control-runtime-generation-1",
        "runtime_descriptor",
        "open:local-gateway:runtime-generation-2",
        "runtime_descriptor",
        "open:observer:runtime-generation-2",
        "runtime_descriptor",
        "open:control:runtime-generation-2",
    ]

    registration.close()


def test_close_audit_uses_the_current_runtime_generation_after_rollover() -> None:
    host = _GatewayHostV1()
    registration = HermesAgentPluginExtension().install(host)
    host.runtime_listener(
        SimpleNamespace(
            profile="default",
            runtime_generation="runtime-generation-2",
            state="ready",
            capabilities=host.runtime_capabilities,
        )
    )

    registration.close()

    assert host.audit_events[-1].runtime_generation == "runtime-generation-2"
    assert dict(host.audit_events[-1].attributes) == {
        "action": "closed",
        "state": "closed",
    }


def test_started_audit_uses_synchronous_listener_generation() -> None:
    class SynchronousRolloverHost(_GatewayHostV1):
        def add_runtime_listener(self, listener: object) -> _Registration:
            registration = super().add_runtime_listener(listener)
            listener(
                SimpleNamespace(
                    profile="default",
                    runtime_generation="runtime-generation-2",
                    state="ready",
                    capabilities=self.runtime_capabilities,
                )
            )
            return registration

    host = SynchronousRolloverHost()
    registration = HermesAgentPluginExtension().install(host)
    try:
        assert host.audit_events[0].runtime_generation == "runtime-generation-2"
    finally:
        registration.close()


def test_started_audit_rereads_generation_after_concurrent_rollover() -> None:
    class BlockingControlRegistrationHost(_GatewayHostV1):
        def __init__(self) -> None:
            super().__init__()
            self.control_registration_entered = threading.Event()
            self.release_control_registration = threading.Event()

        def register_local_endpoint(self, endpoint: object) -> _Registration:
            if endpoint.connection_role == "control":
                self.control_registration_entered.set()
                assert self.release_control_registration.wait(timeout=1)
            return super().register_local_endpoint(endpoint)

    host = BlockingControlRegistrationHost()
    installed: list[object] = []
    install_errors: list[BaseException] = []

    def install() -> None:
        try:
            installed.append(HermesAgentPluginExtension().install(host))
        except BaseException as error:  # noqa: BLE001
            install_errors.append(error)

    install_thread = threading.Thread(target=install, daemon=True)
    install_thread.start()
    assert host.control_registration_entered.wait(timeout=1)
    host.runtime_listener(
        SimpleNamespace(
            profile="default",
            runtime_generation="runtime-generation-2",
            state="ready",
            capabilities=host.runtime_capabilities,
        )
    )
    host.release_control_registration.set()
    install_thread.join(timeout=1)

    assert not install_thread.is_alive()
    assert install_errors == []
    assert host.audit_events[0].runtime_generation == "runtime-generation-2"
    installed[0].close()


def test_failed_audit_uses_synchronous_listener_generation() -> None:
    class SynchronousRolloverHost(_GatewayHostV1):
        def add_runtime_listener(self, listener: object) -> _Registration:
            registration = super().add_runtime_listener(listener)
            listener(
                SimpleNamespace(
                    profile="default",
                    runtime_generation="runtime-generation-2",
                    state="ready",
                    capabilities=self.runtime_capabilities,
                )
            )
            return registration

    host = SynchronousRolloverHost(fail_endpoint_role="control")

    with pytest.raises(RuntimeError, match="cannot register control"):
        HermesAgentPluginExtension().install(host)

    assert host.audit_events[-1].runtime_generation == "runtime-generation-2"
    assert dict(host.audit_events[-1].attributes) == {
        "action": "failed",
        "state": "unavailable",
    }


def test_install_failure_closes_created_resources_in_reverse_order() -> None:
    host = _GatewayHostV1(fail_endpoint_role="control")

    with pytest.raises(RuntimeError, match="cannot register control"):
        HermesAgentPluginExtension().install(host)

    assert host.events == [
        "runtime_descriptor",
        "add_runtime_listener",
        "register_local_endpoint:local-gateway",
        "register_local_endpoint:observer",
        "register_local_endpoint:control",
        "close:observer",
        "close:local-gateway",
        "close:runtime-listener",
        "audit:runtime.lifecycle",
    ]
    assert host.registrations["local-gateway"].close_calls == 1
    assert host.registrations["observer"].close_calls == 1
    assert host.registrations["runtime-listener"].close_calls == 1


def test_close_retains_only_failed_child_for_retry() -> None:
    host = _GatewayHostV1(failing_registration_role="control")
    registration = HermesAgentPluginExtension().install(host)

    with pytest.raises(RuntimeError, match="cannot close control"):
        registration.close()
    host.registrations["control"]._close_error = None
    registration.close()
    registration.close()

    assert host.registrations["control"].close_calls == 2
    assert host.registrations["observer"].close_calls == 1
    assert host.registrations["local-gateway"].close_calls == 1
    assert host.registrations["runtime-listener"].close_calls == 1
    assert host.events[-5:] == [
        "close:observer",
        "close:local-gateway",
        "close:runtime-listener",
        "close:control",
        "audit:runtime.lifecycle",
    ]


def test_install_rejects_missing_registration_and_rolls_back_prior_resources() -> None:
    host = _GatewayHostV1(none_registration_role="observer")

    with pytest.raises(TypeError, match="Registration"):
        HermesAgentPluginExtension().install(host)

    assert host.events == [
        "runtime_descriptor",
        "add_runtime_listener",
        "register_local_endpoint:local-gateway",
        "register_local_endpoint:observer",
        "close:local-gateway",
        "close:runtime-listener",
        "audit:runtime.lifecycle",
    ]
    assert host.registrations["local-gateway"].close_calls == 1
    assert host.registrations["runtime-listener"].close_calls == 1


def test_observer_and_snapshot_adapters_use_exact_runtime_scope() -> None:
    host = _GatewayHostV1()
    registration = HermesAgentPluginExtension().install(host)
    observer = host.endpoints["observer"]
    control = host.endpoints["control"]
    sink = object()
    try:
        prepared = observer.prepare_observer(
            {
                "profile": "default",
                "session_key": "durable-root-1",
                "runtime_generation": "runtime-generation-1",
            },
            sink,
        )
        assert prepared.snapshot.durable_session_key == "durable-root-1"
        assert prepared.snapshot.event_sequence == 7
        assert prepared.activation_deadline_monotonic == 123.0
        subscription = prepared.activate()
        snapshot = control.read_control_snapshot(
            {
                "profile": "default",
                "session_key": "durable-root-1",
                "runtime_generation": "runtime-generation-1",
            }
        )
    finally:
        registration.close()

    observer_request = host.observer_requests[0]
    assert observer_request.profile == "default"
    assert observer_request.durable_session_key == "durable-root-1"
    assert observer_request.runtime_generation == "runtime-generation-1"
    assert host.observer_sink is not sink
    scope = host.snapshot_scopes[0]
    assert scope.profile == "default"
    assert scope.durable_session_key == "durable-root-1"
    assert scope.runtime_generation == "runtime-generation-1"
    assert snapshot.control_revision == 7
    subscription.close()


def test_prepared_observer_can_close_before_activation() -> None:
    host = _GatewayHostV1()
    registration = HermesAgentPluginExtension().install(host)
    observer = host.endpoints["observer"]
    try:
        prepared = observer.prepare_observer(
            {
                "profile": "default",
                "session_key": "durable-root-1",
                "runtime_generation": "runtime-generation-1",
            },
            object(),
        )
        prepared.close()
    finally:
        registration.close()

    assert "prepare_observer" in host.events
    assert "activate_observer" not in host.events
    assert "close_prepared_observer" in host.events


def test_owner_action_carries_exact_host_identity_and_keeps_client_id_separate() -> (
    None
):
    host = _GatewayHostV1()
    registration = HermesAgentPluginExtension().install(host)
    endpoint = host.endpoints["control"]
    transport = _ControlTransport()
    try:
        lease_id = _acquire_lease(endpoint, transport)
        response = endpoint.handle_control_request(
            _rpc(
                request_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                method="prompt.submit",
                params=_mutation_params(lease_id),
            ),
            transport,
        )
    finally:
        registration.close()

    assert response["result"] == {
        "status": "accepted",
        "client_request_id": "mobile-request-1",
        "server_turn_id": "server-turn-1",
    }
    request = host.owner_requests[0]
    assert request.profile == "default"
    assert request.durable_session_key == "durable-root-1"
    assert request.runtime_generation == "runtime-generation-1"
    assert request.command_id == "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    assert request.command_id != response["result"]["client_request_id"]
    assert request.method == "prompt.submit"
    assert dict(request.payload) == {
        "runtime_session_id": "runtime-session-1",
        "client_turn_id": "client-turn-1",
        "text": "Continue the current task.",
    }


@pytest.mark.parametrize(
    ("request_id", "runtime_generation"),
    (
        (None, "runtime-generation-1"),
        (" command-1", "runtime-generation-1"),
        ("command-1", "runtime-generation-stale"),
    ),
)
def test_owner_action_rejects_invalid_command_or_runtime_identity_before_host_call(
    request_id: object,
    runtime_generation: str,
) -> None:
    host = _GatewayHostV1()
    registration = HermesAgentPluginExtension().install(host)
    endpoint = host.endpoints["control"]
    transport = _ControlTransport()
    try:
        lease_id = _acquire_lease(endpoint, transport)
        params = _mutation_params(lease_id)
        params["runtime_generation"] = runtime_generation
        response = endpoint.handle_control_request(
            _rpc(
                request_id=request_id,
                method="prompt.submit",
                params=params,
            ),
            transport,
        )
    finally:
        registration.close()

    expected_code = 4212 if runtime_generation == "runtime-generation-stale" else -32602
    assert response["error"]["code"] == expected_code
    assert host.owner_requests == []


def test_effect_unknown_maps_to_control_v1_unknown_with_original_client_id() -> None:
    host = _GatewayHostV1(
        owner_status="effect_unknown",
        owner_payload={"client_request_id": "host-must-not-replace-client-id"},
    )
    registration = HermesAgentPluginExtension().install(host)
    endpoint = host.endpoints["control"]
    transport = _ControlTransport()
    try:
        lease_id = _acquire_lease(endpoint, transport)
        response = endpoint.handle_control_request(
            _rpc(
                request_id="command-1",
                method="session.interrupt",
                params={
                    "session_key": "durable-root-1",
                    "runtime_session_id": "runtime-session-1",
                    "runtime_generation": "runtime-generation-1",
                    "lease_id": lease_id,
                    "client_request_id": "mobile-request-2",
                },
            ),
            transport,
        )
        status_responses = [
            endpoint.handle_control_request(
                _rpc(
                    request_id=f"status-{attempt}",
                    method="session.command.status",
                    params={
                        "session_key": "durable-root-1",
                        "runtime_session_id": "runtime-session-1",
                        "method": "session.interrupt",
                        "client_request_id": "mobile-request-2",
                    },
                ),
                transport,
            )
            for attempt in (1, 2)
        ]
    finally:
        registration.close()

    assert response["result"] == {
        "status": "unknown",
        "client_request_id": "mobile-request-2",
    }
    assert status_responses == [
        {
            "jsonrpc": "2.0",
            "id": "status-1",
            "error": {"code": 4210, "message": "command_unknown"},
        },
        {
            "jsonrpc": "2.0",
            "id": "status-2",
            "error": {"code": 4210, "message": "command_unknown"},
        },
    ]
    assert len(host.owner_requests) == 1


def test_owner_exception_keeps_control_client_id_on_unknown_result() -> None:
    host = _GatewayHostV1(owner_error=RuntimeError("host outcome unavailable"))
    registration = HermesAgentPluginExtension().install(host)
    endpoint = host.endpoints["control"]
    transport = _ControlTransport()
    try:
        lease_id = _acquire_lease(endpoint, transport)
        response = endpoint.handle_control_request(
            _rpc(
                request_id="command-unknown",
                method="session.interrupt",
                params={
                    "session_key": "durable-root-1",
                    "runtime_session_id": "runtime-session-1",
                    "runtime_generation": "runtime-generation-1",
                    "lease_id": lease_id,
                    "client_request_id": "mobile-request-unknown",
                },
            ),
            transport,
        )
    finally:
        registration.close()

    assert response["result"] == {
        "status": "unknown",
        "client_request_id": "mobile-request-unknown",
    }


def test_zero_valued_runtime_capability_is_not_available() -> None:
    host = _GatewayHostV1(
        runtime_capabilities={
            "session.observe": 1,
            "session.control": 1,
            "prompt.submit": 0,
            "session.interrupt": 1,
        }
    )
    registration = HermesAgentPluginExtension().install(host)
    endpoint = host.endpoints["control"]
    try:
        assert "prompt.submit" not in endpoint.available_methods
        assert "session.interrupt" in endpoint.available_methods
    finally:
        registration.close()


def test_runtime_capabilities_limit_advertised_and_executable_owner_actions() -> None:
    host = _GatewayHostV1(
        runtime_capabilities=frozenset(
            {
                "session.observe",
                "session.control",
                "session.interrupt",
            }
        )
    )
    registration = HermesAgentPluginExtension().install(host)
    endpoint = host.endpoints["control"]
    transport = _ControlTransport()
    try:
        assert endpoint.available_methods == {
            "session.control.acquire",
            "session.control.renew",
            "session.control.release",
            "session.control.status",
            "session.command.status",
            "session.interrupt",
        }
        lease_id = _acquire_lease(endpoint, transport)
        response = endpoint.handle_control_request(
            _rpc(
                request_id="command-unsupported",
                method="prompt.submit",
                params=_mutation_params(lease_id),
            ),
            transport,
        )
    finally:
        registration.close()

    assert response["error"] == {
        "code": 4209,
        "message": "method_not_allowed",
    }
    assert host.owner_requests == []


def test_runtime_listener_revokes_owner_action_before_side_effect_boundary() -> None:
    host = _GatewayHostV1()
    registration = HermesAgentPluginExtension().install(host)
    endpoint = host.endpoints["control"]
    transport = _ControlTransport()
    try:
        lease_id = _acquire_lease(endpoint, transport)
        host.runtime_listener(
            SimpleNamespace(
                profile="default",
                runtime_generation="runtime-generation-1",
                state="ready",
                capabilities=frozenset(
                    {
                        "session.observe",
                        "session.control",
                        "session.interrupt",
                    }
                ),
            )
        )
        assert "prompt.submit" not in endpoint.available_methods
        response = endpoint.handle_control_request(
            _rpc(
                request_id="command-revoked",
                method="prompt.submit",
                params=_mutation_params(lease_id),
            ),
            transport,
        )
    finally:
        registration.close()

    assert response["error"] == {
        "code": 4209,
        "message": "method_not_allowed",
    }
    assert host.owner_requests == []


def test_runtime_generation_rollover_revokes_every_old_generation_resource() -> None:
    host = _GatewayHostV1()
    registration = HermesAgentPluginExtension().install(host)
    observer = host.endpoints["observer"]
    control = host.endpoints["control"]
    transport = _ControlTransport()
    prepared_only = observer.prepare_observer(
        {
            "profile": "default",
            "session_key": "durable-root-1",
            "runtime_generation": "runtime-generation-1",
        },
        object(),
    )
    activated = observer.prepare_observer(
        {
            "profile": "default",
            "session_key": "durable-root-1",
            "runtime_generation": "runtime-generation-1",
        },
        object(),
    )
    subscription = activated.activate()
    old_lease = _acquire_lease(control, transport)

    host.runtime_listener(
        SimpleNamespace(
            profile="default",
            runtime_generation="runtime-generation-2",
            state="ready",
            capabilities=host.runtime_capabilities,
        )
    )

    assert observer.profile == "default"
    assert observer.runtime_generation == "runtime-generation-2"
    assert control.profile == "default"
    assert control.runtime_generation == "runtime-generation-2"
    assert host.observer_preparations[0].close_calls == 1
    assert host.observer_preparations[1].subscription.close_calls == 1

    stale = control.handle_control_request(
        _rpc(
            request_id="stale-command",
            method="prompt.submit",
            params=_mutation_params(old_lease),
        ),
        transport,
    )
    stale_renewal = control.handle_control_request(
        _rpc(
            request_id="stale-renewal",
            method="session.control.renew",
            params={
                "session_key": "durable-root-1",
                "runtime_generation": "runtime-generation-1",
                "lease_id": old_lease,
            },
        ),
        transport,
    )
    replacement = control.handle_control_request(
        _rpc(
            request_id="replacement-acquire",
            method="session.control.acquire",
            params={
                "session_key": "durable-root-1",
                "runtime_session_id": "runtime-session-2",
                "runtime_generation": "runtime-generation-2",
            },
        ),
        transport,
    )

    assert stale["error"]["message"] == "session_binding_mismatch"
    assert stale_renewal["error"]["message"] == "session_binding_mismatch"
    assert replacement["result"]["lease_id"] != old_lease
    with pytest.raises(RuntimeError, match="runtime generation changed"):
        prepared_only.activate()

    prepared_only.close()
    subscription.close()
    registration.close()

    assert host.observer_preparations[0].close_calls == 1
    assert host.observer_preparations[1].subscription.close_calls == 1


def test_rollover_retains_failed_observer_cleanup_for_retry() -> None:
    host = _GatewayHostV1()
    registration = HermesAgentPluginExtension().install(host)
    observer = host.endpoints["observer"]
    control = host.endpoints["control"]
    prepared = observer.prepare_observer(
        {
            "profile": "default",
            "session_key": "durable-root-1",
            "runtime_generation": "runtime-generation-1",
        },
        object(),
    )
    active = observer.prepare_observer(
        {
            "profile": "default",
            "session_key": "durable-root-1",
            "runtime_generation": "runtime-generation-1",
        },
        object(),
    ).activate()
    raw_prepared = host.observer_preparations[0]
    raw_subscription = host.observer_preparations[1].subscription
    assert raw_subscription is not None
    raw_prepared.close_error = RuntimeError("prepared close failed")
    raw_subscription._close_error = RuntimeError("subscription close failed")
    descriptor = SimpleNamespace(
        profile="default",
        runtime_generation="runtime-generation-2",
        state="ready",
        capabilities=host.runtime_capabilities,
    )

    with pytest.raises(RuntimeError, match="prepared close failed"):
        host.runtime_listener(descriptor)

    stale = control.handle_control_request(
        _rpc(
            request_id="stale-acquire-after-failed-cleanup",
            method="session.control.acquire",
            params={
                "session_key": "durable-root-1",
                "runtime_generation": "runtime-generation-1",
            },
        ),
        _ControlTransport(),
    )
    assert stale["error"]["message"] == "session_binding_mismatch"
    with pytest.raises(RuntimeError, match="runtime generation changed"):
        prepared.activate()
    assert raw_prepared.close_calls == 1
    assert raw_subscription.close_calls == 1

    raw_prepared.close_error = None
    raw_subscription._close_error = None
    host.runtime_listener(descriptor)
    prepared.close()
    active.close()

    assert raw_prepared.close_calls == 2
    assert raw_subscription.close_calls == 2
    registration.close()
    assert raw_prepared.close_calls == 2
    assert raw_subscription.close_calls == 2


def test_same_descriptor_retries_failed_endpoint_close_and_rebuilds() -> None:
    host = _GatewayHostV1(failing_registration_role="observer")
    extension_registration = HermesAgentPluginExtension().install(host)
    old_endpoint_registration = host.registrations["observer"]
    descriptor = SimpleNamespace(
        profile="default",
        runtime_generation="runtime-generation-2",
        state="ready",
        capabilities=host.runtime_capabilities,
    )

    with pytest.raises(RuntimeError, match="cannot close observer"):
        host.runtime_listener(descriptor)

    old_endpoint_registration._close_error = None
    host.failing_registration_role = None
    host.runtime_listener(descriptor)

    assert old_endpoint_registration.close_calls == 2
    assert host.events.count("register_local_endpoint:observer") == 2
    assert host.registrations["observer"] is not old_endpoint_registration
    extension_registration.close()


class _RecordingObserverSink:
    def __init__(self) -> None:
        self.deliveries: list[tuple[str, object]] = []

    def on_snapshot(self, snapshot: object) -> None:
        self.deliveries.append(("snapshot", snapshot))

    def on_event(self, event: object) -> None:
        self.deliveries.append(("event", event))


def test_rollover_does_not_deadlock_when_sink_closes_its_registration() -> None:
    host = _GatewayHostV1()
    extension_registration = HermesAgentPluginExtension().install(host)
    observer = host.endpoints["observer"]
    sink_entered = threading.Event()
    allow_self_close = threading.Event()
    self_close_returned = threading.Event()
    revoke_entered = threading.Event()
    subscription_holder: dict[str, object] = {}
    delivery_errors: list[BaseException] = []
    rollover_errors: list[BaseException] = []

    class SelfClosingSink:
        def on_event(self, _event: object) -> None:
            sink_entered.set()
            if not allow_self_close.wait(timeout=1):
                raise AssertionError("test did not release the sink callback")
            subscription_holder["subscription"].close()
            self_close_returned.set()

    subscription = observer.prepare_observer(
        {
            "profile": "default",
            "session_key": "durable-root-1",
            "runtime_generation": "runtime-generation-1",
        },
        SelfClosingSink(),
    ).activate()
    subscription_holder["subscription"] = subscription
    delivery_gate = host.observer_sinks[0]
    if hasattr(delivery_gate, "_mark_revoked"):
        raw_mark_revoked = delivery_gate._mark_revoked

        def signalled_mark_revoked() -> None:
            revoke_entered.set()
            raw_mark_revoked()

        delivery_gate._mark_revoked = signalled_mark_revoked
    else:
        raw_revoke = delivery_gate.revoke

        def signalled_revoke() -> None:
            revoke_entered.set()
            raw_revoke()

        delivery_gate.revoke = signalled_revoke

    def deliver() -> None:
        try:
            delivery_gate.on_event({"sequence": 8})
        except BaseException as error:  # noqa: BLE001
            delivery_errors.append(error)

    descriptor = SimpleNamespace(
        profile="default",
        runtime_generation="runtime-generation-2",
        state="ready",
        capabilities=host.runtime_capabilities,
    )

    def rollover() -> None:
        try:
            host.runtime_listener(descriptor)
        except BaseException as error:  # noqa: BLE001
            rollover_errors.append(error)

    delivery_thread = threading.Thread(target=deliver, daemon=True)
    rollover_thread = threading.Thread(target=rollover, daemon=True)
    delivery_thread.start()
    assert sink_entered.wait(timeout=1)
    rollover_thread.start()
    assert revoke_entered.wait(timeout=1)
    allow_self_close.set()
    delivery_thread.join(timeout=1)
    rollover_thread.join(timeout=1)

    assert not delivery_thread.is_alive(), "sink self-close deadlocked"
    assert not rollover_thread.is_alive(), "runtime rollover deadlocked"
    assert self_close_returned.is_set()
    assert delivery_errors == []
    assert rollover_errors == []
    extension_registration.close()


def test_parallel_sink_callbacks_can_both_self_close_without_deadlock() -> None:
    host = _GatewayHostV1()
    extension_registration = HermesAgentPluginExtension().install(host)
    observer = host.endpoints["observer"]
    callbacks_entered = threading.Barrier(2)
    subscription_holder: dict[str, object] = {}
    delivery_errors: list[BaseException] = []

    class SelfClosingSink:
        def on_event(self, _event: object) -> None:
            callbacks_entered.wait(timeout=1)
            subscription_holder["subscription"].close()

    subscription = observer.prepare_observer(
        {
            "profile": "default",
            "session_key": "durable-root-1",
            "runtime_generation": "runtime-generation-1",
        },
        SelfClosingSink(),
    ).activate()
    subscription_holder["subscription"] = subscription
    delivery_gate = host.observer_sinks[0]

    def deliver(sequence: int) -> None:
        try:
            delivery_gate.on_event({"sequence": sequence})
        except BaseException as error:  # noqa: BLE001
            delivery_errors.append(error)

    delivery_threads = tuple(
        threading.Thread(target=deliver, args=(sequence,), daemon=True)
        for sequence in (8, 9)
    )
    for thread in delivery_threads:
        thread.start()
    for thread in delivery_threads:
        thread.join(timeout=1)

    assert all(not thread.is_alive() for thread in delivery_threads)
    assert delivery_errors == []
    extension_registration.close()


def test_rollover_revokes_external_delivery_before_failed_raw_close() -> None:
    host = _GatewayHostV1()
    registration = HermesAgentPluginExtension().install(host)
    observer = host.endpoints["observer"]
    failing_sink = _RecordingObserverSink()
    successful_sink = _RecordingObserverSink()
    failing = observer.prepare_observer(
        {
            "profile": "default",
            "session_key": "durable-root-1",
            "runtime_generation": "runtime-generation-1",
        },
        failing_sink,
    ).activate()
    successful = observer.prepare_observer(
        {
            "profile": "default",
            "session_key": "durable-root-1",
            "runtime_generation": "runtime-generation-1",
        },
        successful_sink,
    ).activate()
    raw_failing = host.observer_preparations[0].subscription
    raw_successful = host.observer_preparations[1].subscription
    assert raw_failing is not None
    assert raw_successful is not None
    host.observer_sinks[0].on_snapshot({"generation": 1})
    host.observer_sinks[0].on_event({"sequence": 7})
    assert failing_sink.deliveries == [
        ("snapshot", {"generation": 1}),
        ("event", {"sequence": 7}),
    ]
    failing_sink.deliveries.clear()
    raw_failing._close_error = RuntimeError("subscription close failed")
    descriptor = SimpleNamespace(
        profile="default",
        runtime_generation="runtime-generation-2",
        state="ready",
        capabilities=host.runtime_capabilities,
    )

    with pytest.raises(RuntimeError, match="subscription close failed"):
        host.runtime_listener(descriptor)

    host.observer_sinks[0].on_snapshot({"generation": 1})
    host.observer_sinks[0].on_event({"sequence": 8})
    assert failing_sink.deliveries == []
    with pytest.raises(SessionBindingMismatch, match="session binding mismatch"):
        observer.prepare_observer(
            {
                "profile": "default",
                "session_key": "durable-root-1",
                "runtime_generation": "runtime-generation-1",
            },
            _RecordingObserverSink(),
        )
    assert raw_failing.close_calls == 1
    assert raw_successful.close_calls == 1

    raw_failing._close_error = None
    host.runtime_listener(descriptor)
    failing.close()
    successful.close()
    registration.close()

    assert raw_failing.close_calls == 2
    assert raw_successful.close_calls == 1
