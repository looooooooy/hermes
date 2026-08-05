"""Pure domain values for PostgreSQL-backed persistence boundaries."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from re import compile as compile_pattern
from types import MappingProxyType
from typing import Final
from uuid import UUID


class CommandState(str, Enum):
    QUEUED = "queued"
    DISPATCHED = "dispatched"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


ALLOWED_COMMAND_TRANSITIONS: Final[Mapping[CommandState, frozenset[CommandState]]] = (
    MappingProxyType(
        {
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
    )
)


class InvalidCommandTransition(ValueError):
    """Raised when a command state edge is absent from the frozen graph."""


def require_command_transition(
    from_state: CommandState,
    to_state: CommandState,
) -> None:
    """Validate a frozen command state change.

    ASCII state graph::

        queued --> dispatched --> running --> succeeded
          |             |            |
          +----------> cancelled <---+
                        ^   ^
        dispatched --> failed <--- running

    Allowed transitions::

        queued       | dispatched, cancelled
        dispatched   | running, failed, cancelled
        running      | succeeded, failed, cancelled
        succeeded    | none
        failed       | none
        cancelled    | none
    """

    if to_state not in ALLOWED_COMMAND_TRANSITIONS[from_state]:
        raise InvalidCommandTransition(
            f"command transition is not allowed: {from_state.value} -> {to_state.value}"
        )


class PairingSessionState(str, Enum):
    PENDING = "pending"
    CLAIMED = "claimed"
    CONFIRMED = "confirmed"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


ALLOWED_PAIRING_SESSION_TRANSITIONS: Final[
    Mapping[PairingSessionState, frozenset[PairingSessionState]]
] = MappingProxyType(
    {
        PairingSessionState.PENDING: frozenset(
            {
                PairingSessionState.CLAIMED,
                PairingSessionState.EXPIRED,
                PairingSessionState.CANCELLED,
            }
        ),
        PairingSessionState.CLAIMED: frozenset(
            {
                PairingSessionState.CONFIRMED,
                PairingSessionState.EXPIRED,
                PairingSessionState.CANCELLED,
            }
        ),
        PairingSessionState.CONFIRMED: frozenset(
            {
                PairingSessionState.EXPIRED,
                PairingSessionState.CANCELLED,
            }
        ),
        PairingSessionState.EXPIRED: frozenset(),
        PairingSessionState.CANCELLED: frozenset(),
    }
)


class InvalidPairingSessionTransition(ValueError):
    """Raised when a pairing state edge is absent from the frozen graph."""


def require_pairing_session_transition(
    from_state: PairingSessionState,
    to_state: PairingSessionState,
) -> None:
    """Validate a frozen pairing-session state change.

    ASCII state graph::

        pending --> claimed --> confirmed
           |          |            |
           +------> expired <------+
           |          |            |
           +------> cancelled <----+

    Allowed transitions::

        pending     | claimed, expired, cancelled
        claimed     | confirmed, expired, cancelled
        confirmed   | expired, cancelled
        expired     | none
        cancelled   | none
    """

    if to_state not in ALLOWED_PAIRING_SESSION_TRANSITIONS[from_state]:
        raise InvalidPairingSessionTransition(
            f"pairing transition is not allowed: {from_state.value} -> {to_state.value}"
        )


@dataclass(frozen=True, slots=True)
class TransactionContext:
    tenant_id: UUID
    workspace_id: UUID
    purpose: str

    def __post_init__(self) -> None:
        purpose_size = len(self.purpose.encode("utf-8"))
        if not self.purpose.strip() or purpose_size > 128:
            raise ValueError("purpose must contain 1 to 128 UTF-8 bytes")


@dataclass(frozen=True, slots=True)
class OutboxEvent:
    tenant_id: UUID
    workspace_id: UUID
    event_id: UUID
    aggregate_type: str
    aggregate_id: UUID
    event_type: str
    payload: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class AuditEvent:
    tenant_id: UUID
    workspace_id: UUID
    audit_event_id: UUID
    purpose: str
    action: str
    subject_type: str
    subject_id: UUID
    outcome: str
    details: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class InboxMessage:
    tenant_id: UUID
    workspace_id: UUID
    message_id: UUID
    digest: str
    message_type: str

    def __post_init__(self) -> None:
        _require_canonical_digest(self.digest)


class InboxDecision(str, Enum):
    ACCEPT = "accept"
    DUPLICATE = "duplicate"


class InboxDigestConflict(ValueError):
    """Raised when an inbox id is reused with different content."""


_CANONICAL_SHA256 = compile_pattern(r"^[0-9a-f]{64}$")


def _require_canonical_digest(digest: str) -> None:
    if _CANONICAL_SHA256.fullmatch(digest) is None:
        raise ValueError("digest must be a lowercase hexadecimal SHA-256")


def classify_inbox_delivery(
    existing_digest: str | None,
    incoming_digest: str,
) -> InboxDecision:
    """Apply the inbox idempotency rule after lookup by tenant and message id."""

    _require_canonical_digest(incoming_digest)
    if existing_digest is None:
        return InboxDecision.ACCEPT
    _require_canonical_digest(existing_digest)
    if existing_digest == incoming_digest:
        return InboxDecision.DUPLICATE
    raise InboxDigestConflict("inbox message id was reused with a new digest")
