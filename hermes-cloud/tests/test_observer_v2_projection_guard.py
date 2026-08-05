from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from hermes_cloud.adapters.connector_contract_v1 import CloudEnvelopeV1Adapter
from hermes_cloud.domain.observer_projection_v2 import (
    ObserverProjectionV2,
    ObserverProjectionV2Error,
)

VALID = Path(__file__).parent / "fixtures/repository_contracts/fixtures/valid"


def _payload(name: str) -> dict[str, object]:
    value = json.loads((VALID / name).read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _snapshot():
    return CloudEnvelopeV1Adapter().decode_session_snapshot(
        _payload("session-snapshot-v2-payload.json")
    )


def _tool_event(
    sequence: int,
    *,
    revision: int,
    status: str = "completed",
    operation: str = "upsert",
):
    value = _payload("session-event-v2-tool-upsert.json")
    value["event_sequence"] = sequence
    body = value["payload"]
    assert isinstance(body, dict)
    body["revision"] = revision
    body["first_event_sequence"] = 3
    body["operation"] = operation
    if operation == "upsert":
        body["status"] = status
    else:
        for field in ("status", "name", "call_label"):
            body.pop(field, None)
    return CloudEnvelopeV1Adapter().decode_session_event(value)


def test_snapshot_replay_and_live_revision_delete_share_one_guard() -> None:
    projection = ObserverProjectionV2.from_snapshot(_snapshot())

    completed = _tool_event(5, revision=2)
    assert projection.accept(completed) is True
    assert projection.current_sequence == 5
    assert projection.accept(completed) is False
    assert projection.accept(_tool_event(6, revision=3, operation="delete")) is True
    assert projection.current_sequence == 6

    with pytest.raises(ObserverProjectionV2Error, match="recreated"):
        projection.accept(_tool_event(7, revision=1, status="running"))


def test_guard_rejects_snapshot_graph_and_todo_identity_violations() -> None:
    for mutation, reason in (("cycle", "cycle"), ("todo", "todo item")):
        value = _payload("session-snapshot-v2-payload.json")
        if mutation == "cycle":
            subagents = value["subagents"]
            assert isinstance(subagents, list)
            assert isinstance(subagents[0], dict)
            subagents[0]["parent_subagent_id"] = "subagent-child"
        else:
            sections = value["todo_sections"]
            assert isinstance(sections, list)
            assert isinstance(sections[0], dict)
            items = sections[0]["items"]
            assert isinstance(items, list)
            items.append(copy.deepcopy(items[0]))
        snapshot = CloudEnvelopeV1Adapter().decode_session_snapshot(value)

        with pytest.raises(ObserverProjectionV2Error, match=reason):
            ObserverProjectionV2.from_snapshot(snapshot)


def test_guard_rejects_absorbing_and_existing_todo_order_changes() -> None:
    projection = ObserverProjectionV2.from_snapshot(_snapshot())
    completed = _tool_event(5, revision=2)
    projection.accept(completed)

    changed = _tool_event(6, revision=3)
    changed_payload = dict(changed.payload)
    changed_payload["name"] = "Renamed after completion"
    changed = type(changed)(
        profile=changed.profile,
        runtime_generation=changed.runtime_generation,
        session_key=changed.session_key,
        runtime_session_id=changed.runtime_session_id,
        event_type=changed.event_type,
        event_sequence_start=changed.event_sequence_start,
        event_sequence=changed.event_sequence,
        payload=changed_payload,
        observer_contract=2,
    )
    with pytest.raises(ObserverProjectionV2Error, match="absorbing"):
        projection.accept(changed)

    value = _payload("session-snapshot-v2-payload.json")
    sections = value["todo_sections"]
    assert isinstance(sections, list)
    assert isinstance(sections[0], dict)
    items = sections[0]["items"]
    assert isinstance(items, list)
    items.append({"id": "item-2", "label": "Second", "status": "pending"})
    todo_projection = ObserverProjectionV2.from_snapshot(
        CloudEnvelopeV1Adapter().decode_session_snapshot(value)
    )
    event = _payload("session-event-v2-todo-upsert.json")
    event["event_sequence"] = 5
    payload = event["payload"]
    assert isinstance(payload, dict)
    payload["revision"] = 2
    payload["items"] = list(reversed(items))
    with pytest.raises(ObserverProjectionV2Error, match="order"):
        todo_projection.accept(CloudEnvelopeV1Adapter().decode_session_event(event))
