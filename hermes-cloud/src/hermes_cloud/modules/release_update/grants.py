"""Cloud-side short-lived download grant issuance.

This layer never receives OSS AccessKey/Secret values. It depends on a presign port and
binds the returned bearer URL back to the already signed artifact identity.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

from .domain import DownloadGrantV1, ReleaseArtifactRefV1
from .ports import PresignDownloadPort
from .service import UpdateCheckPolicyError, UpdateCheckUnavailable

_DEFAULT_TTL = timedelta(minutes=10)
_MAX_TTL = timedelta(minutes=20)
_MIN_TTL = timedelta(seconds=30)


class ShortLivedDownloadGrantIssuer:
    def __init__(
        self,
        presigner: PresignDownloadPort,
        *,
        ttl: timedelta = _DEFAULT_TTL,
    ) -> None:
        if ttl < _MIN_TTL or ttl > _MAX_TTL:
            raise ValueError("download URL TTL must be between 30 seconds and 20 minutes")
        self._presigner = presigner
        self._ttl = ttl

    def issue_grant(
        self,
        *,
        device_id: str,
        artifact: ReleaseArtifactRefV1,
        now: datetime,
    ) -> DownloadGrantV1:
        observed_now = _utc(now)
        if not _safe_device_id(device_id):
            raise UpdateCheckPolicyError("invalid device identity for download authorization")
        expires_at = observed_now + self._ttl
        try:
            url = self._presigner.presign_get(
                object_key=artifact.object_key,
                expires_at=expires_at,
            )
        except Exception as exc:
            raise UpdateCheckUnavailable("download presign backend is unavailable") from exc
        _validate_presigned_url(url)
        return DownloadGrantV1(
            object_key=artifact.object_key,
            sha256=artifact.sha256,
            size_bytes=artifact.size_bytes,
            url=url,
            expires_at=expires_at,
        )


def _validate_presigned_url(url: str) -> None:
    if not isinstance(url, str) or not url or len(url) > 4096:
        raise UpdateCheckUnavailable("download URL is empty or oversized")
    try:
        parsed = urlsplit(url)
    except ValueError as exc:
        raise UpdateCheckUnavailable("download URL is malformed") from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise UpdateCheckUnavailable("download URL must be credential-free HTTPS")


def _safe_device_id(value: str) -> bool:
    return bool(value) and len(value) <= 160 and all(
        character.isalnum() or character in ".:_+-" for character in value
    )


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise UpdateCheckPolicyError("download authorization time must be timezone-aware")
    return value.astimezone(UTC)
