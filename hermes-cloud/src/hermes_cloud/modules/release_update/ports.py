"""Ports for Hermes Cloud release-update policy decisions."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from .domain import (
    DeviceUpdateContextV1,
    DownloadGrantV1,
    ReleaseArtifactRefV1,
    ReleaseUpdateCandidateV1,
)


class ReleaseCatalogPort(Protocol):
    def select_candidate(
        self,
        context: DeviceUpdateContextV1,
    ) -> ReleaseUpdateCandidateV1 | None: ...


class ReleaseControlReaderPort(Protocol):
    """Read one bounded release-control object from trusted server-side storage."""

    def read_control_object(self, object_key: str) -> bytes: ...


class DownloadGrantIssuerPort(Protocol):
    def issue_grant(
        self,
        *,
        device_id: str,
        artifact: ReleaseArtifactRefV1,
        now: datetime,
    ) -> DownloadGrantV1: ...


class PresignDownloadPort(Protocol):
    """Issue one bearer download URL without exposing storage credentials."""

    def presign_get(
        self,
        *,
        object_key: str,
        expires_at: datetime,
    ) -> str: ...


class OsCompatibilityPort(Protocol):
    def is_compatible(
        self,
        *,
        target: str,
        current_os: str,
        minimum_os: str,
    ) -> bool: ...
