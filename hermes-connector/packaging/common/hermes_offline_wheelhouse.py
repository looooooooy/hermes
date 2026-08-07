"""Verified offline wheelhouse contract for Hermes Managed Runtime.

A wheelhouse is not treated as a loose directory of packages.  It is a closed,
content-addressed release input bound to the exact Core and Connector lock digests.
"""

from __future__ import annotations

import hashlib
import json
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_FILENAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,254}\.whl\Z")
_MAX_MANIFEST_BYTES = 256 * 1024
_MAX_WHEEL_BYTES = 1024 * 1024 * 1024
_MANIFEST_FIELDS = {
    "schema_version",
    "platform",
    "architecture",
    "python_tag",
    "locks",
    "artifacts",
}
_ARTIFACT_FIELDS = {"filename", "sha256", "size_bytes"}


class WheelhouseError(RuntimeError):
    """The offline dependency set cannot be trusted as a release input."""


@dataclass(frozen=True)
class WheelArtifactV1:
    filename: str
    sha256: str
    size_bytes: int


@dataclass(frozen=True)
class WheelhouseManifestV1:
    schema_version: int
    platform: str
    architecture: str
    python_tag: str
    locks: Mapping[str, str]
    artifacts: tuple[WheelArtifactV1, ...]


@dataclass(frozen=True)
class VerifiedWheelhouseV1:
    root: Path
    manifest_path: Path
    manifest_sha256: str
    manifest: WheelhouseManifestV1

    def require_lock(self, name: str, sha256: str) -> None:
        expected = self.manifest.locks.get(name)
        if expected != sha256:
            raise WheelhouseError(f"wheelhouse lock mismatch: {name}")


def load_verified_wheelhouse(
    root: Path,
    *,
    manifest_name: str = "WHEELHOUSE-MANIFEST.json",
) -> VerifiedWheelhouseV1:
    root = Path(root)
    if not root.is_absolute():
        raise WheelhouseError("wheelhouse root must be absolute")
    if root.is_symlink() or not root.is_dir():
        raise WheelhouseError("wheelhouse root is missing or symlinked")

    manifest_path = root / manifest_name
    if manifest_path.name != manifest_name or manifest_path.parent != root:
        raise WheelhouseError("wheelhouse manifest path is invalid")
    payload = _read_regular(manifest_path, maximum_bytes=_MAX_MANIFEST_BYTES)
    try:
        raw = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WheelhouseError("wheelhouse manifest is invalid JSON") from exc
    manifest = _parse_manifest(raw)
    _verify_artifacts(root, manifest)
    return VerifiedWheelhouseV1(
        root=root,
        manifest_path=manifest_path,
        manifest_sha256=hashlib.sha256(payload).hexdigest(),
        manifest=manifest,
    )


def _parse_manifest(raw: object) -> WheelhouseManifestV1:
    if not isinstance(raw, dict) or set(raw) != _MANIFEST_FIELDS:
        raise WheelhouseError("wheelhouse manifest fields are invalid")
    if raw["schema_version"] != 1:
        raise WheelhouseError("unsupported wheelhouse schema")
    for field in ("platform", "architecture", "python_tag"):
        if not isinstance(raw[field], str) or not raw[field].strip() or len(raw[field]) > 64:
            raise WheelhouseError(f"invalid wheelhouse {field}")

    locks_raw = raw["locks"]
    if not isinstance(locks_raw, dict) or set(locks_raw) != {"core", "connector"}:
        raise WheelhouseError("wheelhouse locks must bind core and connector")
    locks: dict[str, str] = {}
    for name, digest in locks_raw.items():
        if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
            raise WheelhouseError(f"invalid wheelhouse lock digest: {name}")
        locks[name] = digest

    artifacts_raw = raw["artifacts"]
    if not isinstance(artifacts_raw, list) or not artifacts_raw:
        raise WheelhouseError("wheelhouse artifacts must be a non-empty list")
    artifacts: list[WheelArtifactV1] = []
    names: set[str] = set()
    for item in artifacts_raw:
        if not isinstance(item, dict) or set(item) != _ARTIFACT_FIELDS:
            raise WheelhouseError("wheelhouse artifact fields are invalid")
        filename = item["filename"]
        digest = item["sha256"]
        size = item["size_bytes"]
        if not isinstance(filename, str) or not _FILENAME.fullmatch(filename):
            raise WheelhouseError("invalid wheelhouse artifact filename")
        if filename in names:
            raise WheelhouseError("duplicate wheelhouse artifact filename")
        if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
            raise WheelhouseError(f"invalid wheelhouse artifact digest: {filename}")
        if not isinstance(size, int) or isinstance(size, bool) or not 0 < size <= _MAX_WHEEL_BYTES:
            raise WheelhouseError(f"invalid wheelhouse artifact size: {filename}")
        names.add(filename)
        artifacts.append(WheelArtifactV1(filename, digest, size))

    return WheelhouseManifestV1(
        schema_version=1,
        platform=raw["platform"],
        architecture=raw["architecture"],
        python_tag=raw["python_tag"],
        locks=MappingProxyType(locks),
        artifacts=tuple(artifacts),
    )


def _verify_artifacts(root: Path, manifest: WheelhouseManifestV1) -> None:
    declared = {artifact.filename for artifact in manifest.artifacts}
    observed = {
        path.name
        for path in root.iterdir()
        if path.name.endswith(".whl") and (path.is_file() or path.is_symlink())
    }
    if observed != declared:
        raise WheelhouseError("wheelhouse contains missing or undeclared wheel artifacts")

    for artifact in manifest.artifacts:
        path = root / artifact.filename
        payload = _read_regular(path, maximum_bytes=_MAX_WHEEL_BYTES)
        if len(payload) != artifact.size_bytes:
            raise WheelhouseError(f"wheelhouse artifact size mismatch: {artifact.filename}")
        if hashlib.sha256(payload).hexdigest() != artifact.sha256:
            raise WheelhouseError(f"wheelhouse artifact digest mismatch: {artifact.filename}")


def _read_regular(path: Path, *, maximum_bytes: int) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise WheelhouseError(f"wheelhouse file is missing or symlinked: {path.name}")
    metadata = path.stat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > maximum_bytes:
        raise WheelhouseError(f"wheelhouse file is not a bounded regular file: {path.name}")
    payload = path.read_bytes()
    after = path.stat()
    if (
        after.st_size != metadata.st_size
        or after.st_mtime_ns != metadata.st_mtime_ns
        or after.st_ino != metadata.st_ino
        or len(payload) != metadata.st_size
    ):
        raise WheelhouseError(f"wheelhouse file changed during verification: {path.name}")
    return payload
