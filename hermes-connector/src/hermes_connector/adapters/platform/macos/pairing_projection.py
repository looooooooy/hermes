"""Private non-secret pairing projections for macOS."""

from __future__ import annotations

import asyncio
import fcntl
import os
import stat
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from typing import Generic, TypeVar
from uuid import UUID, uuid4

from hermes_connector.adapters.pairing_projection_codec import (
    MAX_PAIRING_PROJECTION_BYTES,
    UnsafePairingProjection,
    decode_paired_projection,
    decode_pairing_offer_projection,
    encode_paired_projection,
    encode_pairing_offer_projection,
)
from hermes_connector.domain.pairing import PairedProjection, PairingOfferProjection

_READ_FLAGS = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
_WRITE_FLAGS = (
    os.O_WRONLY
    | os.O_CREAT
    | os.O_EXCL
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)
_LOCK_FLAGS = (
    os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
)
_T = TypeVar("_T")


class _PrivateProjectionStore(Generic[_T]):
    __slots__ = ("_decode", "_encode", "_lock_path", "_path")

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        encode: Callable[[_T], bytes],
        decode: Callable[[bytes], _T],
    ) -> None:
        self._path = Path(path)
        self._lock_path = self._path.parent / f".{self._path.name}.lock"
        self._encode = encode
        self._decode = decode

    async def load(self) -> _T | None:
        return await asyncio.to_thread(self._load_if_present)

    def load_sync(self) -> _T | None:
        return self._load_if_present()

    async def save(self, projection: _T) -> None:
        raw = self._encode(projection)
        await asyncio.to_thread(self._save, raw)

    async def delete(self) -> bool:
        return await asyncio.to_thread(self._delete)

    def check_path(self) -> None:
        self._validate_reference()
        self._validate_parent()
        _validate_lock_path_if_present(self._lock_path)
        if self._path.exists() or self._path.is_symlink():
            self._load()

    def _load_if_present(self) -> _T | None:
        self._validate_reference()
        self._validate_parent()
        if not self._path.exists() and not self._path.is_symlink():
            return None
        return self._load()

    def _load(self) -> _T:
        try:
            before = self._path.lstat()
        except OSError:
            raise UnsafePairingProjection("pairing projection is unavailable") from None
        _validate_file_metadata(before)
        try:
            descriptor = os.open(self._path, _READ_FLAGS)
        except OSError:
            raise UnsafePairingProjection("pairing projection is unavailable") from None
        try:
            opened = os.fstat(descriptor)
            _validate_file_metadata(opened)
            if opened.st_dev != before.st_dev or opened.st_ino != before.st_ino:
                raise UnsafePairingProjection("pairing projection changed during read")
            raw = _read_all(descriptor, opened.st_size)
        finally:
            os.close(descriptor)
        return self._decode(raw)

    def _save(self, raw: bytes) -> None:
        with _exclusive_projection_lock(self._lock_path):
            self._save_unlocked(raw)

    def _save_unlocked(self, raw: bytes) -> None:
        self._validate_reference()
        self._validate_parent()
        if self._path.exists() or self._path.is_symlink():
            self._load()
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
            os.replace(temporary, self._path)
            _fsync_directory(self._path.parent)
        except UnsafePairingProjection:
            raise
        except OSError:
            raise UnsafePairingProjection(
                "pairing projection could not be saved"
            ) from None
        finally:
            if descriptor is not None:
                os.close(descriptor)
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                raise UnsafePairingProjection(
                    "pairing projection temporary cleanup failed"
                ) from None

    def _delete(self) -> bool:
        with _exclusive_projection_lock(self._lock_path):
            return self._delete_unlocked()

    def _delete_unlocked(self) -> bool:
        self._validate_reference()
        self._validate_parent()
        if not self._path.exists() and not self._path.is_symlink():
            return False
        self._load()
        try:
            self._path.unlink()
            _fsync_directory(self._path.parent)
        except OSError:
            raise UnsafePairingProjection(
                "pairing projection could not be deleted"
            ) from None
        return True

    def _validate_reference(self) -> None:
        if not self._path.is_absolute() or "\x00" in str(self._path):
            raise UnsafePairingProjection("pairing projection reference is unsafe")

    def _validate_parent(self) -> None:
        try:
            metadata = self._path.parent.lstat()
        except OSError:
            raise UnsafePairingProjection(
                "pairing projection directory is unavailable"
            ) from None
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise UnsafePairingProjection("pairing projection directory is unsafe")


class MacOSPairedProjectionStore(_PrivateProjectionStore[PairedProjection]):
    def __init__(self, path: str | os.PathLike[str]) -> None:
        super().__init__(
            path,
            encode=encode_paired_projection,
            decode=decode_paired_projection,
        )


class MacOSPairingOfferProjectionStore(_PrivateProjectionStore[PairingOfferProjection]):
    def __init__(self, path: str | os.PathLike[str]) -> None:
        super().__init__(
            path,
            encode=encode_pairing_offer_projection,
            decode=decode_pairing_offer_projection,
        )

    async def delete_if_matches(self, pairing_offer_id: UUID) -> bool:
        if not isinstance(pairing_offer_id, UUID):
            raise TypeError("pairing offer projection version is invalid")
        return await asyncio.to_thread(self._delete_if_matches, pairing_offer_id)

    def _delete_if_matches(self, pairing_offer_id: UUID) -> bool:
        with _exclusive_projection_lock(self._lock_path):
            current = self._load_if_present()
            if current is None or current.pairing_offer_id != pairing_offer_id:
                return False
            return self._delete_unlocked()


def _validate_file_metadata(metadata: os.stat_result) -> None:
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or not 1 <= metadata.st_size <= MAX_PAIRING_PROJECTION_BYTES
    ):
        raise UnsafePairingProjection("pairing projection metadata is unsafe")


def _validate_lock_path_if_present(path: Path) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    except OSError:
        raise UnsafePairingProjection(
            "pairing projection lock is unavailable"
        ) from None
    _validate_lock_metadata(metadata)


def _validate_lock_metadata(metadata: os.stat_result) -> None:
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise UnsafePairingProjection("pairing projection lock is unsafe")


@contextmanager
def _exclusive_projection_lock(path: Path):
    try:
        descriptor = os.open(path, _LOCK_FLAGS, 0o600)
    except OSError:
        raise UnsafePairingProjection(
            "pairing projection lock is unavailable"
        ) from None
    locked = False
    try:
        os.fchmod(descriptor, 0o600)
        opened = os.fstat(descriptor)
        _validate_lock_metadata(opened)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        locked = True
        current = path.lstat()
        _validate_lock_metadata(current)
        if current.st_dev != opened.st_dev or current.st_ino != opened.st_ino:
            raise UnsafePairingProjection("pairing projection lock changed")
        yield
    except UnsafePairingProjection:
        raise
    except OSError:
        raise UnsafePairingProjection("pairing projection lock failed") from None
    finally:
        if locked:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            except OSError:
                pass
        os.close(descriptor)


def _read_all(descriptor: int, expected_size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = expected_size + 1
    while remaining:
        chunk = os.read(descriptor, min(remaining, 4_096))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    raw = b"".join(chunks)
    if len(raw) != expected_size or len(raw) > MAX_PAIRING_PROJECTION_BYTES:
        raise UnsafePairingProjection("pairing projection changed during read")
    return raw


def _write_all(descriptor: int, raw: bytes) -> None:
    view = memoryview(raw)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise UnsafePairingProjection("pairing projection write failed")
        view = view[written:]


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = [
    "MacOSPairedProjectionStore",
    "MacOSPairingOfferProjectionStore",
    "UnsafePairingProjection",
]
