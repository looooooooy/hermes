"""Bounded async macOS Keychain storage backed by a helper-process broker."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import TypeVar

from hermes_connector.adapters.platform.macos.keychain_broker import (
    MacOSKeychainBroker,
)
from hermes_connector.adapters.platform.macos.keychain_errors import (
    KeychainBrokerEffectUnknown,
    KeychainSecretUnavailable,
)

_MAX_SECRET_BYTES = 16_384
_Result = TypeVar("_Result")


class MacOSKeychainSecretStore:
    """Expose one generic-password item without parent-process native calls."""

    __slots__ = ("_account", "_broker", "_service")

    def __init__(
        self,
        *,
        service: str,
        account: str,
        broker: MacOSKeychainBroker | None = None,
    ) -> None:
        self._service = _validate_reference(service)
        self._account = _validate_reference(account)
        self._broker = broker or MacOSKeychainBroker()

    def check_available(self) -> None:
        try:
            self._broker.check_available()
        except Exception:  # noqa: BLE001 - redact all native failures at boundary
            raise KeychainSecretUnavailable("macOS Keychain is unavailable") from None

    async def read_secret(self) -> bytes | None:
        value = await self._invoke(
            self._broker.read_secret(self._service, self._account)
        )
        if value is None:
            return None
        return _validate_secret(value)

    async def create_secret(self, secret: bytes) -> bool:
        value = _validate_secret(secret)
        result = await self._invoke(
            self._broker.create_secret(self._service, self._account, value)
        )
        if type(result) is not bool:
            raise KeychainSecretUnavailable("macOS Keychain create failed")
        return result

    async def write_secret(self, secret: bytes) -> None:
        value = _validate_secret(secret)
        await self._invoke(
            self._broker.write_secret(self._service, self._account, value)
        )

    async def delete_secret(self) -> bool:
        result = await self._invoke(
            self._broker.delete_secret(self._service, self._account)
        )
        if type(result) is not bool:
            raise KeychainSecretUnavailable("macOS Keychain delete failed")
        return result

    async def delete_secret_if_matches(self, expected_sha256: bytes) -> bool:
        if not isinstance(expected_sha256, bytes) or len(expected_sha256) != 32:
            raise KeychainSecretUnavailable(
                "macOS Keychain comparison digest is invalid"
            )
        result = await self._invoke(
            self._broker.delete_secret_if_matches(
                self._service,
                self._account,
                expected_sha256=expected_sha256,
            )
        )
        if type(result) is not bool:
            raise KeychainSecretUnavailable("macOS Keychain compare-delete failed")
        return result

    @staticmethod
    async def _invoke(operation: Awaitable[_Result]) -> _Result:
        try:
            return await operation
        except asyncio.CancelledError:
            raise
        except KeychainSecretUnavailable:
            raise
        except Exception:  # noqa: BLE001 - redact broker failures at boundary
            raise KeychainSecretUnavailable("macOS Keychain operation failed") from None

    def __repr__(self) -> str:
        return "MacOSKeychainSecretStore(<keychain-reference>)"


__all__ = [
    "KeychainBrokerEffectUnknown",
    "KeychainSecretUnavailable",
    "MacOSKeychainSecretStore",
]


def _validate_reference(value: str) -> bytes:
    if (
        not isinstance(value, str)
        or not 1 <= len(value.encode("utf-8")) <= 255
        or any(character in value for character in ("\x00", "\r", "\n"))
        or any(ord(character) < 0x20 for character in value)
    ):
        raise KeychainSecretUnavailable("macOS Keychain reference is unsafe")
    return value.encode("utf-8")


def _validate_secret(value: bytes) -> bytes:
    if (
        not isinstance(value, bytes)
        or not 1 <= len(value) <= _MAX_SECRET_BYTES
        or any(character in value for character in (0, 10, 13))
    ):
        raise KeychainSecretUnavailable("macOS Keychain secret is invalid")
    return value
