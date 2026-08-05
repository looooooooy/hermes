"""Authoritative lifecycle projection guard for Observer output-parity v2."""

from __future__ import annotations

import copy
from collections import deque
from collections.abc import Mapping
from typing import Any

from hermes_cloud.contracts.observer_v2 import (
    ObserverV2ContractError,
    require_display_safe,
)
from hermes_cloud.domain.canonical_json import canonical_payload_digest
from hermes_cloud.domain.connector_gateway import (
    ConnectorObserverEvent,
    ConnectorObserverSnapshot,
)

_COLLECTIONS = ("todo_sections", "subagents", "tools", "terminals")
_EVENT_COLLECTION = {
    "todo.update": "todo_sections",
    "subagent.update": "subagents",
    "tool.update": "tools",
    "terminal.update": "terminals",
}
_IDENTITY_FIELDS = {
    "todo_sections": ("turn_id", "section_id"),
    "subagents": ("turn_id", "subagent_id"),
    "tools": ("turn_id", "tool_call_id"),
    "terminals": ("turn_id", "process_id"),
}
_TERMINAL_STATUSES = {
    "todo_sections": frozenset({"completed", "cancelled"}),
    "subagents": frozenset({"completed", "failed", "interrupted"}),
    "tools": frozenset({"completed", "failed", "interrupted"}),
    "terminals": frozenset({"completed", "failed", "interrupted"}),
}


class ObserverProjectionV2Error(ValueError):
    """A v2 fact cannot be projected without weakening frozen semantics."""


def _thaw(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return copy.deepcopy(value)


def _identity(collection: str, value: Mapping[str, Any]) -> tuple[str, str]:
    first, second = _IDENTITY_FIELDS[collection]
    return str(value[first]), str(value[second])


def _event_digest(event: ConnectorObserverEvent) -> str:
    value: dict[str, object] = {
        "observer_contract": 2,
        "profile": event.profile,
        "runtime_generation": event.runtime_generation,
        "session_key": event.session_key,
        "session_id": event.runtime_session_id,
        "type": event.event_type,
        "event_sequence": event.event_sequence,
        "payload": _thaw(event.payload),
    }
    if event.event_sequence_start != event.event_sequence:
        value["event_sequence_start"] = event.event_sequence_start
    return canonical_payload_digest(value)


class ObserverProjectionV2:
    """Atomic snapshot/replay/live guard for one authoritative runtime."""

    def __init__(self) -> None:
        self.profile = ""
        self.runtime_generation = ""
        self.session_key = ""
        self.runtime_session_id = ""
        self.current_sequence = 0
        self._states: dict[str, dict[tuple[str, str], dict[str, Any]]] = {
            collection: {} for collection in _COLLECTIONS
        }
        self._tombstones: dict[str, set[tuple[str, str]]] = {
            collection: set() for collection in _COLLECTIONS
        }
        self._digests: dict[int, str] = {}
        self._digest_order: deque[int] = deque()

    @classmethod
    def from_snapshot(
        cls,
        snapshot: ConnectorObserverSnapshot,
    ) -> ObserverProjectionV2:
        if snapshot.observer_contract != 2:
            raise ObserverProjectionV2Error("observer snapshot v2 contract is required")
        if snapshot.snapshot_event_sequence > snapshot.event_sequence:
            raise ObserverProjectionV2Error("observer snapshot cursor is invalid")
        try:
            require_display_safe(
                {
                    "messages": snapshot.messages,
                    "inflight": snapshot.inflight,
                    "todo_sections": snapshot.todo_sections,
                    "subagents": snapshot.subagents,
                    "tools": snapshot.tools,
                    "terminals": snapshot.terminals,
                }
            )
        except ObserverV2ContractError as error:
            raise ObserverProjectionV2Error(
                "observer snapshot is not display-safe"
            ) from error

        projection = cls()
        projection.profile = snapshot.profile
        projection.runtime_generation = snapshot.runtime_generation
        projection.session_key = snapshot.session_key
        projection.runtime_session_id = snapshot.runtime_session_id
        projection.current_sequence = snapshot.snapshot_event_sequence
        for collection in _COLLECTIONS:
            values = getattr(snapshot, collection)
            for value in values:
                state = _thaw(value)
                assert isinstance(state, dict)
                key = _identity(collection, state)
                if key in projection._states[collection]:
                    raise ObserverProjectionV2Error(
                        "observer composite entity identity is duplicated"
                    )
                if state["first_event_sequence"] > snapshot.snapshot_event_sequence:
                    raise ObserverProjectionV2Error(
                        "observer first event sequence is invalid"
                    )
                projection._validate_entity(collection, state)
                projection._states[collection][key] = state
        projection._validate_subagent_tree(projection._states["subagents"])

        for replay in snapshot.replay_events:
            if replay.observer_contract != 2:
                raise ObserverProjectionV2Error(
                    "observer replay v2 contract is required"
                )
            projection._accept(replay)
        if projection.current_sequence != snapshot.event_sequence:
            raise ObserverProjectionV2Error(
                "observer replay must cover the snapshot sequence range"
            )
        return projection

    def accept(self, event: ConnectorObserverEvent) -> bool:
        if event.observer_contract != 2:
            raise ObserverProjectionV2Error("observer event v2 contract is required")
        if (
            event.profile != self.profile
            or event.runtime_generation != self.runtime_generation
        ):
            raise ObserverProjectionV2Error("observer event runtime scope changed")
        return self._accept(event)

    def _accept(self, event: ConnectorObserverEvent) -> bool:
        if (
            event.session_key != self.session_key
            or event.runtime_session_id != self.runtime_session_id
        ):
            raise ObserverProjectionV2Error("observer event session scope changed")
        try:
            require_display_safe(event.payload)
        except ObserverV2ContractError as error:
            raise ObserverProjectionV2Error(
                "observer event is not display-safe"
            ) from error
        digest = _event_digest(event)
        if event.event_sequence <= self.current_sequence:
            previous = self._digests.get(event.event_sequence)
            if previous is None:
                raise ObserverProjectionV2Error(
                    "observer duplicate is outside the retained digest window"
                )
            if previous != digest:
                raise ObserverProjectionV2Error(
                    "observer event identity has a different digest"
                )
            return False
        if event.event_sequence_start != self.current_sequence + 1:
            raise ObserverProjectionV2Error(
                "observer event sequence must be contiguous"
            )
        if event.event_type in _EVENT_COLLECTION:
            self._accept_lifecycle(event)
        else:
            self._validate_scoped_output(event)
        self.current_sequence = event.event_sequence
        self._remember_digest(event.event_sequence, digest)
        return True

    def _accept_lifecycle(self, event: ConnectorObserverEvent) -> None:
        if event.event_sequence_start != event.event_sequence:
            raise ObserverProjectionV2Error(
                "observer lifecycle event must be non-mergeable"
            )
        collection = _EVENT_COLLECTION[event.event_type]
        payload = _thaw(event.payload)
        assert isinstance(payload, dict)
        key = _identity(collection, payload)
        states = self._states[collection]
        previous = states.get(key)
        if key in self._tombstones[collection]:
            raise ObserverProjectionV2Error(
                "observer deleted entity cannot be recreated before snapshot"
            )
        expected_revision = 1 if previous is None else previous["revision"] + 1
        if payload["revision"] != expected_revision:
            raise ObserverProjectionV2Error(
                "observer entity revision must be exactly previous plus one"
            )
        if previous is None:
            if payload["first_event_sequence"] != event.event_sequence:
                raise ObserverProjectionV2Error(
                    "observer initial first event sequence is invalid"
                )
        elif payload["first_event_sequence"] != previous["first_event_sequence"]:
            raise ObserverProjectionV2Error(
                "observer first event sequence must remain stable"
            )

        if payload["operation"] == "delete":
            if previous is None:
                raise ObserverProjectionV2Error(
                    "observer delete requires an existing entity"
                )
            if not self._is_terminal(collection, previous):
                raise ObserverProjectionV2Error(
                    "observer delete requires terminal entity state"
                )
            if collection == "subagents" and any(
                state.get("parent_subagent_id") == key[1] and state["turn_id"] == key[0]
                for child_key, state in states.items()
                if child_key != key
            ):
                raise ObserverProjectionV2Error(
                    "observer subagent delete requires a leaf"
                )
            del states[key]
            self._tombstones[collection].add(key)
            return

        candidate = {
            field: item for field, item in payload.items() if field != "operation"
        }
        self._validate_entity(collection, candidate)
        if previous is not None:
            self._validate_absorbing(collection, previous, candidate)
        candidates = dict(states)
        candidates[key] = candidate
        if collection == "subagents":
            parent = candidate.get("parent_subagent_id")
            if parent is not None and (key[0], parent) not in states:
                raise ObserverProjectionV2Error(
                    "observer live subagent parent must already exist"
                )
            self._validate_subagent_tree(candidates)
        states[key] = candidate

    def _validate_scoped_output(self, event: ConnectorObserverEvent) -> None:
        payload = event.payload
        if event.event_type == "tool.output.delta":
            key = str(payload["turn_id"]), str(payload["tool_call_id"])
            if key not in self._states["tools"]:
                raise ObserverProjectionV2Error(
                    "observer tool output lacks a turn-scoped tool"
                )
        elif event.event_type == "agent.terminal.output":
            key = str(payload["turn_id"]), str(payload["process_id"])
            if key not in self._states["terminals"]:
                raise ObserverProjectionV2Error(
                    "observer terminal output lacks a turn-scoped terminal"
                )

    def _validate_entity(self, collection: str, state: Mapping[str, Any]) -> None:
        if state["revision"] < 1 or state["first_event_sequence"] < 1:
            raise ObserverProjectionV2Error("observer entity revision is invalid")
        if collection == "todo_sections":
            item_ids = [item["id"] for item in state["items"]]
            if len(item_ids) != len(set(item_ids)):
                raise ObserverProjectionV2Error("observer todo item ids must be unique")
            if state["status"] in {"completed", "cancelled"} and any(
                item["status"] not in {"completed", "cancelled"}
                for item in state["items"]
            ):
                raise ObserverProjectionV2Error(
                    "observer terminal todo section contains an active item"
                )
        elif collection == "subagents":
            progress = state.get("progress")
            if progress is not None and progress["current"] > progress["total"]:
                raise ObserverProjectionV2Error("observer progress exceeds total")
        elif collection == "terminals":
            status = state["status"]
            exit_code = state.get("exit_code")
            if (status == "completed" and exit_code != 0) or (
                status == "failed" and (exit_code is None or exit_code == 0)
            ):
                raise ObserverProjectionV2Error(
                    "observer terminal exit code is inconsistent"
                )
            if (
                status in {"running", "interrupted", "unknown"}
                and exit_code is not None
            ):
                raise ObserverProjectionV2Error(
                    "observer terminal exit code is inconsistent"
                )

    def _is_terminal(self, collection: str, state: Mapping[str, Any]) -> bool:
        if collection == "todo_sections":
            return state["status"] in _TERMINAL_STATUSES[collection] and all(
                item["status"] in {"completed", "cancelled"} for item in state["items"]
            )
        return state["status"] in _TERMINAL_STATUSES[collection]

    def _validate_absorbing(
        self,
        collection: str,
        previous: Mapping[str, Any],
        candidate: Mapping[str, Any],
    ) -> None:
        if self._is_terminal(collection, previous):
            for field, old_value in previous.items():
                if field != "revision" and candidate.get(field) != old_value:
                    raise ObserverProjectionV2Error(
                        "observer terminal lifecycle state is absorbing"
                    )
        if collection == "todo_sections":
            old_order = [item["id"] for item in previous["items"]]
            new_order = [item["id"] for item in candidate["items"]]
            if new_order[: len(old_order)] != old_order:
                raise ObserverProjectionV2Error(
                    "observer existing todo order must be retained"
                )
            old_items = {item["id"]: item for item in previous["items"]}
            for item in candidate["items"]:
                old = old_items.get(item["id"])
                if (
                    old is not None
                    and old["status"]
                    in {
                        "completed",
                        "cancelled",
                    }
                    and item != old
                ):
                    raise ObserverProjectionV2Error(
                        "observer terminal todo item state is absorbing"
                    )

    @staticmethod
    def _validate_subagent_tree(
        states: Mapping[tuple[str, str], Mapping[str, Any]],
    ) -> None:
        if len(states) > 128:
            raise ObserverProjectionV2Error("observer subagent tree exceeds 128 nodes")
        for key, state in states.items():
            parent = state.get("parent_subagent_id")
            if parent is not None and (key[0], parent) not in states:
                raise ObserverProjectionV2Error("observer subagent tree has an orphan")
            seen = {key[1]}
            depth = 1
            while parent is not None:
                if parent in seen:
                    raise ObserverProjectionV2Error(
                        "observer subagent tree has a cycle"
                    )
                seen.add(parent)
                depth += 1
                if depth > 8:
                    raise ObserverProjectionV2Error(
                        "observer subagent tree exceeds depth 8"
                    )
                parent = states[(key[0], parent)].get("parent_subagent_id")

    def _remember_digest(self, sequence: int, digest: str) -> None:
        if len(self._digest_order) >= 1024:
            oldest = self._digest_order.popleft()
            self._digests.pop(oldest, None)
        self._digest_order.append(sequence)
        self._digests[sequence] = digest


__all__ = ["ObserverProjectionV2", "ObserverProjectionV2Error"]
