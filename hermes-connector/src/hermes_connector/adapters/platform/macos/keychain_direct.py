"""Synchronous Keychain item operations used only inside the helper process."""

from __future__ import annotations

from hermes_connector.adapters.platform.macos.security_framework import (
    MacOSSecurityFrameworkAPI,
    SecurityFrameworkAPIPort,
)

_MAX_RAW_SECRET_BYTES = 16_413


class MacOSDirectKeychainSecretStore:
    """Bind one service/account pair to the direct Security.framework adapter."""

    __slots__ = ("_account", "_api", "_service")

    def __init__(
        self,
        *,
        service: bytes,
        account: bytes,
        api: SecurityFrameworkAPIPort | None = None,
    ) -> None:
        self._service = service
        self._account = account
        self._api = api or MacOSSecurityFrameworkAPI()

    def check_available(self) -> None:
        self._api.check_available()

    def read_raw(self) -> bytes | None:
        return self._api.read_generic_password(
            self._service,
            self._account,
            max_secret_bytes=_MAX_RAW_SECRET_BYTES,
        )

    def create_raw(self, value: bytes) -> bool:
        return self._api.create_generic_password(
            self._service,
            self._account,
            value,
        )

    def write_raw(self, value: bytes) -> None:
        self._api.write_generic_password(
            self._service,
            self._account,
            value,
        )

    def delete_raw_if_digest(self, expected_sha256: bytes) -> bool:
        return self._api.delete_generic_password_if_matches(
            self._service,
            self._account,
            expected_sha256=expected_sha256,
            max_secret_bytes=_MAX_RAW_SECRET_BYTES,
        )

    def __repr__(self) -> str:
        return "MacOSDirectKeychainSecretStore(<keychain-reference>)"
