from __future__ import annotations

from dataclasses import dataclass

from hermes_connector.domain.observer import ObserverEvent, ObserverSnapshot

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
        "todo.update",
        "subagent.update",
        "tool.update",
        "terminal.update",
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
    """An Observer fact cannot be merged without guessing."""


def _event_range(
    event: ObserverEvent,
    *,
    session_key: str,
    runtime_session_id: str,
) -> tuple[int, int]:
    if event.type not in _EVENT_TYPES:
        raise ObserverSequenceError("observer event type is invalid")
    if event.session_key != session_key:
        raise ObserverSequenceError("observer event session does not match")
    if event.session_id != runtime_session_id:
        raise ObserverSequenceError("observer event runtime does not match")
    sequence = event.event_sequence
    sequence_start = event.event_sequence_start or sequence
    if sequence < 1 or sequence_start < 1 or sequence_start > sequence:
        raise ObserverSequenceError("observer event range is invalid")
    if sequence_start < sequence and event.type not in _MERGEABLE_EVENT_TYPES:
        raise ObserverSequenceError("observer event range is not mergeable")
    return sequence_start, sequence


@dataclass(slots=True)
class ObserverSequenceGuard:
    profile: str
    runtime_generation: str
    session_key: str
    runtime_session_id: str
    current_sequence: int

    @classmethod
    def from_snapshot(
        cls,
        snapshot: ObserverSnapshot,
        *,
        expected_profile: str,
        expected_runtime_generation: str,
        expected_session_key: str,
    ) -> ObserverSequenceGuard:
        if snapshot.profile != expected_profile:
            raise ObserverSequenceError("observer snapshot profile does not match")
        if snapshot.runtime_generation != expected_runtime_generation:
            raise ObserverSequenceError("observer snapshot generation does not match")
        if snapshot.session_key != expected_session_key:
            raise ObserverSequenceError("observer snapshot session does not match")
        if snapshot.snapshot_event_sequence > snapshot.event_sequence:
            raise ObserverSequenceError("observer snapshot is ahead of its cursor")

        replay_cursor = snapshot.snapshot_event_sequence
        for event in snapshot.replay_events:
            sequence_start, sequence = _event_range(
                event,
                session_key=snapshot.session_key,
                runtime_session_id=snapshot.runtime_session_id,
            )
            if sequence_start != replay_cursor + 1:
                raise ObserverSequenceError("observer replay sequence has a gap")
            if sequence > snapshot.event_sequence:
                raise ObserverSequenceError("observer replay exceeds its cursor")
            replay_cursor = sequence
        if replay_cursor != snapshot.event_sequence:
            raise ObserverSequenceError("observer replay sequence has a gap")
        return cls(
            profile=snapshot.profile,
            runtime_generation=snapshot.runtime_generation,
            session_key=snapshot.session_key,
            runtime_session_id=snapshot.runtime_session_id,
            current_sequence=snapshot.event_sequence,
        )

    def accept(self, event: ObserverEvent) -> bool:
        sequence_start, sequence = _event_range(
            event,
            session_key=self.session_key,
            runtime_session_id=self.runtime_session_id,
        )
        if sequence <= self.current_sequence:
            return False
        if sequence_start != self.current_sequence + 1:
            raise ObserverSequenceError("observer event sequence has a gap")
        self.current_sequence = sequence
        return True


__all__ = ["ObserverSequenceError", "ObserverSequenceGuard"]
