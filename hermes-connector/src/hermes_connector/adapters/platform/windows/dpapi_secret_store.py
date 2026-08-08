from __future__ import annotations

import asyncio
import ctypes
import hashlib
import hmac
import os
from ctypes import wintypes
from pathlib import Path

from hermes_connector.ports.secure_storage import SecureStorageError

from .private_state import (
    UnsafeWindowsPrivateState,
    atomic_write_private_file,
    delete_private_file,
    ensure_private_directory,
    private_named_mutex,
    read_private_file,
    validate_private_directory,
)

_CRYPTPROTECT_UI_FORBIDDEN = 0x00000001
_MAX_SECRET_BYTES = 16_384
_MAX_PROTECTED_BYTES = _MAX_SECRET_BYTES + 65_536
_DESCRIPTION = "Hermes Connector Secret"


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


_LIBRARIES: tuple[object, object] | None = None


def _libraries() -> tuple[object, object]:
    global _LIBRARIES
    if os.name != "nt":
        raise RuntimeError("Windows DPAPI secret store requires Windows")
    if _LIBRARIES is None:
        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        _configure(crypt32, kernel32)
        _LIBRARIES = crypt32, kernel32
    return _LIBRARIES


def _configure(crypt32: object, kernel32: object) -> None:
    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        wintypes.LPCWSTR,
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptProtectData.restype = wintypes.BOOL
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p


def _validated_label(value: str, *, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or "\x00" in value
        or "\r" in value
        or "\n" in value
        or len(value.encode("utf-8")) > 512
    ):
        raise ValueError(f"Windows secret {field} is invalid")
    return value


def _validated_secret(secret: bytes) -> bytes:
    if (
        not isinstance(secret, bytes)
        or not 1 <= len(secret) <= _MAX_SECRET_BYTES
        or b"\x00" in secret
        or b"\r" in secret
        or b"\n" in secret
    ):
        raise SecureStorageError("secret payload is invalid")
    return secret


class WindowsDPAPISecretStore:
    """Current-user DPAPI secret with private-file persistence and CAS delete."""

    def __init__(
        self,
        *,
        root_directory: Path,
        service: str,
        account: str,
    ) -> None:
        if not isinstance(root_directory, Path) or not root_directory.is_absolute():
            raise ValueError("Windows secret root must be an absolute Path")
        self._root_directory = root_directory
        self._service = _validated_label(service, field="service")
        self._account = _validated_label(account, field="account")
        digest = hashlib.sha256(
            f"{self._service}\0{self._account}".encode("utf-8")
        ).hexdigest()
        self._path = root_directory / f"{digest}.dpapi"
        self._mutex_key = f"dpapi-secret:{root_directory}:{digest}"
        self._entropy = hashlib.sha256(
            b"HermesConnectorDPAPI\0"
            + self._service.encode("utf-8")
            + b"\0"
            + self._account.encode("utf-8")
        ).digest()

    @property
    def path(self) -> Path:
        return self._path

    def check_available(self) -> None:
        _libraries()
        validate_private_directory(self._root_directory)

    @staticmethod
    def provision_root(root_directory: Path) -> Path:
        return ensure_private_directory(root_directory)

    async def read_secret(self) -> bytes | None:
        return await asyncio.to_thread(self._read_sync)

    async def create_secret(self, secret: bytes) -> bool:
        payload = _validated_secret(secret)
        return await asyncio.to_thread(self._create_sync, payload)

    async def write_secret(self, secret: bytes) -> None:
        payload = _validated_secret(secret)
        await asyncio.to_thread(self._write_sync, payload)

    async def delete_secret(self) -> bool:
        return await asyncio.to_thread(self._delete_sync)

    async def delete_secret_if_matches(self, expected_sha256: bytes) -> bool:
        if not isinstance(expected_sha256, bytes) or len(expected_sha256) != 32:
            raise ValueError("expected_sha256 must be 32 bytes")
        return await asyncio.to_thread(self._compare_delete_sync, expected_sha256)

    def _read_sync(self) -> bytes | None:
        with private_named_mutex(self._mutex_key):
            protected = read_private_file(
                self._path,
                maximum=_MAX_PROTECTED_BYTES,
            )
            if protected is None:
                return None
            return _validated_secret(self._unprotect(protected))

    def _create_sync(self, secret: bytes) -> bool:
        with private_named_mutex(self._mutex_key):
            existing = read_private_file(
                self._path,
                maximum=_MAX_PROTECTED_BYTES,
            )
            if existing is not None:
                return False
            atomic_write_private_file(
                self._path,
                self._protect(secret),
                maximum=_MAX_PROTECTED_BYTES,
            )
            return True

    def _write_sync(self, secret: bytes) -> None:
        with private_named_mutex(self._mutex_key):
            atomic_write_private_file(
                self._path,
                self._protect(secret),
                maximum=_MAX_PROTECTED_BYTES,
            )

    def _delete_sync(self) -> bool:
        with private_named_mutex(self._mutex_key):
            return delete_private_file(self._path)

    def _compare_delete_sync(self, expected_sha256: bytes) -> bool:
        with private_named_mutex(self._mutex_key):
            protected = read_private_file(
                self._path,
                maximum=_MAX_PROTECTED_BYTES,
            )
            if protected is None:
                return False
            secret = _validated_secret(self._unprotect(protected))
            if not hmac.compare_digest(hashlib.sha256(secret).digest(), expected_sha256):
                return False
            return delete_private_file(self._path)

    def _protect(self, secret: bytes) -> bytes:
        try:
            crypt32, kernel32 = _libraries()
            data, data_buffer = _blob(secret)
            entropy, entropy_buffer = _blob(self._entropy)
            output = _DataBlob()
            if not crypt32.CryptProtectData(
                ctypes.byref(data),
                _DESCRIPTION,
                ctypes.byref(entropy),
                None,
                None,
                _CRYPTPROTECT_UI_FORBIDDEN,
                ctypes.byref(output),
            ):
                raise OSError(ctypes.get_last_error(), "CryptProtectData failed")
            del data_buffer, entropy_buffer
            try:
                if not output.pbData or not 1 <= output.cbData <= _MAX_PROTECTED_BYTES:
                    raise SecureStorageError("protected secret size is invalid")
                return ctypes.string_at(output.pbData, output.cbData)
            finally:
                kernel32.LocalFree(ctypes.cast(output.pbData, ctypes.c_void_p))
        except SecureStorageError:
            raise
        except (OSError, RuntimeError, UnsafeWindowsPrivateState) as error:
            raise SecureStorageError("Windows secure storage protect failed") from error

    def _unprotect(self, protected: bytes) -> bytes:
        try:
            crypt32, kernel32 = _libraries()
            data, data_buffer = _blob(protected)
            entropy, entropy_buffer = _blob(self._entropy)
            output = _DataBlob()
            description = wintypes.LPWSTR()
            if not crypt32.CryptUnprotectData(
                ctypes.byref(data),
                ctypes.byref(description),
                ctypes.byref(entropy),
                None,
                None,
                _CRYPTPROTECT_UI_FORBIDDEN,
                ctypes.byref(output),
            ):
                raise OSError(ctypes.get_last_error(), "CryptUnprotectData failed")
            del data_buffer, entropy_buffer
            try:
                if not output.pbData or not 1 <= output.cbData <= _MAX_SECRET_BYTES:
                    raise SecureStorageError("unprotected secret size is invalid")
                return ctypes.string_at(output.pbData, output.cbData)
            finally:
                if description:
                    kernel32.LocalFree(ctypes.cast(description, ctypes.c_void_p))
                kernel32.LocalFree(ctypes.cast(output.pbData, ctypes.c_void_p))
        except SecureStorageError:
            raise
        except (OSError, RuntimeError, UnsafeWindowsPrivateState) as error:
            raise SecureStorageError("Windows secure storage unprotect failed") from error


def _blob(raw: bytes) -> tuple[_DataBlob, ctypes.Array[ctypes.c_ubyte]]:
    buffer = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
    return (
        _DataBlob(
            cbData=len(raw),
            pbData=ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)),
        ),
        buffer,
    )


__all__ = ["WindowsDPAPISecretStore"]
