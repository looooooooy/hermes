from __future__ import annotations

import errno
import fcntl
import os
import stat
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


class MacOSInstanceLock:
    """macOS process lock that never removes the dedicated lock file."""

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

        descriptor = self._open_lock_file()
        try:
            metadata = os.fstat(descriptor)
            self._validate_metadata(metadata)
            if self._metadata_validator is not None:
                self._metadata_validator(metadata)
            try:
                flags = fcntl.LOCK_EX
                if not blocking:
                    flags |= fcntl.LOCK_NB
                fcntl.flock(descriptor, flags)
            except OSError as error:
                if error.errno in {errno.EACCES, errno.EAGAIN}:
                    raise AlreadyRunning() from None
                raise InstanceLockError(
                    "unable to acquire connector instance lock"
                ) from error
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
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    def _open_lock_file(self) -> int:
        common_flags = os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC
        try:
            descriptor = os.open(
                self._path,
                common_flags | os.O_CREAT | os.O_EXCL,
                0o600,
            )
        except FileExistsError:
            try:
                return os.open(self._path, common_flags)
            except OSError as error:
                raise self._open_error(error) from None
        except OSError as error:
            raise self._open_error(error) from None

        try:
            os.fchmod(descriptor, 0o600)
        except BaseException:
            os.close(descriptor)
            raise
        return descriptor

    @staticmethod
    def _open_error(error: OSError) -> InstanceLockError:
        if error.errno in {errno.ELOOP, errno.EMLINK}:
            return UnsafeLockFile("lock path must not be a symlink")
        if error.errno == errno.EISDIR:
            return UnsafeLockFile("lock path must be a regular file")
        return InstanceLockError("unable to open connector instance lock")

    @staticmethod
    def _validate_metadata(metadata: os.stat_result) -> None:
        if not stat.S_ISREG(metadata.st_mode):
            raise UnsafeLockFile("lock path must be a regular file")
        if metadata.st_uid != os.getuid():
            raise UnsafeLockFile("lock file owner is not the current uid")
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            raise UnsafeLockFile("lock file permissions must be 0600")
