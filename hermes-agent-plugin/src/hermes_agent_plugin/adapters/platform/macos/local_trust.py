"""macOS trust checks for private Local Gateway filesystem resources."""

from __future__ import annotations

import os
import stat
from collections.abc import Callable
from pathlib import Path

from ...local_protocol.control_v1 import is_canonical_client_instance_id
from ...local_protocol.frame_codec import FrameCodecError, decode_frame
from ...local_protocol.profile import validate_profile

MAX_REGISTRY_BYTES = 16 * 1_024


def validate_instance_id(value: object) -> str:
    """Require an RFC 4122 UUID in lowercase canonical hyphenated form."""
    if not is_canonical_client_instance_id(value):
        raise ValueError("instance_id must be a canonical UUID")
    return value


def is_private_directory(path: Path) -> bool:
    directory = Path(path)
    if not directory.is_absolute():
        return False
    try:
        metadata = directory.lstat()
    except OSError:
        return False
    return (
        stat.S_ISDIR(metadata.st_mode)
        and metadata.st_uid == os.getuid()
        and stat.S_IMODE(metadata.st_mode) & 0o077 == 0
    )


def ensure_private_directory(path: Path) -> Path:
    directory = Path(path)
    if not directory.is_absolute():
        raise ValueError("untrusted local directory")
    try:
        directory.mkdir(parents=True, mode=0o700)
    except FileExistsError:
        pass
    if not is_private_directory(directory):
        raise ValueError("untrusted local directory")
    return directory


def _is_direct_child(path: Path, directory: Path) -> bool:
    child = Path(path)
    parent = Path(directory)
    return child.is_absolute() and parent.is_absolute() and child.parent == parent


def _is_private_regular_file(metadata: os.stat_result) -> bool:
    return (
        stat.S_ISREG(metadata.st_mode)
        and metadata.st_uid == os.getuid()
        and stat.S_IMODE(metadata.st_mode) == 0o600
    )


def _read_bounded_file(descriptor: int) -> bytes | None:
    chunks: list[bytes] = []
    size = 0
    while size <= MAX_REGISTRY_BYTES:
        chunk = os.read(
            descriptor,
            min(4_096, MAX_REGISTRY_BYTES + 1 - size),
        )
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        size += len(chunk)
    return None


def read_private_registry(
    path: Path,
    *,
    directory: Path,
) -> dict | None:
    registry_path = Path(path)
    registry_directory = Path(directory)
    if not is_private_directory(registry_directory) or not _is_direct_child(
        registry_path, registry_directory
    ):
        return None
    try:
        before_open = registry_path.lstat()
    except OSError:
        return None
    if not _is_private_regular_file(before_open):
        return None

    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(
            os,
            "O_NOFOLLOW",
            0,
        )
    )
    try:
        descriptor = os.open(registry_path, flags)
    except OSError:
        return None
    try:
        after_open = os.fstat(descriptor)
        if (
            not _is_private_regular_file(after_open)
            or after_open.st_dev != before_open.st_dev
            or after_open.st_ino != before_open.st_ino
        ):
            return None
        body = _read_bounded_file(descriptor)
    except OSError:
        return None
    finally:
        os.close(descriptor)
    if body is None:
        return None
    try:
        return decode_frame(body)
    except FrameCodecError:
        return None


def is_private_socket(
    path: Path,
    *,
    directory: Path,
) -> bool:
    socket_path = Path(path)
    socket_directory = Path(directory)
    if (
        not is_private_directory(socket_directory)
        or not _is_direct_child(socket_path, socket_directory)
        or socket_path.suffix != ".sock"
    ):
        return False
    try:
        metadata = socket_path.lstat()
    except OSError:
        return False
    return (
        stat.S_ISSOCK(metadata.st_mode)
        and metadata.st_uid == os.getuid()
        and stat.S_IMODE(metadata.st_mode) == 0o600
    )


def _unlink_private_entry(
    path: Path,
    *,
    directory: Path,
    validator: Callable[[os.stat_result], bool],
) -> bool:
    target = Path(path)
    parent = Path(directory)
    if not is_private_directory(parent) or not _is_direct_child(target, parent):
        return False
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(
            os,
            "O_DIRECTORY",
            0,
        )
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(parent, flags)
    except OSError:
        return False
    try:
        directory_metadata = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(directory_metadata.st_mode)
            or directory_metadata.st_uid != os.getuid()
            or stat.S_IMODE(directory_metadata.st_mode) & 0o077
        ):
            return False
        target_metadata = os.stat(
            target.name,
            dir_fd=descriptor,
            follow_symlinks=False,
        )
        if not validator(target_metadata):
            return False
        os.unlink(target.name, dir_fd=descriptor)
        return True
    except OSError:
        return False
    finally:
        os.close(descriptor)


def unlink_private_registry(
    path: Path,
    *,
    directory: Path,
) -> bool:
    return _unlink_private_entry(
        path,
        directory=directory,
        validator=_is_private_regular_file,
    )


def unlink_private_socket(
    path: Path,
    *,
    directory: Path,
) -> bool:
    def is_owned_socket(metadata: os.stat_result) -> bool:
        return (
            stat.S_ISSOCK(metadata.st_mode)
            and metadata.st_uid == os.getuid()
            and stat.S_IMODE(metadata.st_mode) == 0o600
        )

    return _unlink_private_entry(
        path,
        directory=directory,
        validator=is_owned_socket,
    )


def unlink_owned_socket(
    path: Path,
    *,
    directory: Path,
) -> bool:
    """Remove an owned socket during failed initialization, regardless of mode."""

    def is_owned_socket(metadata: os.stat_result) -> bool:
        return stat.S_ISSOCK(metadata.st_mode) and metadata.st_uid == os.getuid()

    return _unlink_private_entry(
        path,
        directory=directory,
        validator=is_owned_socket,
    )


__all__ = [
    "MAX_REGISTRY_BYTES",
    "ensure_private_directory",
    "is_private_directory",
    "is_private_socket",
    "read_private_registry",
    "unlink_owned_socket",
    "unlink_private_registry",
    "unlink_private_socket",
    "validate_instance_id",
    "validate_profile",
]
