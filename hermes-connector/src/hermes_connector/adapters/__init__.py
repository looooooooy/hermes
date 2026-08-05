"""Platform-neutral infrastructure adapters and compatibility exports."""

from hermes_connector.adapters.contract_codec import (
    decode_cloud_envelope,
    decode_local_hello,
    decode_local_welcome,
)
from hermes_connector.adapters.sqlite_storage import SQLiteStorageComponent

__all__ = [
    "AlreadyRunning",
    "MacOSInstanceLock",
    "PosixInstanceLock",
    "SQLiteStorageComponent",
    "UnsafeLockFile",
    "decode_cloud_envelope",
    "decode_local_hello",
    "decode_local_welcome",
]

_PLATFORM_LOCK_EXPORTS = frozenset(
    {
        "AlreadyRunning",
        "MacOSInstanceLock",
        "PosixInstanceLock",
        "UnsafeLockFile",
    }
)


def __getattr__(name: str) -> object:
    if name not in _PLATFORM_LOCK_EXPORTS:
        raise AttributeError(name)
    from hermes_connector.adapters.platform.macos.instance_lock import (
        AlreadyRunning,
        MacOSInstanceLock,
        UnsafeLockFile,
    )

    exports = {
        "AlreadyRunning": AlreadyRunning,
        "MacOSInstanceLock": MacOSInstanceLock,
        "PosixInstanceLock": MacOSInstanceLock,
        "UnsafeLockFile": UnsafeLockFile,
    }
    return exports[name]
