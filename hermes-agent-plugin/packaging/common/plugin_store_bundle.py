"""Build an immutable, signed Hermes Agent Plugin Store release bundle.

The private signing key is read in place and is never copied into the release.
The release is assembled in a private sibling directory and published with one
atomic rename. Runtime state deliberately lives outside the immutable release.
"""

from __future__ import annotations

import base64
import configparser
import hashlib
import io
import json
import os
import re
import shutil
import stat
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from email.parser import BytesParser
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from packaging.version import InvalidVersion, Version

_PLUGIN_ID = "hermes-agent-plugin"
_ENTRYPOINT = {
    "group": "hermes_agent.plugins",
    "name": _PLUGIN_ID,
    "value": "hermes_agent_plugin",
}
_SAFE_ID = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?\Z")
_MAX_KEY_BYTES = 64 * 1024
_MAX_WHEEL_BYTES = 256 * 1024 * 1024


class PluginStoreBundleError(RuntimeError):
    """The release bundle could not be assembled without weakening safety."""


@dataclass(frozen=True)
class PluginStoreBundlePaths:
    """Published paths consumed by the release installer and Hermes Core."""

    release_root: Path
    manifest_path: Path
    trust_store_path: Path
    wheel_path: Path


def _canonical_manifest_bytes(payload: Mapping[str, Any]) -> bytes:
    unsigned = {key: value for key, value in payload.items() if key != "signature"}
    try:
        return json.dumps(
            unsigned,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise PluginStoreBundleError("manifest is not canonical JSON") from error


def _utc(value: datetime, *, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise PluginStoreBundleError(f"{label} must include a timezone")
    if value.utcoffset() is None:
        raise PluginStoreBundleError(f"{label} must include a timezone")
    return value.astimezone(timezone.utc)


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _assert_no_symlink_components(path: Path, *, label: str) -> None:
    if not path.is_absolute():
        raise PluginStoreBundleError(f"{label} must be an absolute canonical path")
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            break
        if stat.S_ISLNK(metadata.st_mode):
            raise PluginStoreBundleError(f"{label} must not contain a symlink")


def _canonical_existing(path: Path, *, label: str, directory: bool) -> Path:
    _assert_no_symlink_components(path, label=label)
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise PluginStoreBundleError(f"{label} does not exist") from error
    if resolved != path:
        raise PluginStoreBundleError(f"{label} must be a canonical path")
    metadata = path.stat()
    if directory and not stat.S_ISDIR(metadata.st_mode):
        raise PluginStoreBundleError(f"{label} must be a directory")
    if not directory and not stat.S_ISREG(metadata.st_mode):
        raise PluginStoreBundleError(f"{label} must be a regular file")
    return path


def _validate_release_root(path: Path) -> Path:
    _assert_no_symlink_components(path, label="release root")
    if path.exists() or path.is_symlink():
        raise PluginStoreBundleError("release root already exists")
    try:
        parent = path.parent.resolve(strict=True)
    except OSError as error:
        raise PluginStoreBundleError("release root parent does not exist") from error
    if parent / path.name != path or path.resolve(strict=False) != path:
        raise PluginStoreBundleError("release root must be a canonical path")
    if not parent.is_dir():
        raise PluginStoreBundleError("release root parent must be a directory")
    return path


def _current_uid() -> int | None:
    getter = getattr(os, "getuid", None)
    return getter() if getter is not None else None


def _validate_store_root(path: Path) -> Path:
    state = _canonical_existing(path, label="plugin store state root", directory=True)
    metadata = state.stat()
    uid = _current_uid()
    if uid is not None and metadata.st_uid != uid:
        raise PluginStoreBundleError("plugin store state root must be owned by current user")
    if stat.S_IMODE(metadata.st_mode) != 0o700:
        raise PluginStoreBundleError("plugin store state root permission must be 0700")
    return state


def _assert_disjoint(release: Path, state: Path) -> None:
    if release == state or release.is_relative_to(state) or state.is_relative_to(release):
        raise PluginStoreBundleError(
            "immutable release and mutable plugin store state must be disjoint"
        )


def _read_private_key(path: Path) -> Ed25519PrivateKey:
    key_path = _canonical_existing(path, label="private signing key", directory=False)
    before = key_path.stat()
    uid = _current_uid()
    if uid is not None and before.st_uid != uid:
        raise PluginStoreBundleError("private signing key must be owned by current user")
    if before.st_nlink != 1:
        raise PluginStoreBundleError("private signing key must not be a hardlink")
    if stat.S_IMODE(before.st_mode) & 0o077:
        raise PluginStoreBundleError("private signing key permission is not private")

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(key_path, flags)
    except OSError as error:
        raise PluginStoreBundleError("private signing key cannot be opened safely") from error
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise PluginStoreBundleError("private signing key must be a regular file")
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise PluginStoreBundleError("private signing key changed while opening")
        if opened.st_nlink != 1:
            raise PluginStoreBundleError("private signing key must not be a hardlink")
        if uid is not None and opened.st_uid != uid:
            raise PluginStoreBundleError("private signing key must be owned by current user")
        if stat.S_IMODE(opened.st_mode) & 0o077:
            raise PluginStoreBundleError("private signing key permission is not private")
        key_bytes = os.read(descriptor, _MAX_KEY_BYTES + 1)
    finally:
        os.close(descriptor)

    if not key_bytes or len(key_bytes) > _MAX_KEY_BYTES:
        raise PluginStoreBundleError("private signing key has an invalid size")
    if not key_bytes.startswith(b"-----BEGIN PRIVATE KEY-----\n"):
        raise PluginStoreBundleError("private signing key must be Ed25519 PKCS8 PEM")
    try:
        private_key = serialization.load_pem_private_key(key_bytes, password=None)
    except (TypeError, ValueError) as error:
        raise PluginStoreBundleError("private signing key is invalid") from error
    if not isinstance(private_key, Ed25519PrivateKey):
        raise PluginStoreBundleError("private signing key must use Ed25519")
    return private_key


def _read_regular_file(path: Path, *, label: str, limit: int) -> bytes:
    source = _canonical_existing(path, label=label, directory=False)
    before = source.stat()
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(source, flags)
    except OSError as error:
        raise PluginStoreBundleError(f"{label} cannot be opened safely") from error
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise PluginStoreBundleError(f"{label} must be a regular file")
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise PluginStoreBundleError(f"{label} changed while opening")
        chunks: list[bytes] = []
        size = 0
        while True:
            chunk = os.read(descriptor, min(1024 * 1024, limit + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > limit:
                raise PluginStoreBundleError(f"{label} exceeds the size limit")
    finally:
        os.close(descriptor)
    return b"".join(chunks)


def _validate_archive_member(info: zipfile.ZipInfo, seen: set[str]) -> None:
    name = info.filename
    if name in seen:
        raise PluginStoreBundleError("wheel contains a duplicate member")
    seen.add(name)
    if "\\" in name:
        raise PluginStoreBundleError("wheel member path is not canonical")
    relative = PurePosixPath(name)
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise PluginStoreBundleError("wheel member path escapes the archive")
    if any(part in {"", "."} for part in relative.parts):
        raise PluginStoreBundleError("wheel member path is not canonical")
    if stat.S_ISLNK(info.external_attr >> 16):
        raise PluginStoreBundleError("wheel member must not be a symlink")
    lowered = name.lower()
    if lowered.endswith(".pth") or "__editable__" in lowered:
        raise PluginStoreBundleError("editable or pth wheel content is forbidden")
    if lowered.endswith(".dist-info/direct_url.json"):
        raise PluginStoreBundleError("direct_url wheel metadata is forbidden")


def _normalized_distribution_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _inspect_wheel(contents: bytes) -> str:
    try:
        archive = zipfile.ZipFile(io.BytesIO(contents))
    except zipfile.BadZipFile as error:
        raise PluginStoreBundleError("plugin artifact is not a valid wheel") from error
    with archive:
        infos = tuple(archive.infolist())
        seen: set[str] = set()
        for info in infos:
            _validate_archive_member(info, seen)
        metadata_infos = [
            info for info in infos if info.filename.endswith(".dist-info/METADATA")
        ]
        entry_infos = [
            info
            for info in infos
            if info.filename.endswith(".dist-info/entry_points.txt")
        ]
        if len(metadata_infos) != 1 or len(entry_infos) != 1:
            raise PluginStoreBundleError(
                "wheel must contain exactly one metadata entrypoint"
            )
        metadata = BytesParser().parsebytes(archive.read(metadata_infos[0]))
        if _normalized_distribution_name(metadata.get("Name", "")) != _PLUGIN_ID:
            raise PluginStoreBundleError("wheel project name does not match plugin id")
        version = metadata.get("Version", "")
        if _SAFE_ID.fullmatch(version) is None:
            raise PluginStoreBundleError("wheel version is not canonical")
        try:
            Version(version)
        except InvalidVersion as error:
            raise PluginStoreBundleError("wheel version is invalid") from error
        parser = configparser.ConfigParser(interpolation=None, strict=True)
        parser.optionxform = str
        try:
            parser.read_string(archive.read(entry_infos[0]).decode("utf-8"))
        except (UnicodeDecodeError, configparser.Error) as error:
            raise PluginStoreBundleError("wheel entrypoint metadata is invalid") from error
        group = _ENTRYPOINT["group"]
        if parser.sections() != [group]:
            raise PluginStoreBundleError(
                "wheel must declare exactly one plugin entrypoint"
            )
        if list(parser.items(group)) != [
            (_ENTRYPOINT["name"], _ENTRYPOINT["value"])
        ]:
            raise PluginStoreBundleError(
                "wheel must declare exactly one plugin entrypoint"
            )
        return version


def _write_file(path: Path, contents: bytes, *, mode: int) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        view = memoryview(contents)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)
        os.fchmod(descriptor, mode)
    finally:
        os.close(descriptor)


def _json_bytes(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _make_immutable(root: Path) -> None:
    files = [path for path in root.rglob("*") if path.is_file()]
    directories = [root, *(path for path in root.rglob("*") if path.is_dir())]
    for path in files:
        path.chmod(0o444)
    for path in sorted(directories, key=lambda item: len(item.parts), reverse=True):
        path.chmod(0o555)


def _remove_partial(root: Path) -> None:
    if not root.exists():
        return
    for current, directories, files in os.walk(root, topdown=False, followlinks=False):
        directory = Path(current)
        for name in files:
            path = directory / name
            if not path.is_symlink():
                path.chmod(0o600)
        for name in directories:
            path = directory / name
            if not path.is_symlink():
                path.chmod(0o700)
        directory.chmod(0o700)
    shutil.rmtree(root)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def assemble_signed_plugin_store_bundle(
    *,
    wheel_path: str | os.PathLike[str],
    private_key_path: str | os.PathLike[str],
    release_root: str | os.PathLike[str],
    store_root: str | os.PathLike[str],
    key_id: str,
    now: datetime,
    issued_at: datetime,
    expires_at: datetime,
    key_not_before: datetime,
    key_not_after: datetime,
) -> PluginStoreBundlePaths:
    """Validate, sign, and atomically publish one Plugin Store release."""

    release = _validate_release_root(Path(release_root))
    state = _validate_store_root(Path(store_root))
    _assert_disjoint(release, state)
    if not isinstance(key_id, str) or _SAFE_ID.fullmatch(key_id) is None:
        raise PluginStoreBundleError("key id is not canonical")

    observed_now = _utc(now, label="now")
    issued = _utc(issued_at, label="issued_at")
    expires = _utc(expires_at, label="expires_at")
    not_before = _utc(key_not_before, label="key_not_before")
    not_after = _utc(key_not_after, label="key_not_after")
    if observed_now >= expires:
        raise PluginStoreBundleError("plugin bundle is expired")
    if issued > observed_now:
        raise PluginStoreBundleError("plugin bundle issued_at is in the future")
    if issued >= expires:
        raise PluginStoreBundleError("plugin bundle expires_at must follow issued_at")
    if not_before > observed_now or not_before > issued:
        raise PluginStoreBundleError("signing key is not yet valid")
    if observed_now >= not_after or expires > not_after:
        raise PluginStoreBundleError("signing key expires before plugin bundle")

    private_key = _read_private_key(Path(private_key_path))
    wheel_contents = _read_regular_file(
        Path(wheel_path), label="plugin wheel", limit=_MAX_WHEEL_BYTES
    )
    version = _inspect_wheel(wheel_contents)
    wheel_sha256 = hashlib.sha256(wheel_contents).hexdigest()
    wheel_name = Path(wheel_path).name

    final_wheel = (
        release
        / "plugin"
        / "artifacts"
        / _PLUGIN_ID
        / version
        / wheel_sha256
        / wheel_name
    )
    final_manifest = release / "plugin/metadata/signed-plugin-manifest.json"
    final_trust_store = release / "plugin/metadata/trust-store.json"
    public_key = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    trust_store = {
        "schema_version": 1,
        "keys": [
            {
                "key_id": key_id,
                "signature_algorithm": "ed25519",
                "public_key": base64.b64encode(public_key).decode("ascii"),
                "not_before": _timestamp(not_before),
                "not_after": _timestamp(not_after),
            }
        ],
    }
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "plugin_id": _PLUGIN_ID,
        "version": version,
        "wheel_path": str(final_wheel),
        "wheel_sha256": wheel_sha256,
        "store_root": str(state),
        "entrypoint": dict(_ENTRYPOINT),
        "signature_algorithm": "ed25519",
        "key_id": key_id,
        "issued_at": _timestamp(issued),
        "expires_at": _timestamp(expires),
        "signature": "",
    }
    manifest["signature"] = base64.b64encode(
        private_key.sign(_canonical_manifest_bytes(manifest))
    ).decode("ascii")

    partial = Path(
        tempfile.mkdtemp(prefix=f".{release.name}.partial-", dir=release.parent)
    )
    try:
        staged_wheel = partial / final_wheel.relative_to(release)
        staged_manifest = partial / final_manifest.relative_to(release)
        staged_trust_store = partial / final_trust_store.relative_to(release)
        staged_wheel.parent.mkdir(parents=True, mode=0o700)
        staged_manifest.parent.mkdir(parents=True, mode=0o700)
        _write_file(staged_wheel, wheel_contents, mode=0o444)
        if hashlib.sha256(staged_wheel.read_bytes()).hexdigest() != wheel_sha256:
            raise PluginStoreBundleError("staged plugin wheel SHA256 mismatch")
        _write_file(staged_manifest, _json_bytes(manifest), mode=0o444)
        _write_file(staged_trust_store, _json_bytes(trust_store), mode=0o444)
        _make_immutable(partial)
        _fsync_directory(staged_wheel.parent)
        _fsync_directory(staged_manifest.parent)
        _fsync_directory(partial)
        try:
            os.replace(partial, release)
        except OSError as error:
            raise PluginStoreBundleError("plugin store bundle publish failed") from error
        _fsync_directory(release.parent)
    except Exception:
        _remove_partial(partial)
        raise

    return PluginStoreBundlePaths(
        release_root=release,
        manifest_path=final_manifest,
        trust_store_path=final_trust_store,
        wheel_path=final_wheel,
    )


__all__ = [
    "PluginStoreBundleError",
    "PluginStoreBundlePaths",
    "assemble_signed_plugin_store_bundle",
]
