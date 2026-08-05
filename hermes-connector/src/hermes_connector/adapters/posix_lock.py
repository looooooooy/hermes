"""Compatibility aliases for the former POSIX instance-lock import path."""

from hermes_connector.adapters.platform.macos.instance_lock import (
    AlreadyRunning,
    InstanceLockError,
    MacOSInstanceLock,
    MetadataValidator,
    UnsafeLockFile,
)

PosixInstanceLock = MacOSInstanceLock

__all__ = [
    "AlreadyRunning",
    "InstanceLockError",
    "MetadataValidator",
    "PosixInstanceLock",
    "UnsafeLockFile",
]
