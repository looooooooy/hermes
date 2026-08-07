from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from hermes_cloud.modules.release_update.domain import ReleaseArtifactRefV1
from hermes_cloud.modules.release_update.grants import ShortLivedDownloadGrantIssuer
from hermes_cloud.modules.release_update.service import (
    UpdateCheckPolicyError,
    UpdateCheckUnavailable,
)


class _Presigner:
    def __init__(self, url: str = "https://updates.example.test/a.bin?token=short") -> None:
        self.url = url
        self.calls: list[tuple[str, datetime]] = []

    def presign_get(self, *, object_key: str, expires_at: datetime) -> str:
        self.calls.append((object_key, expires_at))
        return self.url


def _artifact() -> ReleaseArtifactRefV1:
    return ReleaseArtifactRefV1(
        kind="managed-release",
        object_key="artifacts/v1/sha256/aa/" + "a" * 64 + "/payload.bin",
        sha256="a" * 64,
        size_bytes=4096,
    )


def test_grant_issuer_binds_presign_to_signed_artifact_and_ten_minute_ttl() -> None:
    presigner = _Presigner()
    issuer = ShortLivedDownloadGrantIssuer(presigner)
    now = datetime(2026, 8, 7, 16, 0, tzinfo=UTC)

    grant = issuer.issue_grant(
        device_id="77777777-7777-4777-8777-777777777777",
        artifact=_artifact(),
        now=now,
    )

    assert presigner.calls == [(_artifact().object_key, now + timedelta(minutes=10))]
    assert grant.object_key == _artifact().object_key
    assert grant.sha256 == _artifact().sha256
    assert grant.size_bytes == _artifact().size_bytes
    assert grant.url == presigner.url
    assert grant.expires_at == now + timedelta(minutes=10)


def test_grant_issuer_classifies_insecure_presign_output_as_service_unavailable() -> None:
    now = datetime(2026, 8, 7, 16, 0, tzinfo=UTC)
    for url in (
        "http://updates.example.test/a.bin",
        "https://user:pass@updates.example.test/a.bin",
        "https://updates.example.test/a.bin#fragment",
    ):
        issuer = ShortLivedDownloadGrantIssuer(_Presigner(url))
        with pytest.raises(UpdateCheckUnavailable):
            issuer.issue_grant(
                device_id="77777777-7777-4777-8777-777777777777",
                artifact=_artifact(),
                now=now,
            )


def test_grant_issuer_rejects_out_of_policy_ttl_and_naive_request_time() -> None:
    with pytest.raises(ValueError):
        ShortLivedDownloadGrantIssuer(_Presigner(), ttl=timedelta(minutes=21))
    with pytest.raises(ValueError):
        ShortLivedDownloadGrantIssuer(_Presigner(), ttl=timedelta(seconds=10))

    issuer = ShortLivedDownloadGrantIssuer(_Presigner())
    with pytest.raises(UpdateCheckPolicyError):
        issuer.issue_grant(
            device_id="77777777-7777-4777-8777-777777777777",
            artifact=_artifact(),
            now=datetime(2026, 8, 7, 16, 0),
        )
