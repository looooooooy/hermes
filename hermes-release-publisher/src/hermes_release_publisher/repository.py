from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Protocol

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_CHANNEL = re.compile(r"[a-z][a-z0-9-]{0,31}\Z")
_RELEASE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9.+-]{0,127}\Z")
_MAX_FILE_BYTES = 8 * 1024 * 1024 * 1024


class PublisherError(RuntimeError):
    """Release publication cannot continue without weakening immutability."""


class ObjectAlreadyExists(PublisherError):
    """Backend rejected an immutable object overwrite."""


@dataclass(frozen=True)
class BucketMap:
    staging: str
    artifacts: str
    control: str
    evidence: str


@dataclass(frozen=True)
class RemoteObject:
    content_length: int
    metadata: Mapping[str, str]
    version_id: str | None = None
    etag: str | None = None
    crc64: str | None = None


@dataclass(frozen=True)
class UploadResult:
    status_code: int
    request_id: str | None
    version_id: str | None
    etag: str | None
    crc64: str | None


@dataclass(frozen=True)
class PublishReceipt:
    schema_version: int
    bucket: str
    object_key: str
    sha256: str
    size_bytes: int
    kind: str
    generation: int | None
    version_id: str | None
    etag: str | None
    crc64: str | None
    reused: bool
    immutable_generation_key: str | None = None

    def to_json(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "bucket": self.bucket,
            "object_key": self.object_key,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "kind": self.kind,
            "generation": self.generation,
            "version_id": self.version_id,
            "etag": self.etag,
            "crc64": self.crc64,
            "reused": self.reused,
            "immutable_generation_key": self.immutable_generation_key,
        }


class ObjectStoreBackend(Protocol):
    def head(self, bucket: str, key: str) -> RemoteObject | None: ...

    def put_file(
        self,
        bucket: str,
        key: str,
        path: Path,
        *,
        metadata: Mapping[str, str],
        content_type: str,
        cache_control: str,
        forbid_overwrite: bool,
    ) -> UploadResult: ...

    def put_bytes(
        self,
        bucket: str,
        key: str,
        payload: bytes,
        *,
        metadata: Mapping[str, str],
        content_type: str,
        cache_control: str,
        forbid_overwrite: bool,
    ) -> UploadResult: ...


class ReleasePublisher:
    def __init__(self, backend: ObjectStoreBackend, buckets: BucketMap) -> None:
        self._backend = backend
        self._buckets = buckets
        for label, value in (
            ("staging", buckets.staging),
            ("artifacts", buckets.artifacts),
            ("control", buckets.control),
            ("evidence", buckets.evidence),
        ):
            if not _valid_bucket(value):
                raise PublisherError(f"invalid {label} bucket name")

    def publish_artifact(self, path: Path, *, kind: str) -> PublishReceipt:
        return self._publish_immutable_file(
            self._buckets.artifacts,
            path,
            namespace="artifacts",
            kind=kind,
        )

    def publish_evidence(self, path: Path, *, kind: str) -> PublishReceipt:
        return self._publish_immutable_file(
            self._buckets.evidence,
            path,
            namespace="evidence",
            kind=kind,
        )

    def publish_release_envelope(
        self,
        path: Path,
        *,
        release_id: str,
        release_generation: int,
    ) -> PublishReceipt:
        if _RELEASE_ID.fullmatch(release_id) is None or release_generation <= 0:
            raise PublisherError("release identity is invalid")
        payload = _read_regular(path)
        _require_signed_payload_identity(
            payload,
            expected={"release_id": release_id, "release_generation": release_generation},
        )
        key = f"releases/v1/{release_id}/release-envelope.json"
        return self._publish_immutable_bytes(
            self._buckets.control,
            key,
            payload,
            kind="release-envelope",
            generation=release_generation,
            cache_control="public,max-age=31536000,immutable",
        )

    def promote_channel(
        self,
        path: Path,
        *,
        channel: str,
        channel_generation: int,
    ) -> PublishReceipt:
        if _CHANNEL.fullmatch(channel) is None or channel_generation <= 0:
            raise PublisherError("channel identity is invalid")
        payload = _read_regular(path)
        _require_signed_payload_identity(
            payload,
            expected={"channel": channel, "channel_generation": channel_generation},
        )
        generation_key = (
            f"channels/v1/{channel}/generations/{channel_generation:020d}.json"
        )
        immutable = self._publish_immutable_bytes(
            self._buckets.control,
            generation_key,
            payload,
            kind="channel-generation",
            generation=channel_generation,
            cache_control="public,max-age=31536000,immutable",
        )
        pointer_key = f"channels/v1/{channel}/current.json"
        return self._publish_control_pointer(
            pointer_key,
            payload,
            kind="channel-pointer",
            generation=channel_generation,
            immutable_generation_key=immutable.object_key,
        )

    def publish_block_manifest(
        self,
        path: Path,
        *,
        block_generation: int,
    ) -> PublishReceipt:
        if block_generation <= 0:
            raise PublisherError("block_generation must be positive")
        payload = _read_regular(path)
        _require_signed_payload_identity(
            payload,
            expected={"block_generation": block_generation},
        )
        generation_key = f"blocks/v1/generations/{block_generation:020d}.json"
        immutable = self._publish_immutable_bytes(
            self._buckets.control,
            generation_key,
            payload,
            kind="block-generation",
            generation=block_generation,
            cache_control="public,max-age=31536000,immutable",
        )
        return self._publish_control_pointer(
            "blocks/v1/current.json",
            payload,
            kind="block-pointer",
            generation=block_generation,
            immutable_generation_key=immutable.object_key,
        )

    def _publish_immutable_file(
        self,
        bucket: str,
        path: Path,
        *,
        namespace: str,
        kind: str,
    ) -> PublishReceipt:
        source = _canonical_file(path)
        digest = _sha256_file(source)
        size = source.stat().st_size
        key = content_addressed_key(namespace, digest, source.name)
        metadata = _metadata(digest, size, kind, None)
        existing = self._backend.head(bucket, key)
        if existing is not None:
            _verify_remote(existing, digest, size, kind, None)
            return _receipt(bucket, key, digest, size, kind, None, existing, True)
        try:
            result = self._backend.put_file(
                bucket,
                key,
                source,
                metadata=metadata,
                content_type=mimetypes.guess_type(source.name)[0] or "application/octet-stream",
                cache_control="public,max-age=31536000,immutable",
                forbid_overwrite=True,
            )
        except ObjectAlreadyExists:
            existing = self._backend.head(bucket, key)
            if existing is None:
                raise PublisherError("immutable object collision cannot be reconciled")
            _verify_remote(existing, digest, size, kind, None)
            return _receipt(bucket, key, digest, size, kind, None, existing, True)
        remote = self._backend.head(bucket, key)
        if remote is None:
            raise PublisherError("uploaded immutable object is not readable by HeadObject")
        _verify_remote(remote, digest, size, kind, None)
        return _receipt_from_upload(bucket, key, digest, size, kind, None, result, remote)

    def _publish_immutable_bytes(
        self,
        bucket: str,
        key: str,
        payload: bytes,
        *,
        kind: str,
        generation: int,
        cache_control: str,
    ) -> PublishReceipt:
        digest = hashlib.sha256(payload).hexdigest()
        size = len(payload)
        metadata = _metadata(digest, size, kind, generation)
        existing = self._backend.head(bucket, key)
        if existing is not None:
            _verify_remote(existing, digest, size, kind, generation)
            return _receipt(bucket, key, digest, size, kind, generation, existing, True)
        try:
            result = self._backend.put_bytes(
                bucket,
                key,
                payload,
                metadata=metadata,
                content_type="application/json",
                cache_control=cache_control,
                forbid_overwrite=True,
            )
        except ObjectAlreadyExists:
            existing = self._backend.head(bucket, key)
            if existing is None:
                raise PublisherError("immutable control object collision cannot be reconciled")
            _verify_remote(existing, digest, size, kind, generation)
            return _receipt(bucket, key, digest, size, kind, generation, existing, True)
        remote = self._backend.head(bucket, key)
        if remote is None:
            raise PublisherError("uploaded immutable control object is not readable")
        _verify_remote(remote, digest, size, kind, generation)
        return _receipt_from_upload(bucket, key, digest, size, kind, generation, result, remote)

    def _publish_control_pointer(
        self,
        key: str,
        payload: bytes,
        *,
        kind: str,
        generation: int,
        immutable_generation_key: str,
    ) -> PublishReceipt:
        digest = hashlib.sha256(payload).hexdigest()
        size = len(payload)
        current = self._backend.head(self._buckets.control, key)
        if current is not None:
            remote_generation = _metadata_generation(current)
            if remote_generation > generation:
                raise PublisherError(
                    f"refusing control generation regression: remote={remote_generation}, candidate={generation}"
                )
            if remote_generation == generation:
                _verify_remote(current, digest, size, kind, generation)
                receipt = _receipt(
                    self._buckets.control,
                    key,
                    digest,
                    size,
                    kind,
                    generation,
                    current,
                    True,
                )
                return PublishReceipt(
                    **{**receipt.__dict__, "immutable_generation_key": immutable_generation_key}
                )

        result = self._backend.put_bytes(
            self._buckets.control,
            key,
            payload,
            metadata=_metadata(digest, size, kind, generation),
            content_type="application/json",
            cache_control="no-cache,max-age=0,must-revalidate",
            forbid_overwrite=False,
        )
        remote = self._backend.head(self._buckets.control, key)
        if remote is None:
            raise PublisherError("control pointer is not readable after publication")
        observed_generation = _metadata_generation(remote)
        if observed_generation != generation:
            raise PublisherError(
                f"control pointer lost publication race: observed generation {observed_generation}"
            )
        _verify_remote(remote, digest, size, kind, generation)
        receipt = _receipt_from_upload(
            self._buckets.control,
            key,
            digest,
            size,
            kind,
            generation,
            result,
            remote,
        )
        return PublishReceipt(
            **{**receipt.__dict__, "immutable_generation_key": immutable_generation_key}
        )


def content_addressed_key(namespace: str, sha256: str, filename: str) -> str:
    if namespace not in {"artifacts", "evidence"}:
        raise PublisherError("unsupported content-addressed namespace")
    if _SHA256.fullmatch(sha256) is None:
        raise PublisherError("invalid SHA-256")
    if not _safe_filename(filename):
        raise PublisherError("unsafe artifact filename")
    return f"{namespace}/v1/sha256/{sha256[:2]}/{sha256}/{filename}"


def _publish_metadata_value(value: object) -> str:
    return str(value).strip()


def _metadata(
    sha256: str,
    size: int,
    kind: str,
    generation: int | None,
) -> dict[str, str]:
    if not kind or len(kind) > 64 or any(character.isspace() for character in kind):
        raise PublisherError("invalid publication kind")
    metadata = {
        "hermes-schema": "1",
        "hermes-sha256": sha256,
        "hermes-size-bytes": str(size),
        "hermes-kind": kind,
    }
    if generation is not None:
        metadata["hermes-generation"] = str(generation)
    return metadata


def _metadata_generation(remote: RemoteObject) -> int:
    value = _normalized_metadata(remote.metadata).get("hermes-generation")
    if value is None or not value.isdigit():
        raise PublisherError("remote control object has no canonical generation metadata")
    generation = int(value)
    if generation <= 0:
        raise PublisherError("remote control generation must be positive")
    return generation


def _verify_remote(
    remote: RemoteObject,
    sha256: str,
    size: int,
    kind: str,
    generation: int | None,
) -> None:
    metadata = _normalized_metadata(remote.metadata)
    if remote.content_length != size:
        raise PublisherError("remote object size does not match local release input")
    expected = _metadata(sha256, size, kind, generation)
    for key, value in expected.items():
        if metadata.get(key) != value:
            raise PublisherError(f"remote object metadata mismatch: {key}")


def _normalized_metadata(metadata: Mapping[str, str]) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for key, value in metadata.items():
        name = str(key).lower()
        if name.startswith("x-oss-meta-"):
            name = name.removeprefix("x-oss-meta-")
        normalized[name] = _publish_metadata_value(value)
    return normalized


def _receipt(
    bucket: str,
    key: str,
    digest: str,
    size: int,
    kind: str,
    generation: int | None,
    remote: RemoteObject,
    reused: bool,
) -> PublishReceipt:
    return PublishReceipt(
        schema_version=1,
        bucket=bucket,
        object_key=key,
        sha256=digest,
        size_bytes=size,
        kind=kind,
        generation=generation,
        version_id=remote.version_id,
        etag=remote.etag,
        crc64=remote.crc64,
        reused=reused,
    )


def _receipt_from_upload(
    bucket: str,
    key: str,
    digest: str,
    size: int,
    kind: str,
    generation: int | None,
    result: UploadResult,
    remote: RemoteObject,
) -> PublishReceipt:
    if result.status_code < 200 or result.status_code >= 300:
        raise PublisherError(f"OSS upload returned status {result.status_code}")
    return PublishReceipt(
        schema_version=1,
        bucket=bucket,
        object_key=key,
        sha256=digest,
        size_bytes=size,
        kind=kind,
        generation=generation,
        version_id=remote.version_id or result.version_id,
        etag=remote.etag or result.etag,
        crc64=remote.crc64 or result.crc64,
        reused=False,
    )


def _read_regular(path: Path) -> bytes:
    source = _canonical_file(path)
    before = source.stat()
    if before.st_size == 0 or before.st_size > _MAX_FILE_BYTES:
        raise PublisherError("release input file size is invalid")
    payload = source.read_bytes()
    after = source.stat()
    if before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns:
        raise PublisherError("release input changed while reading")
    return payload


def _canonical_file(path: Path) -> Path:
    path = Path(path)
    if not path.is_absolute():
        path = path.resolve()
    if path.is_symlink() or not path.is_file() or path.resolve(strict=True) != path:
        raise PublisherError("release input must be a canonical regular non-symlink file")
    return path


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require_signed_payload_identity(payload: bytes, *, expected: Mapping[str, object]) -> None:
    try:
        document = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PublisherError("signed control input is not JSON") from error
    if not isinstance(document, dict) or not isinstance(document.get("payload"), dict):
        raise PublisherError("signed control input has no payload object")
    control = document["payload"]
    for key, value in expected.items():
        if control.get(key) != value:
            raise PublisherError(f"signed control input {key} does not match publication request")


def _valid_bucket(value: str) -> bool:
    return (
        3 <= len(value) <= 63
        and value[0].isalnum()
        and value[-1].isalnum()
        and all(character.islower() or character.isdigit() or character == "-" for character in value)
    )


def _safe_filename(value: str) -> bool:
    return (
        bool(value)
        and len(value) <= 255
        and value not in {".", ".."}
        and "/" not in value
        and "\\" not in value
        and all(character.isalnum() or character in "._+-" for character in value)
    )


class OssV2Backend:
    """Thin Alibaba Cloud OSS Python SDK V2 adapter.

    Publisher policy intentionally remains outside this class so fake backends can prove
    immutability and generation semantics without network credentials.
    """

    def __init__(self, client: object, oss_module: object, *, server_side_encryption: str | None) -> None:
        self._client = client
        self._oss = oss_module
        self._server_side_encryption = server_side_encryption

    @classmethod
    def from_environment(
        cls,
        *,
        region: str,
        endpoint: str | None = None,
        server_side_encryption: str | None = "AES256",
    ) -> "OssV2Backend":
        if not region or region != region.strip():
            raise PublisherError("OSS region is invalid")
        if server_side_encryption not in {None, "AES256", "KMS"}:
            raise PublisherError("unsupported OSS server-side encryption mode")
        try:
            import alibabacloud_oss_v2 as oss
        except ImportError as error:
            raise PublisherError("alibabacloud-oss-v2 is not installed") from error

        provider = oss.credentials.EnvironmentVariableCredentialsProvider()
        config = oss.config.load_default()
        config.credentials_provider = provider
        config.region = region
        if endpoint:
            config.endpoint = endpoint
        return cls(oss.Client(config), oss, server_side_encryption=server_side_encryption)

    def head(self, bucket: str, key: str) -> RemoteObject | None:
        try:
            result = self._client.head_object(self._oss.HeadObjectRequest(bucket=bucket, key=key))
        except self._oss.exceptions.OperationError as error:
            if _operation_status(error) == 404:
                return None
            raise PublisherError(f"OSS HeadObject failed: {error}") from error
        return RemoteObject(
            content_length=int(result.content_length),
            metadata=dict(getattr(result, "metadata", None) or {}),
            version_id=getattr(result, "version_id", None),
            etag=getattr(result, "etag", None),
            crc64=_optional_text(getattr(result, "hash_crc64", None)),
        )

    def put_file(
        self,
        bucket: str,
        key: str,
        path: Path,
        *,
        metadata: Mapping[str, str],
        content_type: str,
        cache_control: str,
        forbid_overwrite: bool,
    ) -> UploadResult:
        request = self._oss.PutObjectRequest(
            bucket=bucket,
            key=key,
            metadata=dict(metadata),
            content_type=content_type,
            cache_control=cache_control,
            forbid_overwrite=forbid_overwrite,
            server_side_encryption=self._server_side_encryption,
        )
        try:
            result = self._client.put_object_from_file(request, str(path))
        except self._oss.exceptions.OperationError as error:
            if forbid_overwrite and _operation_status(error) in {409, 412}:
                raise ObjectAlreadyExists(key) from error
            raise PublisherError(f"OSS PutObject failed: {error}") from error
        return _upload_result(result)

    def put_bytes(
        self,
        bucket: str,
        key: str,
        payload: bytes,
        *,
        metadata: Mapping[str, str],
        content_type: str,
        cache_control: str,
        forbid_overwrite: bool,
    ) -> UploadResult:
        request = self._oss.PutObjectRequest(
            bucket=bucket,
            key=key,
            body=payload,
            metadata=dict(metadata),
            content_type=content_type,
            cache_control=cache_control,
            forbid_overwrite=forbid_overwrite,
            server_side_encryption=self._server_side_encryption,
        )
        try:
            result = self._client.put_object(request)
        except self._oss.exceptions.OperationError as error:
            if forbid_overwrite and _operation_status(error) in {409, 412}:
                raise ObjectAlreadyExists(key) from error
            raise PublisherError(f"OSS PutObject failed: {error}") from error
        return _upload_result(result)


def _operation_status(error: BaseException) -> int | None:
    kwargs = getattr(error, "kwargs", None)
    if not isinstance(kwargs, dict):
        return None
    service_error = kwargs.get("error")
    status = getattr(service_error, "status_code", None)
    return int(status) if isinstance(status, int) else None


def _upload_result(result: object) -> UploadResult:
    return UploadResult(
        status_code=int(getattr(result, "status_code", 0)),
        request_id=_optional_text(getattr(result, "request_id", None)),
        version_id=_optional_text(getattr(result, "version_id", None)),
        etag=_optional_text(getattr(result, "etag", None)),
        crc64=_optional_text(getattr(result, "hash_crc64", None)),
    )


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None
