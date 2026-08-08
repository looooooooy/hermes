"""Platform-neutral Cloud token provider over a secure secret-store port."""

from __future__ import annotations

import asyncio

from hermes_connector.ports.cloud import CloudCredentialUnavailable
from hermes_connector.ports.secure_storage import SecureSecretStorePort

_MAX_TOKEN_BYTES = 16_384


class SecureStoreCloudTokenProvider:
    """Persist Cloud access tokens only through the selected OS secure store."""

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
        return "SecureStoreCloudTokenProvider(<secure-store-reference>)"


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


__all__ = ["SecureStoreCloudTokenProvider"]
