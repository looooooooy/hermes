from __future__ import annotations

import pytest

from hermes_cloud.adapters.connector_contract_v1 import CloudEnvelopeV1Adapter


def _todo_event(sequence: int = 1) -> dict[str, object]:
    return {
        "observer_contract": 2,
        "profile": "default",
        "runtime_generation": "generation-v2",
        "session_key": "session-root-1",
        "session_id": "runtime-session-v2",
        "type": "todo.update",
        "event_sequence": sequence,
        "payload": {
            "turn_id": "turn-1",
            "section_id": "todo-1",
            "revision": 1,
            "first_event_sequence": 1,
            "operation": "upsert",
            "status": "in_progress",
            "items": [{"id": "item-1", "label": "Run tests", "status": "in_progress"}],
        },
    }


def _snapshot_v2() -> dict[str, object]:
    return {
        "observer_contract": 2,
        "profile": "default",
        "runtime_generation": "generation-v2",
        "session_key": "session-root-1",
        "runtime_session_id": "runtime-session-v2",
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
        "replay_events": [_todo_event()],
    }


def test_generated_v2_snapshot_schema_decodes_full_replay_and_lifecycle_baseline() -> (
    None
):
    snapshot = CloudEnvelopeV1Adapter().decode_session_snapshot(_snapshot_v2())

    assert snapshot.observer_contract == 2
    assert snapshot.runtime_generation == "generation-v2"
    assert snapshot.todo_sections == ()
    assert snapshot.subagents == ()
    assert snapshot.tools == ()
    assert snapshot.terminals == ()
    assert snapshot.replay_events[0].observer_contract == 2
    assert snapshot.replay_events[0].event_type == "todo.update"


@pytest.mark.parametrize("private_field", ["client_secret", "api_token", "tool_args"])
def test_v2_extensions_recursively_reject_private_fields(
    private_field: str,
) -> None:
    payload = _todo_event()
    payload["extensions"] = {
        "vendor.private": {"nested": [{"deeper": {private_field: "must-not-cross"}}]}
    }

    with pytest.raises(ValueError, match="envelope is invalid"):
        CloudEnvelopeV1Adapter().decode_session_event(payload)


@pytest.mark.parametrize(
    "credential",
    [
        "Authorization: Basic dXNlcjpwYXNzd29yZA==",
        "password=hunter2-secret",
        "password=hunter2",
        "api_key: abcdefghijklmnop",
        "token=provider-token-value",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1c2VyLTEifQ.signature123456",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2ln",
        "AKIAIOSFODNN7EXAMPLE",
        "AIzaSyDexampleProviderCredential123456",
    ],
)
def test_v2_display_text_rejects_common_credential_shapes_without_echoing_value(
    credential: str,
) -> None:
    payload = _snapshot_v2()
    payload["messages"] = [{"role": "assistant", "content": credential}]

    with pytest.raises(ValueError, match="envelope is invalid") as rejected:
        CloudEnvelopeV1Adapter().decode_session_snapshot(payload)

    assert credential not in str(rejected.value)


@pytest.mark.parametrize(
    "safe_text",
    [
        "tokenizer.pathology.version",
        "Release 1.2.3 is ready.",
        "Open docs/reference/three.part.name for details.",
        "Basic authentication is disabled.",
        "Basic YWJjZA== is not a user-password credential.",
    ],
)
def test_v2_display_text_does_not_mistake_benign_three_part_text_for_credentials(
    safe_text: str,
) -> None:
    payload = _snapshot_v2()
    payload["messages"] = [{"role": "assistant", "content": safe_text}]

    decoded = CloudEnvelopeV1Adapter().decode_session_snapshot(payload)

    assert decoded.messages[0]["content"] == safe_text


def test_v2_safe_display_text_and_nonnegative_token_counts_remain_valid() -> None:
    payload = _snapshot_v2()
    payload["messages"] = [
        {"role": "assistant", "content": "Deployment checks completed safely."}
    ]
    payload["subagents"] = [
        {
            "turn_id": "turn-1",
            "subagent_id": "quality-review",
            "revision": 1,
            "first_event_sequence": 1,
            "parent_subagent_id": None,
            "name": "Quality review",
            "goal": "Review the release",
            "summary": "All focused checks passed.",
            "status": "completed",
            "token_counts": {"input": 12, "output": 7, "reasoning": 3},
        }
    ]

    decoded = CloudEnvelopeV1Adapter().decode_session_snapshot(payload)

    assert decoded.messages[0]["content"] == "Deployment checks completed safely."
    assert decoded.subagents[0]["token_counts"] == {
        "input": 12,
        "output": 7,
        "reasoning": 3,
    }
