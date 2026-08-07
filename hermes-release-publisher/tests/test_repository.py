from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from hermes_release_publisher import (
    BucketMap,
    ObjectAlreadyExists,
    PublisherError,
    ReleasePublisher,
    RemoteObject,
    UploadResult,
    content_addressed_key,
)


@dataclass
class StoredObject:
    body: bytes
    remote: RemoteObject


class FakeBackend:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], StoredObject] = {}
        self.version = 0

    def head(self, bucket: str, key: str) -> RemoteObject | None:
        stored = self.objects.get((bucket, key))
        return stored.remote if stored else None

    def put_file(
        self,
        bucket: str,
        key: str,
        path: Path,
        *,
        metadata,
        content_type: str,
        cache_control: str,
        forbid_overwrite: bool,
    ) -> UploadResult:
        return self.put_bytes(
            bucket,
            key,
            path.read_bytes(),
            metadata=metadata,
            content_type=content_type,
            cache_control=cache_control,
            forbid_overwrite=forbid_overwrite,
        )

    def put_bytes(
        self,
        bucket: str,
        key: str,
        payload: bytes,
        *,
        metadata,
        content_type: str,
        cache_control: str,
        forbid_overwrite: bool,
    ) -> UploadResult:
        identity = (bucket, key)
        if forbid_overwrite and identity in self.objects:
            raise ObjectAlreadyExists(key)
        self.version += 1
        version_id = f"v{self.version}"
        remote = RemoteObject(
            content_length=len(payload),
            metadata=dict(metadata),
            version_id=version_id,
            etag=f"etag-{self.version}",
            crc64=str(self.version),
        )
        self.objects[identity] = StoredObject(payload, remote)
        return UploadResult(200, f"request-{self.version}", version_id, remote.etag, remote.crc64)


def buckets() -> BucketMap:
    return BucketMap(
        staging="hermes-release-staging",
        artifacts="hermes-release-artifacts",
        control="hermes-release-control",
        evidence="hermes-release-evidence",
    )


def signed_envelope(payload: dict[str, object]) -> bytes:
    return json.dumps(
        {
            "schema_version": 1,
            "key_id": "release-key-1",
            "signature_algorithm": "ed25519",
            "signed_at": "2026-08-07T14:00:00Z",
            "payload": payload,
            "signature": "qualification-signature",
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def write(path: Path, payload: bytes) -> Path:
    path.write_bytes(payload)
    return path.resolve()


def test_artifact_key_is_content_addressed_and_idempotent(tmp_path: Path) -> None:
    backend = FakeBackend()
    publisher = ReleasePublisher(backend, buckets())
    source = write(tmp_path / "Hermes-1.4.2.dmg", b"signed-installer")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()

    first = publisher.publish_artifact(source, kind="macos-installer")
    second = publisher.publish_artifact(source, kind="macos-installer")

    assert first.object_key == content_addressed_key("artifacts", digest, source.name)
    assert first.reused is False
    assert second.reused is True
    assert second.version_id == first.version_id
    assert backend.head(buckets().artifacts, first.object_key).metadata["hermes-sha256"] == digest


def test_immutable_object_collision_fails_closed(tmp_path: Path) -> None:
    backend = FakeBackend()
    publisher = ReleasePublisher(backend, buckets())
    source = write(tmp_path / "payload.bin", b"expected")
    digest = hashlib.sha256(b"expected").hexdigest()
    key = content_addressed_key("artifacts", digest, source.name)
    backend.objects[(buckets().artifacts, key)] = StoredObject(
        b"tampered",
        RemoteObject(
            content_length=len(b"tampered"),
            metadata={
                "hermes-schema": "1",
                "hermes-sha256": hashlib.sha256(b"tampered").hexdigest(),
                "hermes-size-bytes": str(len(b"tampered")),
                "hermes-kind": "runtime-payload",
            },
            version_id="v-existing",
        ),
    )

    with pytest.raises(PublisherError, match="size|metadata"):
        publisher.publish_artifact(source, kind="runtime-payload")


def test_release_envelope_is_immutable_and_identity_bound(tmp_path: Path) -> None:
    backend = FakeBackend()
    publisher = ReleasePublisher(backend, buckets())
    release_id = "1.4.2+20260807.3.g9839a049"
    source = write(
        tmp_path / "release.json",
        signed_envelope({"release_id": release_id, "release_generation": 1042}),
    )

    first = publisher.publish_release_envelope(source, release_id=release_id, release_generation=1042)
    second = publisher.publish_release_envelope(source, release_id=release_id, release_generation=1042)

    assert first.object_key == f"releases/v1/{release_id}/release-envelope.json"
    assert first.reused is False
    assert second.reused is True
    assert second.generation == 1042


def test_channel_publication_uses_immutable_generation_then_mutable_pointer(tmp_path: Path) -> None:
    backend = FakeBackend()
    publisher = ReleasePublisher(backend, buckets())
    source = write(
        tmp_path / "stable-82.json",
        signed_envelope({"channel": "stable", "channel_generation": 82}),
    )

    receipt = publisher.promote_channel(source, channel="stable", channel_generation=82)

    assert receipt.object_key == "channels/v1/stable/current.json"
    assert receipt.immutable_generation_key == "channels/v1/stable/generations/00000000000000000082.json"
    assert backend.head(buckets().control, receipt.immutable_generation_key) is not None
    assert backend.head(buckets().control, receipt.object_key).metadata["hermes-generation"] == "82"


def test_channel_generation_regression_is_rejected(tmp_path: Path) -> None:
    backend = FakeBackend()
    publisher = ReleasePublisher(backend, buckets())
    current = write(
        tmp_path / "stable-82.json",
        signed_envelope({"channel": "stable", "channel_generation": 82}),
    )
    stale = write(
        tmp_path / "stable-81.json",
        signed_envelope({"channel": "stable", "channel_generation": 81}),
    )
    publisher.promote_channel(current, channel="stable", channel_generation=82)

    with pytest.raises(PublisherError, match="generation regression"):
        publisher.promote_channel(stale, channel="stable", channel_generation=81)

    assert backend.head(buckets().control, "channels/v1/stable/current.json").metadata[
        "hermes-generation"
    ] == "82"


def test_same_channel_generation_with_different_payload_is_collision(tmp_path: Path) -> None:
    backend = FakeBackend()
    publisher = ReleasePublisher(backend, buckets())
    first = write(
        tmp_path / "stable-a.json",
        signed_envelope({"channel": "stable", "channel_generation": 82, "release_id": "A"}),
    )
    second = write(
        tmp_path / "stable-b.json",
        signed_envelope({"channel": "stable", "channel_generation": 82, "release_id": "B"}),
    )
    publisher.promote_channel(first, channel="stable", channel_generation=82)

    with pytest.raises(PublisherError, match="size|metadata"):
        publisher.promote_channel(second, channel="stable", channel_generation=82)


def test_block_pointer_is_monotonic_and_has_generation_history(tmp_path: Path) -> None:
    backend = FakeBackend()
    publisher = ReleasePublisher(backend, buckets())
    block = write(
        tmp_path / "block-9.json",
        signed_envelope({"block_generation": 9, "blocked_releases": []}),
    )

    receipt = publisher.publish_block_manifest(block, block_generation=9)

    assert receipt.object_key == "blocks/v1/current.json"
    assert receipt.immutable_generation_key == "blocks/v1/generations/00000000000000000009.json"
    assert backend.head(buckets().control, receipt.object_key).metadata["hermes-generation"] == "9"
