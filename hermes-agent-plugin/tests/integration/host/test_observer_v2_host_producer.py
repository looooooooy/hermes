"""Future Host double integration for observer output-parity v2."""

from __future__ import annotations

from functools import partial
from types import SimpleNamespace

import pytest
from tests.test_support.host_spi_v1 import TEST_HOST_SPI_FACTORIES

from hermes_agent_plugin.adapters.host.extension import (
    HermesAgentPluginExtension as _HermesAgentPluginExtension,
)
from hermes_agent_plugin.adapters.host.observer_v2 import (
    OUTPUT_PARITY_CAPABILITY,
    ObserverV2Violation,
)

HermesAgentPluginExtension = partial(
    _HermesAgentPluginExtension,
    host_spi_factories=TEST_HOST_SPI_FACTORIES,
)


class _Registration:
    def __init__(self) -> None:
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1


class _Prepared:
    activation_deadline_monotonic = 100.0

    def __init__(self, snapshot: dict[str, object]) -> None:
        self.snapshot = snapshot
        self.close_calls = 0
        self.subscription = _Registration()

    def activate(self) -> _Registration:
        return self.subscription

    def close(self) -> None:
        self.close_calls += 1


class _FutureOutputParityHost:
    host_api_version = 1

    def __init__(self) -> None:
        self.endpoints: dict[str, object] = {}
        self.requests: list[object] = []
        self.sinks: list[object] = []
        self.prepared: list[_Prepared] = []
        self.generation = "generation-1"

    def runtime_descriptor(self) -> object:
        return SimpleNamespace(
            profile="default",
            runtime_generation=self.generation,
            state="ready",
            capabilities=frozenset(
                {"session.observe", "session.control", OUTPUT_PARITY_CAPABILITY}
            ),
        )

    def add_runtime_listener(self, listener: object) -> _Registration:
        self.listener = listener
        return _Registration()

    def register_local_endpoint(self, endpoint: object) -> _Registration:
        self.endpoints[endpoint.connection_role] = endpoint
        return _Registration()

    def prepare_observer(self, request: object, sink: object) -> _Prepared:
        self.requests.append(request)
        self.sinks.append(sink)
        prepared = _Prepared(_snapshot())
        self.prepared.append(prepared)
        return prepared

    def control_snapshot(self, _scope: object) -> object:
        return SimpleNamespace(control_revision=0)

    def invoke_owner_action(self, _request: object) -> object:
        return SimpleNamespace(status="accepted", payload={})

    def audit(self, _event: object) -> None:
        return None

    def rollover(self) -> None:
        self.generation = "generation-2"
        self.listener(self.runtime_descriptor())


class _Sink:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    def on_event(self, event: dict[str, object]) -> None:
        self.events.append(event)


def _event(sequence: int, revision: int) -> dict[str, object]:
    return {
        "observer_contract": 2,
        "profile": "default",
        "runtime_generation": "generation-1",
        "session_key": "durable-1",
        "session_id": "runtime-1",
        "type": "tool.update",
        "event_sequence": sequence,
        "payload": {
            "turn_id": "turn-1",
            "tool_call_id": "tool-1",
            "revision": revision,
            "first_event_sequence": 1,
            "operation": "upsert",
            "status": "running",
            "name": "shell",
        },
    }


def _snapshot() -> dict[str, object]:
    return {
        "observer_contract": 2,
        "profile": "default",
        "runtime_generation": "generation-1",
        "session_key": "durable-1",
        "runtime_session_id": "runtime-1",
        "running": True,
        "status": "running",
        "event_sequence": 1,
        "snapshot_event_sequence": 0,
        "messages": [],
        "inflight": {
            "user": None,
            "assistant": None,
            "streaming": False,
            "error": None,
        },
        "todo_sections": [],
        "subagents": [],
        "tools": [],
        "terminals": [],
        "replay_events": [_event(1, 1)],
    }


def test_host_double_snapshot_replay_live_invalid_rollover_and_close() -> None:
    host = _FutureOutputParityHost()
    extension_registration = HermesAgentPluginExtension().install(host)
    sink = _Sink()
    prepared = host.endpoints["observer"].prepare_observer(
        {
            "observer_contract": 2,
            "profile": "default",
            "session_key": "durable-1",
            "runtime_generation": "generation-1",
        },
        sink,
    )
    assert prepared.snapshot["replay_events"][0]["event_sequence"] == 1
    subscription = prepared.activate()

    host.sinks[0].on_event(_event(2, 2))
    assert [event["event_sequence"] for event in sink.events] == [2]
    with pytest.raises(ObserverV2Violation, match="contiguous"):
        host.sinks[0].on_event(_event(4, 3))
    host.sinks[0].on_event(_event(3, 3))
    assert [event["event_sequence"] for event in sink.events] == [2]

    host.rollover()
    subscription.close()
    extension_registration.close()

    assert host.requests[0].observer_contract == 2
    assert host.prepared[0].subscription.close_calls == 1


def test_host_rejects_unsafe_extension_before_relay_delivery() -> None:
    host = _FutureOutputParityHost()
    extension_registration = HermesAgentPluginExtension().install(host)
    sink = _Sink()
    prepared = host.endpoints["observer"].prepare_observer(
        {
            "observer_contract": 2,
            "profile": "default",
            "session_key": "durable-1",
            "runtime_generation": "generation-1",
        },
        sink,
    )
    subscription = prepared.activate()
    unsafe = _event(2, 2)
    unsafe["extensions"] = {
        "vendor.private": {
            "tool_args": "super-sensitive-value",
        }
    }

    with pytest.raises(ObserverV2Violation) as raised:
        host.sinks[0].on_event(unsafe)

    assert "super-sensitive-value" not in str(raised.value)
    assert sink.events == []
    subscription.close()
    extension_registration.close()


def test_host_redacts_basic_authorization_before_relay_delivery() -> None:
    host = _FutureOutputParityHost()
    extension_registration = HermesAgentPluginExtension().install(host)
    sink = _Sink()
    prepared = host.endpoints["observer"].prepare_observer(
        {
            "observer_contract": 2,
            "profile": "default",
            "session_key": "durable-1",
            "runtime_generation": "generation-1",
        },
        sink,
    )
    subscription = prepared.activate()
    event = _event(2, 2)
    event["payload"]["summary"] = (
        "Authorization: Basic dXNlcjpwYXNzd29yZA=="
    )

    host.sinks[0].on_event(event)

    assert sink.events[0]["payload"]["summary"] == (
        "Authorization: Basic [REDACTED]"
    )
    assert "dXNlcjpwYXNzd29yZA" not in str(sink.events)
    subscription.close()
    extension_registration.close()


@pytest.mark.parametrize(
    "safe_text",
    (
        "Basic authentication is disabled.",
        "Basic YWJjZA== is not a user-password credential.",
    ),
)
def test_host_preserves_noncredential_basic_text_before_relay_delivery(
    safe_text: str,
) -> None:
    host = _FutureOutputParityHost()
    extension_registration = HermesAgentPluginExtension().install(host)
    sink = _Sink()
    prepared = host.endpoints["observer"].prepare_observer(
        {
            "observer_contract": 2,
            "profile": "default",
            "session_key": "durable-1",
            "runtime_generation": "generation-1",
        },
        sink,
    )
    subscription = prepared.activate()
    event = _event(2, 2)
    event["payload"]["summary"] = safe_text

    host.sinks[0].on_event(event)

    assert sink.events[0]["payload"]["summary"] == safe_text
    subscription.close()
    extension_registration.close()
