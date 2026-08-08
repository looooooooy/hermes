from __future__ import annotations

import os
from pathlib import Path

from .private_state import (
    atomic_write_private_file,
    private_file_exists,
    private_named_mutex,
    validate_private_directory,
    validate_private_file,
)


def ensure_private_empty_file(path: str | os.PathLike[str]) -> Path:
    """Create a zero-length current-user-only file without an insecure create gap."""

    target = Path(path)
    validate_private_directory(target.parent)
    mutex_key = f"private-empty-file:{target}"
    with private_named_mutex(mutex_key):
        if private_file_exists(target):
            validate_private_file(target)
            return target
        atomic_write_private_file(target, b"\x00", maximum=1)
        flags = os.O_RDWR | getattr(os, "O_BINARY", 0)
        descriptor = os.open(target, flags)
        try:
            os.ftruncate(descriptor, 0)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        validate_private_file(target)
    return target


__all__ = ["ensure_private_empty_file"]
