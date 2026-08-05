"""Direct, non-interactive macOS Security.framework Keychain adapter."""

from __future__ import annotations

import ctypes
import hashlib
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

_SECURITY_FRAMEWORK = "/System/Library/Frameworks/Security.framework/Security"
_CORE_FOUNDATION = "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
_ERR_SEC_SUCCESS = 0
_ERR_SEC_DUPLICATE_ITEM = -25_299
_ERR_SEC_ITEM_NOT_FOUND = -25_300
_MAX_REFERENCE_BYTES = 255
_MAX_WRITE_ATTEMPTS = 3
_UTF8_ENCODING = 0x08000100


class _CFDictionaryKeyCallBacks(ctypes.Structure):
    _fields_ = [
        ("version", ctypes.c_long),
        ("retain", ctypes.c_void_p),
        ("release", ctypes.c_void_p),
        ("copy_description", ctypes.c_void_p),
        ("equal", ctypes.c_void_p),
        ("hash", ctypes.c_void_p),
    ]


class _CFDictionaryValueCallBacks(ctypes.Structure):
    _fields_ = [
        ("version", ctypes.c_long),
        ("retain", ctypes.c_void_p),
        ("release", ctypes.c_void_p),
        ("copy_description", ctypes.c_void_p),
        ("equal", ctypes.c_void_p),
    ]


class SecurityFrameworkOperationError(ValueError):
    """A bounded Security.framework operation could not complete safely."""


@dataclass(frozen=True, slots=True, repr=False)
class NativeItemResult:
    status: int
    item_reference: object | None

    def __repr__(self) -> str:
        return f"NativeItemResult(status={self.status}, item_reference=<redacted>)"


@dataclass(frozen=True, slots=True, repr=False)
class NativePasswordLookup:
    status: int
    item_reference: object | None
    password_pointer: object | None
    password_length: int

    def __repr__(self) -> str:
        return (
            f"NativePasswordLookup(status={self.status}, handles=<redacted>, "
            f"password_length={self.password_length})"
        )


class SecurityFrameworkBindingsPort(Protocol):
    def check_available(self) -> None: ...

    def disable_user_interaction(self) -> int: ...

    def find_generic_password(
        self,
        service: bytes,
        account: bytes,
    ) -> NativePasswordLookup: ...

    def add_generic_password(
        self,
        service: bytes,
        account: bytes,
        secret: bytes,
        verifier: bytes,
    ) -> NativeItemResult: ...

    def update_generic_password(
        self,
        service: bytes,
        account: bytes,
        secret: bytes,
        verifier: bytes,
    ) -> int: ...

    def delete_generic_password_if_verifier(
        self,
        service: bytes,
        account: bytes,
        verifier: bytes,
    ) -> int: ...

    def delete_item(self, item_reference: object) -> int: ...

    def copy_password(self, pointer: object, length: int) -> bytes: ...

    def free_password(self, pointer: object) -> None: ...

    def release_item(self, item_reference: object) -> None: ...


class SecurityFrameworkAPIPort(Protocol):
    def check_available(self) -> None: ...

    def read_generic_password(
        self,
        service: bytes,
        account: bytes,
        *,
        max_secret_bytes: int,
    ) -> bytes | None: ...

    def create_generic_password(
        self,
        service: bytes,
        account: bytes,
        secret: bytes,
    ) -> bool: ...

    def write_generic_password(
        self,
        service: bytes,
        account: bytes,
        secret: bytes,
    ) -> None: ...

    def delete_generic_password(self, service: bytes, account: bytes) -> bool: ...

    def delete_generic_password_if_matches(
        self,
        service: bytes,
        account: bytes,
        *,
        expected_sha256: bytes,
        max_secret_bytes: int,
    ) -> bool: ...


class MacOSSecurityFrameworkAPI:
    """Manage generic-password items through native Keychain operations."""

    __slots__ = ("_bindings",)

    def __init__(
        self,
        *,
        bindings: SecurityFrameworkBindingsPort | None = None,
    ) -> None:
        self._bindings = bindings or CtypesSecurityFrameworkBindings()

    def check_available(self) -> None:
        try:
            self._bindings.check_available()
        except Exception:  # noqa: BLE001 - native boundary always redacts failure
            raise SecurityFrameworkOperationError(
                "macOS Security.framework is unavailable"
            ) from None

    def read_generic_password(
        self,
        service: bytes,
        account: bytes,
        *,
        max_secret_bytes: int,
    ) -> bytes | None:
        service, account = _validate_reference(service, account)
        _validate_maximum(max_secret_bytes)
        self._begin_noninteractive()
        lookup = self._find(service, account)
        if lookup.status == _ERR_SEC_ITEM_NOT_FOUND:
            _release_lookup(self._bindings, lookup)
            return None
        try:
            _require_lookup(lookup, max_secret_bytes=max_secret_bytes)
            assert lookup.password_pointer is not None
            value = self._bindings.copy_password(
                lookup.password_pointer,
                lookup.password_length,
            )
            if len(value) != lookup.password_length:
                raise SecurityFrameworkOperationError(
                    "Keychain content changed during copy"
                )
            return value
        except SecurityFrameworkOperationError:
            raise
        except Exception:  # noqa: BLE001 - native boundary always redacts failure
            raise SecurityFrameworkOperationError("Keychain read failed") from None
        finally:
            _release_lookup(self._bindings, lookup)

    def create_generic_password(
        self,
        service: bytes,
        account: bytes,
        secret: bytes,
    ) -> bool:
        service, account = _validate_reference(service, account)
        secret = _validate_secret(secret)
        verifier = hashlib.sha256(secret).digest()
        self._begin_noninteractive()
        try:
            result = self._bindings.add_generic_password(
                service,
                account,
                secret,
                verifier,
            )
        except Exception:  # noqa: BLE001 - native boundary always redacts failure
            raise SecurityFrameworkOperationError("Keychain create failed") from None
        try:
            if result.status == _ERR_SEC_DUPLICATE_ITEM:
                return False
            _require_success(result.status, "Keychain create failed")
            return True
        finally:
            _release_item(self._bindings, result.item_reference)

    def write_generic_password(
        self,
        service: bytes,
        account: bytes,
        secret: bytes,
    ) -> None:
        service, account = _validate_reference(service, account)
        secret = _validate_secret(secret)
        verifier = hashlib.sha256(secret).digest()
        self._begin_noninteractive()
        for _ in range(_MAX_WRITE_ATTEMPTS):
            try:
                status = self._bindings.update_generic_password(
                    service,
                    account,
                    secret,
                    verifier,
                )
            except Exception:  # noqa: BLE001 - native boundary redacts failure
                raise SecurityFrameworkOperationError("Keychain write failed") from None
            if status == _ERR_SEC_SUCCESS:
                return
            if status != _ERR_SEC_ITEM_NOT_FOUND:
                _require_success(status, "Keychain write failed")

            result = self._add(service, account, secret, verifier)
            try:
                if result.status == _ERR_SEC_SUCCESS:
                    return
                if result.status != _ERR_SEC_DUPLICATE_ITEM:
                    _require_success(result.status, "Keychain write failed")
            finally:
                _release_item(self._bindings, result.item_reference)
        raise SecurityFrameworkOperationError("Keychain write did not converge")

    def delete_generic_password(self, service: bytes, account: bytes) -> bool:
        service, account = _validate_reference(service, account)
        self._begin_noninteractive()
        lookup = self._find(service, account)
        try:
            if lookup.status == _ERR_SEC_ITEM_NOT_FOUND:
                return False
            _require_lookup(lookup, max_secret_bytes=None)
            assert lookup.item_reference is not None
            status = self._bindings.delete_item(lookup.item_reference)
            if status == _ERR_SEC_ITEM_NOT_FOUND:
                return False
            _require_success(status, "Keychain delete failed")
            return True
        except SecurityFrameworkOperationError:
            raise
        except Exception:  # noqa: BLE001 - native boundary always redacts failure
            raise SecurityFrameworkOperationError("Keychain delete failed") from None
        finally:
            _release_lookup(self._bindings, lookup)

    def delete_generic_password_if_matches(
        self,
        service: bytes,
        account: bytes,
        *,
        expected_sha256: bytes,
        max_secret_bytes: int,
    ) -> bool:
        service, account = _validate_reference(service, account)
        if not isinstance(expected_sha256, bytes) or len(expected_sha256) != 32:
            raise SecurityFrameworkOperationError(
                "Keychain comparison digest is invalid"
            )
        _validate_maximum(max_secret_bytes)
        self._begin_noninteractive()
        try:
            status = self._bindings.delete_generic_password_if_verifier(
                service,
                account,
                expected_sha256,
            )
            if status == _ERR_SEC_ITEM_NOT_FOUND:
                return False
            _require_success(status, "Keychain compare-delete failed")
            return True
        except SecurityFrameworkOperationError:
            raise
        except Exception:  # noqa: BLE001 - native boundary always redacts failure
            raise SecurityFrameworkOperationError(
                "Keychain compare-delete failed"
            ) from None

    def _begin_noninteractive(self) -> None:
        try:
            status = self._bindings.disable_user_interaction()
        except Exception:  # noqa: BLE001 - native boundary always redacts failure
            raise SecurityFrameworkOperationError(
                "Keychain non-interactive mode failed"
            ) from None
        _require_success(status, "Keychain non-interactive mode failed")

    def _find(self, service: bytes, account: bytes) -> NativePasswordLookup:
        try:
            return self._bindings.find_generic_password(service, account)
        except Exception:  # noqa: BLE001 - native boundary always redacts failure
            raise SecurityFrameworkOperationError("Keychain lookup failed") from None

    def _add(
        self,
        service: bytes,
        account: bytes,
        secret: bytes,
        verifier: bytes,
    ) -> NativeItemResult:
        try:
            return self._bindings.add_generic_password(
                service,
                account,
                secret,
                verifier,
            )
        except Exception:  # noqa: BLE001 - native boundary always redacts failure
            raise SecurityFrameworkOperationError("Keychain write failed") from None

    def __repr__(self) -> str:
        return "MacOSSecurityFrameworkAPI(<native-keychain>)"


class CtypesSecurityFrameworkBindings:
    """Minimal ctypes bindings for generic-password Keychain operations."""

    __slots__ = (
        "_cf_data_create",
        "_cf_dictionary_create",
        "_cf_dictionary_key_callbacks",
        "_cf_dictionary_value_callbacks",
        "_cf_release",
        "_cf_string_create",
        "_delete",
        "_find",
        "_free_content",
        "_key_attr_account",
        "_key_attr_generic",
        "_key_attr_service",
        "_key_class",
        "_key_match_limit",
        "_key_value_data",
        "_match_limit_one",
        "_sec_class_generic_password",
        "_sec_item_add",
        "_sec_item_delete",
        "_sec_item_update",
        "_set_interaction",
    )

    def __init__(self) -> None:
        try:
            security = ctypes.CDLL(_SECURITY_FRAMEWORK)
            core_foundation = ctypes.CDLL(_CORE_FOUNDATION)
            self._set_interaction = security.SecKeychainSetUserInteractionAllowed
            self._find = security.SecKeychainFindGenericPassword
            self._delete = security.SecKeychainItemDelete
            self._free_content = security.SecKeychainItemFreeContent
            self._sec_item_add = security.SecItemAdd
            self._sec_item_update = security.SecItemUpdate
            self._sec_item_delete = security.SecItemDelete
            self._cf_data_create = core_foundation.CFDataCreate
            self._cf_string_create = core_foundation.CFStringCreateWithBytes
            self._cf_dictionary_create = core_foundation.CFDictionaryCreate
            self._cf_release = core_foundation.CFRelease
            self._cf_dictionary_key_callbacks = ctypes.addressof(
                _CFDictionaryKeyCallBacks.in_dll(
                    core_foundation,
                    "kCFTypeDictionaryKeyCallBacks",
                )
            )
            self._cf_dictionary_value_callbacks = ctypes.addressof(
                _CFDictionaryValueCallBacks.in_dll(
                    core_foundation,
                    "kCFTypeDictionaryValueCallBacks",
                )
            )
            self._key_class = _constant_pointer(security, "kSecClass")
            self._sec_class_generic_password = _constant_pointer(
                security,
                "kSecClassGenericPassword",
            )
            self._key_attr_service = _constant_pointer(
                security,
                "kSecAttrService",
            )
            self._key_attr_account = _constant_pointer(
                security,
                "kSecAttrAccount",
            )
            self._key_attr_generic = _constant_pointer(
                security,
                "kSecAttrGeneric",
            )
            self._key_value_data = _constant_pointer(security, "kSecValueData")
            self._key_match_limit = _constant_pointer(
                security,
                "kSecMatchLimit",
            )
            self._match_limit_one = _constant_pointer(
                security,
                "kSecMatchLimitOne",
            )
            self._configure_signatures()
        except (AttributeError, OSError):
            raise SecurityFrameworkOperationError(
                "macOS Security.framework is unavailable"
            ) from None

    def _configure_signatures(self) -> None:
        pointer = ctypes.c_void_p
        uint32 = ctypes.c_uint32
        status = ctypes.c_int32
        self._set_interaction.argtypes = [ctypes.c_ubyte]
        self._set_interaction.restype = status
        self._find.argtypes = [
            pointer,
            uint32,
            pointer,
            uint32,
            pointer,
            ctypes.POINTER(uint32),
            ctypes.POINTER(pointer),
            ctypes.POINTER(pointer),
        ]
        self._find.restype = status
        self._delete.argtypes = [pointer]
        self._delete.restype = status
        self._free_content.argtypes = [pointer, pointer]
        self._free_content.restype = status
        self._sec_item_add.argtypes = [pointer, ctypes.POINTER(pointer)]
        self._sec_item_add.restype = status
        self._sec_item_update.argtypes = [pointer, pointer]
        self._sec_item_update.restype = status
        self._sec_item_delete.argtypes = [pointer]
        self._sec_item_delete.restype = status
        self._cf_data_create.argtypes = [
            pointer,
            pointer,
            ctypes.c_long,
        ]
        self._cf_data_create.restype = pointer
        self._cf_string_create.argtypes = [
            pointer,
            pointer,
            ctypes.c_long,
            ctypes.c_uint32,
            ctypes.c_ubyte,
        ]
        self._cf_string_create.restype = pointer
        self._cf_dictionary_create.argtypes = [
            pointer,
            ctypes.POINTER(pointer),
            ctypes.POINTER(pointer),
            ctypes.c_long,
            pointer,
            pointer,
        ]
        self._cf_dictionary_create.restype = pointer
        self._cf_release.argtypes = [pointer]
        self._cf_release.restype = None

    def check_available(self) -> None:
        return None

    def disable_user_interaction(self) -> int:
        return int(self._set_interaction(0))

    def find_generic_password(
        self,
        service: bytes,
        account: bytes,
    ) -> NativePasswordLookup:
        password_length = ctypes.c_uint32()
        password_pointer = ctypes.c_void_p()
        item_reference = ctypes.c_void_p()
        service_buffer = ctypes.create_string_buffer(service)
        account_buffer = ctypes.create_string_buffer(account)
        try:
            status = self._find(
                None,
                len(service),
                ctypes.cast(service_buffer, ctypes.c_void_p),
                len(account),
                ctypes.cast(account_buffer, ctypes.c_void_p),
                ctypes.byref(password_length),
                ctypes.byref(password_pointer),
                ctypes.byref(item_reference),
            )
        finally:
            _clear_ctypes_buffer(account_buffer)
            _clear_ctypes_buffer(service_buffer)
        return NativePasswordLookup(
            int(status),
            item_reference.value,
            password_pointer.value,
            int(password_length.value),
        )

    def add_generic_password(
        self,
        service: bytes,
        account: bytes,
        secret: bytes,
        verifier: bytes,
    ) -> NativeItemResult:
        references: list[int] = []
        buffers: list[object] = []
        attributes = None
        try:
            service_ref, service_buffer = self._create_cf_string(service)
            references.append(service_ref)
            buffers.append(service_buffer)
            account_ref, account_buffer = self._create_cf_string(account)
            references.append(account_ref)
            buffers.append(account_buffer)
            secret_ref, secret_buffer = self._create_cf_data(secret)
            references.append(secret_ref)
            buffers.append(secret_buffer)
            verifier_ref, verifier_buffer = self._create_cf_data(verifier)
            references.append(verifier_ref)
            buffers.append(verifier_buffer)
            attributes = self._create_dictionary(
                (
                    (self._key_class, self._sec_class_generic_password),
                    (self._key_attr_service, service_ref),
                    (self._key_attr_account, account_ref),
                    (self._key_value_data, secret_ref),
                    (self._key_attr_generic, verifier_ref),
                )
            )
            status = self._sec_item_add(
                _native_pointer(attributes),
                None,
            )
        finally:
            self._release_cf_values(attributes, references, buffers)
        return NativeItemResult(int(status), None)

    def update_generic_password(
        self,
        service: bytes,
        account: bytes,
        secret: bytes,
        verifier: bytes,
    ) -> int:
        references: list[int] = []
        buffers: list[object] = []
        query = None
        attributes = None
        try:
            service_ref, service_buffer = self._create_cf_string(service)
            references.append(service_ref)
            buffers.append(service_buffer)
            account_ref, account_buffer = self._create_cf_string(account)
            references.append(account_ref)
            buffers.append(account_buffer)
            secret_ref, secret_buffer = self._create_cf_data(secret)
            references.append(secret_ref)
            buffers.append(secret_buffer)
            verifier_ref, verifier_buffer = self._create_cf_data(verifier)
            references.append(verifier_ref)
            buffers.append(verifier_buffer)
            query = self._create_dictionary(
                (
                    (self._key_class, self._sec_class_generic_password),
                    (self._key_attr_service, service_ref),
                    (self._key_attr_account, account_ref),
                )
            )
            attributes = self._create_dictionary(
                (
                    (self._key_value_data, secret_ref),
                    (self._key_attr_generic, verifier_ref),
                )
            )
            return int(
                self._sec_item_update(
                    _native_pointer(query),
                    _native_pointer(attributes),
                )
            )
        finally:
            self._release_cf_values(attributes, (), ())
            self._release_cf_values(query, references, buffers)

    def delete_generic_password_if_verifier(
        self,
        service: bytes,
        account: bytes,
        verifier: bytes,
    ) -> int:
        references: list[int] = []
        buffers: list[object] = []
        query = None
        try:
            service_ref, service_buffer = self._create_cf_string(service)
            references.append(service_ref)
            buffers.append(service_buffer)
            account_ref, account_buffer = self._create_cf_string(account)
            references.append(account_ref)
            buffers.append(account_buffer)
            verifier_ref, verifier_buffer = self._create_cf_data(verifier)
            references.append(verifier_ref)
            buffers.append(verifier_buffer)
            query = self._create_dictionary(
                (
                    (self._key_class, self._sec_class_generic_password),
                    (self._key_attr_service, service_ref),
                    (self._key_attr_account, account_ref),
                    (self._key_attr_generic, verifier_ref),
                    (self._key_match_limit, self._match_limit_one),
                )
            )
            return int(self._sec_item_delete(_native_pointer(query)))
        finally:
            self._release_cf_values(query, references, buffers)

    def _create_cf_string(self, value: bytes) -> tuple[int, object]:
        buffer = ctypes.create_string_buffer(value, len(value))
        try:
            reference = self._cf_string_create(
                None,
                ctypes.cast(buffer, ctypes.c_void_p),
                len(value),
                _UTF8_ENCODING,
                0,
            )
            return _require_created_reference(reference), buffer
        except BaseException:
            _clear_ctypes_buffer(buffer)
            raise

    def _create_cf_data(self, value: bytes) -> tuple[int, object]:
        buffer = ctypes.create_string_buffer(value, len(value))
        try:
            reference = self._cf_data_create(
                None,
                ctypes.cast(buffer, ctypes.c_void_p),
                len(value),
            )
            return _require_created_reference(reference), buffer
        except BaseException:
            _clear_ctypes_buffer(buffer)
            raise

    def _create_dictionary(
        self,
        pairs: tuple[tuple[int, int], ...],
    ) -> int:
        keys = (ctypes.c_void_p * len(pairs))(*(key for key, _ in pairs))
        values = (ctypes.c_void_p * len(pairs))(*(value for _, value in pairs))
        reference = self._cf_dictionary_create(
            None,
            keys,
            values,
            len(pairs),
            ctypes.c_void_p(self._cf_dictionary_key_callbacks),
            ctypes.c_void_p(self._cf_dictionary_value_callbacks),
        )
        return _require_created_reference(reference)

    def _release_cf_values(
        self,
        dictionary: int | None,
        references: Iterable[int],
        buffers: Iterable[object],
    ) -> None:
        reference_values = tuple(references)
        buffer_values = tuple(buffers)
        try:
            if dictionary is not None:
                self._cf_release(_native_pointer(dictionary))
            for reference in reversed(reference_values):
                self._cf_release(_native_pointer(reference))
        finally:
            for buffer in reversed(buffer_values):
                _clear_ctypes_buffer(buffer)

    def delete_item(self, item_reference: object) -> int:
        return int(self._delete(_native_pointer(item_reference)))

    def copy_password(self, pointer: object, length: int) -> bytes:
        return ctypes.string_at(_native_pointer(pointer), length)

    def free_password(self, pointer: object) -> None:
        status = int(self._free_content(None, _native_pointer(pointer)))
        _require_success(status, "Keychain content release failed")

    def release_item(self, item_reference: object) -> None:
        self._cf_release(_native_pointer(item_reference))

    def __repr__(self) -> str:
        return "CtypesSecurityFrameworkBindings(<native-symbols>)"


def _validate_reference(service: bytes, account: bytes) -> tuple[bytes, bytes]:
    for value in (service, account):
        if (
            not isinstance(value, bytes)
            or not 1 <= len(value) <= _MAX_REFERENCE_BYTES
            or b"\x00" in value
        ):
            raise SecurityFrameworkOperationError("Keychain reference is invalid")
    return service, account


def _validate_secret(secret: bytes) -> bytes:
    if not isinstance(secret, bytes) or not secret:
        raise SecurityFrameworkOperationError("Keychain secret is invalid")
    return secret


def _validate_maximum(maximum: int) -> None:
    if type(maximum) is not int or not 1 <= maximum <= 65_536:
        raise SecurityFrameworkOperationError("Keychain limit is invalid")


def _require_lookup(
    lookup: NativePasswordLookup,
    *,
    max_secret_bytes: int | None,
) -> None:
    _require_success(lookup.status, "Keychain lookup failed")
    if (
        lookup.item_reference is None
        or lookup.password_pointer is None
        or type(lookup.password_length) is not int
        or lookup.password_length < 1
        or max_secret_bytes is not None
        and lookup.password_length > max_secret_bytes
    ):
        raise SecurityFrameworkOperationError("Keychain lookup result is unsafe")


def _require_success(status: int, message: str) -> None:
    if type(status) is not int or status != _ERR_SEC_SUCCESS:
        raise SecurityFrameworkOperationError(message)


def _release_lookup(
    bindings: SecurityFrameworkBindingsPort,
    lookup: NativePasswordLookup,
) -> None:
    first_error: Exception | None = None
    if lookup.password_pointer is not None:
        try:
            bindings.free_password(lookup.password_pointer)
        except Exception as error:  # noqa: BLE001 - release both resources
            first_error = error
    if lookup.item_reference is not None:
        try:
            bindings.release_item(lookup.item_reference)
        except Exception as error:  # noqa: BLE001 - native release boundary
            if first_error is None:
                first_error = error
    if first_error is not None:
        raise SecurityFrameworkOperationError(
            "Keychain native resource release failed"
        ) from None


def _release_item(
    bindings: SecurityFrameworkBindingsPort,
    item_reference: object | None,
) -> None:
    if item_reference is None:
        return
    try:
        bindings.release_item(item_reference)
    except Exception:  # noqa: BLE001 - native release boundary always redacts
        raise SecurityFrameworkOperationError(
            "Keychain native item release failed"
        ) from None


def _native_pointer(value: object) -> ctypes.c_void_p:
    if type(value) is not int or value <= 0:
        raise SecurityFrameworkOperationError("Keychain native reference is invalid")
    return ctypes.c_void_p(value)


def _constant_pointer(library: object, name: str) -> int:
    try:
        value = ctypes.c_void_p.in_dll(library, name).value
    except (TypeError, ValueError):
        raise SecurityFrameworkOperationError(
            "macOS Security.framework is unavailable"
        ) from None
    return _require_created_reference(value)


def _require_created_reference(value: object) -> int:
    if isinstance(value, ctypes.c_void_p):
        value = value.value
    if type(value) is not int or value <= 0:
        raise SecurityFrameworkOperationError("Keychain native allocation failed")
    return value


def _clear_ctypes_buffer(buffer: object) -> None:
    ctypes.memset(ctypes.addressof(buffer), 0, ctypes.sizeof(buffer))
