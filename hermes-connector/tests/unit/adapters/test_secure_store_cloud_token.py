from __future__ import annotations

import pytest

from hermes_connector.adapters.platform.macos.credentials import (
    MacOSKeychainCloudTokenProvider,
)
from hermes_connector.adapters.secure_store_cloud_token import (
    SecureStoreCloudTokenProvider,
)
from hermes_connector.ports.cloud import CloudCredentialUnavailable


class _Store:
    def __init__(self) -> None:
        self.value: bytes | None = None
        self.available = True

    def check_available(self) -> None:
        if not self.available:
            raise RuntimeError("secret-native-detail")

    async def read_secret(self) -> bytes | None:
        if not self.available:
            raise RuntimeError("secret-native-detail")
        return self.value

    async def create_secret(self, secret: bytes) -> bool:
        if self.value is not None:
            return False
        self.value = secret
        return True

    async def write_secret(self, secret: bytes) -> None:
        self.value = secret

    async def delete_secret(self) -> bool:
        existed = self.value is not None
        self.value = None
        return existed

    async def delete_secret_if_matches(self, expected_sha256: bytes) -> bool:
        del expected_sha256
        raise AssertionError("cloud token provider does not use compare-delete")


@pytest.mark.asyncio
async def test_shared_cloud_token_roundtrip_and_macos_compatibility() -> None:
    store = _Store()
    shared = SecureStoreCloudTokenProvider(store)
    macos = MacOSKeychainCloudTokenProvider(store)

    shared.check()
    await shared.store_access_token("token-123")
    assert await macos.access_token() == "token-123"
    assert "token-123" not in repr(shared)
    assert "token-123" not in repr(macos)

    await macos.clear_access_token()
    with pytest.raises(CloudCredentialUnavailable):
        await shared.access_token()


@pytest.mark.asyncio
@pytest.mark.parametrize("token", ("", " token", "token ", "two words", "line\nbreak"))
async def test_shared_cloud_token_rejects_unsafe_content(token: str) -> None:
    store = _Store()
    provider = SecureStoreCloudTokenProvider(store)

    with pytest.raises(CloudCredentialUnavailable):
        await provider.store_access_token(token)
    assert store.value is None


@pytest.mark.asyncio
async def test_shared_cloud_token_redacts_secure_store_failure() -> None:
    store = _Store()
    store.available = False
    provider = SecureStoreCloudTokenProvider(store)

    with pytest.raises(CloudCredentialUnavailable) as caught:
        await provider.access_token()
    assert "secret-native-detail" not in str(caught.value)
