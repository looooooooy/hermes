from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType
from typing import Final


class CloudSessionState(StrEnum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    NEGOTIATING = "negotiating"
    ACTIVE = "active"
    RECONCILING = "reconciling"
    DRAINING = "draining"


class InvalidCloudSessionTransition(ValueError):
    def __init__(
        self,
        source: CloudSessionState,
        target: CloudSessionState,
    ) -> None:
        super().__init__(
            f"cloud session transition not allowed: {source.value} -> {target.value}"
        )
        self.source = source
        self.target = target


# DISCONNECTED
#      |
#      v
# CONNECTING -----------> DISCONNECTED
#      |
#      v
# NEGOTIATING ----------> DISCONNECTED
#      | \
#      |  +------------> RECONCILING ----------> DISCONNECTED
#      |                    |  \                       ^
#      v                    |   +-------> DRAINING ----+
# ACTIVE <------------------+                ^
#      | \                                   |
#      |  +----------------------------------+
#      +-------------------> RECONCILING
#      +-------------------> DISCONNECTED
CLOUD_SESSION_TRANSITIONS: Final[
    Mapping[CloudSessionState, frozenset[CloudSessionState]]
] = MappingProxyType(
    {
        CloudSessionState.DISCONNECTED: frozenset({CloudSessionState.CONNECTING}),
        CloudSessionState.CONNECTING: frozenset(
            {
                CloudSessionState.NEGOTIATING,
                CloudSessionState.DISCONNECTED,
            }
        ),
        CloudSessionState.NEGOTIATING: frozenset(
            {
                CloudSessionState.ACTIVE,
                CloudSessionState.RECONCILING,
                CloudSessionState.DISCONNECTED,
            }
        ),
        CloudSessionState.ACTIVE: frozenset(
            {
                CloudSessionState.RECONCILING,
                CloudSessionState.DRAINING,
                CloudSessionState.DISCONNECTED,
            }
        ),
        CloudSessionState.RECONCILING: frozenset(
            {
                CloudSessionState.ACTIVE,
                CloudSessionState.DRAINING,
                CloudSessionState.DISCONNECTED,
            }
        ),
        CloudSessionState.DRAINING: frozenset({CloudSessionState.DISCONNECTED}),
    }
)


def transition_cloud_session(
    source: CloudSessionState,
    target: CloudSessionState,
) -> CloudSessionState:
    if target not in CLOUD_SESSION_TRANSITIONS[source]:
        raise InvalidCloudSessionTransition(source, target)
    return target
