"""Cloud credential providers for the macOS Connector."""

from __future__ import annotations

import asyncio
import os
import stat
from pathlib import Path

from hermes_connector.ports.cloud import CloudCredentialUnavailable
from hermes_connector.ports.secure_storage import SecureSecretStorePort

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


class MacOSKeychainCloudTokenProvider:
    """Persist Cloud access tokens only through a macOS secure-store adapter."""

    __slots__ = ("_store",)

    def __init__(self, store: SecureSecretStorePort) -> None:
        self._store = store

    def check(self) -> None:
        try:
            self._store.check_available()
        except Exception:  # noqa: BLE001 - redact all store failures at boundary
            raise CloudCredentialUnavailable(
                "cloud credential storage is unavailable"
            ) from None

    async def access_token(self) -> str:
        try:
            raw = await self._store.read_secret()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - redact all store failures at boundary
            raise CloudCredentialUnavailable(
                "cloud credential storage is unavailable"
            ) from None
        if raw is None:
            raise CloudCredentialUnavailable("cloud credential is unavailable")
        try:
            token = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            raise CloudCredentialUnavailable(
                "cloud credential encoding is invalid"
            ) from None
        return _validate_token(token)

    async def store_access_token(self, token: str) -> None:
        value = _validate_token(token)
        try:
            await self._store.write_secret(value.encode("utf-8"))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - redact all store failures at boundary
            raise CloudCredentialUnavailable(
                "cloud credential storage is unavailable"
            ) from None

    async def clear_access_token(self) -> None:
        try:
            await self._store.delete_secret()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - redact all store failures at boundary
            raise CloudCredentialUnavailable(
                "cloud credential storage is unavailable"
            ) from None

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


def _validate_token(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8")) > _MAX_TOKEN_BYTES
        or value != value.strip()
        or any(character.isspace() for character in value)
    ):
        raise CloudCredentialUnavailable("cloud credential content is invalid")
    return value
