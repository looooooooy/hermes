from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from hermes_release_publisher.presign import OssV4DownloadPresigner
from hermes_release_publisher.repository import PublisherError


class _GetObjectRequest:
    def __init__(self, *, bucket: str, key: str) -> None:
        self.bucket = bucket
        self.key = key


class _Client:
    def __init__(self, *, url: str, expiration: datetime, signed_headers=None) -> None:
        self.url = url
        self.expiration = expiration
        self.signed_headers = signed_headers or {}
        self.calls: list[tuple[object, datetime]] = []

    def presign(self, request: object, *, expiration: datetime) -> object:
        self.calls.append((request, expiration))
        return SimpleNamespace(
            method="GET",
            url=self.url,
            expiration=self.expiration,
            signed_headers=self.signed_headers,
        )


def _oss() -> object:
    return SimpleNamespace(GetObjectRequest=_GetObjectRequest)


def test_presigner_uses_exact_bucket_key_and_absolute_expiration() -> None:
    expiration = datetime(2026, 8, 7, 16, 10, tzinfo=UTC)
    client = _Client(
        url=(
            "https://updates.example.test/artifacts/payload.bin"
            "?x-oss-signature-version=OSS4-HMAC-SHA256&x-oss-signature=abc"
        ),
        expiration=expiration,
    )
    presigner = OssV4DownloadPresigner(client, _oss(), bucket="hermes-release-artifacts")

    url = presigner.presign_get(
        object_key="artifacts/v1/sha256/aa/" + "a" * 64 + "/payload.bin",
        expires_at=expiration,
    )

    assert url == client.url
    assert len(client.calls) == 1
    request, observed_expiration = client.calls[0]
    assert request.bucket == "hermes-release-artifacts"
    assert request.key.endswith("/payload.bin")
    assert observed_expiration == expiration


def test_presigner_rejects_signed_header_dependency_and_insecure_url() -> None:
    expiration = datetime(2026, 8, 7, 16, 10, tzinfo=UTC)
    signed_header = _Client(
        url="https://updates.example.test/payload.bin?x-oss-signature=abc",
        expiration=expiration,
        signed_headers={"x-oss-request-payer": "requester"},
    )
    with pytest.raises(PublisherError, match="signed headers"):
        OssV4DownloadPresigner(
            signed_header,
            _oss(),
            bucket="hermes-release-artifacts",
        ).presign_get(object_key="artifacts/payload.bin", expires_at=expiration)

    insecure = _Client(
        url="http://updates.example.test/payload.bin?x-oss-signature=abc",
        expiration=expiration,
    )
    with pytest.raises(PublisherError, match="HTTPS"):
        OssV4DownloadPresigner(
            insecure,
            _oss(),
            bucket="hermes-release-artifacts",
        ).presign_get(object_key="artifacts/payload.bin", expires_at=expiration)


def test_presigner_rejects_path_traversal_or_expiration_mismatch() -> None:
    expiration = datetime(2026, 8, 7, 16, 10, tzinfo=UTC)
    client = _Client(
        url="https://updates.example.test/payload.bin?x-oss-signature=abc",
        expiration=expiration,
    )
    presigner = OssV4DownloadPresigner(client, _oss(), bucket="hermes-release-artifacts")
    with pytest.raises(PublisherError, match="object key"):
        presigner.presign_get(object_key="../payload.bin", expires_at=expiration)

    mismatch = _Client(
        url="https://updates.example.test/payload.bin?x-oss-signature=abc",
        expiration=datetime(2026, 8, 7, 16, 11, tzinfo=UTC),
    )
    with pytest.raises(PublisherError, match="expiry"):
        OssV4DownloadPresigner(
            mismatch,
            _oss(),
            bucket="hermes-release-artifacts",
        ).presign_get(object_key="artifacts/payload.bin", expires_at=expiration)
