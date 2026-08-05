"""Connector domain types and immutable rules."""

from hermes_connector.domain.contract_messages import (
    CloudEnvelope,
    LocalHello,
    LocalWelcome,
)
from hermes_connector.domain.state import (
    CONNECTOR_TRANSITIONS,
    ConnectorState,
    InvalidStateTransition,
    transition_connector,
)
from hermes_connector.domain.storage import (
    InboxPutResult,
    InboxRecord,
    OutboxRecord,
)

__all__ = [
    "CONNECTOR_TRANSITIONS",
    "CloudEnvelope",
    "ConnectorState",
    "InboxPutResult",
    "InboxRecord",
    "InvalidStateTransition",
    "LocalHello",
    "LocalWelcome",
    "OutboxRecord",
    "transition_connector",
]
