"""Pure data contracts for Hermes Cloud release/update decisions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Mapping


class UpdateDecisionStatusV1(StrEnum):
    UP_TO_DATE = "up_to_date"
    AVAILABLE = "available"
    MANDATORY = "mandatory"
    DEFERRED = "deferred"
    INELIGIBLE = "ineligible"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class ReleaseArtifactRefV1:
    kind: str
    object_key: str
    sha256: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class DownloadGrantV1:
    object_key: str
    sha256: str
    size_bytes: int
    url: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class DeviceUpdateContextV1:
    device_id: str
    organization_id: str
    target: str
    os_version: str
    active_release_id: str | None
    active_release_generation: int
    highest_release_generation: int
    requested_channel: str
    enterprise_pin_release_id: str | None = None


@dataclass(frozen=True, slots=True)
class ReleaseUpdateCandidateV1:
    release_id: str
    product_version: str
    release_generation: int
    channel: str
    channel_generation: int
    target: str
    minimum_os: str
    rollout_basis_points: int
    minimum_safe_release_generation: int
    security_critical: bool
    mandatory_after: datetime | None
    rollback_authorized: bool
    blocked: bool
    artifacts: tuple[ReleaseArtifactRefV1, ...]
    release_envelope: Mapping[str, object]
    channel_envelope: Mapping[str, object]
    block_envelope: Mapping[str, object]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "release_envelope",
            MappingProxyType(dict(self.release_envelope)),
        )
        object.__setattr__(
            self,
            "channel_envelope",
            MappingProxyType(dict(self.channel_envelope)),
        )
        object.__setattr__(
            self,
            "block_envelope",
            MappingProxyType(dict(self.block_envelope)),
        )


@dataclass(frozen=True, slots=True)
class UpdateDecisionV1:
    status: UpdateDecisionStatusV1
    reason_code: str
    release_id: str | None
    product_version: str | None
    release_generation: int | None
    channel: str
    rollout_bucket: int | None
    mandatory: bool
    release_envelope: Mapping[str, object] | None
    channel_envelope: Mapping[str, object] | None
    block_envelope: Mapping[str, object] | None
    download_grants: tuple[DownloadGrantV1, ...]
