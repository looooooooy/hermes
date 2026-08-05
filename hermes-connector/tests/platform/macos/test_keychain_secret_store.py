from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from hermes_connector.adapters.platform.macos.keychain import (
    KeychainSecretUnavailable,
    MacOSKeychainSecretStore,
)

CONNECTOR_ROOT = Path(__file__).parents[3]


class _FakeSecurityFrameworkAPI:
    def __init__(self) -> None:
        self.available_checks = 0
        self.calls: list[str] = []
        self.secret: bytes | None = None
        self.failure: Exception | None = None
        self.replace_before_compare_delete: bytes | None = None

    def check_available(self) -> None:
        self.available_checks += 1
        self.calls.append("check_available")
        self._fail_if_requested()

    def read_generic_password(
        self,
        service: bytes,
        account: bytes,
        *,
        max_secret_bytes: int,
    ) -> bytes | None:
        self.calls.append("read")
        self._validate_reference(service, account)
        assert max_secret_bytes == 16_384
        self._fail_if_requested()
        return self.secret

    def create_generic_password(
        self,
        service: bytes,
        account: bytes,
        secret: bytes,
    ) -> bool:
        self.calls.append("create")
        self._validate_reference(service, account)
        self._fail_if_requested()
        if self.secret is not None:
            return False
        self.secret = secret
        return True

    def write_generic_password(
        self,
        service: bytes,
        account: bytes,
        secret: bytes,
    ) -> None:
        self.calls.append("write")
        self._validate_reference(service, account)
        self._fail_if_requested()
        self.secret = secret

    def delete_generic_password(self, service: bytes, account: bytes) -> bool:
        self.calls.append("delete")
        self._validate_reference(service, account)
        self._fail_if_requested()
        if self.secret is None:
            return False
        self.secret = None
        return True

    def delete_generic_password_if_matches(
        self,
        service: bytes,
        account: bytes,
        *,
        expected_sha256: bytes,
        max_secret_bytes: int,
    ) -> bool:
        self.calls.append("compare_delete")
        self._validate_reference(service, account)
        assert max_secret_bytes == 16_384
        self._fail_if_requested()
        if self.replace_before_compare_delete is not None:
            self.secret = self.replace_before_compare_delete
            self.replace_before_compare_delete = None
        if self.secret is None:
            return False
        if hashlib.sha256(self.secret).digest() != expected_sha256:
            return False
        self.secret = None
        return True

    def _validate_reference(self, service: bytes, account: bytes) -> None:
        assert service == b"wiki.seaotter.hermes.connector.test"
        assert account == b"v1:test-device"

    def _fail_if_requested(self) -> None:
        if self.failure is not None:
            raise self.failure

    def __repr__(self) -> str:
        return "_FakeSecurityFrameworkAPI(<native-state-redacted>)"


class _FakeBroker:
    def __init__(self, api: _FakeSecurityFrameworkAPI) -> None:
        self.api = api

    def check_available(self) -> None:
        self.api.check_available()

    async def read_secret(self, service: bytes, account: bytes) -> bytes | None:
        return self.api.read_generic_password(
            service,
            account,
            max_secret_bytes=16_384,
        )

    async def create_secret(
        self,
        service: bytes,
        account: bytes,
        secret: bytes,
    ) -> bool:
        return self.api.create_generic_password(service, account, secret)

    async def write_secret(
        self,
        service: bytes,
        account: bytes,
        secret: bytes,
    ) -> None:
        self.api.write_generic_password(service, account, secret)

    async def delete_secret(self, service: bytes, account: bytes) -> bool:
        return self.api.delete_generic_password(service, account)

    async def delete_secret_if_matches(
        self,
        service: bytes,
        account: bytes,
        *,
        expected_sha256: bytes,
    ) -> bool:
        return self.api.delete_generic_password_if_matches(
            service,
            account,
            expected_sha256=expected_sha256,
            max_secret_bytes=16_384,
        )


def _store(api: _FakeSecurityFrameworkAPI) -> MacOSKeychainSecretStore:
    return MacOSKeychainSecretStore(
        service="wiki.seaotter.hermes.connector.test",
        account="v1:test-device",
        broker=_FakeBroker(api),  # type: ignore[arg-type]
    )


def test_availability_check_does_not_read_or_mutate_keychain() -> None:
    api = _FakeSecurityFrameworkAPI()
    store = _store(api)

    store.check_available()

    assert api.calls == ["check_available"]
    assert api.secret is None


@pytest.mark.asyncio
async def test_store_uses_native_api_for_bounded_create_read_write_delete() -> None:
    api = _FakeSecurityFrameworkAPI()
    store = _store(api)
    secret = b"secret-that-never-enters-argv"

    assert await store.read_secret() is None
    assert await store.create_secret(secret)
    assert not await store.create_secret(b"must-not-overwrite")
    assert await store.read_secret() == secret
    await store.write_secret(b"rotated-secret")
    assert await store.read_secret() == b"rotated-secret"
    assert await store.delete_secret()
    assert not await store.delete_secret()

    assert api.calls == [
        "read",
        "create",
        "create",
        "read",
        "write",
        "read",
        "delete",
        "delete",
    ]
    assert secret.decode() not in repr(store)
    assert secret.decode() not in repr(api)


@pytest.mark.asyncio
async def test_compare_delete_preserves_concurrently_recreated_item() -> None:
    api = _FakeSecurityFrameworkAPI()
    store = _store(api)
    old_secret = b"old-secret"
    replacement = b"replacement-secret"
    assert await store.create_secret(old_secret)
    api.replace_before_compare_delete = replacement

    deleted = await store.delete_secret_if_matches(hashlib.sha256(old_secret).digest())

    assert not deleted
    assert api.secret == replacement


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "secret",
    (
        b"",
        b"contains\nnewline",
        b"contains\rcarriage-return",
        b"contains\x00nul",
        b"x" * 16_385,
    ),
)
async def test_store_rejects_unsafe_secret_before_native_call(secret: bytes) -> None:
    api = _FakeSecurityFrameworkAPI()
    store = _store(api)

    with pytest.raises(KeychainSecretUnavailable):
        await store.write_secret(secret)

    assert api.calls == []


@pytest.mark.asyncio
async def test_native_error_is_redacted_at_async_store_boundary() -> None:
    api = _FakeSecurityFrameworkAPI()
    api.failure = RuntimeError("native-secret-must-not-escape")
    store = _store(api)

    with pytest.raises(KeychainSecretUnavailable) as caught:
        await store.read_secret()

    assert "native-secret" not in str(caught.value)


def test_no_security_cli_or_shell_path_remains_in_production_macos_store() -> None:
    platform_source = CONNECTOR_ROOT / "src/hermes_connector/adapters/platform/macos"
    production = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (
            platform_source / "keychain.py",
            platform_source / "keychain_broker.py",
            platform_source / "keychain_direct.py",
            platform_source / "security_framework.py",
        )
    )

    assert "/usr/bin/security" not in production
    assert "add-generic-password" not in production
    assert "create_subprocess_shell" not in production
    assert "shell=True" not in production
    assert "asyncio.create_subprocess_exec" in production
    assert not (platform_source / "security_subprocess.py").exists()
