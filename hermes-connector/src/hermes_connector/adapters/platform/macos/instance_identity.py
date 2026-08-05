"""Stable non-secret Connector instance identities for macOS."""

from __future__ import annotations

import errno
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

_MAX_STATE_BYTES = 4_096
_READ_FLAGS = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
_WRITE_FLAGS = (
    os.O_WRONLY
    | os.O_CREAT
    | os.O_EXCL
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)
_STATE_FIELDS = frozenset(
    {
        "version",
        "connector_instance_id",
        "client_instance_id",
    }
)


class UnsafeInstanceIdentity(ValueError):
    """The stable instance identity path or content is unsafe."""


@dataclass(frozen=True, slots=True)
class InstanceIdentities:
    connector_instance_id: UUID
    client_instance_id: UUID


class MacOSInstanceIdentityStore:
    """Atomically create or strictly load one private identity state file."""

    __slots__ = ("_path",)

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self._path = Path(path)

    def check_path(self) -> None:
        self._validate_reference()
        if self._path.exists() or self._path.is_symlink():
            self._load()
            return
        self._validate_parent()

    def load_or_create(self) -> InstanceIdentities:
        self._validate_reference()
        if self._path.exists() or self._path.is_symlink():
            return self._load()
        self._validate_parent()
        identities = InstanceIdentities(uuid4(), uuid4())
        self._publish(identities)
        return self._load()

    def _validate_reference(self) -> None:
        if not self._path.is_absolute() or "\x00" in str(self._path):
            raise UnsafeInstanceIdentity("instance identity reference is unsafe")

    def _validate_parent(self) -> None:
        try:
            metadata = self._path.parent.lstat()
        except OSError:
            raise UnsafeInstanceIdentity(
                "instance identity directory is unavailable"
            ) from None
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise UnsafeInstanceIdentity("instance identity directory is unsafe")

    def _load(self) -> InstanceIdentities:
        try:
            before = self._path.lstat()
        except OSError:
            raise UnsafeInstanceIdentity("instance identity is unavailable") from None
        _validate_state_metadata(before)
        try:
            descriptor = os.open(self._path, _READ_FLAGS)
        except OSError:
            raise UnsafeInstanceIdentity("instance identity is unavailable") from None
        try:
            opened = os.fstat(descriptor)
            _validate_state_metadata(opened)
            if opened.st_dev != before.st_dev or opened.st_ino != before.st_ino:
                raise UnsafeInstanceIdentity("instance identity changed during read")
            raw = _read_state(descriptor, opened.st_size)
        finally:
            os.close(descriptor)
        return _decode_identities(raw)

    def _publish(self, identities: InstanceIdentities) -> None:
        raw = _encode_identities(identities)
        temporary = self._path.parent / (
            f".{self._path.name}.{os.getpid()}.{uuid4().hex}.tmp"
        )
        descriptor: int | None = None
        try:
            descriptor = os.open(temporary, _WRITE_FLAGS, 0o600)
            os.fchmod(descriptor, 0o600)
            _write_all(descriptor, raw)
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            try:
                os.link(temporary, self._path, follow_symlinks=False)
            except FileExistsError:
                pass
            except OSError as error:
                if error.errno != errno.EEXIST:
                    raise UnsafeInstanceIdentity(
                        "instance identity could not be published"
                    ) from None
            _fsync_directory(self._path.parent)
        except UnsafeInstanceIdentity:
            raise
        except OSError:
            raise UnsafeInstanceIdentity(
                "instance identity could not be published"
            ) from None
        finally:
            if descriptor is not None:
                os.close(descriptor)
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                raise UnsafeInstanceIdentity(
                    "instance identity temporary cleanup failed"
                ) from None


def _validate_state_metadata(metadata: os.stat_result) -> None:
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or not 1 <= metadata.st_size <= _MAX_STATE_BYTES
    ):
        raise UnsafeInstanceIdentity("instance identity metadata is unsafe")


def _read_state(descriptor: int, expected_size: int) -> bytes:
    raw = os.read(descriptor, _MAX_STATE_BYTES + 1)
    if len(raw) != expected_size or len(raw) > _MAX_STATE_BYTES:
        raise UnsafeInstanceIdentity("instance identity changed during read")
    return raw


def _decode_identities(raw: bytes) -> InstanceIdentities:
    try:
        value = json.loads(raw.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, ValueError):
        raise UnsafeInstanceIdentity("instance identity content is invalid") from None
    if (
        not isinstance(value, dict)
        or frozenset(value) != _STATE_FIELDS
        or type(value.get("version")) is not int
        or value["version"] != 1
    ):
        raise UnsafeInstanceIdentity("instance identity content is invalid")
    connector = _canonical_uuid(value.get("connector_instance_id"))
    client = _canonical_uuid(value.get("client_instance_id"))
    if connector == client:
        raise UnsafeInstanceIdentity("instance identity content is invalid")
    return InstanceIdentities(connector, client)


def _canonical_uuid(value: object) -> UUID:
    if not isinstance(value, str):
        raise UnsafeInstanceIdentity("instance identity content is invalid")
    try:
        parsed = UUID(value)
    except ValueError:
        raise UnsafeInstanceIdentity("instance identity content is invalid") from None
    if str(parsed) != value:
        raise UnsafeInstanceIdentity("instance identity content is invalid")
    return parsed


def _encode_identities(identities: InstanceIdentities) -> bytes:
    return json.dumps(
        {
            "client_instance_id": str(identities.client_instance_id),
            "connector_instance_id": str(identities.connector_instance_id),
            "version": 1,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _write_all(descriptor: int, raw: bytes) -> None:
    view = memoryview(raw)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise UnsafeInstanceIdentity("instance identity write failed")
        view = view[written:]


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
