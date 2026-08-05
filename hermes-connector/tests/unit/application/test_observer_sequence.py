from __future__ import annotations

from types import MappingProxyType

import pytest

from hermes_connector.application.observer_sequence import (
    ObserverSequenceError,
    ObserverSequenceGuard,
)
from hermes_connector.domain.observer import ObserverEvent, ObserverSnapshot


def _event(
    sequence: int,
    *,
    sequence_start: int | None = None,
    event_type: str = "message.delta",
    session_key: str = "session-root-1",
    session_id: str = "runtime-session-1",
) -> ObserverEvent:
    return ObserverEvent(
        type=event_type,
        session_id=session_id,
        session_key=session_key,
        event_sequence=sequence,
        event_sequence_start=sequence_start,
        payload=MappingProxyType({"text": "delta"}),
    )


def _snapshot(
    *,
    snapshot_sequence: int = 4,
    event_sequence: int = 5,
    replay_events: tuple[ObserverEvent, ...] = (_event(5),),
) -> ObserverSnapshot:
    return ObserverSnapshot(
        profile="default",
        runtime_generation="runtime-generation-1",
        session_key="session-root-1",
        runtime_session_id="runtime-session-1",
        running=True,
        status="running",
        event_sequence=event_sequence,
        snapshot_event_sequence=snapshot_sequence,
        messages=(),
        inflight=MappingProxyType(
            {
                "user": None,
                "assistant": None,
                "streaming": False,
                "error": None,
            }
        ),
        replay_events=replay_events,
    )


def test_snapshot_establishes_contiguous_identity_bound_cursor() -> None:
    guard = ObserverSequenceGuard.from_snapshot(
        _snapshot(),
        expected_profile="default",
        expected_runtime_generation="runtime-generation-1",
        expected_session_key="session-root-1",
    )

    assert guard.current_sequence == 5
    assert guard.accept(_event(6)) is True
    assert guard.current_sequence == 6
    assert guard.accept(_event(6)) is False
    assert guard.current_sequence == 6


@pytest.mark.parametrize(
    "snapshot",
    (
        _snapshot(snapshot_sequence=6, event_sequence=5, replay_events=()),
        _snapshot(snapshot_sequence=4, event_sequence=6, replay_events=(_event(6),)),
        _snapshot(
            snapshot_sequence=4,
            event_sequence=5,
            replay_events=(_event(5, session_key="another-session"),),
        ),
        _snapshot(
            snapshot_sequence=4,
            event_sequence=5,
            replay_events=(_event(5, session_id="another-runtime"),),
        ),
    ),
)
def test_snapshot_rejects_cursor_gap_or_identity_mismatch(
    snapshot: ObserverSnapshot,
) -> None:
    with pytest.raises(ObserverSequenceError):
        ObserverSequenceGuard.from_snapshot(
            snapshot,
            expected_profile="default",
            expected_runtime_generation="runtime-generation-1",
            expected_session_key="session-root-1",
        )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("expected_profile", "other"),
        ("expected_runtime_generation", "runtime-generation-2"),
        ("expected_session_key", "another-session"),
    ),
)
def test_snapshot_rejects_requested_authority_mismatch(
    field: str,
    value: str,
) -> None:
    expected = {
        "expected_profile": "default",
        "expected_runtime_generation": "runtime-generation-1",
        "expected_session_key": "session-root-1",
    }
    expected[field] = value

    with pytest.raises(ObserverSequenceError):
        ObserverSequenceGuard.from_snapshot(_snapshot(), **expected)


def test_live_gap_does_not_advance_cursor() -> None:
    guard = ObserverSequenceGuard.from_snapshot(
        _snapshot(),
        expected_profile="default",
        expected_runtime_generation="runtime-generation-1",
        expected_session_key="session-root-1",
    )

    with pytest.raises(ObserverSequenceError, match="gap"):
        guard.accept(_event(7))

    assert guard.current_sequence == 5


@pytest.mark.parametrize(
    "event",
    (
        _event(6, session_key="another-session"),
        _event(6, session_id="another-runtime"),
    ),
)
def test_live_identity_mismatch_does_not_advance_cursor(
    event: ObserverEvent,
) -> None:
    guard = ObserverSequenceGuard.from_snapshot(
        _snapshot(),
        expected_profile="default",
        expected_runtime_generation="runtime-generation-1",
        expected_session_key="session-root-1",
    )

    with pytest.raises(ObserverSequenceError, match="session|runtime"):
        guard.accept(event)

    assert guard.current_sequence == 5


def test_only_mergeable_event_types_may_span_sequence_range() -> None:
    guard = ObserverSequenceGuard.from_snapshot(
        _snapshot(),
        expected_profile="default",
        expected_runtime_generation="runtime-generation-1",
        expected_session_key="session-root-1",
    )

    with pytest.raises(ObserverSequenceError, match="mergeable"):
        guard.accept(
            _event(
                7,
                sequence_start=6,
                event_type="message.complete",
            )
        )

    assert guard.accept(_event(7, sequence_start=6)) is True
    assert guard.current_sequence == 7
