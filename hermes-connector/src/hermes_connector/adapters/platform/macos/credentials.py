"""Cloud credential providers for the macOS Connector."""

from __future__ import annotations

import asyncio
import os
import stat
from pathlib import Path

from hermes_connector.adapters.secure_store_cloud_token import (
    SecureStoreCloudTokenProvider,
)
from hermes_connector.ports.cloud import CloudCredentialUnavailable

_MAX_TOKEN_BYTES = 16_384
_READ_FLAGS = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)


class UnsafeCredentialFile(CloudCredentialUnavailable):
    """The configured credential reference or file is unsafe."""


class MacOSFileCloudTokenProvider:
    """Read an explicit one-shot migration source, never an OS store."""

    __slots__ = ("_cleared", "_path")

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self._path = Path(path)
        self._cleared = False

    async def access_token(self) -> str:
        if self._cleared:
            raise UnsafeCredentialFile("cloud credential is unavailable")
        return await asyncio.to_thread(self._read_token)

    async def clear_access_token(self) -> None:
        self._cleared = True

    def check(self) -> None:
        self._read_token()

    def check_reference(self) -> None:
        """Validate migration source metadata without reading legacy plaintext."""

        if not self._path.is_absolute() or "\x00" in str(self._path):
            raise UnsafeCredentialFile("cloud credential reference is unsafe")
        try:
            before = self._path.lstat()
        except OSError:
            raise UnsafeCredentialFile("cloud credential is unavailable") from None
        _validate_metadata(before)
        try:
            descriptor = os.open(self._path, _READ_FLAGS)
        except OSError:
            raise UnsafeCredentialFile("cloud credential is unavailable") from None
        try:
            opened = os.fstat(descriptor)
            _validate_metadata(opened)
            if opened.st_dev != before.st_dev or opened.st_ino != before.st_ino:
                raise UnsafeCredentialFile("cloud credential changed during validation")
        finally:
            os.close(descriptor)

    def __repr__(self) -> str:
        return "MacOSFileCloudTokenProvider(<credential-reference>)"

    def _read_token(self) -> str:
        if not self._path.is_absolute() or "\x00" in str(self._path):
            raise UnsafeCredentialFile("cloud credential reference is unsafe")
        try:
            before = self._path.lstat()
        except OSError:
            raise UnsafeCredentialFile("cloud credential is unavailable") from None
        _validate_metadata(before)

        try:
            descriptor = os.open(self._path, _READ_FLAGS)
        except OSError:
            raise UnsafeCredentialFile("cloud credential is unavailable") from None
        try:
            opened = os.fstat(descriptor)
            _validate_metadata(opened)
            if opened.st_dev != before.st_dev or opened.st_ino != before.st_ino:
                raise UnsafeCredentialFile("cloud credential changed during read")
            raw = _read_bounded(descriptor, opened.st_size)
        finally:
            os.close(descriptor)

        try:
            text = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            raise UnsafeCredentialFile("cloud credential encoding is invalid") from None
        token = text.rstrip("\r\n")
        if (
            not token
            or token != token.strip()
            or any(character.isspace() for character in token)
        ):
            raise UnsafeCredentialFile("cloud credential content is invalid")
        return token


class MacOSKeychainCloudTokenProvider(SecureStoreCloudTokenProvider):
    """Compatibility wrapper preserving the established macOS adapter name."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "MacOSKeychainCloudTokenProvider(<keychain-reference>)"


def _validate_metadata(metadata: os.stat_result) -> None:
    if not stat.S_ISREG(metadata.st_mode):
        raise UnsafeCredentialFile("cloud credential must be a regular file")
    if metadata.st_uid != os.geteuid():
        raise UnsafeCredentialFile("cloud credential owner is invalid")
    if stat.S_IMODE(metadata.st_mode) & ~0o600:
        raise UnsafeCredentialFile("cloud credential permissions exceed 0600")
    if not 1 <= metadata.st_size <= _MAX_TOKEN_BYTES:
        raise UnsafeCredentialFile("cloud credential size is invalid")


def _read_bounded(descriptor: int, expected_size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = expected_size + 1
    while remaining:
        chunk = os.read(descriptor, min(remaining, 4096))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    raw = b"".join(chunks)
    if len(raw) != expected_size or len(raw) > _MAX_TOKEN_BYTES:
        raise UnsafeCredentialFile("cloud credential changed during read")
    return raw


__all__ = [
    "MacOSFileCloudTokenProvider",
    "MacOSKeychainCloudTokenProvider",
    "UnsafeCredentialFile",
]
