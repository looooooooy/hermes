"""Fail-closed sequence guard for a single observer subscription."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

_EVENT_TYPES = frozenset(
    {
        "message.start",
        "message.delta",
        "message.complete",
        "agent.terminal.output",
        "reasoning.delta",
        "status.update",
        "thinking.delta",
        "tool.output.delta",
    }
)
_MERGEABLE_EVENT_TYPES = frozenset(
    {
        "agent.terminal.output",
        "message.delta",
        "reasoning.delta",
        "status.update",
        "thinking.delta",
        "tool.output.delta",
    }
)


class ObserverSequenceError(ValueError):
    """Observer snapshot or event cannot be merged without guessing."""


def _sequence(value: object, *, minimum: int) -> int:
    if type(value) is not int or value < minimum:
        raise ObserverSequenceError("observer sequence is invalid")
    return value


def _event_range(
    event: object,
    *,
    profile: str,
    runtime_generation: str,
    session_key: str,
    runtime_session_id: str,
) -> tuple[int, int]:
    if not isinstance(event, dict):
        raise ObserverSequenceError("observer event is invalid")
    event_type = event.get("type")
    if event_type not in _EVENT_TYPES:
        raise ObserverSequenceError("observer event type is invalid")
    if event.get("profile") != profile:
        raise ObserverSequenceError("observer event profile does not match")
    if event.get("runtime_generation") != runtime_generation:
        raise ObserverSequenceError("observer event generation does not match")
    if event.get("session_key") != session_key:
        raise ObserverSequenceError("observer event session does not match")
    if event.get("session_id") != runtime_session_id:
        raise ObserverSequenceError("observer event runtime does not match")

    sequence = _sequence(event.get("event_sequence"), minimum=1)
    sequence_start = _sequence(
        event.get("event_sequence_start", sequence),
        minimum=1,
    )
    if sequence_start > sequence:
        raise ObserverSequenceError("observer event range is reversed")
    if sequence_start < sequence and event_type not in _MERGEABLE_EVENT_TYPES:
        raise ObserverSequenceError("observer event range is not mergeable")
    return sequence_start, sequence


@dataclass
class ObserverSequenceGuard:
    """Track the contiguous cursor established by an authoritative snapshot.

    Cursor transitions:
        SNAPSHOT(n) -- event(n + 1..m) --> LIVE(m)
        LIVE(n)     -- event(<= n) --> LIVE(n)       [stale duplicate]
        LIVE(n)     -- event(start != n+1) --> GAP    [caller reconnects]

    A gap never mutates the cursor. The relay closes the downstream observer so
    its next subscription obtains a new authoritative snapshot.
    """

    session_key: str
    profile: str
    runtime_generation: str
    runtime_session_id: str
    current_sequence: int

    @classmethod
    def from_snapshot(
        cls,
        snapshot: dict[str, Any],
        *,
        requested_session_key: str,
        requested_profile: str,
        requested_runtime_generation: str,
    ) -> ObserverSequenceGuard:
        profile = snapshot.get("profile")
        runtime_generation = snapshot.get("runtime_generation")
        session_key = snapshot.get("session_key")
        runtime_session_id = snapshot.get("runtime_session_id")
        snapshot_sequence = _sequence(
            snapshot.get("snapshot_event_sequence"),
            minimum=0,
        )
        current_sequence = _sequence(
            snapshot.get("event_sequence"),
            minimum=0,
        )
        replay_events = snapshot.get("replay_events")
        if profile != requested_profile:
            raise ObserverSequenceError("observer snapshot profile does not match")
        if runtime_generation != requested_runtime_generation:
            raise ObserverSequenceError("observer snapshot generation does not match")
        if session_key != requested_session_key:
            raise ObserverSequenceError("observer snapshot session does not match")
        if not isinstance(runtime_session_id, str) or not runtime_session_id:
            raise ObserverSequenceError("observer snapshot runtime is invalid")
        if not isinstance(replay_events, list):
            raise ObserverSequenceError("observer replay is invalid")
        if snapshot_sequence > current_sequence:
            raise ObserverSequenceError("observer snapshot is ahead of its cursor")

        replay_cursor = snapshot_sequence
        for event in replay_events:
            sequence_start, sequence = _event_range(
                event,
                profile=profile,
                runtime_generation=runtime_generation,
                session_key=session_key,
                runtime_session_id=runtime_session_id,
            )
            if sequence_start != replay_cursor + 1:
                raise ObserverSequenceError("observer replay sequence has a gap")
            if sequence > current_sequence:
                raise ObserverSequenceError("observer replay exceeds its cursor")
            replay_cursor = sequence
        if replay_cursor != current_sequence:
            raise ObserverSequenceError("observer replay does not reach its cursor")
        return cls(
            session_key=session_key,
            profile=profile,
            runtime_generation=runtime_generation,
            runtime_session_id=runtime_session_id,
            current_sequence=current_sequence,
        )

    def accept(self, frame: dict[str, Any]) -> bool:
        if frame.get("method") != "event":
            raise ObserverSequenceError("observer frame is not an event")
        params = frame.get("params")
        if not isinstance(params, dict):
            raise ObserverSequenceError("observer event params are invalid")
        sequence_start, sequence = _event_range(
            params,
            profile=self.profile,
            runtime_generation=self.runtime_generation,
            session_key=self.session_key,
            runtime_session_id=self.runtime_session_id,
        )
        if sequence <= self.current_sequence:
            return False
        if sequence_start != self.current_sequence + 1:
            raise ObserverSequenceError("observer event sequence has a gap")
        self.current_sequence = sequence
        return True


__all__ = [
    "ObserverSequenceError",
    "ObserverSequenceGuard",
]
