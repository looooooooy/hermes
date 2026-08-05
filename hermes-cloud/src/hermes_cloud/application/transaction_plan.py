"""Application orchestration for atomic fact, outbox, and audit writes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Generic, Protocol, TypeVar

from hermes_cloud.domain.persistence import (
    AuditEvent,
    OutboxEvent,
    TransactionContext,
)
from hermes_cloud.ports.persistence import (
    AuditPort,
    OutboxRepositoryPort,
    TransactionPort,
)

FactT = TypeVar("FactT")
ResultT = TypeVar("ResultT")


class _FactWriter(Protocol[FactT, ResultT]):
    def __call__(
        self,
        fact: FactT,
        *,
        deadline: datetime,
    ) -> ResultT: ...


@dataclass(frozen=True, slots=True)
class AtomicWritePlan(Generic[FactT]):
    """One database transaction's complete durable intent."""

    context: TransactionContext
    deadline: datetime
    fact: FactT
    outbox_event: OutboxEvent
    audit_event: AuditEvent

    def __post_init__(self) -> None:
        if self.deadline.utcoffset() is None:
            raise ValueError("transaction deadline must include a timezone")
        scope = (self.context.tenant_id, self.context.workspace_id)
        if scope != (
            self.outbox_event.tenant_id,
            self.outbox_event.workspace_id,
        ):
            raise ValueError("outbox event must share the transaction scope")
        if scope != (
            self.audit_event.tenant_id,
            self.audit_event.workspace_id,
        ):
            raise ValueError("audit event must share the transaction scope")
        if self.context.purpose != self.audit_event.purpose:
            raise ValueError("audit purpose must match the transaction purpose")


class AtomicWriteCoordinator:
    """Keep business fact, outbox, and audit writes in one transaction."""

    def __init__(
        self,
        *,
        transactions: TransactionPort,
        outbox: OutboxRepositoryPort,
        audit: AuditPort,
    ) -> None:
        self._transactions = transactions
        self._outbox = outbox
        self._audit = audit

    def execute(
        self,
        plan: AtomicWritePlan[FactT],
        *,
        write_fact: _FactWriter[FactT, ResultT],
    ) -> ResultT:
        """Write fact, outbox, and audit before allowing a single commit.

        ``write_fact`` must only use the ambient database transaction. Network
        calls and other irreversible effects do not belong in this operation.
        """

        def operation() -> ResultT:
            result = write_fact(plan.fact, deadline=plan.deadline)
            self._outbox.append(
                plan.outbox_event,
                deadline=plan.deadline,
            )
            self._audit.append(
                plan.audit_event,
                deadline=plan.deadline,
            )
            return result

        return self._transactions.run(
            context=plan.context,
            deadline=plan.deadline,
            operation=operation,
        )
