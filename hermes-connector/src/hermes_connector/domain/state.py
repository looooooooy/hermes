from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType
from typing import Final


class ConnectorState(StrEnum):
    INSTALLED = "installed"
    WAITING_FOR_AGENT = "waiting_for_agent"
    UNPAIRED = "unpaired"
    PAIRING = "pairing"
    CLOUD_CONNECTING = "cloud_connecting"
    AGENT_DISCOVERING = "agent_discovering"
    RECONCILING = "reconciling"
    READY = "ready"
    DRAINING = "draining"
    DEGRADED = "degraded"
    AGENT_UNAVAILABLE = "agent_unavailable"
    STOPPED = "stopped"
    REVOKED = "revoked"


class InvalidStateTransition(ValueError):
    def __init__(self, source: ConnectorState, target: ConnectorState) -> None:
        super().__init__(
            f"connector transition not allowed: {source.value} -> {target.value}"
        )
        self.source = source
        self.target = target


# [*]                 -> INSTALLED
# INSTALLED           -> WAITING_FOR_AGENT  (Agent absent)
# INSTALLED           -> UNPAIRED           (Agent detected)
# WAITING_FOR_AGENT   -> UNPAIRED           (Agent appears)
# UNPAIRED            -> PAIRING            (User starts pairing)
# PAIRING             -> CLOUD_CONNECTING   (Device activated)
# PAIRING             -> UNPAIRED           (Expired or rejected)
# CLOUD_CONNECTING    -> AGENT_DISCOVERING  (Cloud ready)
# AGENT_DISCOVERING   -> RECONCILING        (Local Gateway ready)
# AGENT_DISCOVERING   -> AGENT_UNAVAILABLE  (No compatible Plugin)
# RECONCILING         -> READY              (State reconciled)
# READY               -> DRAINING           (Update or controlled stop)
# READY               -> DEGRADED           (Partial dependency failure)
# READY               -> AGENT_UNAVAILABLE  (Agent stopped)
# AGENT_UNAVAILABLE   -> RECONCILING        (New runtime appears)
# DEGRADED            -> RECONCILING        (Dependency restored)
# DRAINING            -> STOPPED
# READY               -> REVOKED            (Device revoked)
# REVOKED             -> UNPAIRED           (Local cleanup complete)
#
# Complete immutable transition table for the Connector domain state machine.
CONNECTOR_TRANSITIONS: Final[Mapping[ConnectorState, frozenset[ConnectorState]]] = (
    MappingProxyType(
        {
            ConnectorState.INSTALLED: frozenset(
                {
                    ConnectorState.WAITING_FOR_AGENT,
                    ConnectorState.UNPAIRED,
                }
            ),
            ConnectorState.WAITING_FOR_AGENT: frozenset({ConnectorState.UNPAIRED}),
            ConnectorState.UNPAIRED: frozenset({ConnectorState.PAIRING}),
            ConnectorState.PAIRING: frozenset(
                {
                    ConnectorState.CLOUD_CONNECTING,
                    ConnectorState.UNPAIRED,
                }
            ),
            ConnectorState.CLOUD_CONNECTING: frozenset(
                {ConnectorState.AGENT_DISCOVERING}
            ),
            ConnectorState.AGENT_DISCOVERING: frozenset(
                {
                    ConnectorState.RECONCILING,
                    ConnectorState.AGENT_UNAVAILABLE,
                }
            ),
            ConnectorState.RECONCILING: frozenset({ConnectorState.READY}),
            ConnectorState.READY: frozenset(
                {
                    ConnectorState.DRAINING,
                    ConnectorState.DEGRADED,
                    ConnectorState.AGENT_UNAVAILABLE,
                    ConnectorState.REVOKED,
                }
            ),
            ConnectorState.DRAINING: frozenset({ConnectorState.STOPPED}),
            ConnectorState.DEGRADED: frozenset({ConnectorState.RECONCILING}),
            ConnectorState.AGENT_UNAVAILABLE: frozenset({ConnectorState.RECONCILING}),
            ConnectorState.STOPPED: frozenset(),
            ConnectorState.REVOKED: frozenset({ConnectorState.UNPAIRED}),
        }
    )
)


def transition_connector(
    source: ConnectorState,
    target: ConnectorState,
) -> ConnectorState:
    if target not in CONNECTOR_TRANSITIONS[source]:
        raise InvalidStateTransition(source, target)
    return target
