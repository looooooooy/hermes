from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Final

DISCOVERY_DESCRIPTOR_VERSION: Final = 2
DISCOVERY_DESCRIPTOR_FIELDS: Final = frozenset(
    {
        "version",
        "pid",
        "profile",
        "runtime_generation",
        "socket_path",
        "instance_id",
        "process_start_time_ns",
        "process_executable",
        "process_executable_device",
        "process_executable_inode",
        "host_bundle_id",
    }
)


@dataclass(frozen=True, slots=True)
class ProcessIdentityEvidence:
    start_time_ns: int
    executable_path: Path
    executable_device: int
    executable_inode: int


@dataclass(frozen=True, slots=True)
class AgentEndpoint:
    pid: int
    profile: str
    socket_path: Path
    instance_id: str
    runtime_generation: str
    host_bundle_id: str
    process_identity: ProcessIdentityEvidence
    socket_device: int
    socket_inode: int
    registry_path: Path


@dataclass(frozen=True, slots=True)
class LocalRuntimeAuthority:
    """Runtime identity and capabilities negotiated with the local Hermes Core."""

    profile: str
    runtime_generation: str
    instance_id: str
    host_bundle_id: str
    process_identity: ProcessIdentityEvidence
    required_capabilities: tuple[str, ...]
    optional_capabilities: tuple[str, ...]


class LocalGatewayState(StrEnum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    NEGOTIATING = "negotiating"
    ACTIVE = "active"
    RECONCILING = "reconciling"
    DRAINING = "draining"


class InvalidLocalGatewayTransition(ValueError):
    def __init__(self, source: LocalGatewayState, target: LocalGatewayState) -> None:
        super().__init__(
            f"local gateway transition not allowed: {source.value} -> {target.value}"
        )
        self.source = source
        self.target = target


# Frozen SESSION_PROTOCOL v1:
#
# DISCONNECTED -> CONNECTING -> NEGOTIATING -> ACTIVE -> DRAINING
#       ^              |              |           |          |
#       |              |              v           v          |
#       +--------------+-------- RECONCILING -----+----------+
LOCAL_GATEWAY_TRANSITIONS: Final[
    Mapping[LocalGatewayState, frozenset[LocalGatewayState]]
] = MappingProxyType(
    {
        LocalGatewayState.DISCONNECTED: frozenset({LocalGatewayState.CONNECTING}),
        LocalGatewayState.CONNECTING: frozenset(
            {
                LocalGatewayState.NEGOTIATING,
                LocalGatewayState.DISCONNECTED,
            }
        ),
        LocalGatewayState.NEGOTIATING: frozenset(
            {
                LocalGatewayState.ACTIVE,
                LocalGatewayState.RECONCILING,
                LocalGatewayState.DISCONNECTED,
            }
        ),
        LocalGatewayState.ACTIVE: frozenset(
            {
                LocalGatewayState.RECONCILING,
                LocalGatewayState.DRAINING,
                LocalGatewayState.DISCONNECTED,
            }
        ),
        LocalGatewayState.RECONCILING: frozenset(
            {
                LocalGatewayState.ACTIVE,
                LocalGatewayState.DRAINING,
                LocalGatewayState.DISCONNECTED,
            }
        ),
        LocalGatewayState.DRAINING: frozenset({LocalGatewayState.DISCONNECTED}),
    }
)


def transition_local_gateway(
    source: LocalGatewayState,
    target: LocalGatewayState,
) -> LocalGatewayState:
    if target not in LOCAL_GATEWAY_TRANSITIONS[source]:
        raise InvalidLocalGatewayTransition(source, target)
    return target
