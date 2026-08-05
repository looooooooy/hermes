"""Observer output-parity v2 Host producer contract."""

from __future__ import annotations

import copy
from functools import partial
from types import SimpleNamespace

import pytest
from tests.test_support.host_spi_v1 import TEST_HOST_SPI_FACTORIES

from hermes_agent_plugin.adapters.host.extension import (
    HermesAgentPluginExtension as _HermesAgentPluginExtension,
)
from hermes_agent_plugin.adapters.host.observer_v2 import (
    OUTPUT_PARITY_CAPABILITY,
    ObserverV2Projection,
    ObserverV2Violation,
    load_observer_v2_bundle,
)
from hermes_agent_plugin.adapters.local_protocol.handshake_v1 import (
    LocalContractV1Adapter,
    LocalHello,
    decode_local_welcome,
    encode_local_hello,
)
from hermes_agent_plugin.ports import local_relay as local_relay_port

HermesAgentPluginExtension = partial(
    _HermesAgentPluginExtension,
    host_spi_factories=TEST_HOST_SPI_FACTORIES,
)


def _snapshot() -> dict[str, object]:
    return {
        "observer_contract": 2,
        "profile": "default",
        "runtime_generation": "generation-1",
        "session_key": "durable-1",
        "runtime_session_id": "runtime-1",
        "running": True,
        "status": "running",
        "event_sequence": 0,
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
        "replay_events": [],
    }


def _event(
    sequence: int,
    event_type: str,
    payload: dict[str, object],
) -> dict[str, object]:
    return {
        "observer_contract": 2,
        "profile": "default",
        "runtime_generation": "generation-1",
        "session_key": "durable-1",
        "session_id": "runtime-1",
        "type": event_type,
        "event_sequence": sequence,
        "payload": payload,
    }


def _todo_payload(
    *, revision: int, status: str = "in_progress"
) -> dict[str, object]:
    return {
        "turn_id": "turn-1",
        "section_id": "section-1",
        "revision": revision,
        "first_event_sequence": 1,
        "operation": "upsert",
        "status": status,
        "items": [
            {"id": "todo-1", "label": "Ship safely", "status": status}
        ],
    }


class _Registration:
    def __init__(self) -> None:
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1


class _Prepared:
    activation_deadline_monotonic = 100.0

    def __init__(self, snapshot: object) -> None:
        self.snapshot = snapshot
        self.close_calls = 0
        self.activate_calls = 0
        self.registration = _Registration()

    def activate(self) -> _Registration:
        self.activate_calls += 1
        return self.registration

    def close(self) -> None:
        self.close_calls += 1


class _Host:
    host_api_version = 1

    def __init__(self, *, snapshot: object, output_parity: bool) -> None:
        self.snapshot = snapshot
        self.output_parity = output_parity
        self.endpoints: dict[str, object] = {}
        self.prepared: list[_Prepared] = []
        self.observer_requests: list[object] = []
        self.observer_sinks: list[object] = []
        self.listener = None

    @property
    def capabilities(self) -> frozenset[str]:
        capabilities = {"session.observe", "session.control"}
        if self.output_parity:
            capabilities.add(OUTPUT_PARITY_CAPABILITY)
        return frozenset(capabilities)

    def runtime_descriptor(self) -> object:
        return SimpleNamespace(
            profile="default",
            runtime_generation="generation-1",
            state="ready",
            capabilities=self.capabilities,
        )

    def add_runtime_listener(self, listener: object) -> _Registration:
        self.listener = listener
        return _Registration()

    def register_local_endpoint(self, endpoint: object) -> _Registration:
        self.endpoints[endpoint.connection_role] = endpoint
        return _Registration()

    def prepare_observer(self, request: object, sink: object) -> _Prepared:
        self.observer_requests.append(request)
        self.observer_sinks.append(sink)
        prepared = _Prepared(copy.deepcopy(self.snapshot))
        self.prepared.append(prepared)
        return prepared

    def control_snapshot(self, _scope: object) -> object:
        return SimpleNamespace(control_revision=0)

    def invoke_owner_action(self, _request: object) -> object:
        return SimpleNamespace(status="accepted", payload={})

    def audit(self, _event: object) -> None:
        return None


class _Sink:
    def __init__(self) -> None:
        self.events: list[object] = []

    def on_event(self, event: object) -> None:
        self.events.append(event)


def _prepare_v2(endpoint: object, sink: object) -> object:
    return endpoint.prepare_observer(
        {
            "observer_contract": 2,
            "profile": "default",
            "session_key": "durable-1",
            "runtime_generation": "generation-1",
        },
        sink,
    )


def test_bundle_loads_policy_and_both_exact_v2_schemas() -> None:
    bundle = load_observer_v2_bundle()

    assert bundle.capability == OUTPUT_PARITY_CAPABILITY
    assert bundle.snapshot_schema["properties"]["observer_contract"] == {"const": 2}
    assert bundle.event_schema["properties"]["observer_contract"] == {"const": 2}
    assert bundle.snapshot_schema["properties"]["replay_events"]["items"] == {
        "$ref": "session-event-v2.schema.json"
    }


def test_descriptor_and_handshake_advertise_v2_only_when_host_explicitly_does() -> (
    None
):
    capable = _Host(snapshot=_snapshot(), output_parity=True)
    incapable = _Host(snapshot=_snapshot(), output_parity=False)
    capable_registration = HermesAgentPluginExtension().install(capable)
    incapable_registration = HermesAgentPluginExtension().install(incapable)
    try:
        capable_endpoint = capable.endpoints["observer"]
        incapable_endpoint = incapable.endpoints["observer"]
        assert capable_endpoint.contract_versions == frozenset({1, 2})
        assert OUTPUT_PARITY_CAPABILITY in capable_endpoint.available_capabilities
        assert incapable_endpoint.contract_versions == frozenset({1})
        assert OUTPUT_PARITY_CAPABILITY not in incapable_endpoint.available_capabilities
        hello = encode_local_hello(
            LocalHello(
                contract_version=1,
                message_type="local.hello",
                client_instance_id="11111111-1111-4111-8111-111111111111",
                profile="default",
                required_capabilities=("session.observe",),
                optional_capabilities=(OUTPUT_PARITY_CAPABILITY,),
                extensions={},
            )
        )
        capable_welcome = decode_local_welcome(
            LocalContractV1Adapter(
                runtime_generation="generation-1",
                available_capabilities=capable_endpoint.available_capabilities,
            ).handle_hello(hello)
        )
        incapable_welcome = decode_local_welcome(
            LocalContractV1Adapter(
                runtime_generation="generation-1",
                available_capabilities=incapable_endpoint.available_capabilities,
            ).handle_hello(hello)
        )
        assert OUTPUT_PARITY_CAPABILITY in capable_welcome.accepted_capabilities
        assert (
            OUTPUT_PARITY_CAPABILITY
            in incapable_welcome.unavailable_optional_capabilities
        )
        with pytest.raises(RuntimeError, match="output parity v2 is unavailable"):
            _prepare_v2(incapable_endpoint, _Sink())
        assert incapable.observer_requests == []
    finally:
        capable_registration.close()
        incapable_registration.close()


def test_resource_drift_fails_closed_before_endpoint_registration(monkeypatch) -> None:
    import hermes_agent_plugin.adapters.host.extension as extension_module

    host = _Host(snapshot=_snapshot(), output_parity=True)

    def drifted_bundle() -> object:
        raise ObserverV2Violation("observer v2 contract resources drifted")

    monkeypatch.setattr(extension_module, "load_observer_v2_bundle", drifted_bundle)

    with pytest.raises(RuntimeError, match="output parity v2 contract is unavailable"):
        HermesAgentPluginExtension().install(host)

    assert host.endpoints == {}
    assert host.observer_requests == []


def test_output_parity_capability_version_mismatch_fails_closed() -> None:
    class VersionMismatchHost(_Host):
        def runtime_descriptor(self) -> object:
            return SimpleNamespace(
                profile="default",
                runtime_generation="generation-1",
                state="ready",
                capabilities={
                    "session.observe": 1,
                    "session.control": 1,
                    OUTPUT_PARITY_CAPABILITY: 2,
                },
            )

    host = VersionMismatchHost(snapshot=_snapshot(), output_parity=True)

    with pytest.raises(RuntimeError, match="output parity capability version"):
        HermesAgentPluginExtension().install(host)

    assert host.endpoints == {}


def test_capability_changes_and_runtime_rollover_rebuild_observer_endpoint() -> None:
    class ContractRecordingHost(_Host):
        def __init__(self) -> None:
            super().__init__(snapshot=_snapshot(), output_parity=True)
            self.observer_endpoint_registrations: list[tuple[int, _Registration]] = []

        def register_local_endpoint(self, endpoint: object) -> _Registration:
            registration = _Registration()
            if endpoint.connection_role == "observer":
                self.observer_endpoint_registrations.append(
                    (
                        local_relay_port.current_observer_endpoint_contract(),
                        registration,
                    )
                )
            self.endpoints[endpoint.connection_role] = endpoint
            return registration

    host = ContractRecordingHost()
    registration = HermesAgentPluginExtension().install(host)

    assert [item[0] for item in host.observer_endpoint_registrations] == [2]
    assert local_relay_port.current_observer_endpoint_contract() == 1

    host.output_parity = False
    host.listener(host.runtime_descriptor())
    assert [item[0] for item in host.observer_endpoint_registrations] == [2, 1]
    assert host.observer_endpoint_registrations[0][1].close_calls == 1

    host.output_parity = True
    host.listener(host.runtime_descriptor())
    assert [item[0] for item in host.observer_endpoint_registrations] == [2, 1, 2]
    assert host.observer_endpoint_registrations[1][1].close_calls == 1

    host.listener(
        SimpleNamespace(
            profile="default",
            runtime_generation="generation-2",
            state="ready",
            capabilities=host.capabilities,
        )
    )
    assert [item[0] for item in host.observer_endpoint_registrations] == [2, 1, 2, 2]
    assert host.observer_endpoint_registrations[2][1].close_calls == 1

    registration.close()
    assert host.observer_endpoint_registrations[3][1].close_calls == 1


def test_v2_snapshot_replay_live_unsubscribe_and_rollover_are_one_sequence() -> None:
    snapshot = _snapshot()
    snapshot["event_sequence"] = 1
    snapshot["replay_events"] = [
        _event(1, "todo.update", _todo_payload(revision=1))
    ]
    host = _Host(snapshot=snapshot, output_parity=True)
    registration = HermesAgentPluginExtension().install(host)
    sink = _Sink()
    prepared = _prepare_v2(host.endpoints["observer"], sink)
    try:
        assert prepared.snapshot == snapshot
        request = host.observer_requests[0]
        assert request.observer_contract == 2
        assert request.required_capabilities == frozenset(
            {OUTPUT_PARITY_CAPABILITY}
        )
        subscription = prepared.activate()
        host.observer_sinks[0].on_event(
            _event(2, "todo.update", _todo_payload(revision=2, status="completed"))
        )
        assert [event["event_sequence"] for event in sink.events] == [2]

        host.listener(
            SimpleNamespace(
                profile="default",
                runtime_generation="generation-2",
                state="ready",
                capabilities=host.capabilities,
            )
        )
        host.observer_sinks[0].on_event(
            _event(3, "todo.update", _todo_payload(revision=3, status="completed"))
        )
        assert [event["event_sequence"] for event in sink.events] == [2]
        assert host.prepared[0].registration.close_calls == 1
        subscription.close()
    finally:
        registration.close()


def test_v2_host_adapter_stamps_contract_and_empty_collections_before_schema_gate() -> (
    None
):
    snapshot = _snapshot()
    snapshot.pop("observer_contract")
    for collection in ("todo_sections", "subagents", "tools", "terminals"):
        snapshot.pop(collection)
    host = _Host(snapshot=snapshot, output_parity=True)
    registration = HermesAgentPluginExtension().install(host)
    sink = _Sink()

    prepared = _prepare_v2(host.endpoints["observer"], sink)
    assert prepared.snapshot["observer_contract"] == 2
    assert prepared.snapshot["todo_sections"] == []
    assert prepared.snapshot["subagents"] == []
    assert prepared.snapshot["tools"] == []
    assert prepared.snapshot["terminals"] == []

    subscription = prepared.activate()
    event = _event(1, "status.update", {"status": "running", "running": True})
    event.pop("observer_contract")
    host.observer_sinks[0].on_event(event)

    assert sink.events[0]["observer_contract"] == 2
    subscription.close()
    registration.close()


def test_v2_host_adapter_exposes_a_callable_schema_gated_sink() -> None:
    class CallableSinkHost(_Host):
        def prepare_observer(self, request: object, sink: object) -> _Prepared:
            if not callable(sink):
                raise ValueError("observer sink must be callable")
            return super().prepare_observer(request, sink)

    host = CallableSinkHost(snapshot=_snapshot(), output_parity=True)
    registration = HermesAgentPluginExtension().install(host)

    prepared = _prepare_v2(host.endpoints["observer"], _Sink())

    prepared.close()
    registration.close()


def test_midstream_host_capability_loss_closes_v2_before_renegotiation() -> None:
    host = _Host(snapshot=_snapshot(), output_parity=True)
    registration = HermesAgentPluginExtension().install(host)
    prepared = _prepare_v2(host.endpoints["observer"], _Sink())
    subscription = prepared.activate()

    host.output_parity = False
    host.listener(host.runtime_descriptor())

    assert host.endpoints["observer"].contract_versions == frozenset({1})
    assert host.prepared[0].registration.close_calls == 1
    subscription.close()
    registration.close()


def test_v2_rejects_delivery_before_activate_and_drops_after_unsubscribe() -> None:
    early_host = _Host(snapshot=_snapshot(), output_parity=True)
    early_registration = HermesAgentPluginExtension().install(early_host)
    early_sink = _Sink()
    early_prepared = _prepare_v2(
        early_host.endpoints["observer"], early_sink
    )
    with pytest.raises(ObserverV2Violation, match="not active"):
        early_host.observer_sinks[0].on_event(
            _event(1, "status.update", {"status": "running", "running": True})
        )
    with pytest.raises(ObserverV2Violation, match="unavailable"):
        early_prepared.activate()
    assert early_host.prepared[0].activate_calls == 0
    assert early_sink.events == []
    early_prepared.close()
    early_registration.close()

    host = _Host(snapshot=_snapshot(), output_parity=True)
    registration = HermesAgentPluginExtension().install(host)
    sink = _Sink()
    subscription = _prepare_v2(host.endpoints["observer"], sink).activate()
    subscription.close()
    host.observer_sinks[0].on_event(
        _event(1, "status.update", {"status": "running", "running": True})
    )
    assert sink.events == []
    assert host.prepared[0].registration.close_calls == 1
    registration.close()


def test_invalid_snapshot_closes_prepared_without_activation_or_delivery() -> None:
    snapshot = _snapshot()
    snapshot["event_sequence"] = 1
    snapshot["snapshot_event_sequence"] = 1
    snapshot["todo_sections"] = [
        {
            **_todo_payload(revision=1),
            "items": [
                {"id": "same", "label": "One", "status": "pending"},
                {"id": "same", "label": "Two", "status": "pending"},
            ],
        }
    ]
    snapshot["todo_sections"][0].pop("operation")
    host = _Host(snapshot=snapshot, output_parity=True)
    registration = HermesAgentPluginExtension().install(host)
    try:
        with pytest.raises(ObserverV2Violation, match="todo item ids"):
            _prepare_v2(host.endpoints["observer"], _Sink())
        assert host.prepared[0].close_calls == 1
        assert host.prepared[0].activate_calls == 0
    finally:
        registration.close()


def test_projection_rejects_gap_stale_revision_and_nonterminal_delete_atomically() -> (
    None
):
    projection = ObserverV2Projection.from_snapshot(_snapshot())
    accepted = projection.accept_event(
        _event(1, "tool.update", {
            "turn_id": "turn-1",
            "tool_call_id": "tool-1",
            "revision": 1,
            "first_event_sequence": 1,
            "operation": "upsert",
            "status": "running",
            "name": "shell",
        })
    )
    assert accepted["event_sequence"] == 1

    for invalid, reason in (
        (_event(3, "status.update", {"status": "running", "running": True}), "contiguous"),
        (_event(2, "tool.update", {
            "turn_id": "turn-1", "tool_call_id": "tool-1", "revision": 1,
            "first_event_sequence": 1, "operation": "upsert",
            "status": "running", "name": "shell",
        }), "revision"),
        (_event(2, "tool.update", {
            "turn_id": "turn-1", "tool_call_id": "tool-1", "revision": 2,
            "first_event_sequence": 1, "operation": "delete",
        }), "terminal"),
    ):
        with pytest.raises(ObserverV2Violation, match=reason):
            projection.accept_event(invalid)
        assert projection.event_sequence == 1


def test_projection_redacts_display_text_and_rejects_forbidden_raw_fields() -> None:
    projection = ObserverV2Projection.from_snapshot(_snapshot())
    projection.accept_event(
        _event(1, "tool.update", {
            "turn_id": "turn-1",
            "tool_call_id": "tool-1",
            "revision": 1,
            "first_event_sequence": 1,
            "operation": "upsert",
            "status": "running",
            "name": "shell",
        })
    )
    safe = projection.accept_event(
        _event(
            2,
            "tool.output.delta",
            {
                "turn_id": "turn-1",
                "tool_call_id": "tool-1",
                "text": "Authorization: Bearer secret-token",
            },
        )
    )
    assert safe["payload"]["text"] == "Authorization: Bearer [REDACTED]"

    unsafe = _event(3, "tool.output.delta", {
        "turn_id": "turn-1",
        "tool_call_id": "tool-1",
        "text": "safe",
        "raw_output": "secret",
    })
    with pytest.raises(ObserverV2Violation, match="forbidden Host fact"):
        projection.accept_event(unsafe)
    assert projection.event_sequence == 2


@pytest.mark.parametrize(
    "sensitive_key",
    (
        "client_secret",
        "api_token",
        "tool_args",
        "rawToolOutput",
        "approvalPayload",
        "privatePath",
        "accessKey",
    ),
)
def test_projection_rejects_compound_sensitive_extension_keys_without_echo(
    sensitive_key: str,
) -> None:
    projection = ObserverV2Projection.from_snapshot(_snapshot())
    event = _event(
        1,
        "status.update",
        {"status": "running", "running": True},
    )
    event["extensions"] = {
        "vendor.display": {sensitive_key: "super-sensitive-value"}
    }

    with pytest.raises(ObserverV2Violation) as raised:
        projection.accept_event(event)

    assert "super-sensitive-value" not in str(raised.value)
    assert projection.event_sequence == 0


@pytest.mark.parametrize(
    "credential_like_value",
    (
        "Bearer abcdefghijklmnop",
        "Basic dXNlcjpwYXNz",
        "Authorization: Basic dXNlcjpwYXNzd29yZA==",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.signature123",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.c2ln",
        "password=hunter2-credential",
        "secret: classified-value",
        "token=opaque-token-value",
        "api-key: provider-key-value",
        "sk-abcdefghijklmnopqrstuvwxyz",
        "ghp_abcdefghijklmnopqrstuvwxyz",
        "xoxb-1234567890-abcdefghijkl",
        "AKIAABCDEFGHIJKLMNOP",
        "ASIAABCDEFGHIJKLMNOP",
        "AIzaSyExampleProviderCredential123456",
        "ya29.provider-access-credential",
        "glpat-provider-access-credential",
        "hf_provider_access_credential",
    ),
)
def test_projection_rejects_credential_like_extension_values_without_echo(
    credential_like_value: str,
) -> None:
    projection = ObserverV2Projection.from_snapshot(_snapshot())
    event = _event(
        1,
        "status.update",
        {"status": "running", "running": True},
    )
    event["extensions"] = {
        "vendor.display": {"note": credential_like_value}
    }

    with pytest.raises(ObserverV2Violation) as raised:
        projection.accept_event(event)

    assert credential_like_value not in str(raised.value)
    assert projection.event_sequence == 0


@pytest.mark.parametrize(
    "display_safe_value",
    (
        "Basic authentication is disabled.",
        "Basic YWJjZA== is not a user-password credential.",
        "release 1.2.3 is available",
        "visit docs.example.com for details",
        "a.b.c",
        "aGVhZGVy.cGF5bG9hZA.c2lnbmF0dXJl",
    ),
)
def test_projection_allows_noncredential_basic_and_dotted_display_text(
    display_safe_value: str,
) -> None:
    projection = ObserverV2Projection.from_snapshot(_snapshot())
    event = _event(
        1,
        "status.update",
        {"status": "running", "running": True},
    )
    event["extensions"] = {"vendor.display": {"note": display_safe_value}}

    accepted = projection.accept_event(event)

    assert accepted["extensions"]["vendor.display"]["note"] == display_safe_value


@pytest.mark.parametrize(
    ("display_field", "credential_like_value"),
    (
        ("content", "Authorization: Basic dXNlcjpwYXNzd29yZA=="),
        ("summary", "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.c2ln"),
        ("text", "password=hunter2-credential"),
    ),
)
def test_projection_redacts_credentials_from_explicit_display_fields(
    display_field: str,
    credential_like_value: str,
) -> None:
    projection = ObserverV2Projection(load_observer_v2_bundle())
    snapshot = _snapshot()
    if display_field == "content":
        snapshot["messages"] = [
            {"role": "assistant", "content": credential_like_value}
        ]
        safe = projection.install_snapshot(snapshot)
        observed = safe["messages"][0]["content"]
    else:
        projection.install_snapshot(snapshot)
        if display_field == "summary":
            safe = projection.accept_event(
                _event(
                    1,
                    "tool.update",
                    {
                        "turn_id": "turn-1",
                        "tool_call_id": "tool-1",
                        "revision": 1,
                        "first_event_sequence": 1,
                        "operation": "upsert",
                        "status": "running",
                        "name": "shell",
                        "summary": credential_like_value,
                    },
                )
            )
        else:
            projection.accept_event(
                _event(
                    1,
                    "tool.update",
                    {
                        "turn_id": "turn-1",
                        "tool_call_id": "tool-1",
                        "revision": 1,
                        "first_event_sequence": 1,
                        "operation": "upsert",
                        "status": "running",
                        "name": "shell",
                    },
                )
            )
            safe = projection.accept_event(
                _event(
                    2,
                    "tool.output.delta",
                    {
                        "turn_id": "turn-1",
                        "tool_call_id": "tool-1",
                        "text": credential_like_value,
                    },
                )
            )
        observed = safe["payload"][display_field]

    assert "[REDACTED]" in observed
    assert credential_like_value not in str(safe)


def test_projection_allows_display_metadata_and_aggregate_token_counts() -> None:
    projection = ObserverV2Projection.from_snapshot(_snapshot())
    event = _event(
        1,
        "status.update",
        {"status": "running", "running": True},
    )
    event["extensions"] = {
        "vendor.display": {
            "status_label": "Running safely",
            "tokenizer_name": "sentence-piece",
            "privateer_label": "crew member",
            "pathology_code": "normal",
            "secretary_label": "operations",
            "access_level": "standard",
            "client_version": "1.0",
            "token_counts": {"input": 10, "output": 2, "reasoning": 1},
        }
    }

    accepted = projection.accept_event(event)

    assert accepted["extensions"] == event["extensions"]


def test_projection_rejects_control_characters_in_extensions_without_echo() -> None:
    projection = ObserverV2Projection.from_snapshot(_snapshot())
    event = _event(
        1,
        "status.update",
        {"status": "running", "running": True},
    )
    event["extensions"] = {"vendor.display": {"note": "unsafe\u0000value"}}

    with pytest.raises(ObserverV2Violation) as raised:
        projection.accept_event(event)

    assert "unsafe" not in str(raised.value)
    assert projection.event_sequence == 0


@pytest.mark.parametrize("oversized_kind", ("object", "array"))
def test_projection_rejects_generated_extension_structure_bounds(
    oversized_kind: str,
) -> None:
    projection = ObserverV2Projection.from_snapshot(_snapshot())
    event = _event(
        1,
        "status.update",
        {"status": "running", "running": True},
    )
    if oversized_kind == "object":
        extension_value: object = {f"field_{index}": index for index in range(1025)}
    else:
        extension_value = list(range(1025))
    event["extensions"] = {"vendor.display": {"metadata": extension_value}}

    with pytest.raises(ObserverV2Violation, match="bound"):
        projection.accept_event(event)

    assert projection.event_sequence == 0


@pytest.mark.parametrize("unsafe_kind", ("frame", "nesting", "non_json"))
def test_projection_enforces_global_generated_contract_bounds(
    unsafe_kind: str,
) -> None:
    snapshot = _snapshot()
    if unsafe_kind == "frame":
        snapshot["messages"] = [
            {"role": "assistant", "content": "x" * 4096} for _ in range(100)
        ]
        expected = "frame"
    elif unsafe_kind == "nesting":
        nested: dict[str, object] = {}
        cursor = nested
        for _ in range(34):
            child: dict[str, object] = {}
            cursor["child"] = child
            cursor = child
        snapshot["extensions"] = {"hermes.test": nested}
        expected = "nesting"
    else:
        snapshot["extensions"] = {"hermes.test": {"value": float("nan")}}
        expected = "canonical JSON"

    with pytest.raises(ObserverV2Violation, match=expected):
        ObserverV2Projection.from_snapshot(snapshot)


def test_todo_full_replacement_retains_order_appends_and_applies_safe_default() -> (
    None
):
    projection = ObserverV2Projection.from_snapshot(_snapshot())
    first = _todo_payload(revision=1)
    first["items"] = [
        {"id": "todo-1", "label": "One", "status": "pending"},
        {"id": "todo-2", "label": "Two", "status": "pending"},
    ]
    projection.accept_event(_event(1, "todo.update", first))
    reordered = _todo_payload(revision=2)
    reordered["items"] = list(reversed(first["items"]))
    with pytest.raises(ObserverV2Violation, match="todo order"):
        projection.accept_event(_event(2, "todo.update", reordered))

    replacement = _todo_payload(revision=2)
    replacement["items"] = [
        *first["items"],
        {"id": "todo-3", "label": "  ", "status": "pending"},
    ]
    accepted = projection.accept_event(_event(2, "todo.update", replacement))
    assert [item["id"] for item in accepted["payload"]["items"]] == [
        "todo-1",
        "todo-2",
        "todo-3",
    ]
    assert accepted["payload"]["items"][2]["label"] == "Task"


def test_subagent_graph_rejects_cycle_depth_and_nonleaf_delete() -> None:
    projection = ObserverV2Projection.from_snapshot(_snapshot())

    def subagent(
        sequence: int,
        subagent_id: str,
        parent: str | None,
        *,
        revision: int = 1,
        status: str = "running",
        operation: str = "upsert",
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "turn_id": "turn-1",
            "subagent_id": subagent_id,
            "revision": revision,
            "first_event_sequence": sequence if revision == 1 else 1,
            "operation": operation,
        }
        if operation == "upsert":
            payload.update(
                {
                    "parent_subagent_id": parent,
                    "name": "worker",
                    "goal": "finish",
                    "summary": None,
                    "status": status,
                }
            )
        return _event(sequence, "subagent.update", payload)

    projection.accept_event(subagent(1, "root", None))
    projection.accept_event(subagent(2, "child", "root"))
    cycle = subagent(3, "root", "child", revision=2)
    cycle["payload"]["first_event_sequence"] = 1
    with pytest.raises(ObserverV2Violation, match="cycle"):
        projection.accept_event(cycle)

    root_terminal = subagent(
        3, "root", None, revision=2, status="completed"
    )
    root_terminal["payload"]["first_event_sequence"] = 1
    projection.accept_event(root_terminal)
    delete_root = subagent(4, "root", None, revision=3, operation="delete")
    delete_root["payload"]["first_event_sequence"] = 1
    with pytest.raises(ObserverV2Violation, match="leaf"):
        projection.accept_event(delete_root)

    deep_snapshot = _snapshot()
    deep_snapshot["event_sequence"] = 1
    deep_snapshot["snapshot_event_sequence"] = 1
    deep_snapshot["subagents"] = [
        {
            "turn_id": "turn-1",
            "subagent_id": f"node-{index}",
            "revision": 1,
            "first_event_sequence": 1,
            "parent_subagent_id": None if index == 0 else f"node-{index - 1}",
            "name": "worker",
            "goal": "finish",
            "summary": None,
            "status": "running",
        }
        for index in range(9)
    ]
    with pytest.raises(ObserverV2Violation, match="depth 8"):
        ObserverV2Projection.from_snapshot(deep_snapshot)


def test_terminal_exit_consistency_and_terminal_delete_are_enforced() -> None:
    projection = ObserverV2Projection.from_snapshot(_snapshot())

    def terminal(
        sequence: int,
        revision: int,
        status: str | None,
        *,
        exit_code: int | None = None,
        operation: str = "upsert",
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "turn_id": "turn-1",
            "process_id": "process-1",
            "revision": revision,
            "first_event_sequence": 1,
            "operation": operation,
        }
        if status is not None:
            payload["status"] = status
        if exit_code is not None:
            payload["exit_code"] = exit_code
        return _event(sequence, "terminal.update", payload)

    projection.accept_event(terminal(1, 1, "running"))
    with pytest.raises(ObserverV2Violation, match="terminal"):
        projection.accept_event(terminal(2, 2, None, operation="delete"))
    with pytest.raises(ObserverV2Violation, match="schema"):
        projection.accept_event(terminal(2, 2, "completed", exit_code=9))
    projection.accept_event(terminal(2, 2, "completed", exit_code=0))
    projection.accept_event(terminal(3, 3, None, operation="delete"))
    assert projection.event_sequence == 3
