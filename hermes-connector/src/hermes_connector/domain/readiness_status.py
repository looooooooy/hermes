"""Non-secret cross-process Connector readiness receipt contract."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final

from hermes_connector.domain.cloud_session import CloudSessionState
from hermes_connector.domain.local_gateway import ProcessIdentityEvidence

STATUS_RECEIPT_FIELDS: Final = frozenset(
    {
        "release_id",
        "pid",
        "process_start_time_ns",
        "process_executable",
        "process_executable_device",
        "process_executable_inode",
        "runtime_generation",
        "local_authority_identity",
        "cloud_state",
        "updated_at",
        "ready",
    }
)
LOCAL_AUTHORITY_IDENTITY_FIELDS: Final = frozenset(
    {"profile", "instance_id", "host_bundle_id"}
)
ACTIVATION_LOCAL_CAPABILITIES: Final = frozenset(
    {"session.observe", "session.control", "session.catalog.v1"}
)
STATUS_RECEIPT_TTL_SECONDS: Final = 30.0
STATUS_RECEIPT_FUTURE_SKEW_SECONDS: Final = 5.0
_RELEASE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


def validate_release_id(value: object) -> str:
    if not isinstance(value, str) or _RELEASE_ID.fullmatch(value) is None:
        raise ValueError("Connector release id is invalid")
    return value


@dataclass(frozen=True, slots=True)
class LocalAuthorityIdentity:
    profile: str
    instance_id: str
    host_bundle_id: str


@dataclass(frozen=True, slots=True)
class ConnectorStatusReceipt:
    release_id: str
    pid: int
    process_identity: ProcessIdentityEvidence
    runtime_generation: str
    local_authority_identity: LocalAuthorityIdentity
    cloud_state: CloudSessionState
    updated_at: datetime
    ready: bool

    @property
    def executable_path(self) -> Path:
        return self.process_identity.executable_path
