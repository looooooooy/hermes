"""Stable persistence ports for future PostgreSQL adapters."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Protocol, TypeVar

from hermes_cloud.domain.persistence import (
    AuditEvent,
    InboxDecision,
    InboxMessage,
    OutboxEvent,
    TransactionContext,
)

ResultT = TypeVar("ResultT")


class TransactionPort(Protocol):
    """Own the database transaction side effect boundary.

    Deadline: begin, context setup, operation, and commit must all finish before
    the supplied deadline. Idempotency: the adapter must not retry the callable;
    retry belongs to a use case with an explicit idempotency key. Side effects:
    set tenant, workspace, and purpose as transaction-local values, commit only
    after success, and roll back every database write on any exception.
    """

    def run(
        self,
        *,
        context: TransactionContext,
        deadline: datetime,
        operation: Callable[[], ResultT],
    ) -> ResultT:
        """Run one callback in one scoped transaction."""
        ...


class InboxRepositoryPort(Protocol):
    """Persist an inbox claim without crossing the database side effect boundary.

    Deadline: the atomic insert-or-read must finish before the supplied
    deadline. Idempotency: the same tenant/message id and digest returns
    ``DUPLICATE``; a different digest raises ``InboxDigestConflict``. Side
    effects: only the inbox row may change, and no handler or external message
    may run from this port.
    """

    def record(
        self,
        message: InboxMessage,
        *,
        deadline: datetime,
    ) -> InboxDecision:
        """Atomically insert or classify one inbox message."""
        ...


class OutboxRepositoryPort(Protocol):
    """Append an outbox row inside the caller's database side effect boundary.

    Deadline: the write must finish before the supplied deadline. Idempotency:
    tenant/event id uniquely identifies an append and an exact retry is a
    no-op. Side effects: persist only the outbox row; publishing to a broker or
    network is explicitly outside this port.
    """

    def append(
        self,
        event: OutboxEvent,
        *,
        deadline: datetime,
    ) -> None:
        """Append one event using the ambient transaction."""
        ...


class AuditPort(Protocol):
    """Append an immutable audit row within the database side effect boundary.

    Deadline: the append must finish before the supplied deadline. Idempotency:
    tenant/audit event id uniquely identifies the record and an exact retry is
    a no-op. Side effects: append only; update, delete, remote logging, and
    notification are outside this port.
    """

    def append(
        self,
        event: AuditEvent,
        *,
        deadline: datetime,
    ) -> None:
        """Append one audit event using the ambient transaction."""
        ...
