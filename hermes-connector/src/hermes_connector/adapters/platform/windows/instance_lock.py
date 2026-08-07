from __future__ import annotations

import errno
import msvcrt
import os
from collections.abc import Callable
from pathlib import Path


class InstanceLockError(RuntimeError):
    pass


class AlreadyRunning(InstanceLockError):
    def __init__(self) -> None:
        super().__init__("connector instance already running")


class UnsafeLockFile(InstanceLockError):
    pass


MetadataValidator = Callable[[os.stat_result], None]


class WindowsInstanceLock:
    """Non-blocking Windows byte-range lock retained for process lifetime."""

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        metadata_validator: MetadataValidator | None = None,
    ) -> None:
        self._path = Path(path)
        self._metadata_validator = metadata_validator
        self._fd: int | None = None

    @property
    def is_held(self) -> bool:
        return self._fd is not None

    @property
    def fileno(self) -> int:
        if self._fd is None:
            raise ValueError("instance lock is not held")
        return self._fd

    def acquire(self, *, blocking: bool = False) -> None:
        if self._fd is not None:
            return
        if blocking:
            raise ValueError("Windows Connector instance lock must be non-blocking")
        if not self._path.is_absolute() or ".." in self._path.parts:
            raise UnsafeLockFile("lock path must be absolute and canonical")
        if self._path.is_symlink():
            raise UnsafeLockFile("lock path must not be a symlink")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(self._path, os.O_RDWR | os.O_CREAT | os.O_BINARY, 0o600)
        except OSError as error:
            raise InstanceLockError("unable to open connector instance lock") from error
        try:
            metadata = os.fstat(descriptor)
            if not self._path.is_file() or self._path.is_symlink():
                raise UnsafeLockFile("lock path must be a regular file")
            if self._metadata_validator is not None:
                self._metadata_validator(metadata)
            if metadata.st_size == 0:
                os.write(descriptor, b"0")
                os.fsync(descriptor)
            os.lseek(descriptor, 0, os.SEEK_SET)
            try:
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            except OSError as error:
                if error.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK, 13, 36}:
                    raise AlreadyRunning() from None
                raise InstanceLockError("unable to acquire connector instance lock") from error
        except BaseException:
            os.close(descriptor)
            raise
        self._fd = descriptor

    def close(self) -> None:
        descriptor = self._fd
        if descriptor is None:
            return
        self._fd = None
        try:
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        finally:
            os.close(descriptor)


__all__ = [
    "AlreadyRunning",
    "InstanceLockError",
    "UnsafeLockFile",
    "WindowsInstanceLock",
]
