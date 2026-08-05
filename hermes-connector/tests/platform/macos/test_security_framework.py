from __future__ import annotations

import ctypes
import hashlib
from dataclasses import dataclass

import pytest

from hermes_connector.adapters.platform.macos.keychain_direct import (
    MacOSDirectKeychainSecretStore,
)
from hermes_connector.adapters.platform.macos.security_framework import (
    CtypesSecurityFrameworkBindings,
    MacOSSecurityFrameworkAPI,
    NativeItemResult,
    NativePasswordLookup,
    SecurityFrameworkOperationError,
)

_SUCCESS = 0
_DUPLICATE = -25_299
_NOT_FOUND = -25_300


@dataclass
class _Item:
    identifier: int
    service: bytes
    account: bytes
    secret: bytes
    verifier: bytes | None
    live: bool = True


@dataclass
class _PasswordPointer:
    secret: bytes
    freed: bool = False


class _FakeSecurityFrameworkBindings:
    def __init__(self) -> None:
        self.available_checks = 0
        self.interaction_disabled = 0
        self.next_identifier = 1
        self.current: dict[tuple[bytes, bytes], _Item] = {}
        self.released: list[int] = []
        self.freed_passwords: list[_PasswordPointer] = []
        self.before_delete = None
        self.before_conditional_delete = None
        self.update_failure = False
        self.calls: list[str] = []

    def check_available(self) -> None:
        self.available_checks += 1
        self.calls.append("check_available")

    def disable_user_interaction(self) -> int:
        self.interaction_disabled += 1
        self.calls.append("disable_user_interaction")
        return _SUCCESS

    def find_generic_password(
        self,
        service: bytes,
        account: bytes,
    ) -> NativePasswordLookup:
        self.calls.append("find")
        item = self.current.get((service, account))
        if item is None or not item.live:
            return NativePasswordLookup(_NOT_FOUND, None, None, 0)
        pointer = _PasswordPointer(item.secret)
        return NativePasswordLookup(_SUCCESS, item, pointer, len(item.secret))

    def add_generic_password(
        self,
        service: bytes,
        account: bytes,
        secret: bytes,
        verifier: bytes,
    ) -> NativeItemResult:
        self.calls.append("add")
        current = self.current.get((service, account))
        if current is not None and current.live:
            return NativeItemResult(_DUPLICATE, None)
        item = _Item(
            identifier=self.next_identifier,
            service=service,
            account=account,
            secret=secret,
            verifier=verifier,
        )
        self.next_identifier += 1
        self.current[(service, account)] = item
        return NativeItemResult(_SUCCESS, item)

    def update_generic_password(
        self,
        service: bytes,
        account: bytes,
        secret: bytes,
        verifier: bytes,
    ) -> int:
        self.calls.append("update")
        current = self.current.get((service, account))
        if current is None or not current.live:
            return _NOT_FOUND
        if self.update_failure:
            return -1
        current.secret, current.verifier = secret, verifier
        return _SUCCESS

    def delete_generic_password_if_verifier(
        self,
        service: bytes,
        account: bytes,
        verifier: bytes,
    ) -> int:
        self.calls.append("conditional_delete")
        if self.before_conditional_delete is not None:
            callback, self.before_conditional_delete = (
                self.before_conditional_delete,
                None,
            )
            callback()
        current = self.current.get((service, account))
        if current is None or not current.live or current.verifier != verifier:
            return _NOT_FOUND
        current.live = False
        del self.current[(service, account)]
        return _SUCCESS

    def delete_item(self, item_reference: object) -> int:
        self.calls.append("delete")
        assert isinstance(item_reference, _Item)
        if self.before_delete is not None:
            callback, self.before_delete = self.before_delete, None
            callback(item_reference)
        current = self.current.get((item_reference.service, item_reference.account))
        if not item_reference.live or current is not item_reference:
            return _NOT_FOUND
        item_reference.live = False
        del self.current[(item_reference.service, item_reference.account)]
        return _SUCCESS

    def copy_password(self, pointer: object, length: int) -> bytes:
        self.calls.append("copy_password")
        assert isinstance(pointer, _PasswordPointer)
        assert not pointer.freed
        assert length == len(pointer.secret)
        return pointer.secret

    def free_password(self, pointer: object) -> None:
        self.calls.append("free_password")
        assert isinstance(pointer, _PasswordPointer)
        assert not pointer.freed
        pointer.freed = True
        self.freed_passwords.append(pointer)

    def release_item(self, item_reference: object) -> None:
        self.calls.append("release_item")
        assert isinstance(item_reference, _Item)
        self.released.append(item_reference.identifier)

    def replace_after_lookup(self, stale: _Item, replacement: bytes) -> None:
        stale.live = False
        new_item = _Item(
            identifier=self.next_identifier,
            service=stale.service,
            account=stale.account,
            secret=replacement,
            verifier=hashlib.sha256(replacement).digest(),
        )
        self.next_identifier += 1
        self.current[(stale.service, stale.account)] = new_item

    def __repr__(self) -> str:
        return "_FakeSecurityFrameworkBindings(<native-state-redacted>)"


def _api() -> tuple[MacOSSecurityFrameworkAPI, _FakeSecurityFrameworkBindings]:
    bindings = _FakeSecurityFrameworkBindings()
    return MacOSSecurityFrameworkAPI(bindings=bindings), bindings


def test_availability_load_check_does_not_access_or_mutate_keychain() -> None:
    api, bindings = _api()

    api.check_available()

    assert bindings.calls == ["check_available"]
    assert bindings.interaction_disabled == 0


def test_direct_child_adapter_binds_raw_item_operations() -> None:
    api, bindings = _api()
    service = b"wiki.seaotter.hermes.connector.test"
    account = b"v1:test-device"
    direct = MacOSDirectKeychainSecretStore(
        service=service,
        account=account,
        api=api,
    )

    assert direct.read_raw() is None
    assert direct.create_raw(b"enveloped-secret")
    assert direct.read_raw() == b"enveloped-secret"
    direct.write_raw(b"replacement-envelope")
    assert direct.delete_raw_if_digest(hashlib.sha256(b"replacement-envelope").digest())
    assert direct.read_raw() is None
    assert bindings.current == {}


def test_read_copies_bounded_secret_and_releases_content_and_item() -> None:
    api, bindings = _api()
    service = b"wiki.seaotter.hermes.connector.test"
    account = b"v1:test-device"
    assert api.create_generic_password(service, account, b"bounded-secret")
    bindings.calls.clear()
    bindings.released.clear()

    value = api.read_generic_password(service, account, max_secret_bytes=128)

    assert value == b"bounded-secret"
    assert bindings.calls == [
        "disable_user_interaction",
        "find",
        "copy_password",
        "free_password",
        "release_item",
    ]
    assert len(bindings.freed_passwords) == 1
    assert len(bindings.released) == 1
    assert "bounded-secret" not in repr(api)
    assert "bounded-secret" not in repr(bindings)


def test_oversized_native_content_fails_and_still_releases_all_resources() -> None:
    api, bindings = _api()
    service = b"wiki.seaotter.hermes.connector.test"
    account = b"v1:test-device"
    assert api.create_generic_password(service, account, b"x" * 129)
    bindings.released.clear()

    with pytest.raises(SecurityFrameworkOperationError):
        api.read_generic_password(service, account, max_secret_bytes=128)

    assert len(bindings.freed_passwords) == 1
    assert len(bindings.released) == 1


def test_create_does_not_overwrite_and_write_updates_exact_item() -> None:
    api, bindings = _api()
    service = b"wiki.seaotter.hermes.connector.test"
    account = b"v1:test-device"

    assert api.create_generic_password(service, account, b"first-secret")
    original_identifier = bindings.current[(service, account)].identifier
    assert not api.create_generic_password(service, account, b"must-not-overwrite")
    api.write_generic_password(service, account, b"rotated-secret")

    current = bindings.current[(service, account)]
    assert current.secret == b"rotated-secret"
    assert current.identifier == original_identifier
    assert current.verifier == hashlib.sha256(b"rotated-secret").digest()
    assert bindings.calls.count("add") == 2
    assert bindings.calls.count("update") == 1
    assert bindings.calls.count("delete") == 0
    assert "first-secret" not in repr(api)
    assert "rotated-secret" not in repr(api)


def test_compare_delete_uses_stable_item_reference_and_cannot_delete_recreated_k2() -> (
    None
):
    api, bindings = _api()
    service = b"wiki.seaotter.hermes.connector.device-key.v1"
    account = b"connector-instance:test"
    old_secret = b"old-private-seed"
    replacement = b"new-private-seed"
    assert api.create_generic_password(service, account, old_secret)
    old_digest = hashlib.sha256(old_secret).digest()

    bindings.before_conditional_delete = lambda: bindings.replace_after_lookup(
        bindings.current[(service, account)],
        replacement,
    )

    deleted = api.delete_generic_password_if_matches(
        service,
        account,
        expected_sha256=old_digest,
        max_secret_bytes=128,
    )

    assert not deleted
    assert bindings.current[(service, account)].secret == replacement
    assert bindings.current[(service, account)].live
    assert bindings.calls[-1] == "conditional_delete"


def test_compare_delete_cannot_delete_same_item_after_concurrent_adapter_write() -> (
    None
):
    api, bindings = _api()
    service = b"wiki.seaotter.hermes.connector.device-key.v1"
    account = b"connector-instance:test"
    original = b"original-envelope"
    replacement = b"replacement-envelope"
    assert api.create_generic_password(service, account, original)

    bindings.before_conditional_delete = lambda: api.write_generic_password(
        service,
        account,
        replacement,
    )

    deleted = api.delete_generic_password_if_matches(
        service,
        account,
        expected_sha256=hashlib.sha256(original).digest(),
        max_secret_bytes=128,
    )

    assert not deleted
    assert bindings.current[(service, account)].secret == replacement
    assert bindings.calls.count("update") == 1


def test_failed_atomic_update_preserves_old_data_and_verifier() -> None:
    api, bindings = _api()
    service = b"wiki.seaotter.hermes.connector.test"
    account = b"v1:test-device"
    original = b"original-envelope"
    assert api.create_generic_password(service, account, original)
    item = bindings.current[(service, account)]
    original_identifier = item.identifier
    bindings.update_failure = True

    with pytest.raises(SecurityFrameworkOperationError):
        api.write_generic_password(service, account, b"replacement-envelope")

    current = bindings.current[(service, account)]
    assert current.identifier == original_identifier
    assert current.secret == original
    assert current.verifier == hashlib.sha256(original).digest()


def test_legacy_untagged_item_is_not_conditionally_deleted_and_write_upgrades() -> None:
    api, bindings = _api()
    service = b"wiki.seaotter.hermes.connector.test"
    account = b"v1:test-device"
    legacy = b"legacy-secret"
    assert api.create_generic_password(service, account, legacy)
    item = bindings.current[(service, account)]
    item.verifier = None

    assert not api.delete_generic_password_if_matches(
        service,
        account,
        expected_sha256=hashlib.sha256(legacy).digest(),
        max_secret_bytes=128,
    )
    assert bindings.current[(service, account)] is item

    api.write_generic_password(service, account, legacy)
    assert bindings.current[(service, account)] is item
    assert item.verifier == hashlib.sha256(legacy).digest()


def test_delete_releases_exact_item_and_is_idempotent() -> None:
    api, bindings = _api()
    service = b"wiki.seaotter.hermes.connector.test"
    account = b"v1:test-device"
    assert api.create_generic_password(service, account, b"secret")

    assert api.delete_generic_password(service, account)
    assert not api.delete_generic_password(service, account)
    assert len(bindings.released) >= 2


def test_native_result_repr_redacts_pointer_and_content_handles() -> None:
    item = object()
    pointer = object()

    assert repr(NativeItemResult(0, item)) == (
        "NativeItemResult(status=0, item_reference=<redacted>)"
    )
    assert repr(NativePasswordLookup(0, item, pointer, 12)) == (
        "NativePasswordLookup(status=0, handles=<redacted>, password_length=12)"
    )


def test_ctypes_secitem_operations_are_atomic_and_clear_input_buffers() -> None:
    bindings = object.__new__(CtypesSecurityFrameworkBindings)
    next_reference = 100
    dictionaries: dict[int, dict[int, int]] = {}
    buffers: list[tuple[object, int]] = []
    released: list[int] = []
    calls: dict[str, dict[int, int]] = {}

    def pointer_value(pointer: object) -> int:
        value = ctypes.cast(pointer, ctypes.c_void_p).value
        assert value is not None
        return value

    def allocate() -> int:
        nonlocal next_reference
        next_reference += 1
        return next_reference

    def create_value(
        _allocator: object,
        pointer: object,
        length: int,
        *_options: object,
    ) -> int:
        buffers.append((pointer, length))
        return allocate()

    def create_dictionary(
        _allocator: object,
        keys: object,
        values: object,
        count: int,
        _key_callbacks: object,
        _value_callbacks: object,
    ) -> int:
        reference = allocate()
        dictionaries[reference] = {
            pointer_value(keys[index]): pointer_value(values[index])
            for index in range(count)
        }
        return reference

    def add(attributes: object, _result: object) -> int:
        calls["add"] = dictionaries[pointer_value(attributes)]
        return _DUPLICATE

    def update(query: object, attributes: object) -> int:
        calls["update_query"] = dictionaries[pointer_value(query)]
        calls["update_attributes"] = dictionaries[pointer_value(attributes)]
        return _SUCCESS

    def delete(query: object) -> int:
        calls["delete_query"] = dictionaries[pointer_value(query)]
        return _NOT_FOUND

    bindings._key_class = 1
    bindings._sec_class_generic_password = 2
    bindings._key_attr_service = 3
    bindings._key_attr_account = 4
    bindings._key_attr_generic = 5
    bindings._key_value_data = 6
    bindings._key_match_limit = 7
    bindings._match_limit_one = 8
    bindings._cf_dictionary_key_callbacks = 9
    bindings._cf_dictionary_value_callbacks = 10
    bindings._cf_string_create = create_value
    bindings._cf_data_create = create_value
    bindings._cf_dictionary_create = create_dictionary
    bindings._cf_release = lambda reference: released.append(pointer_value(reference))
    bindings._sec_item_add = add
    bindings._sec_item_update = update
    bindings._sec_item_delete = delete
    verifier = hashlib.sha256(b"private-seed").digest()

    result = bindings.add_generic_password(
        b"wiki.seaotter.hermes.connector.device-key.v1",
        b"connector-instance:test",
        b"private-seed",
        verifier,
    )
    update_status = bindings.update_generic_password(
        b"wiki.seaotter.hermes.connector.device-key.v1",
        b"connector-instance:test",
        b"rotated-seed",
        hashlib.sha256(b"rotated-seed").digest(),
    )
    delete_status = bindings.delete_generic_password_if_verifier(
        b"wiki.seaotter.hermes.connector.device-key.v1",
        b"connector-instance:test",
        verifier,
    )

    assert result.status == _DUPLICATE
    assert update_status == _SUCCESS
    assert delete_status == _NOT_FOUND
    assert set(calls["add"]) == {1, 3, 4, 5, 6}
    assert set(calls["update_query"]) == {1, 3, 4}
    assert set(calls["update_attributes"]) == {5, 6}
    assert set(calls["delete_query"]) == {1, 3, 4, 5, 7}
    assert 6 not in calls["delete_query"]
    for pointer, length in buffers:
        assert ctypes.string_at(pointer, length) == b"\x00" * length

    buffers.clear()
    released.clear()

    def fail_data_create(
        _allocator: object,
        pointer: object,
        length: int,
    ) -> int:
        buffers.append((pointer, length))
        raise RuntimeError("injected CFData failure")

    bindings._cf_data_create = fail_data_create
    with pytest.raises(RuntimeError):
        bindings.add_generic_password(
            b"wiki.seaotter.hermes.connector.device-key.v1",
            b"connector-instance:test",
            b"private-seed",
            verifier,
        )

    assert len(released) == 2
    for pointer, length in buffers:
        assert ctypes.string_at(pointer, length) == b"\x00" * length
