"""Stable Connector instance identities over Windows private state."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from hermes_connector.adapters.instance_identity_state import (
    InstanceIdentities,
    MAX_INSTANCE_IDENTITY_BYTES,
    UnsafeInstanceIdentity,
    decode_instance_identities,
    encode_instance_identities,
)

from .private_state import (
    UnsafeWindowsPrivateState,
    atomic_write_private_file,
    private_named_mutex,
    read_private_file,
    validate_private_directory,
)


class WindowsInstanceIdentityStore:
    """Atomically create or strictly load one private Windows identity state."""

    __slots__ = ("_mutex_key", "_path")

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._mutex_key = f"instance-identity:{self._path}"

    def check_path(self) -> None:
        try:
            self._validate_reference()
            validate_private_directory(self._path.parent)
            raw = read_private_file(
                self._path,
                maximum=MAX_INSTANCE_IDENTITY_BYTES,
            )
            if raw is not None:
                decode_instance_identities(raw)
        except UnsafeInstanceIdentity:
            raise
        except (OSError, RuntimeError, ValueError, UnsafeWindowsPrivateState):
            raise UnsafeInstanceIdentity("instance identity path is unsafe") from None

    def load_or_create(self) -> InstanceIdentities:
        try:
            self._validate_reference()
            validate_private_directory(self._path.parent)
            with private_named_mutex(self._mutex_key):
                raw = read_private_file(
                    self._path,
                    maximum=MAX_INSTANCE_IDENTITY_BYTES,
                )
                if raw is not None:
                    return decode_instance_identities(raw)
                identities = InstanceIdentities(uuid4(), uuid4())
                atomic_write_private_file(
                    self._path,
                    encode_instance_identities(identities),
                    maximum=MAX_INSTANCE_IDENTITY_BYTES,
                )
                persisted = read_private_file(
                    self._path,
                    maximum=MAX_INSTANCE_IDENTITY_BYTES,
                )
                if persisted is None:
                    raise UnsafeInstanceIdentity("instance identity is unavailable")
                return decode_instance_identities(persisted)
        except UnsafeInstanceIdentity:
            raise
        except (OSError, RuntimeError, ValueError, UnsafeWindowsPrivateState):
            raise UnsafeInstanceIdentity("instance identity is unavailable") from None

    def _validate_reference(self) -> None:
        if not self._path.is_absolute() or "\x00" in str(self._path):
            raise UnsafeInstanceIdentity("instance identity reference is unsafe")


__all__ = [
    "InstanceIdentities",
    "UnsafeInstanceIdentity",
    "WindowsInstanceIdentityStore",
]
