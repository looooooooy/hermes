from __future__ import annotations

from dataclasses import dataclass

import pytest

from hermes_agent_plugin.application.update_safety import (
    HostUpdateSafetyAggregator,
    UpdateSafetyViolation,
)


@dataclass
class _Binding:
    profile: str = "default"
    generation: str = "generation-42"
    ready: bool = True

    def snapshot(self) -> tuple[str, str, bool]:
        return self.profile, self.generation, self.ready


class _Host:
    def __init__(self, pages: list[dict], snapshots: dict[str, dict]) -> None:
        self._pages = iter(pages)
        self._snapshots = snapshots

    def session_catalog(self, _request: object) -> object:
        return next(self._pages)

    def control_snapshot(self, scope: object) -> object:
        assert isinstance(scope, dict)
        return self._snapshots[scope["durable_session_key"]]


def _entry(session_key: str, *actions: str) -> dict:
    return {
        "profile": "default",
        "durable_session_key": session_key,
        "runtime_generation": "generation-42",
        "available_actions": frozenset(actions),
    }


def _snapshot(session_key: str, payload: dict) -> dict:
    return {
        "profile": "default",
        "durable_session_key": session_key,
        "runtime_generation": "generation-42",
        "control_revision": 7,
        "payload": payload,
    }


def _page(*sessions: dict, revision: int = 9, cursor: str | None = None) -> dict:
    return {
        "profile": "default",
        "runtime_generation": "generation-42",
        "catalog_revision": revision,
        "sessions": sessions,
        "next_cursor": cursor,
    }


def _aggregator(host: object, binding: _Binding | None = None) -> HostUpdateSafetyAggregator:
    return HostUpdateSafetyAggregator(
        host=host,
        binding=binding or _Binding(),
        session_catalog_request=lambda **value: value,
        control_scope=lambda **value: value,
    )


def test_aggregates_paginated_authoritative_counts_without_session_identity() -> None:
    host = _Host(
        [
            _page(
                _entry("session-a", "approval.respond", "clarify.respond"),
                cursor="page-2",
            ),
            _page(_entry("session-b", "approval.respond", "clarify.respond")),
        ],
        {
            "session-a": _snapshot(
                "session-a",
                {
                    "status": "running",
                    "pending_request_ids": ["approval-a"],
                    "pending_interaction_counts": {"approval": 1, "clarify": 0},
                },
            ),
            "session-b": _snapshot(
                "session-b",
                {
                    "status": "waiting",
                    "pending_request_ids": ["clarify-a", "clarify-b"],
                    "pending_interaction_counts": {"approval": 0, "clarify": 2},
                },
            ),
        },
    )

    payload = _aggregator(host).snapshot().payload()

    assert payload == {
        "schema_version": 1,
        "profile": "default",
        "runtime_generation": "generation-42",
        "active_tasks": 1,
        "pending_approvals": 1,
        "pending_clarifications": 2,
        "evidence_complete": True,
    }
    assert "session-a" not in repr(payload)
    assert "approval-a" not in repr(payload)


def test_legacy_core_empty_pending_ids_are_exactly_safe() -> None:
    host = _Host(
        [_page(_entry("session-a", "approval.respond", "clarify.respond"))],
        {
            "session-a": _snapshot(
                "session-a",
                {"status": "waiting", "pending_request_ids": []},
            )
        },
    )

    snapshot = _aggregator(host).snapshot()

    assert snapshot.pending_approvals == 0
    assert snapshot.pending_clarifications == 0
    assert snapshot.evidence_complete is True


def test_legacy_core_opaque_pending_ids_fail_closed_without_guessing_kind() -> None:
    host = _Host(
        [_page(_entry("session-a", "approval.respond", "clarify.respond"))],
        {
            "session-a": _snapshot(
                "session-a",
                {"status": "waiting", "pending_request_ids": ["opaque-a"]},
            )
        },
    )

    with pytest.raises(UpdateSafetyViolation, match="kinds are unavailable"):
        _aggregator(host).snapshot()


def test_session_without_pending_actions_does_not_require_pending_fields() -> None:
    host = _Host(
        [_page(_entry("session-acp", "session.interrupt", "session.steer"))],
        {"session-acp": _snapshot("session-acp", {"status": "running"})},
    )

    snapshot = _aggregator(host).snapshot()

    assert snapshot.active_tasks == 1
    assert snapshot.pending_approvals == 0
    assert snapshot.pending_clarifications == 0


def test_catalog_revision_change_fails_closed() -> None:
    host = _Host(
        [
            _page(_entry("session-a"), revision=1, cursor="page-2"),
            _page(_entry("session-b"), revision=2),
        ],
        {
            "session-a": _snapshot("session-a", {"status": "waiting"}),
            "session-b": _snapshot("session-b", {"status": "waiting"}),
        },
    )

    with pytest.raises(UpdateSafetyViolation, match="revision changed"):
        _aggregator(host).snapshot()


def test_runtime_generation_rollover_during_capture_fails_closed() -> None:
    binding = _Binding()

    class _RollingHost(_Host):
        def control_snapshot(self, scope: object) -> object:
            result = super().control_snapshot(scope)
            binding.generation = "generation-43"
            return result

    host = _RollingHost(
        [_page(_entry("session-a"))],
        {"session-a": _snapshot("session-a", {"status": "waiting"})},
    )

    with pytest.raises(UpdateSafetyViolation, match="generation changed"):
        _aggregator(host, binding).snapshot()


def test_exact_pending_counts_must_match_opaque_id_cardinality() -> None:
    host = _Host(
        [_page(_entry("session-a", "approval.respond"))],
        {
            "session-a": _snapshot(
                "session-a",
                {
                    "status": "waiting",
                    "pending_request_ids": ["approval-a"],
                    "pending_interaction_counts": {"approval": 0, "clarify": 0},
                },
            )
        },
    )

    with pytest.raises(UpdateSafetyViolation, match="evidence disagrees"):
        _aggregator(host).snapshot()
