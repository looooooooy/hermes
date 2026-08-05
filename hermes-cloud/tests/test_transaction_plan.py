from __future__ import annotations

import inspect
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest

from hermes_cloud.application.transaction_plan import (
    AtomicWriteCoordinator,
    AtomicWritePlan,
)
from hermes_cloud.domain.persistence import (
    ALLOWED_COMMAND_TRANSITIONS,
    AuditEvent,
    CommandState,
    InboxDecision,
    InboxDigestConflict,
    InvalidCommandTransition,
    OutboxEvent,
    TransactionContext,
    classify_inbox_delivery,
    require_command_transition,
)
from hermes_cloud.ports.persistence import (
    AuditPort,
    InboxRepositoryPort,
    OutboxRepositoryPort,
    TransactionPort,
)

TENANT_ID = UUID("11111111-1111-4111-8111-111111111111")
WORKSPACE_ID = UUID("22222222-2222-4222-8222-222222222222")
EVENT_ID = UUID("33333333-3333-4333-8333-333333333333")
SUBJECT_ID = UUID("44444444-4444-4444-8444-444444444444")
NOW = datetime(2026, 7, 30, tzinfo=UTC)
DEADLINE = NOW + timedelta(seconds=5)
DIGEST = "a" * 64


def _context() -> TransactionContext:
    return TransactionContext(
        tenant_id=TENANT_ID,
        workspace_id=WORKSPACE_ID,
        purpose="command.dispatch",
    )


def _outbox() -> OutboxEvent:
    return OutboxEvent(
        tenant_id=TENANT_ID,
        workspace_id=WORKSPACE_ID,
        event_id=EVENT_ID,
        aggregate_type="command",
        aggregate_id=SUBJECT_ID,
        event_type="command.dispatched",
        payload={"state": "dispatched"},
    )


def _audit() -> AuditEvent:
    return AuditEvent(
        tenant_id=TENANT_ID,
        workspace_id=WORKSPACE_ID,
        audit_event_id=EVENT_ID,
        purpose="command.dispatch",
        action="command.dispatch",
        subject_type="command",
        subject_id=SUBJECT_ID,
        outcome="accepted",
        details={"state": "dispatched"},
    )


@pytest.mark.parametrize(
    "port",
    (TransactionPort, InboxRepositoryPort, OutboxRepositoryPort, AuditPort),
)
def test_port_contracts_document_deadline_idempotency_and_side_effects(
    port: type[object],
) -> None:
    contract = inspect.getdoc(port)
    assert contract is not None
    normalized = contract.lower()
    assert "deadline" in normalized
    assert "idempoten" in normalized
    assert "side effect" in normalized


def test_inbox_same_id_and_digest_is_idempotent_but_digest_change_conflicts() -> None:
    assert classify_inbox_delivery(None, DIGEST) is InboxDecision.ACCEPT
    assert classify_inbox_delivery(DIGEST, DIGEST) is InboxDecision.DUPLICATE

    with pytest.raises(InboxDigestConflict):
        classify_inbox_delivery(DIGEST, "b" * 64)


@pytest.mark.parametrize("digest", ("A" * 64, "a" * 63, "not-a-digest"))
def test_inbox_rejects_noncanonical_sha256_digest(digest: str) -> None:
    with pytest.raises(ValueError):
        classify_inbox_delivery(None, digest)


def test_command_transition_table_matches_documented_state_machine() -> None:
    assert ALLOWED_COMMAND_TRANSITIONS == {
        CommandState.QUEUED: frozenset(
            {CommandState.DISPATCHED, CommandState.CANCELLED}
        ),
        CommandState.DISPATCHED: frozenset(
            {
                CommandState.RUNNING,
                CommandState.FAILED,
                CommandState.CANCELLED,
            }
        ),
        CommandState.RUNNING: frozenset(
            {
                CommandState.SUCCEEDED,
                CommandState.FAILED,
                CommandState.CANCELLED,
            }
        ),
        CommandState.SUCCEEDED: frozenset(),
        CommandState.FAILED: frozenset(),
        CommandState.CANCELLED: frozenset(),
    }
    require_command_transition(CommandState.QUEUED, CommandState.DISPATCHED)

    with pytest.raises(InvalidCommandTransition):
        require_command_transition(CommandState.SUCCEEDED, CommandState.RUNNING)

    documentation = inspect.getdoc(require_command_transition)
    assert documentation is not None
    assert "queued" in documentation
    assert "-->" in documentation
    assert "Allowed transitions" in documentation


class _RecordingTransaction:
    def __init__(self, calls: list[object]) -> None:
        self.calls = calls

    def run(
        self,
        *,
        context: TransactionContext,
        deadline: datetime,
        operation: Callable[[], Any],
    ) -> Any:
        self.calls.append(("begin", context, deadline))
        try:
            result = operation()
        except BaseException:
            self.calls.append("rollback")
            raise
        self.calls.append("commit")
        return result


class _RecordingOutbox:
    def __init__(
        self,
        calls: list[object],
        *,
        failure: Exception | None = None,
    ) -> None:
        self.calls = calls
        self.failure = failure

    def append(self, event: OutboxEvent, *, deadline: datetime) -> None:
        self.calls.append(("outbox", event, deadline))
        if self.failure is not None:
            raise self.failure


class _RecordingAudit:
    def __init__(self, calls: list[object]) -> None:
        self.calls = calls

    def append(self, event: AuditEvent, *, deadline: datetime) -> None:
        self.calls.append(("audit", event, deadline))


def test_atomic_write_orders_fact_outbox_audit_then_commit() -> None:
    calls: list[object] = []
    plan = AtomicWritePlan(
        context=_context(),
        deadline=DEADLINE,
        fact={"command_id": str(SUBJECT_ID), "state": "dispatched"},
        outbox_event=_outbox(),
        audit_event=_audit(),
    )
    coordinator = AtomicWriteCoordinator(
        transactions=_RecordingTransaction(calls),
        outbox=_RecordingOutbox(calls),
        audit=_RecordingAudit(calls),
    )

    def write_fact(fact: object, *, deadline: datetime) -> str:
        calls.append(("fact", fact, deadline))
        return "written"

    assert coordinator.execute(plan, write_fact=write_fact) == "written"
    assert [call if isinstance(call, str) else call[0] for call in calls] == [
        "begin",
        "fact",
        "outbox",
        "audit",
        "commit",
    ]


def test_atomic_write_rolls_back_and_stops_after_outbox_failure() -> None:
    calls: list[object] = []
    failure = RuntimeError("outbox write failed")
    plan = AtomicWritePlan(
        context=_context(),
        deadline=DEADLINE,
        fact={"command_id": str(SUBJECT_ID), "state": "dispatched"},
        outbox_event=_outbox(),
        audit_event=_audit(),
    )
    coordinator = AtomicWriteCoordinator(
        transactions=_RecordingTransaction(calls),
        outbox=_RecordingOutbox(calls, failure=failure),
        audit=_RecordingAudit(calls),
    )

    def write_fact(fact: object, *, deadline: datetime) -> None:
        calls.append(("fact", fact, deadline))

    with pytest.raises(RuntimeError, match="outbox write failed"):
        coordinator.execute(plan, write_fact=write_fact)

    assert [call if isinstance(call, str) else call[0] for call in calls] == [
        "begin",
        "fact",
        "outbox",
        "rollback",
    ]
