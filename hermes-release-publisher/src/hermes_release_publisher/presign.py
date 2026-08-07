"""Alibaba OSS V4 presigned download URLs for Hermes update artifacts.

This adapter is intentionally small. Release/update policy remains in Cloud and the client
never receives OSS credentials; it only receives the resulting short-lived bearer URL.
"""

from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import urlsplit

from .repository import PublisherError


class OssV4DownloadPresigner:
    def __init__(self, client: object, oss_module: object, *, bucket: str) -> None:
        if not bucket or len(bucket) > 63:
            raise PublisherError("OSS artifact bucket is invalid")
        self._client = client
        self._oss = oss_module
        self._bucket = bucket

    @classmethod
    def from_environment(
        cls,
        *,
        region: str,
        bucket: str,
        endpoint: str | None = None,
        use_cname: bool = False,
    ) -> "OssV4DownloadPresigner":
        if not region or region != region.strip():
            raise PublisherError("OSS region is invalid")
        try:
            import alibabacloud_oss_v2 as oss
        except ImportError as error:
            raise PublisherError("alibabacloud-oss-v2 is not installed") from error

        provider = oss.credentials.EnvironmentVariableCredentialsProvider()
        config = oss.config.load_default()
        config.credentials_provider = provider
        config.region = region
        if endpoint:
            parsed = urlsplit(endpoint)
            if parsed.scheme != "https" or parsed.hostname is None:
                raise PublisherError("OSS presign endpoint must be HTTPS")
            config.endpoint = endpoint
        if use_cname:
            if not endpoint:
                raise PublisherError("OSS CNAME presign requires an explicit HTTPS endpoint")
            config.use_cname = True
        return cls(oss.Client(config), oss, bucket=bucket)

    def presign_get(self, *, object_key: str, expires_at: datetime) -> str:
        if not _safe_object_key(object_key):
            raise PublisherError("OSS presign object key is invalid")
        if expires_at.tzinfo is None:
            raise PublisherError("OSS presign expiry must be timezone-aware")
        expiration = expires_at.astimezone(UTC)
        try:
            result = self._client.presign(
                self._oss.GetObjectRequest(bucket=self._bucket, key=object_key),
                expiration=expiration,
            )
        except Exception as error:  # noqa: BLE001 - SDK errors stay behind this adapter
            raise PublisherError("OSS presign failed") from error

        method = str(getattr(result, "method", "")).upper()
        url = str(getattr(result, "url", ""))
        signed_headers = dict(getattr(result, "signed_headers", None) or {})
        returned_expiration = getattr(result, "expiration", None)
        if method != "GET":
            raise PublisherError("OSS presign result must authorize GET")
        if signed_headers:
            raise PublisherError("OSS presign result unexpectedly requires signed headers")
        if returned_expiration is not None:
            if returned_expiration.tzinfo is None:
                raise PublisherError("OSS presign result expiry must be timezone-aware")
            if returned_expiration.astimezone(UTC) != expiration:
                raise PublisherError("OSS presign result expiry does not match request")
        _require_https_bearer_url(url)
        return url


def _safe_object_key(value: str) -> bool:
    if not value or len(value) > 1024 or value.startswith("/") or "\\" in value:
        return False
    parts = value.split("/")
    return all(
        part not in {"", ".", ".."}
        and len(part) <= 255
        and all(character.isalnum() or character in "._+-" for character in part)
        for part in parts
    )


def _require_https_bearer_url(url: str) -> None:
    if not url or len(url) > 4096:
        raise PublisherError("OSS presign URL is empty or oversized")
    try:
        parsed = urlsplit(url)
    except ValueError as error:
        raise PublisherError("OSS presign URL is malformed") from error
    if (
        parsed.scheme != "https"
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise PublisherError("OSS presign URL must be credential-free HTTPS")
