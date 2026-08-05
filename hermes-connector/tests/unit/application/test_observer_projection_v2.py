from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_connector.adapters.cloud.codec import (
    ConnectorProtocolCodec,
    InvalidCloudFrame,
)
from hermes_connector.application.observer_projection_v2 import (
    ObserverProjectionV2,
    ObserverProjectionV2Error,
)
from hermes_connector.contracts.observer_v2 import load_observer_v2_contracts

CONTRACTS = Path(__file__).parents[4] / "contracts"
VALID = CONTRACTS / "fixtures" / "valid"
INVALID = CONTRACTS / "fixtures" / "invalid"


def _payload(name: str) -> dict[str, object]:
    return json.loads((VALID / name).read_text(encoding="utf-8"))


def _snapshot():
    return ConnectorProtocolCodec().decode_session_snapshot_v2(
        (VALID / "session-snapshot-v2-payload.json").read_bytes()
    )


def _event(
    sequence: int,
    *,
    revision: int,
    status: str = "completed",
    operation: str = "upsert",
) -> object:
    payload = _payload("session-event-v2-tool-upsert.json")
    payload["event_sequence"] = sequence
    body = payload["payload"]
    assert isinstance(body, dict)
    body["revision"] = revision
    body["first_event_sequence"] = 3
    body["operation"] = operation
    if operation == "upsert":
        body["status"] = status
    else:
        for field in ("status", "name", "call_label"):
            body.pop(field, None)
    return ConnectorProtocolCodec().decode_session_event_v2_payload(payload)


def _mergeable_event(start: int, end: int, *, text: str = "merged") -> object:
    return ConnectorProtocolCodec().decode_session_event_v2_payload(
        {
            "observer_contract": 2,
            "profile": "default",
            "runtime_generation": "runtime-20260801-01",
            "session_key": "session-root-1",
            "session_id": "runtime-session-1",
            "type": "message.delta",
            "event_sequence_start": start,
            "event_sequence": end,
            "payload": {"text": text},
        }
    )


def _lifecycle_payload(
    fixture_name: str,
    *,
    sequence: int,
    revision: int,
    first_event_sequence: int,
    identity_field: str | None = None,
    identity_value: str | None = None,
) -> dict[str, object]:
    payload = _payload(fixture_name)
    payload["event_sequence"] = sequence
    body = payload["payload"]
    assert isinstance(body, dict)
    body["revision"] = revision
    body["first_event_sequence"] = first_event_sequence
    if identity_field is not None:
        assert identity_value is not None
        body[identity_field] = identity_value
    return payload


def test_snapshot_and_live_revision_delete_use_one_atomic_projection() -> None:
    projection = ObserverProjectionV2.from_snapshot(
        _snapshot(),
        expected_profile="default",
        expected_runtime_generation="runtime-20260801-01",
        expected_session_key="session-root-1",
    )

    completed = _event(5, revision=2)
    assert projection.accept(completed) is True
    assert projection.current_sequence == 5
    assert projection.accept(completed) is False
    projection.accept(_event(6, revision=3, operation="delete"))
    assert projection.current_sequence == 6


def test_gap_stale_revision_and_digest_conflict_do_not_advance() -> None:
    projection = ObserverProjectionV2.from_snapshot(
        _snapshot(),
        expected_profile="default",
        expected_runtime_generation="runtime-20260801-01",
        expected_session_key="session-root-1",
    )
    with pytest.raises(ObserverProjectionV2Error, match="contiguous"):
        projection.accept(_event(6, revision=2))
    with pytest.raises(ObserverProjectionV2Error, match="revision"):
        projection.accept(_event(5, revision=1))
    assert projection.current_sequence == 4

    accepted = _event(5, revision=2)
    projection.accept(accepted)
    conflicting_payload = _payload("session-event-v2-tool-upsert.json")
    conflicting_payload["event_sequence"] = 5
    body = conflicting_payload["payload"]
    assert isinstance(body, dict)
    body.update(
        {
            "revision": 2,
            "first_event_sequence": 3,
            "status": "completed",
            "name": "different digest",
        }
    )
    conflict = ConnectorProtocolCodec().decode_session_event_v2_payload(
        conflicting_payload
    )
    with pytest.raises(ObserverProjectionV2Error, match="digest"):
        projection.accept(conflict)
    assert projection.current_sequence == 5


def test_replay_and_live_mergeable_ranges_advance_by_start_and_end() -> None:
    payload = _payload("session-snapshot-v2-payload.json")
    replay = {
        "observer_contract": 2,
        "profile": "default",
        "runtime_generation": "runtime-20260801-01",
        "session_key": "session-root-1",
        "session_id": "runtime-session-1",
        "type": "message.delta",
        "event_sequence_start": 5,
        "event_sequence": 6,
        "payload": {"text": "replay"},
    }
    payload["event_sequence"] = 6
    payload["replay_events"] = [replay]
    snapshot = ConnectorProtocolCodec().decode_session_snapshot_v2_payload(payload)

    replay_projection = ObserverProjectionV2.from_snapshot(
        snapshot,
        expected_profile="default",
        expected_runtime_generation="runtime-20260801-01",
        expected_session_key="session-root-1",
    )
    assert replay_projection.current_sequence == 6

    live_projection = ObserverProjectionV2.from_snapshot(
        _snapshot(),
        expected_profile="default",
        expected_runtime_generation="runtime-20260801-01",
        expected_session_key="session-root-1",
    )
    merged = _mergeable_event(5, 6)
    assert live_projection.accept(merged) is True
    assert live_projection.current_sequence == 6
    assert live_projection.accept(merged) is False
    with pytest.raises(ObserverProjectionV2Error, match="digest"):
        live_projection.accept(_mergeable_event(4, 6))


def test_digest_window_retains_exactly_the_latest_1024_transport_identities() -> None:
    projection = ObserverProjectionV2.from_snapshot(
        _snapshot(),
        expected_profile="default",
        expected_runtime_generation="runtime-20260801-01",
        expected_session_key="session-root-1",
    )
    first = _mergeable_event(5, 5, text="event-5")
    projection.accept(first)
    for sequence in range(6, 1029):
        projection.accept(
            _mergeable_event(sequence, sequence, text=f"event-{sequence}")
        )

    assert projection.accept(first) is False
    projection.accept(_mergeable_event(1029, 1029, text="event-1029"))
    with pytest.raises(ObserverProjectionV2Error, match="retained digest window"):
        projection.accept(first)


def test_new_lifecycle_entity_allows_earlier_first_event_sequence() -> None:
    projection = ObserverProjectionV2.from_snapshot(
        _snapshot(),
        expected_profile="default",
        expected_runtime_generation="runtime-20260801-01",
        expected_session_key="session-root-1",
    )
    payload = _lifecycle_payload(
        "session-event-v2-tool-upsert.json",
        sequence=5,
        revision=1,
        first_event_sequence=4,
        identity_field="tool_call_id",
        identity_value="tool-earlier-origin",
    )

    assert projection.accept(
        ConnectorProtocolCodec().decode_session_event_v2_payload(payload)
    )


@pytest.mark.parametrize(
    (
        "collection",
        "event_type",
        "identity_field",
        "core_mutation",
        "safe_enrichment",
    ),
    (
        (
            "subagents",
            "subagent.update",
            "subagent_id",
            {"name": "changed core name"},
            {"summary": "completed safely", "duration_ms": 12},
        ),
        (
            "tools",
            "tool.update",
            "tool_call_id",
            {"call_label": "new core label"},
            {"summary": "completed safely", "duration_ms": 12},
        ),
        (
            "terminals",
            "terminal.update",
            "process_id",
            {"status": "failed", "exit_code": 1},
            {"summary": "completed safely", "duration_ms": 12},
        ),
    ),
)
def test_terminal_lifecycle_only_enriches_missing_safe_metadata(
    collection: str,
    event_type: str,
    identity_field: str,
    core_mutation: dict[str, object],
    safe_enrichment: dict[str, object],
) -> None:
    payload = _payload("session-snapshot-v2-payload.json")
    states = payload[collection]
    assert isinstance(states, list)
    state = states[0]
    state["status"] = "completed"
    if collection == "terminals":
        state["exit_code"] = 0
    snapshot = ConnectorProtocolCodec().decode_session_snapshot_v2_payload(payload)
    projection = ObserverProjectionV2.from_snapshot(
        snapshot,
        expected_profile="default",
        expected_runtime_generation="runtime-20260801-01",
        expected_session_key="session-root-1",
    )
    candidate = dict(state)
    candidate.update(
        {
            "operation": "upsert",
            "revision": 2,
            **safe_enrichment,
        }
    )
    event_payload = {
        "observer_contract": 2,
        "profile": "default",
        "runtime_generation": "runtime-20260801-01",
        "session_key": "session-root-1",
        "session_id": "runtime-session-1",
        "type": event_type,
        "event_sequence": 5,
        "payload": candidate,
    }
    assert projection.accept(
        ConnectorProtocolCodec().decode_session_event_v2_payload(event_payload)
    )

    changed_safe = dict(candidate)
    changed_safe.update({"revision": 3, "summary": "changed after enrichment"})
    event_payload.update({"event_sequence": 6, "payload": changed_safe})
    with pytest.raises(ObserverProjectionV2Error, match="terminal"):
        projection.accept(
            ConnectorProtocolCodec().decode_session_event_v2_payload(event_payload)
        )

    changed_core = dict(candidate)
    changed_core.update({"revision": 3, **core_mutation})
    assert changed_core[identity_field] == state[identity_field]
    event_payload.update({"event_sequence": 6, "payload": changed_core})
    with pytest.raises(ObserverProjectionV2Error, match="terminal"):
        projection.accept(
            ConnectorProtocolCodec().decode_session_event_v2_payload(event_payload)
        )


def test_todo_existing_labels_and_order_are_frozen_while_tail_append_is_allowed() -> (
    None
):
    projection = ObserverProjectionV2.from_snapshot(
        _snapshot(),
        expected_profile="default",
        expected_runtime_generation="runtime-20260801-01",
        expected_session_key="session-root-1",
    )
    changed_label = _lifecycle_payload(
        "session-event-v2-todo-upsert.json",
        sequence=5,
        revision=2,
        first_event_sequence=1,
    )
    body = changed_label["payload"]
    assert isinstance(body, dict)
    items = body["items"]
    assert isinstance(items, list)
    items[0]["label"] = "changed label"
    with pytest.raises(ObserverProjectionV2Error, match="label"):
        projection.accept(
            ConnectorProtocolCodec().decode_session_event_v2_payload(changed_label)
        )

    accepted = _lifecycle_payload(
        "session-event-v2-todo-upsert.json",
        sequence=5,
        revision=2,
        first_event_sequence=1,
    )
    assert projection.accept(
        ConnectorProtocolCodec().decode_session_event_v2_payload(accepted)
    )

    removed = _lifecycle_payload(
        "session-event-v2-todo-upsert.json",
        sequence=6,
        revision=3,
        first_event_sequence=1,
    )
    removed_body = removed["payload"]
    assert isinstance(removed_body, dict)
    removed_items = removed_body["items"]
    assert isinstance(removed_items, list)
    removed_body["items"] = removed_items[:1]
    with pytest.raises(ObserverProjectionV2Error, match="order"):
        projection.accept(
            ConnectorProtocolCodec().decode_session_event_v2_payload(removed)
        )


@pytest.mark.parametrize(
    ("collection", "limit_name", "fixture", "identity_field", "identity_prefix"),
    (
        ("todo_sections", "max_todo_sections", "session-event-v2-todo-upsert.json", "section_id", "todo"),
        ("subagents", "max_subagents", "session-event-v2-subagent-upsert.json", "subagent_id", "subagent"),
        ("tools", "max_tools", "session-event-v2-tool-upsert.json", "tool_call_id", "tool"),
        ("terminals", "max_terminals", "session-event-v2-terminal-upsert.json", "process_id", "terminal"),
    ),
)
def test_live_collection_accepts_exact_limit_and_rejects_plus_one(
    collection: str,
    limit_name: str,
    fixture: str,
    identity_field: str,
    identity_prefix: str,
) -> None:
    limit = int(load_observer_v2_contracts().policy["limits"][limit_name])
    payload = _payload("session-snapshot-v2-payload.json")
    states = payload[collection]
    assert isinstance(states, list)
    template = dict(states[0])
    if collection == "subagents":
        template["parent_subagent_id"] = None
    payload[collection] = [
        {**template, identity_field: f"{identity_prefix}-{index}"}
        for index in range(limit - 1)
    ]
    snapshot = ConnectorProtocolCodec().decode_session_snapshot_v2_payload(payload)
    projection = ObserverProjectionV2.from_snapshot(
        snapshot,
        expected_profile="default",
        expected_runtime_generation="runtime-20260801-01",
        expected_session_key="session-root-1",
    )

    at_limit = _lifecycle_payload(
        fixture,
        sequence=5,
        revision=1,
        first_event_sequence=5,
        identity_field=identity_field,
        identity_value=f"{identity_prefix}-{limit - 1}",
    )
    assert projection.accept(
        ConnectorProtocolCodec().decode_session_event_v2_payload(at_limit)
    )

    overflow = _lifecycle_payload(
        fixture,
        sequence=6,
        revision=1,
        first_event_sequence=6,
        identity_field=identity_field,
        identity_value=f"{identity_prefix}-{limit}",
    )
    with pytest.raises(ObserverProjectionV2Error, match="limit"):
        projection.accept(
            ConnectorProtocolCodec().decode_session_event_v2_payload(overflow)
        )


def test_snapshot_rejects_subagent_cycle_and_duplicate_todo_identity() -> None:
    for mutation, reason in (("cycle", "cycle"), ("todo", "todo item")):
        payload = _payload("session-snapshot-v2-payload.json")
        if mutation == "cycle":
            subagents = payload["subagents"]
            assert isinstance(subagents, list)
            subagents[0]["parent_subagent_id"] = "subagent-child"
        else:
            sections = payload["todo_sections"]
            assert isinstance(sections, list)
            sections[0]["items"].append(dict(sections[0]["items"][0]))
        snapshot = ConnectorProtocolCodec().decode_session_snapshot_v2_payload(payload)

        with pytest.raises(ObserverProjectionV2Error, match=reason):
            ObserverProjectionV2.from_snapshot(
                snapshot,
                expected_profile="default",
                expected_runtime_generation="runtime-20260801-01",
                expected_session_key="session-root-1",
            )


@pytest.mark.parametrize(
    "fixture_name",
    (
        "session-event-v2-tool-raw-args.json",
        "session-event-v2-terminal-stream.json",
        "session-event-v2-lifecycle-range.json",
    ),
)
def test_generated_decoder_rejects_unsafe_or_mergeable_lifecycle_payloads(
    fixture_name: str,
) -> None:
    with pytest.raises(InvalidCloudFrame):
        ConnectorProtocolCodec().decode_session_event_v2(
            (INVALID / fixture_name).read_bytes()
        )
