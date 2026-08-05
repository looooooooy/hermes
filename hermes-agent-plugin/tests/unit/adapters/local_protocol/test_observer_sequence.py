import pytest

from hermes_agent_plugin.adapters.local_protocol.observer_sequence import (
    ObserverSequenceError,
    ObserverSequenceGuard,
)


def _snapshot(**overrides):
    value = {
        "profile": "default",
        "runtime_generation": "runtime-generation-1",
        "session_key": "durable-key",
        "runtime_session_id": "runtime-id",
        "snapshot_event_sequence": 4,
        "event_sequence": 4,
        "replay_events": [],
    }
    value.update(overrides)
    return value


def _event(sequence: object, **overrides):
    params = {
        "type": "message.delta",
        "profile": "default",
        "runtime_generation": "runtime-generation-1",
        "session_id": "runtime-id",
        "session_key": "durable-key",
        "event_sequence": sequence,
        "payload": {"text": "hello"},
    }
    params.update(overrides)
    return {"jsonrpc": "2.0", "method": "event", "params": params}


def _replay(
    sequence: object,
    *,
    sequence_start: object | None = None,
    event_type: str = "message.delta",
    session_id: str = "runtime-id",
    session_key: str = "durable-key",
):
    event = {
        "type": event_type,
        "profile": "default",
        "runtime_generation": "runtime-generation-1",
        "session_id": session_id,
        "session_key": session_key,
        "event_sequence": sequence,
        "payload": {"text": "hello"},
    }
    if sequence_start is not None:
        event["event_sequence_start"] = sequence_start
    return event


def test_guard_ignores_stale_event_and_advances_only_contiguous_sequence() -> None:
    guard = ObserverSequenceGuard.from_snapshot(
        _snapshot(),
        requested_session_key="durable-key",
        requested_profile="default",
        requested_runtime_generation="runtime-generation-1",
    )

    assert guard.accept(_event(4)) is False
    assert guard.current_sequence == 4
    assert guard.accept(_event(5)) is True
    assert guard.current_sequence == 5


def test_snapshot_replay_and_live_mergeable_ranges_must_be_contiguous() -> None:
    guard = ObserverSequenceGuard.from_snapshot(
        _snapshot(
            snapshot_event_sequence=2,
            event_sequence=5,
            replay_events=[
                _replay(4, sequence_start=3),
                _replay(5, event_type="message.complete"),
            ],
        ),
        requested_session_key="durable-key",
        requested_profile="default",
        requested_runtime_generation="runtime-generation-1",
    )

    assert guard.current_sequence == 5
    assert guard.accept(_event(7, event_sequence_start=6)) is True
    assert guard.current_sequence == 7


@pytest.mark.parametrize(
    "snapshot",
    [
        _snapshot(session_key="other"),
        _snapshot(profile="other"),
        _snapshot(runtime_generation="runtime-generation-2"),
        _snapshot(runtime_session_id=""),
        _snapshot(snapshot_event_sequence=True),
        _snapshot(snapshot_event_sequence=5, event_sequence=4),
        _snapshot(snapshot_event_sequence=3, event_sequence=4, replay_events=[]),
        _snapshot(replay_events=None),
        _snapshot(
            snapshot_event_sequence=2,
            event_sequence=4,
            replay_events=[_replay(4)],
        ),
        _snapshot(
            snapshot_event_sequence=2,
            event_sequence=3,
            replay_events=[_replay(3, session_key="other")],
        ),
        _snapshot(
            snapshot_event_sequence=2,
            event_sequence=3,
            replay_events=[_replay(3, session_id="other-runtime")],
        ),
        _snapshot(
            snapshot_event_sequence=2,
            event_sequence=3,
            replay_events=[_replay(3, sequence_start=4)],
        ),
        _snapshot(
            snapshot_event_sequence=1,
            event_sequence=3,
            replay_events=[
                _replay(
                    3,
                    sequence_start=2,
                    event_type="message.complete",
                )
            ],
        ),
        _snapshot(
            snapshot_event_sequence=2,
            event_sequence=3,
            replay_events=[_replay(4)],
        ),
        _snapshot(
            snapshot_event_sequence=2,
            event_sequence=4,
            replay_events=[_replay(3)],
        ),
        _snapshot(
            snapshot_event_sequence=2,
            event_sequence=3,
            replay_events=[_replay(3, event_type="future.event")],
        ),
        _snapshot(
            snapshot_event_sequence=2,
            event_sequence=3,
            replay_events=["not-an-event"],
        ),
    ],
)
def test_invalid_or_incomplete_snapshot_fails_closed(snapshot: dict) -> None:
    with pytest.raises(ObserverSequenceError):
        ObserverSequenceGuard.from_snapshot(
            snapshot,
            requested_session_key="durable-key",
            requested_profile="default",
            requested_runtime_generation="runtime-generation-1",
        )


@pytest.mark.parametrize(
    "event",
    [
        _event(6),
        _event(True),
        _event(5, session_key="other"),
        _event(5, session_id="other-runtime"),
        _event(5, profile="other"),
        _event(5, runtime_generation="runtime-generation-2"),
        _event(6, event_sequence_start=5, type="message.complete"),
        _event(5, type="future.event"),
        {
            "jsonrpc": "2.0",
            "method": "event",
            "params": {
                "type": "message.delta",
                "runtime_session_id": "runtime-id",
                "session_key": "durable-key",
                "event_sequence": 5,
                "payload": {"text": "hello"},
            },
        },
        {"jsonrpc": "2.0", "method": "event", "params": {}},
    ],
)
def test_gap_malformed_or_cross_session_event_does_not_advance(event: dict) -> None:
    guard = ObserverSequenceGuard.from_snapshot(
        _snapshot(),
        requested_session_key="durable-key",
        requested_profile="default",
        requested_runtime_generation="runtime-generation-1",
    )

    with pytest.raises(ObserverSequenceError):
        guard.accept(event)

    assert guard.current_sequence == 4
