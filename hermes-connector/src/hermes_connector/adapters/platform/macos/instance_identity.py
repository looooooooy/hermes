"""Stable non-secret Connector instance identities for macOS."""

from __future__ import annotations

import errno
import os
import stat
from pathlib import Path
from uuid import uuid4

from hermes_connector.adapters.instance_identity_state import (
    MAX_INSTANCE_IDENTITY_BYTES,
    InstanceIdentities,
    UnsafeInstanceIdentity,
    decode_instance_identities,
    encode_instance_identities,
)

_READ_FLAGS = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
_WRITE_FLAGS = (
    os.O_WRONLY
    | os.O_CREAT
    | os.O_EXCL
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)


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
        return decode_instance_identities(raw)

    def _publish(self, identities: InstanceIdentities) -> None:
        raw = encode_instance_identities(identities)
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
        or not 1 <= metadata.st_size <= MAX_INSTANCE_IDENTITY_BYTES
    ):
        raise UnsafeInstanceIdentity("instance identity metadata is unsafe")


def _read_state(descriptor: int, expected_size: int) -> bytes:
    raw = os.read(descriptor, MAX_INSTANCE_IDENTITY_BYTES + 1)
    if len(raw) != expected_size or len(raw) > MAX_INSTANCE_IDENTITY_BYTES:
        raise UnsafeInstanceIdentity("instance identity changed during read")
    return raw


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


__all__ = [
    "InstanceIdentities",
    "MacOSInstanceIdentityStore",
    "UnsafeInstanceIdentity",
]
